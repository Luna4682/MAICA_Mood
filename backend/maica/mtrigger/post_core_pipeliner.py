"""
And as pre_core_pipeliner, we need a pipeline to connect everything post-generation together.

And ofc we start from MTrigger.
This could also include many things like saving session and MSpire cache.
And quality check, for sure.
"""

from typing import *

from .mtrigger_llm import MtPipeliner
from maica.mtools import ms_to_cache, quality_chk, memory_concl
from maica.maica_utils import *

_Bt = BilingualText

async def _fact_ingest_bg(text: str, user_id: int, session_num: int, dry_run: bool, fsc=None, sync: bool = True):
    """后台：提取 → 漏斗 → 裁决。（dry_run 时只写日志，不落库）

    D17：本流程在回复【之后】运行；且由调用方 fire-and-forget，绝不阻塞对话。
    ★ 2026-10-18：记忆跨会话通用 —— 不管当前是哪个聊天会话，事实都写进
      MEMORY_SESSION 这个共享作用域（详见 fact_store.MEMORY_SESSION）。
    ★ 2026-10-22：**返回这次落库命中的事实行 id 列表**（问了同一件事的"重复提及"
      走 FACT-DUPLICATE 分支时返回已存在的那一行），供问卷导入记录"这一题产生了哪些行"。
      返回值语义：
         list（可能为空）  正常跑完；空 = r8 判定没信息 / 全被漏斗挡掉
         None              流程异常（调用方可以据此"下次再试"）
    """
    ids: list = []
    try:
        from maica.maica_utils.fact_store import MEMORY_SESSION
        session_num = MEMORY_SESSION
        import maica.maica_utils.debug_log as _dl
        from maica.maica_utils import DatabaseUtils
        from maica.mtools.fact_extract import extract_facts
        from maica.maica_utils.fact_store import (
            is_discardable, find_same_key, insert_candidate, resolve_conflicts,
            apply_conflicts, load_rules, fact_key, _source_rank, sync_player_facts,
        )
        # ★ speak_time：这句话【被认为是在什么时候说出来的】（2026-10-12 补）
        #   一句话一个时刻，同一句抽出的所有候选共用 ——
        #   这样同句的两条事实在裁决排序里完全同时，谁也不会压掉谁。
        #   不是在 DB 写入时才取，也不是 auto-update；只在"确认要落库"这一刻取一次。
        #   ★ 2026-10-18：改到【提取之前】取 —— 提取器没就绪时会等 20 秒再重试，
        #     放在后面会让"说的时刻"整体晚 20 秒，而它表示的是玩家说话的时间。
        import datetime as _dt
        speak_time = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        # ★ 提取器已接上本地小模型 r8：fsc 里带着 extract_conn（MAICA_EXTRACT_ADDR）
        #   提示词取唯一来源 _tools/system_prompt.txt，解析走 fact_json.parse_facts
        cands = await extract_facts(text, fsc)
        if not cands:
            _dl.dbg("FACT-NONE", "no fact extracted", payload=text)
            return ids
        order = list(load_rules().get("source_priority") or [])
        for cand in cands:
            # 实体链接：代称不写死在代码里，全部走 advanced/mappings.json（开源可改）
            from maica.maica_utils.fact_store import resolve_entity
            cand["subject"] = resolve_entity(cand.get("subject_raw") or "")
            cand["object"] = (cand.get("object_raw") or "").strip()
            bad, why = is_discardable(cand.get("fact_text") or "")
            if bad:
                _dl.dbg("FACT-DISCARD", "layer=funnel2 reason=%s" % why, payload=cand.get("fact_text"))
                continue
            _dl.dbg("FACT-CANDIDATE",
                    "pred=%s obj=%s pol=%s certainty=%s self_contained=%s" % (
                        cand.get("predicate"), cand.get("object"), cand.get("polarity"),
                        cand.get("certainty"), cand.get("self_contained")),
                    payload=cand.get("fact_text"))
            async with DatabaseUtils.SessionData() as dbs:
                same = await find_same_key(dbs, user_id, session_num, cand)
            # ★ 2026-10-18：这里原来还会比"来源优先级"，但 persist_facts 里只有
            #   player_direct（唯一写入方就是本函数），比较恒为 True —— 等于死代码。
            #   现在只保留可观测性（预告会取代哪一行）+ dry_run 拦截。
            if same:
                _dl.dbg("FACT-WOULD-SUPERSEDE", "existing=#%s key=%s" % (
                    getattr(same[0], "id", None), list(fact_key(same[0]))))
            if dry_run:
                _dl.dbg("FACT-WOULD-INSERT", "key=(%s,%s,%s)" % (
                    cand.get("subject"), cand.get("predicate"), cand.get("object")))
                continue

            # ★ 2026-10-18 新增：同键 + 同极性 + 措辞几乎相同 → 不新增行，
            #   只把"最后一次提及时间"刷新到这一行上（避免同一件事反复入库把库撑大）。
            _dup = None
            for _r in same:
                if getattr(_r, "superseded_by", None):
                    continue
                try:
                    if int(getattr(_r, "polarity", 1) or 1) != int(cand.get("polarity") or 1):
                        continue
                except Exception:
                    continue
                # 确定性不同（explicit vs speculation）不算重复 —— 那是语义变化，要保留
                if (getattr(_r, "certainty", "") or "explicit") != (cand.get("certainty") or "explicit"):
                    continue
                # 时间词不同（"昨天喜欢" vs "喜欢"）也不合并，避免丢时间信息
                _te_old = getattr(_r, "time_expression", None) or None
                _te_new = cand.get("time_expression") or None
                if _te_old != _te_new and (_te_old or _te_new):
                    continue
                if near_same_text(getattr(_r, "fact_text", ""), cand.get("fact_text") or ""):
                    _dup = _r
                    break
            if _dup is not None:
                _dup_id = getattr(_dup, "id", None)
                try:
                    async with DatabaseUtils.SessionData() as dbs:
                        async with dbs.begin():
                            from maica.maica_utils import SqlPersistFact
                            _row = await dbs.get(SqlPersistFact, _dup_id)
                            if _row is not None:
                                _row.speak_time = speak_time   # 刷新"最后一次提及"
                                # ★ 2026-10-19：推测类重复提及 = 续期（TTL 从最新一次说起算）
                                if (getattr(_row, "certainty", "") or "").strip() == "speculation":
                                    import datetime as _dt
                                    from maica.maica_utils.fact_store import _speculation_ttl_days
                                    _row.expires_at = (speak_time or _dt.datetime.utcnow()) + \
                                        _dt.timedelta(days=_speculation_ttl_days())
                except Exception:
                    pass
                _dl.dbg("FACT-DUPLICATE", "refresh speak_time on #%s (no new row)" % _dup_id)
                if _dup_id is not None:
                    ids.append(int(_dup_id))
                continue

            async with DatabaseUtils.SessionData() as dbs:
                async with dbs.begin():
                    new_row = await insert_candidate(dbs, user_id, session_num, cand,
                                                     speak_time=speak_time)
                    new_id = None
                    try:
                        await dbs.flush()          # 拿自增主键（commit 前就 flush 出来）
                        new_id = int(getattr(new_row, "id", None) or 0) or None
                    except Exception:
                        new_id = None
            if new_id is not None:
                ids.append(new_id)
            async with DatabaseUtils.SessionData() as dbs:
                allrows = await find_same_key(dbs, user_id, session_num, cand)
            actions = resolve_conflicts(allrows)
            if actions:
                async with DatabaseUtils.SessionData() as dbs:
                    async with dbs.begin():
                        await apply_conflicts(dbs, actions)
                _dl.dbg("FACT-APPLIED", "superseded %d" % len(actions))
        # 落库完成后再把【当前生效的玩家事实】同步进向量库（带 ref_id 指回本行），
        # 否则会出现"记得下、想不起来"：SQLite 有、向量库搜不到。
        # sync=False 时由调用方（问卷批量导入）统一同步一次，避免每句都重建向量
        if not dry_run and fsc is not None and sync:
            _res = await sync_player_facts(fsc, user_id, session_num)
            _dl.dbg("FACT-VECTOR-SYNC", "player_direct total=%s linked=%s" % (
                _res.get("total"), _res.get("linked")))
        return ids
    except Exception as exc:
        try:
            import maica.maica_utils.debug_log as _dl2
            _dl2.dbg("FACT-ERROR", "ingest failed: %r" % (exc,))
        except Exception:
            pass
        return None


