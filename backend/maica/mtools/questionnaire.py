# -*- coding: utf-8 -*-
u"""问卷导入：把玩家在网页里填的答案变成事实库里的明确事实。

流程（2026-10-22 定）：
  网页（webui_server.py）写 <backend>/data/questionnaire.db 的 answers 表：
      qid | value | sentence | content_hash | imported_hash
  这里只处理 content_hash != imported_hash 的行：
      sentence 喂给 r8（走和聊天完全相同的提取→漏斗→裁决流水线）→ 落库
      成功后把 imported_hash 写成 content_hash（失败就不写 → 下一轮自动重试）

为什么复用 `_fact_ingest_bg`：那条路径已经做了「实体归一 → 去重（同键同极性只刷
speak_time）→ 冲突裁决（superseded_by）→ 向量同步」，自己重写一份必然漏掉某一步
（尤其是向量同步，漏了就是"库里记得、召回搜不到"）。

触发：post_core_pipeliner 每轮回复后调用 `maybe_schedule(fsc)`，有脏行就起一个
后台任务（不阻塞对话）。导出一次后用 imported_hash 挡住，不会重复导入。
"""

import asyncio
import os
import sqlite3

_LOCK = asyncio.Lock()
_RUNNING = False


def _log(msg):
    try:
        import maica.maica_utils.debug_log as _dl
        _dl.dbg("QUESTIONNAIRE", msg)
    except Exception:
        try:
            print("[questionnaire] " + str(msg))
        except Exception:
            pass


def data_dir():
    """<backend>/data —— 优先看 MAICA_DATA_DB，回退按模块位置推。"""
    p = os.environ.get("MAICA_DATA_DB") or ""
    if p and ("/" in p or "\\" in p):
        return os.path.dirname(p.replace("\\", "/"))
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "data")


def db_path():
    return os.path.join(data_dir(), "questionnaire.db")


