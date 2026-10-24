"""
Import layer 4.2
"""
from __future__ import annotations

import asyncio
import orjson
import datetime

from typing import *
from enum import StrEnum
from math import ceil
from pydantic import BaseModel, Field, TypeAdapter, create_model
from random import sample
from dateutil.relativedelta import relativedelta
from .maica_utils import *
from .chat_session import *
from .fact_render import render_fact, format_recall_line, resolve_tz

if TYPE_CHECKING:
    from maica.maica_utils import FullSocketsContainer

_Bt = BilingualText


def _subst_player_name(text: str, pname: str = "") -> str:
    """把文本里的 `{player_name}` 换成存档里的真名。

    ★ 为什么要在这里换（2026-10-18）：占位符替换只发生在 system prompt 与 user 消息上
      （chat_session._utilize_context），工具结果不走那条路。而 recall 出来的条目
      （savefile_field / savefile_derived 有 90 多行）写的都是 `{player_name}`，
      不换的话莫妮卡看到的就是字面花括号。
    · pname 取不到（存档访问关闭）→ 原样返回，交给上层/前端处理。
    """
    try:
        t = str(text or "")
        return t.replace("{player_name}", str(pname)) if pname else t
    except Exception:
        try:
            return str(text or "")
        except Exception:
            return ""

# 向量召回的相似度下限（余弦距离，暂定 0.5）。低于它的命中一律不要。
# 依据实测：正确事实的相似度带宽约 0.52~0.72，无关句约 0.47，所以 0.5 是个合理起手值。
# 要调就改这一个数，别散在别处。
VECTOR_SIMILARITY_MIN = 0.5

# 低优先层的“强制召回”阈值（2026-10-24 用户定）。
# 原来三层是纯命中即停：player_direct 中了，field / derived 里有再强的命中也不带。
# 但实测会出现“同一件事在 derived 里有更贴切的旧表述”被整层丢掉的情况。
# 现在的规则：高优先层命中后，低优先层里相似度 >= 0.7 的条目仍然强制带上，
# 三层共用同一个 topk 名额（默认 5），高优先层先占。
VECTOR_FORCE_MIN = 0.7


def pick_layered_hits(rows_by_layer, topk=5, force_min=VECTOR_FORCE_MIN):
    """三层召回的取舍逻辑（纯函数，便于单测）。

    rows_by_layer: [(label, [ {"score", "ref_id", "raw_text"}, ... ]), ...]，按优先级从高到低。
    返回 [(label, item), ...]，顺序即注入顺序。

    规则：
      · 本层之前还没有任何命中 → 本层是最高优先命中，全部采用；
      · 本层之前已有命中 → 只采用相似度 >= force_min 的条目（强制召回）；
      · 三层合计不超过 topk，先到先得。
    """
    picked = []
    used = 0
    seen = set()
    for label, rows in rows_by_layer:
        if not rows:
            continue
        forced = bool(picked)
        for item in rows:
            if used >= topk:
                break
            try:
                score = float(item.get("score") or 0.0)
            except Exception:
                score = 0.0
            if forced and score < force_min:
                continue
            text = item.get("raw_text") or ""
            if text:
                if text in seen:
                    continue
                seen.add(text)
            picked.append((label, item))
            used += 1
        if used >= topk:
            break
    return picked


