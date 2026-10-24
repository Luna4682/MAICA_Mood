# -*- coding: utf-8 -*-
"""persist_items 表：knowledge / savefile_derived / savefile_field 的权威存储。

这三类是【整句文本】，没有主谓宾，所以不放进 persist_facts（那张表靠
"主语+谓语+宾语"做冲突裁决）。权威在 SQLite 本表；向量库只做索引，
通过 ref_id 指回本表，本表用 vector_id 反向指回向量库那一行。

约定（2026-10-10 定）：
  · 四类标签：knowledge / player_direct / savefile_derived / savefile_field
  · knowledge 全局一份（user_id=-1, chat_session_num=0），启动时写一次，幂等
  · 同步方式与向量库一致：本轮没出现的行直接删掉
"""

from __future__ import annotations


async def ensure_table():
    """幂等建表（迁移运行器只认版本变化，对新表不可靠，所以自建）。"""
    from maica.maica_utils import DatabaseUtils, SqlPersistItem
    async with DatabaseUtils.engine_data.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: SqlPersistItem.__table__.create(sync_conn, checkfirst=True)
        )


async def load_texts(user_id: int, session_num: int, source: str) -> list:
    """读回该范围下【生效的】文本，按行号排序。"""
    from maica.maica_utils import DatabaseUtils, SqlPersistItem
    import sqlalchemy
    stmt = sqlalchemy.select(SqlPersistItem).where(
        SqlPersistItem.user_id == user_id,
        SqlPersistItem.chat_session_num == session_num,
        SqlPersistItem.source == source,
    ).order_by(SqlPersistItem.id)
    async with DatabaseUtils.SessionData() as dbs:
        rows = (await dbs.scalars(stmt)).all()
    return [r.raw_text for r in rows if getattr(r, "enabled", True) and not getattr(r, "superseded_by", None)]


async def count_items(user_id: int, session_num: int, source: str) -> int:
    """该范围下有多少行（不论是否生效）。给"知识是否已入库"这类幂等判断用。"""
    try:
        from maica.maica_utils import DatabaseUtils, SqlPersistItem
        import sqlalchemy
        stmt = sqlalchemy.select(sqlalchemy.func.count()).select_from(SqlPersistItem).where(
            SqlPersistItem.user_id == user_id,
            SqlPersistItem.chat_session_num == session_num,
            SqlPersistItem.source == source,
        )
        async with DatabaseUtils.SessionData() as dbs:
            return int((await dbs.scalar(stmt)) or 0)
    except Exception:
        return 0


async def sync_items(sockets, user_id: int, session_num: int, source: str, texts) -> dict:
    """把一批整句文本同步进 persist_items，再带 ref_id 同步进向量库，并回写 vector_id。

    差集语义：本轮没出现的文本，其 SQLite 行与向量行都会被删掉。
    返回 {"add": n, "del": n, "total": n}；任何失败都返回空字典，绝不影响对话。
    """
    try:
        from maica.maica_utils import DatabaseUtils, SqlPersistItem
        import sqlalchemy

        requested = {str(t).strip() for t in (texts or []) if str(t).strip()}
        await ensure_table()

        async with DatabaseUtils.SessionData() as dbs:
            rows = (await dbs.scalars(sqlalchemy.select(SqlPersistItem).where(
                SqlPersistItem.user_id == user_id,
                SqlPersistItem.chat_session_num == session_num,
                SqlPersistItem.source == source,
            ))).all()
            have = {r.raw_text: r for r in rows}

            to_del = [r.id for text, r in have.items() if text not in requested]
            to_add = [text for text in requested if text not in have]

        # 写完就把读会话结束掉：同一个会话里 SELECT 已自动开启事务，
        # 再 begin() 会报 "A transaction is already begun"，所以写一律用新会话。
        if to_del or to_add:
            async with DatabaseUtils.SessionData() as dbs:
                async with dbs.begin():
                    if to_del:
                        doomed = (await dbs.scalars(sqlalchemy.select(SqlPersistItem).where(
                            SqlPersistItem.id.in_(to_del)
                        ))).all()
                        for r in doomed:
                            await dbs.delete(r)
                    for text in to_add:
                        dbs.add(SqlPersistItem(
                            user_id=user_id,
                            chat_session_num=session_num,
                            source=source,
                            raw_text=text,
                            enabled=True,
                        ))

        # 重新读一遍，拿到（含新插入行的）行号，作为 ref_id
        async with DatabaseUtils.SessionData() as dbs:
            rows2 = (await dbs.scalars(sqlalchemy.select(SqlPersistItem).where(
                SqlPersistItem.user_id == user_id,
                SqlPersistItem.chat_session_num == session_num,
                SqlPersistItem.source == source,
            ))).all()
        ref_map = {r.raw_text: str(r.id) for r in rows2}

        # 带 ref_id 同步进向量库，再把向量行号写回 SQLite（双向连接）
        if sockets is not None and getattr(sockets, "is_vector_ready", False):
            vec_map = await sockets.vector_pool.sync_texts(
                embedding_conn=sockets.embedding_conn,
                data=set(ref_map),
                filters={
                    "user_id": user_id,
                    "chat_session_num": session_num,
                    "type": source,
                },
                ref_ids=ref_map,
            )
            if vec_map:
                # 先把「行号 → 向量行号」算好并结束读会话，再开新会话写回，
                # 同样是避免同一会话里 SELECT 之后再 begin()。
                async with DatabaseUtils.SessionData() as dbs:
                    rows3 = (await dbs.scalars(sqlalchemy.select(SqlPersistItem).where(
                        SqlPersistItem.user_id == user_id,
                        SqlPersistItem.chat_session_num == session_num,
                        SqlPersistItem.source == source,
                    ))).all()
                    updates = [(r.id, (vec_map or {}).get(r.raw_text), r.vector_id) for r in rows3]
                updates = [(rid, vid) for rid, vid, old in updates if vid and old != vid]
                if updates:
                    async with DatabaseUtils.SessionData() as dbs:
                        async with dbs.begin():
                            for rid, vid in updates:
                                r = await dbs.get(SqlPersistItem, rid)
                                if r is not None and r.vector_id != vid:
                                    r.vector_id = vid

        return {"add": len(to_add), "del": len(to_del), "total": len(ref_map)}
    except Exception as exc:
        try:
            import maica.maica_utils.debug_log as _dl
            _dl.dbg("ITEM-SYNC-ERROR", "%s: %r" % (source, exc))
        except Exception:
            pass
        return {}
