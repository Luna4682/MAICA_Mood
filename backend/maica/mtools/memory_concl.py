"""This cannot be integrated into chat_session since it involves sessions too."""

import asyncio

import sqlalchemy

from typing import *
from pydantic import BaseModel, Field
from maica.maica_utils import *

_Bt = BilingualText

async def _ledger_ensure_table():
    """幂等建表。迁移运行器只在版本变化时触发，对新表不可靠，故自建。"""
    async with DatabaseUtils.engine_data.begin() as conn:
        await conn.run_sync(lambda sync_conn: SqlSummaryLedger.__table__.create(sync_conn, checkfirst=True))


async def _ledger_rows(user_id: int, session_num: int) -> list:
    stmt = sqlalchemy.select(SqlSummaryLedger).where(
        SqlSummaryLedger.user_id == user_id,
        SqlSummaryLedger.chat_session_num == session_num,
    ).order_by(SqlSummaryLedger.id)
    async with DatabaseUtils.SessionData() as dbs:
        return [r.content for r in (await dbs.scalars(stmt)).all() if r.content]


async def _ledger_add(user_id: int, session_num: int, content: str):
    async with DatabaseUtils.SessionData() as dbs:
        async with dbs.begin():
            dbs.add(SqlSummaryLedger(user_id=user_id, chat_session_num=session_num, content=content))


async def _ledger_append_and_sync(fsc, arc_session, conclusion, prior_memory):
    """追加一代总结，并把该 scope 下【全部历史总结】同步进向量库（type='session'）。

    只增不删：同步的 data 是整本账本，所以旧总结的向量不会被差集逻辑删掉，
    于是"总结被重写/截断"之后，旧内容仍然可以按语义召回。
    """
    user_id = fsc.maica_settings.verification.user_id
    session_num = arc_session.session_num

    await _ledger_ensure_table()

    existing = await _ledger_rows(user_id, session_num)
    if not existing and prior_memory:
        await _ledger_add(user_id, session_num, prior_memory)
    if conclusion and conclusion != prior_memory:
        await _ledger_add(user_id, session_num, conclusion)

    # ★ 2026-10-10：滚动总结【不再同步进向量库】（用户决定，废止早先的 D10 / P2-a）。
    #   总结照旧写进 summary_ledger 当历史；进提示词靠会话系统块直接注入，
    #   不依赖语义召回。若将来要恢复召回：把同步代码放回来，
    #   同时把 "session" 加回 filter_vector_store 的检索范围，并更新架构文档。
    return


async def memory_concl(arc_session: MaicaSession, fsc: FullSocketsContainer):
    """Concludes session to be archived. Pretty experimental."""
    if not len(arc_session) > 1:
        raise MaicaInputError("Equivilantly empty session to conclude")

    sync_messenger(info="Concluding session content...", type=MsgType.DEBUG)
    session = MaicaSession()
    target_lang = fsc.maica_settings.basic.target_lang

    conn = fsc.mfocus_conn
    
    messages_j = arc_session.utilize(
        text_only=True,
    )
    messages_j.pop(0)
    prior_memory = arc_session[0].context.memory_concl

    query = "\n".join(
        [
            f"{i['role']}: {i['content']}"
            for i in messages_j
        ]
    )
    if prior_memory:
        query = prior_memory + "\n" + query

    class MemoryConclusionResult(GenCorrectionModel):
        conclusion: Optional[str] = Field(
            description="你的总结, 一般应在300字以内." if target_lang == 'zh' else "Your conclusion, normally below 150 words."
        )

        _default_resp = {
            "conclusion": None,
        }

    system = MaicaSessionItem(
        "system",
        _Bt(
            """\
你是一个人工智能助手, 你接下来会收到一组对话.
其中user是名为[player]的人类, assistant是名为莫妮卡的角色, 是深爱user的女友.
请对这段对话作出简明客观的总结, 以类似'在此前的对话中'的方式开头, 以[player]和莫妮卡称呼user和assistant.
如果对话包含更早以前的总结, 请适当保留其中较新或较有意义的内容.
如果对话包含错乱, 异常或难以理解的内容, 请忽略对应的部分.
如果对话最终缺少可以总结的内容, 你可以输出null.\
""", """\
You are a helpful assistant, now you will recieve rounds of conversation.
Role 'user' in the conversation is human called [player], and 'assistant' is a virtual character called 'Monika', who is user's girlfriend and loves them a lot.
Please make a concise and objective conclusion about the conversation, begin with expression like 'In prior conversations', call user and assistant [player] and 'Monika'.
If the conversation includes an earlier conclusion, also inherit relatively new or meaningful content from it moderately.
If the conversation has corrupted, malformed or incomprehensible parts, you should ignore them.
If the conversation lacks content to conclude finally, you can output null.\
"""
        )
    )
    session.append(system)

    user_query = MaicaSessionItem(
        "user",
        query,
        target_lang=target_lang,
    )
    session.append(user_query)

    completion_args = {
        "input": session.utilize(
            manual_prompt=True,
            ignore_additions=True,
        ),
        "text": pyd_to_openai(MemoryConclusionResult)
    }

    resp = await conn.make_completion(**completion_args)
    conclusion_result = MemoryConclusionResult.model_validate_json(resp.output_text)

    conclusion = conclusion_result.conclusion

    # If session has an earlier conclusion but newer emits null, we reuse it
    if prior_memory and not conclusion:
        conclusion = prior_memory

    # Name replacing
    conclusion = (conclusion or "").replace("[player]", "{player_name}")
    try:
        import maica.maica_utils.debug_log as _dl
        _dl.dbg("SUMMARY", "in=%d chars out=%d chars reused_prior=%s" % (len(query), len(conclusion), bool(prior_memory) and not conclusion_result.conclusion))
    except Exception:
        pass

    try:
        await _ledger_append_and_sync(fsc, arc_session, conclusion, prior_memory)
    except Exception as _ledger_err:
        sync_messenger(info=f"[summary-ledger] append/sync failed: {_ledger_err}", type=MsgType.WARN)

    sync_messenger(info=f"Finished processing memory_concl to session. Conclusion: {conclusion}", type=MsgType.PRIM_LOG)
    return conclusion