class SessionPersistentLlmMixin():

    session_num: int
    fsc: FullSocketsContainer
    content: dict
    content_temp: dict


    async def to_vector_store(self, _data: Optional[list] = None):
        """把这一会话的【存档类】内容同步进 SQLite（persist_items）与向量库索引。

        ★ 分工表（2026-10-10 定，四类标签各有归属）：
            knowledge        后端启动时统一写一次（全局一份，见 mtools/zsco/rag.py）
            savefile_derived 本函数负责
            savefile_field   本函数负责
            player_direct    由事实入库那条支线负责（fact_store.sync_player_facts）
            session          滚动总结【不进向量库】，只留 SQLite 流水表当历史

        ★ 权威在 SQLite：先经 persist_items 把整句文本写成行、拿到行号，
          再带 ref_id 同步进向量库；向量库那边只是索引。
        """
        if not self.fsc.is_vector_ready:
            return
        user_id = self.fsc.maica_settings.verification.user_id
        # ★ 2026-10-18：整句条目也跨会话通用 —— 统一写进 MEMORY_SESSION，
        #   不再跟着当前聊天会话号走（否则每换一次会话就存一份副本）。
        from maica.maica_utils.fact_store import MEMORY_SESSION
        session_num = MEMORY_SESSION

        from maica.maica_utils.persist_items import sync_items

        if _data is not None:
            # 兼容旧调用：空列表＝存档被删除，把两类【由存档生成】的内容一起清掉
            # （knowledge 是全局静态的，与存档无关，不动）；
            # 非空列表＝调用方直接提供存档类文本，按 savefile_derived 处理。
            if _data:
                await sync_items(self.fsc, user_id, session_num, "savefile_derived", _data)
            else:
                for source in ("savefile_derived", "savefile_field"):
                    await sync_items(self.fsc, user_id, session_num, source, set())
            return

        parts = self.form_info_parts()
        for source in ("savefile_derived", "savefile_field"):
            await sync_items(self.fsc, user_id, session_num, source, parts.get(source) or set())

        # ★ 2026-10-18：knowledge 兜底。启动时若嵌入服务还没就绪，启动那次会跳过；
        #   这里在"向量库已就绪 + 有会话"时补一次，库里已有就什么都不做。
        try:
            from maica.mtools.zsco.rag import ensure_knowledge
            await ensure_knowledge(self.fsc)
        except Exception:
            pass


    async def filter_vector_store(self, query: str, topk: int = 5) -> Set:
        """按【来源优先级】分层召回，返回权威文本的集合。

        规则（2026-10-10 定，2026-10-18 改 knowledge 与层序，2026-10-24 改强制召回）：
          · knowledge 是【旁路层】：独立用 0.5 阈值查，最多取相似度最高的 2 条，
            不参与"命中即停"。它讲的是莫妮卡和这个世界，不与玩家事实抢名额，
            但也不该无条件全带（无关话题会稀释 prompt）。
          · 玩家侧三层 player_direct > savefile_field > savefile_derived：
            三层都查，先命中那层全采用；更低优先层里相似度 >= VECTOR_FORCE_MIN(0.7)
            的条目【强制召回】，三层合计不超过 topk（默认 5），高优先层先占名额。
            同一层内部有多条就都返回。
            field（对话里记下的）优先于 derived（存档快照）—— 快照粗且旧。
          · 命中后一律用 ref_id 回 SQLite 取【权威文本】；向量库那份只当索引。
            player_direct 取回来的是结构化短句，会再拼成"谁 + 相对时间 + 说过"
            （见 _resolve_authoritative）；另外三层原样返回（只换 {player_name}）。
          · session（滚动总结）不再参与召回 —— 它不进向量库，只留 SQLite 流水表。
        """
        vector_pool = self.fsc.vector_pool
        if not self.fsc.is_vector_ready:
            return set()

        user_id = self.fsc.maica_settings.verification.user_id
        # ★ 2026-10-18：召回也查共享作用域 —— 记忆跨会话通用，
        #   换会话不该让她忘掉你亲口说过的事。
        from maica.maica_utils.fact_store import MEMORY_SESSION
        session_num = MEMORY_SESSION
        hits = []   # [(标签, ref_id, 兜底文本)]

        KNOWLEDGE_TOPK = 2      # 旁路层：最多 2 条
        KNOWLEDGE_MIN = 0.5     # 相似度阈值（与玩家侧一致）

        async def _search(label: str, scope_user: int, scope_session: int,
                          _topk: int = None, _min: float = None):
            try:
                return await vector_pool.search_with_refs(
                    embedding_conn=self.fsc.embedding_conn,
                    data=[query],
                    filters={
                        "user_id": scope_user,
                        "chat_session_num": scope_session,
                        "type": label,
                    },
                    topk=_topk if _topk else topk,
                    similarity_min=VECTOR_SIMILARITY_MIN if _min is None else _min,
                )
            except Exception:
                return []

        # ① knowledge：全局一份（user_id=-1, session=0），独立阈值、最多 2 条、不参与命中即停。
        #    search_with_refs 已按 score 降序返回，这里再切一刀保险。
        _k = await _search("knowledge", -1, 0, _topk=KNOWLEDGE_TOPK, _min=KNOWLEDGE_MIN)
        for item in _k[:KNOWLEDGE_TOPK]:
            hits.append(("knowledge", item.get("ref_id"), item.get("raw_text")))
        try:
            import maica.maica_utils.debug_log as _dl
            _dl.dbg("VEC-TIER", "knowledge 旁路层 %d 条（阈值 %.2f，最多 %d）"
                    % (len(_k[:KNOWLEDGE_TOPK]), KNOWLEDGE_MIN, KNOWLEDGE_TOPK))
        except Exception:
            pass

        # ② 玩家侧三层：逐层查，高优先层命中后，低优先层里 >= 0.7 的仍然强制带上
        #    ★ 2026-10-18 调序：savefile_field（对话里记下的）优先于 savefile_derived（存档快照）。
        #      理由：derived 是初始化时按话题字段一次性渲染的，之后不会更新，粗且旧；
        #      field 是这一局的对话里记下的，新且具体。让旧快照压新记忆是反的。
        #      该顺序与 fact_store 的 source_priority 一致：player_direct > field > derived。
        #    ★ 2026-10-24：不再是纯命中即停。三层都查，用 pick_layered_hits 统一取舍：
        #      先出现命中的那层全采用，之后的层只收 >= VECTOR_FORCE_MIN 的条目，合计不超过 topk。
        layer_rows = []
        for label in ("player_direct", "savefile_field", "savefile_derived"):
            rows = await _search(label, user_id, session_num)
            layer_rows.append((label, rows))
        chosen = pick_layered_hits(layer_rows, topk=topk)
        for label, item in chosen:
            hits.append((label, item.get("ref_id"), item.get("raw_text")))
        try:
            import maica.maica_utils.debug_log as _dl
            if chosen:
                _dl.dbg("VEC-TIER", "采用 %s"
                        % "；".join("%s(%.2f)" % (l, float(i.get("score") or 0.0)) for l, i in chosen))
        except Exception:
            pass

        # ④ 推测层（短期池，2026-10-19）：**只有在玩家侧三层都没命中时才出**，
        #    最多 1 条，阈值比其他层高一点（0.55），渲染成"好像说过"。
        #    它永远压不过明确事实 —— 这是"确定事实优先"的硬约束。
        if not chosen:
            SPEC_TOP = 1
            SPEC_MIN = 0.55
            spec = await _search("player_speculation", user_id, session_num,
                                 _topk=SPEC_TOP, _min=SPEC_MIN)
            for item in spec[:SPEC_TOP]:
                hits.append(("player_speculation", item.get("ref_id"), item.get("raw_text")))
            try:
                import maica.maica_utils.debug_log as _dl
                _dl.dbg("VEC-TIER", "推测层 %d 条（阈值 %.2f，最多 %d，弱语气）"
                        % (len(spec[:SPEC_TOP]), SPEC_MIN, SPEC_TOP))
            except Exception:
                pass

        return set(await self._resolve_authoritative(hits))


    @staticmethod
    def _dedupe_substrings(texts: list) -> list:
        """丢掉"被别的条目包含"的短条目（保留信息更全的那条）。

        背景（2026-10-18）：莫妮卡的 write_memory 会分多次记同一件事，粒度不同，
        比如 "{player_name}喜欢莫妮卡写的诗" 与 "{player_name}喜欢莫妮卡写的诗，尤其喜欢最后一句"。
        前端只做完全相等去重，所以两条都进了库；召回时同时返回就是重复。
        这里只在【召回结果】上做包含判定，不动 SQLite（历史仍完整，玩家可自行删）。
        """
        try:
            import re as _re
            def _key(s):
                return _re.sub(r"\s+", "", str(s or "")).rstrip("。.!！?？,，;；、 ")
            items = []
            for t in texts or []:
                k = _key(t)
                if k:
                    items.append((t, k))
            if len(items) < 2:
                return [t for t, _ in items]
            keep = []
            for i, (t, k) in enumerate(items):
                dropped = False
                for j, (_, k2) in enumerate(items):
                    if i == j:
                        continue
                    # 严格更长且包含 -> 当前这条是冗余
                    if len(k2) > len(k) and k in k2:
                        dropped = True
                        break
                if not dropped:
                    keep.append(t)
            return keep
        except Exception:
            return list(texts or [])

    async def _resolve_authoritative(self, hits: list) -> list:
        """把 (标签, ref_id, 兜底文本) 解析成【注入给莫妮卡的一行】：能回 SQLite 就回，回不到用兜底。

        向量库只是索引，所以命中后应当以 SQLite 那一行为准（D3）。

        ★ 2026-10-18：两类条目的组装方式不同 ——
          · player_direct（结构化事实）：用 fact_render 渲染成短句，再拼上
            "谁 + 相对时间 + 说过"，例如 `小明三天前说过：我写了一首诗`。
            时间在【召回这一刻】用 now - speak_time 按天现算（不存库，存下来的相对时间
            第二天就过期）。渲染失败/字段缺失时 render_fact 自己回退原话分句。
          · knowledge / savefile_field / savefile_derived：本来就是整句陈述，原样返回，
            只把 `{player_name}` 换成存档里的真名（tool result 不经过 system prompt 的
            占位符替换，不换的话莫妮卡会看到字面的 {player_name}）。

        返回前做一次包含关系去重（见 _dedupe_substrings）。
        """
        # 名字与时区：取不到就退回占位符 / UTC（savefile_access 关闭时就是 None）
        pname = ""
        try:
            pname = self.pname or ""
        except Exception:
            pname = ""
        tz = None
        target_lang = "zh"
        try:
            basic = self.fsc.maica_settings.basic
            target_lang = basic.target_lang or "zh"
            tz = resolve_tz(basic.tz, target_lang)
        except Exception:
            tz = None

        try:
            from maica.maica_utils import DatabaseUtils, SqlPersistFact, SqlPersistItem
            import sqlalchemy

            fact_ids, item_ids = [], []
            for label, ref_id, _ in hits:
                if not ref_id:
                    continue
                try:
                    rid = int(ref_id)
                except Exception:
                    continue
                if label in ("player_direct", "player_speculation"):
                    fact_ids.append(rid)
                else:
                    item_ids.append(rid)

            mapping = {}
            if fact_ids or item_ids:
                async with DatabaseUtils.SessionData() as dbs:
                    if fact_ids:
                        stmt = sqlalchemy.select(SqlPersistFact).where(SqlPersistFact.id.in_(fact_ids))
                        for r in (await dbs.scalars(stmt)).all():
                            weak = (getattr(r, "certainty", "") or "").strip() == "speculation"
                            line = format_recall_line(
                                render_fact(r),
                                getattr(r, "speak_time", None),
                                pname=pname,
                                tz=tz,
                                target_lang=target_lang,
                                weak=weak,
                            )
                            mapping[("player_direct", r.id)] = line
                            if weak:
                                mapping[("player_speculation", r.id)] = line
                    if item_ids:
                        stmt = sqlalchemy.select(SqlPersistItem).where(SqlPersistItem.id.in_(item_ids))
                        for r in (await dbs.scalars(stmt)).all():
                            mapping[(r.source, r.id)] = _subst_player_name(r.raw_text, pname)

            out = []
            for label, ref_id, fallback in hits:
                text = None
                if ref_id:
                    try:
                        text = mapping.get((label, int(ref_id)))
                    except Exception:
                        text = None
                out.append(text or fallback)
            return self._dedupe_substrings([t for t in out if t])
        except Exception:
            return self._dedupe_substrings(
                [_subst_player_name(fallback, pname) for _, _, fallback in hits if fallback])


    async def filter_reranker(self, query: str, documents: Optional[set] = None, topk: int = 2) -> list:
        """More precisely filter results, suggest using filter_vector_store first."""
        reranking_conn = self.fsc.reranking_conn
        if not self.fsc.is_reranking_ready:
            return []

        if documents is None:
            # ★ 2026-10-10 改：候选只来自向量库召回 —— 那边已经按来源分层（高优先层先占、
            #   低优先层仅强制召回 >=0.7 的条目），并用 ref_id 回 SQLite 取了权威文本。原先把整份 form_info(where='temp')
            #   并进来，等于绕过来源权威判定，低优先级层的内容会重新混入。
            if not self.fsc.is_vector_ready:
                return []
            documents = await self.filter_vector_store(query, 10)

        documents = list(documents)

        if not documents:
            return []

        reranking_params = {
            "query": query,
            "documents": documents,
            "top_n": topk,
        }

        resp = await reranking_conn.make_reranking(**reranking_params)

        cfd_min = 0.5
        res_list = []

        _relevances = []
        for i in resp["results"]:
            _relevances.append(i["relevance_score"])
            if i["relevance_score"] >= cfd_min:
                res_list.append(i["document"]["text"])

        sync_messenger(info=f"Reranking found {len(_relevances)} results" + (f", distance range {min(_relevances)}~{max(_relevances)}" if _relevances else ""), type=MsgType.DEBUG)
        sync_messenger(info=f"{len(res_list)} results adopted", type=MsgType.DEBUG)

        return res_list


    async def filter_llm(self, query: str, documents: Optional[set] = None, topk: int = 3) -> list:
        """Traditional MFocus sfe implementation."""
        session = MaicaSession()
        target_lang = self.fsc.maica_settings.basic.target_lang
        user_id = self.fsc.maica_settings.verification.user_id
        session_num = self.fsc.maica_settings.temp.chat_session
        conn = self.fsc.mnerve_conn or self.fsc.mfocus_conn

        if documents is None:
            # ★ 2026-10-10 改：同 filter_reranker —— 候选只来自向量库召回，
            #   不再把整份 savefile 内容并进来绕过来源权威判定。
            if not self.fsc.is_vector_ready:
                return []
            documents = await self.filter_vector_store(query, 10)

        documents = list(documents)

        if not documents:
            return []

        if TYPE_CHECKING:
            type DocEnum = str
        else:
            DocEnum = StrEnum(
                f"DocEnum_{user_id}_{session_num}",
                {
                    k: k for k
                    in documents
                }
            )

        class PersSelectionResults(GenCorrectionModel):
            items: list[DocEnum] = Field(
                min_length=0,
                max_length=topk,
                description=f"0到{topk}个最相关的条目, 原样输出." if target_lang == 'zh' else f"0 ~ {topk} most relevant items, output as-is."
            )

        system = MaicaSessionItem(
            "system",
            _Bt(
f"""\
你是一个人工智能助手, 你的任务是从信息中查找与问题最相关的条目.
你是角色"莫妮卡". 你应选择0到{topk}条互不重复的条目, 并原样输出.
如果没有任何条目与问题相关, 你可以输出空值.\
""",
f"""\
You are a helpful assistant, your task is finding most relevant items with the query from provided information.
Your character is called "Monika". You should choose 0 ~ {topk} unique items and output them as-is.
If none of the information is relevant with query, you can output empty.\
"""
            ),
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
            "text": pyd_to_openai(PersSelectionResults)
        }

        resp = await conn.make_completion(**completion_args)

        def _norm(_s):
            try:
                _s = _s if isinstance(_s, str) else str(_s)
            except Exception:
                _s = ""
            for _ch in (u"\u3002", u".", u"\uff0e", u" ", u"\u3000", u"\n"):
                _s = _s.replace(_ch, u"")
            return _s.strip().lower()

        # 1) 严格校验：模型完全按 schema 原样输出时最省事
        try:
            _strict = PersSelectionResults.model_validate_json(resp.output_text)
            return list(_strict.items)[:topk]
        except Exception:
            pass

        # 2) 宽松兜底：模型常会把 {player_name} 之类占位符替换成真实内容（这是
        #    自然且正确的行为），严格校验必然失败。这里按"归一化 / 包含"把条目
        #    对回原文档；再不行就整段放弃并返回空，**绝不抛异常**，避免整个回合报错。
        picked = []
        try:
            import json as _json
            _raw = _json.loads(resp.output_text)
            if isinstance(_raw, dict):
                _raw = _raw.get("items", [])
            if not isinstance(_raw, list):
                _raw = [_raw]
        except Exception:
            _raw = [x for x in str(resp.output_text).split("\n") if x.strip()]

        _doc_map = {}
        for _d in documents:
            _doc_map.setdefault(_norm(_d), _d)
        _norm_keys = list(_doc_map.keys())

        for _item in _raw:
            if not isinstance(_item, str):
                continue
            _k = _norm(_item)
            _hit = _doc_map.get(_k)
            if _hit is None:
                for _nd in _norm_keys:
                    if _nd and (_nd in _k or _k in _nd):
                        _hit = _doc_map[_nd]
                        break
            if _hit is not None and _hit not in picked:
                picked.append(_hit)
            if len(picked) >= topk:
                break

        sync_messenger(
            info="PersSelection strict validation failed; loosely recovered %d/%d items" % (len(picked), len(_raw)),
            type=MsgType.DEBUG,
        )
        return picked
    