def _connect():
    conn = sqlite3.connect(db_path(), timeout=3.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def _ensure_columns(conn):
    """给老库补 `fact_ids` / `removed` 两列（幂等；webui 建表时也有，双保险）。"""
    try:
        # PRAGMA 返回的列顺序：cid|name|type|...；用下标取，避免调用方没设 row_factory
        cols = {r[1] for r in conn.execute("PRAGMA table_info(answers)")}
        if "fact_ids" not in cols:
            conn.execute("ALTER TABLE answers ADD COLUMN fact_ids TEXT")
        if "removed" not in cols:
            conn.execute("ALTER TABLE answers ADD COLUMN removed INTEGER DEFAULT 0")
        conn.commit()
    except Exception as e:
        _log("ensure columns failed: %r" % (e,))


def _split_ids(raw):
    out = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def _join_ids(ids):
    return ",".join(str(int(i)) for i in ids)


def read_dirty():
    """返回需要处理的答案行。

    两类：
      · 改过的：content_hash != imported_hash
      · 撤回的：removed=1（玩家把这一题清空了）—— 句子是空的，要删掉它产生的事实
    """
    p = db_path()
    if not os.path.isfile(p):
        return []
    try:
        conn = _connect()
    except Exception:
        return []
    try:
        _ensure_columns(conn)
        rows = conn.execute(
            "SELECT qid, value, sentence, content_hash, imported_hash, fact_ids, removed"
            " FROM answers"
            " WHERE COALESCE(removed, 0) = 1"
            "    OR (sentence IS NOT NULL AND sentence != ''"
            "        AND (imported_hash IS NULL OR imported_hash != content_hash))"
            " ORDER BY qid").fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        _log("read_dirty failed: %r" % (e,))
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _fact_id_owners():
    """{事实行 id: {qid,...}} —— 一行可能被两题共用（同键去重会复用同一行）。"""
    owners = {}
    p = db_path()
    if not os.path.isfile(p):
        return owners
    try:
        conn = _connect()
    except Exception:
        return owners
    try:
        _ensure_columns(conn)
        for r in conn.execute("SELECT qid, fact_ids FROM answers"):
            for fid in _split_ids(r["fact_ids"]):
                owners.setdefault(fid, set()).add(r["qid"])
    except Exception as e:
        _log("read fact_id owners failed: %r" % (e,))
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return owners


def write_fact_ids(qid, ids):
    """记下"这一题的答案产生了哪几行事实"（下次改答案时据此取代/删除）。"""
    try:
        conn = _connect()
    except Exception:
        return False
    try:
        conn.execute("UPDATE answers SET fact_ids=? WHERE qid=?", (_join_ids(ids), qid))
        conn.commit()
        return True
    except Exception as e:
        _log("write_fact_ids failed: %r" % (e,))
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def drop_answer(qid):
    """撤回处理完了，把墓碑行删掉。"""
    try:
        conn = _connect()
    except Exception:
        return False
    try:
        conn.execute("DELETE FROM answers WHERE qid=?", (qid,))
        conn.commit()
        return True
    except Exception as e:
        _log("drop_answer failed: %r" % (e,))
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


async def _supersede_rows(old_ids, winner, qid, owners):
    """把这一题用过的旧行标成"被 winner 取代"。

    · 只处理**只有这一题在引用**的行：两题答案同键时后端会复用同一行，删它会把另一题也带走
    · 已经是被取代状态的行不重复处理
    """
    kill = [i for i in old_ids
            if i and int(i) != int(winner) and owners.get(i, set()) <= {qid}]
    if not kill:
        return 0
    import sqlalchemy
    from maica.maica_utils import DatabaseUtils, SqlPersistFact
    n = 0
    async with DatabaseUtils.SessionData() as dbs:
        async with dbs.begin():
            rows = (await dbs.scalars(sqlalchemy.select(SqlPersistFact).where(
                SqlPersistFact.id.in_(kill)))).all()
            for r in rows:
                if getattr(r, "superseded_by", None):
                    continue
                r.superseded_by = int(winner)
                n += 1
    return n


async def _delete_rows(ids, qid, owners):
    """撤回答案：把它产生的事实行硬删（和事实页上的"删除"同义）。"""
    kill = [i for i in ids if i and owners.get(i, set()) <= {qid}]
    if not kill:
        return 0
    import sqlalchemy
    from maica.maica_utils import DatabaseUtils, SqlPersistFact
    n = 0
    async with DatabaseUtils.SessionData() as dbs:
        async with dbs.begin():
            rows = (await dbs.scalars(sqlalchemy.select(SqlPersistFact).where(
                SqlPersistFact.id.in_(kill)))).all()
            for r in rows:
                await dbs.delete(r)
                n += 1
    return n


def mark_imported(qid, content_hash):
    try:
        conn = _connect()
    except Exception:
        return False
    try:
        conn.execute("UPDATE answers SET imported_hash=? WHERE qid=?", (content_hash, qid))
        conn.commit()
        return True
    except Exception as e:
        _log("mark_imported failed: %r" % (e,))
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _backfill_fact_ids():
    """升级兼容：给"早就导过、但当时还没有 fact_ids 这一列"的行补上 id。

    匹配方式是最笨也最稳的：拿答案的整句去 `persist_facts.fact_text` 里找（精确相等）。
    只在第一次导入时对老行做一次，之后都由 write_fact_ids 维护。
    """
    import sqlite3 as _sq
    qp = db_path()
    mp = os.path.join(data_dir(), "maica.db")
    if not os.path.isfile(qp) or not os.path.isfile(mp):
        return 0
    try:
        conn = _connect()
    except Exception:
        return 0
    try:
        _ensure_columns(conn)
        todo = conn.execute(
            "SELECT qid, sentence FROM answers"
            " WHERE (fact_ids IS NULL OR fact_ids='')"
            "   AND imported_hash IS NOT NULL AND sentence IS NOT NULL AND sentence != ''"
        ).fetchall()
    except Exception:
        todo = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not todo:
        return 0
    n = 0
    try:
        mconn = _sq.connect("file:%s?mode=ro" % mp.replace("\\", "/"), uri=True, timeout=3.0)
    except Exception:
        return 0
    try:
        for r in todo:
            try:
                ids = [row[0] for row in mconn.execute(
                    "SELECT fact_id FROM persist_facts WHERE fact_text=? ORDER BY fact_id",
                    (r["sentence"],))]
            except Exception:
                continue
            if ids:
                if write_fact_ids(r["qid"], ids):
                    n += 1
                    _log("backfill qid=%s -> %s" % (r["qid"], ids))
    finally:
        try:
            mconn.close()
        except Exception:
            pass
    return n


async def import_once(fsc, user_id):
    """把脏行逐条导入。返回成功处理的条数。

    ★ 2026-10-22：**改答案 = 更新记忆**（用户定）。
      每行记着上一轮导入产生的 fact_ids；这次导入成功后再按"这题换了答案"处理：
        · 新答案落了库 → 旧行标 superseded_by=新行（事实页不再显示、召回也不再出）
        · 玩家把答案清空（removed=1）→ 旧行硬删（等价于在事实页点删除）
      多答案题（1/2、3/4/5 这种重复题）靠"每题一个 qid"天然支持：各题各管各的 fact_ids。
    """
    from maica.maica_utils.fact_store import MEMORY_SESSION, sync_player_facts
    from maica.mtrigger.post_core_pipeliner import _fact_ingest_bg

    rows = read_dirty()
    if not rows:
        return 0
    _log("import start: %d row(s)" % len(rows))
    try:
        _backfill_fact_ids()        # 老库补 fact_ids（只在第一次有作用）
        rows = read_dirty()         # 补完之后重读，拿到的就是补好的 fact_ids
    except Exception as e:
        _log("backfill skipped: %r" % (e,))
    owners = _fact_id_owners()
    done = 0
    for r in rows:
        qid = r.get("qid")
        old_ids = _split_ids(r.get("fact_ids"))
        # ---- 撤回：玩家把这一题清空了 ----
        if int(r.get("removed") or 0) == 1:
            try:
                n = await _delete_rows(old_ids, qid, owners)
            except Exception as e:
                _log("remove failed qid=%s: %r" % (qid, e))
                continue
            if drop_answer(qid):
                done += 1
                _log("removed qid=%s (deleted %d fact row(s))" % (qid, n))
            continue

        sentence = (r.get("sentence") or "").strip()
        if not sentence:
            continue
        try:
            # sync=False：等全部导完再统一同步一次向量库（每句同步一次太浪费）
            new_ids = await _fact_ingest_bg(sentence, user_id, MEMORY_SESSION, False, fsc, sync=False)
        except Exception as e:
            _log("import failed qid=%s: %r" % (qid, e))
            continue
        if new_ids is None:
            # 流程异常（不是"没信息"）→ 不标记，下一轮自动重试
            _log("import error qid=%s (will retry)" % qid)
            continue
        new_ids = [int(i) for i in new_ids if i]
        new_ids = sorted(set(new_ids))          # 同一行可能被两个候选命中，去个重
        if not new_ids:
            # r8 判定这句没信息 / 被漏斗挡掉：这是确定性结果，重试也没用。
            # 只把 imported_hash 写上挡住重试，旧行保持不动（不误删）
            mark_imported(qid, r.get("content_hash"))
            _log("import empty qid=%s (%s)" % (qid, sentence))
            continue
        try:
            n = await _supersede_rows(old_ids, new_ids[0], qid, owners)
        except Exception as e:
            _log("supersede failed qid=%s: %r" % (qid, e))
            n = 0
        if write_fact_ids(qid, new_ids) and mark_imported(qid, r.get("content_hash")):
            done += 1
            _log("imported qid=%s ids=%s superseded=%d (%s)" % (qid, new_ids, n, sentence))
    try:
        await sync_player_facts(fsc, user_id, MEMORY_SESSION)
    except Exception as e:
        _log("vector sync failed: %r" % (e,))
    _log("import done: %d/%d" % (done, len(rows)))
    return done


async def _run(fsc, user_id):
    global _RUNNING
    if _LOCK.locked():
        return 0
    _RUNNING = True
    try:
        async with _LOCK:
            return await import_once(fsc, user_id)
    finally:
        _RUNNING = False


def note_user(fsc):
    """记下"最近一次聊天的是谁"。

    ★ 2026-10-22 修：原来只在"有脏问卷行"时才记，结果玩家没填问卷 / 已经导过
      之后，/mm/facts 永远拿不到 user_id，网页就显示"没拿到用户 id"。
      现在每轮回复都记一次（极便宜），且不依赖问卷有没有内容。
    """
    try:
        set_active_user(fsc.maica_settings.verification.user_id)
        return True
    except Exception:
        return False


def fallback_user_id():
    """兜底：从事实库里猜"这是谁的库"（本地后端只有一个玩家）。

    只在 active_user_id() 为空时用；没有任何事实行就返回 None。
    """
    p = os.path.join(data_dir(), "maica.db")
    if not os.path.isfile(p):
        return None
    try:
        conn = sqlite3.connect(p, timeout=2.0)
        try:
            row = conn.execute(
                "SELECT user_id FROM persist_facts ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        if row and row[0] is not None:
            set_active_user(row[0])
            return int(row[0])
    except Exception:
        pass
    return None


def maybe_schedule(fsc):
    """每轮回复后调一次：有脏行就起后台导入（fire-and-forget）。"""
    try:
        note_user(fsc)
        if _RUNNING or _LOCK.locked():
            return False
        if not read_dirty():
            return False
        try:
            user_id = fsc.maica_settings.verification.user_id
        except Exception:
            return False
        asyncio.create_task(_run(fsc, user_id))
        return True
    except Exception as e:
        _log("maybe_schedule failed: %r" % (e,))
        return False

# ---------------------------------------------------------------------------
#  当前活跃用户 + 删除后的向量库重同步
#
#  /mm/facts 这两个接口是给本机网页用的（val=False，没有 MAICA token），
#  所以拿不到"这是谁"。办法：聊天/导入时顺手记下最近一次的用户 id，
#  网页就按这个用户读写 —— 本地后端只有一个玩家，够用且不会串。
# ---------------------------------------------------------------------------
_ACTIVE_USER_ID = None
_RESYNC_NEEDED = False


def set_active_user(uid):
    global _ACTIVE_USER_ID
    try:
        _ACTIVE_USER_ID = int(uid)
    except Exception:
        pass


def active_user_id():
    return _ACTIVE_USER_ID


def request_vector_resync():
    """网页删掉一条事实后调用：让下一轮聊天把向量库对齐一次。"""
    global _RESYNC_NEEDED
    _RESYNC_NEEDED = True


def take_vector_resync():
    global _RESYNC_NEEDED
    if _RESYNC_NEEDED:
        _RESYNC_NEEDED = False
        return True
    return False


def resync_pending(fsc):
    """每轮回复后调用：有删除待同步就起个后台任务。"""
    try:
        if not _RESYNC_NEEDED or _LOCK.locked():
            return False
        uid = active_user_id()
        if uid is None:
            try:
                uid = fsc.maica_settings.verification.user_id
            except Exception:
                return False
        asyncio.create_task(_resync(fsc, uid))
        return True
    except Exception as e:
        _log("resync_pending failed: %r" % (e,))
        return False


async def _resync(fsc, user_id):
    from maica.maica_utils.fact_store import MEMORY_SESSION, sync_player_facts
    take_vector_resync()
    try:
        async with _LOCK:
            res = await sync_player_facts(fsc, user_id, MEMORY_SESSION)
        _log("vector resync after delete: %r" % (res,))
    except Exception as e:
        _log("vector resync failed: %r" % (e,))
        request_vector_resync()      # 失败就下次再来