async def post_core_pipelines(
        session: MaicaSession,
        fsc: FullSocketsContainer,
        sp: SessionPersistent,
        st: SessionTrigger,
    ):
    """Schedule everything here."""
    session_item = session[-1]

    async def fact_ingest_pipeline():
        """P3-C：回复之后才做事实入库，且 fire-and-forget（绝不阻塞、绝不延长会话锁）。"""
        # ★ 2026-10-22：不管要不要入库，先记下"这轮是谁在聊" —— 网页的 /mm/facts
        #   用它定位用户；以前只在有问卷脏行时才记，导致网页永远"没拿到用户 id"。
        try:
            from maica.mtools.questionnaire import note_user as _q_note
            _q_note(fsc)
        except Exception:
            pass
        try:
            from maica.maica_utils.fact_store import facts_settings
            cfg = facts_settings()
            if not cfg.get("enabled"):
                # ★ 2026-10-18：别静默。否则"没有事实"会被误判成提取器坏了。
                try:
                    import maica.maica_utils.debug_log as _dl_off
                    _dl_off.dbg("FACT-DISABLED", "facts.enabled=false, pipeline skipped")
                except Exception:
                    pass
                return
            # 收尾阶段 session[-1] 已经是莫妮卡的回复，必须回头找最后一条玩家消息
            last_user = None
            for _it in reversed(session):
                if getattr(_it, "role", None) == "user":
                    last_user = _it
                    break
            text = str(getattr(last_user, "content", "") or "") if last_user is not None else ""
            if not text.strip():
                try:
                    import maica.maica_utils.debug_log as _dl_skip
                    _dl_skip.dbg("FACT-SKIP", "no usable user message in session")
                except Exception:
                    pass
                return
            # ★ 2026-10-22：问卷有新答案就先排一个后台导入（和聊天事实同一套流水线）
            try:
                from maica.mtools.questionnaire import maybe_schedule as _q_maybe
                _q_maybe(fsc)
            except Exception:
                pass
            # 网页里删过事实 → 让下一轮把向量库对齐一次（否则召回还能搜到删掉的）
            try:
                from maica.mtools.questionnaire import resync_pending as _q_resync
                _q_resync(fsc)
            except Exception:
                pass
            import asyncio as _aio
            _aio.create_task(_fact_ingest_bg(
                text,
                fsc.maica_settings.verification.user_id,
                session.session_num,
                bool(cfg.get("dry_run", True)),
                fsc,
            ))
        except Exception:
            pass

    async def mt_pipeline():
        """
        I think this could go parallelly with other pipelines.
        """
        if (
            fsc.maica_settings.use_mt_now
        ):

            mtp = MtPipeliner(session, fsc, sp, st)
            await mtp.run_mt_pipeline()

    async def quality_chk_pipeline():
        """gen_quality_chk implementation."""
        if (
            fsc.maica_settings.extra.gen_quality_chk
            and len(session) >= 3 * 2 + 1
        ):
            res, cfd = await quality_chk(session, fsc)
            await fsc.messenger(
                'maica_quality_status',
                [res, cfd],
                200,
                no_print=True,
            )

    async def save_ms_pipeline():
        """If we have ms cache to save."""
        if (
            fsc.maica_settings.temp.mspire._mfc_m
            and fsc.maica_settings.temp.mspire._mfc_m.hash
            and not fsc.maica_settings.temp.mspire._mfc_m.result
        ):
            mfc_m = fsc.maica_settings.temp.mspire._mfc_m
            mfc_m.result = session_item.content

            await ms_to_cache(mfc_m, fsc)

    async def save_session_pipeline():
        """Saving session. Also making memory conclusion if should."""
        if (
            fsc.maica_settings.temp.chat_session > 0
        ):
            
            archiver, stat = await session.crop_length()
            if len(archiver) > 1:

                if fsc.maica_settings.extra.memory_concl_arc >= 1:
                    # Means we should conclude memory on trim
                    concl = await memory_concl(archiver, fsc)

                    if concl:
                        session[0].context.memory_concl = concl

                await archiver.to_partial_archive()

            await session.to_db()

            match stat:
                case 2:
                    await fsc.messenger(
                        'maica_history_sliced',
                        f"Session exceeded {fsc.maica_settings.basic.session_len_limit} characters and sliced",
                        204,
                    )
                case 1:
                    await fsc.messenger(
                        'maica_history_slice_hint',
                        f"Session exceeded {fsc.maica_settings.basic.session_len_limit * (2/3)} characters, will slice at {fsc.maica_settings.basic.session_len_limit}",
                        200,
                        no_print=True,
                    )
                case _:
                    sync_messenger(info="Session stored, length acceptable", type=MsgType.DEBUG)

    # Finally, form all these together
    tasks_stages: list[list[Callable[[], Awaitable]]] = [
        [
            mt_pipeline,
            quality_chk_pipeline,
            save_ms_pipeline,
        ],
        [
            save_session_pipeline, # We make it wait to avoid failure behavior inconsistency
        ],
        [
            fact_ingest_pipeline,  # P3-C：回复之后才入库；本身 fire-and-forget，不阻塞
        ],
    ]

    await run_staged_tasks(tasks_stages)

    # And we should be good to move on