class SessionTriggerLlmMixin():

    session_num: int
    fsc: FullSocketsContainer
    content: list
    content_temp: list


    async def predict_trigger(self, query: str):
        """We make st do this itself, since we used llm in sp already."""
        session = MaicaSession()
        target_lang = self.fsc.maica_settings.basic.target_lang
        conn = self.fsc.mnerve_conn or self.fsc.mfocus_conn

        text_l = []
        for tr in self._get_triggers():
            t, _ = tr.to_descr()
            if t:
                text_l.append(t)

        descr_text = list_to_bullets(text_l)

        if not descr_text:
            return False, None

        # Dynamic class here, since each time the enum changes
        # We also write the alternative non-precision way
        # if True:
        #     TrigSelectionResults = create_model(
        #         "TrigSelectionResults",
        #         item=(
        #             Optional[
        #                 Literal[*choices_l]
        #             ],
        #             Field(
        #                 ...,
        #                 description="你选择的条目, 原样输出." if target_lang == 'zh' else "The item you choose, output as-is."
        #             )
        #         )
        #     )
        # else:
        #     class TrigSelectionResults(BaseModel):
        #         item: Optional[str] = Field(
        #             description="你选择的条目, 原样输出." if target_lang == 'zh' else "The item you choose, output as-is."
        #         )

        # No that's dumb and costy. We just need to verify a true-or-false, if the query can be satisfied.
        class TrigSelectionResults(GenCorrectionModel):
            requested: bool = Field(
                description="是否涉及游戏操作." if target_lang == 'zh' else "If any in-game action is involved."
            )
            operation: Optional[str] = Field(
                description="你选择的工具, 原样输出." if target_lang == 'zh' else "The tool you choose, output as-is."
            )
            _default_resp = {
                "requested": False,
                "operation": None,
            }

        system = MaicaSessionItem(
            "system",
            _Bt(
"""\
你是一个人工智能助手, 你的任务是根据用户要求, 从提供的工具中作出选择.
你是角色"莫妮卡". 提供的工具均用于游戏内操作, 请严格遵循以下规则:
- 如果用户要求与除对话外的游戏操作无关, 对requested输出false.
- 如果有关, 对requested输出true.
    - 如果没有合适的工具满足要求, 或requested为false, 对operation输出null.
    - 如果有, 对operation输出对应的工具选择.
以下是工具列表:\
""",
"""\
You are a helpful assistant, your task is choosing from provided tools according to user's request.
Your character is called "Monika". Provided tools are all used for in-game actions, please precisely follow these rules:
- If user request does not involve in-game actions except chatting, output false in "requested" field.
- If it does involve, output true in "requested" field.
    - If none of provided tools could satisfy request, or "requested" field is false, output null in "operation" field.
    - If there is, output corresponding tool choice in "operaiton" field.
Here is the tools list:\
"""
            ) + descr_text,
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
            "text": pyd_to_openai(TrigSelectionResults)
        }

        resp = await conn.make_completion(**completion_args)
        selection_result = TrigSelectionResults.model_validate_json(resp.output_text)

        return selection_result.requested, selection_result.operation
