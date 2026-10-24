# -*- coding: utf-8 -*-
"""结构化事实库：落库漏斗与冲突裁决（P3-B）。

设计要点（详见《需求与实现路径.md》§7）：

1. 漏斗第②层用【整句锚定】，**绝不用子串匹配** ——
   否则"我喜欢睡觉前听音乐"会因为含"睡觉"而被误杀。
2. 裁决**不看任何分数**：先看来源优先级（§7.2 四档），同源再看 updated_at。
3. `enabled`（玩家显式开关）与 `superseded_by`（被更新取代）是两个字段，
   裁决**只写 `superseded_by`**，绝不碰 `enabled`（D12）。
4. 一切失败都降级为"不改动"，绝不抛异常影响对话。

本模块**尚未接入对话流水线**（等小模型的提取接上），因此当前对现有行为零影响。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------
# 配置：advanced/hard_rules.json（D22 配置外置；坏了就回退内置默认）
# --------------------------------------------------------------------------

_DEFAULT_RULES: dict[str, Any] = {
    "discard_exact": [
        "你好", "您好", "在吗", "在么", "早", "早安", "晚安", "下午好", "晚上好",
        "哈哈", "哈哈哈", "呵呵", "嗯", "哦", "噢", "啊", "呃", "额",
        "好", "好的", "行", "是的", "对", "是", "谢谢", "谢了",
    ],
    "discard_max_chars": 8,
    "source_priority": ["player_direct", "savefile_field", "savefile_derived", "knowledge"],
}

_RULES_CACHE: dict[str, Any] = {}


def _rules_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 后端根目录（3 层）
    return os.path.join(root, "advanced", "hard_rules.json")


def load_rules() -> dict[str, Any]:
    """读取硬规则；任何失败都回退默认值。"""
    try:
        path = _rules_path()
        mtime = os.path.getmtime(path)
        if _RULES_CACHE.get("mtime") == mtime:
            return _RULES_CACHE["rules"]
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        rules = dict(_DEFAULT_RULES)
        if isinstance(raw, dict):
            for key in ("discard_exact", "discard_max_chars", "source_priority"):
                if key in raw:
                    rules[key] = raw[key]
        _RULES_CACHE["mtime"] = mtime
        _RULES_CACHE["rules"] = rules
        return rules
    except Exception:
        return dict(_DEFAULT_RULES)


# --------------------------------------------------------------------------
# 漏斗第②层：后端硬规则（整句锚定）
# --------------------------------------------------------------------------

_FILLER_CHARS = set("嗯哦噢啊呃额哈哈呵嘿诶唉呀吧么嘛的了呢哎哟喂~～")


def is_discardable(text: str) -> tuple[bool, str]:
    """判断一句话是否应当在硬规则层被丢弃。返回 (丢弃?, 原因)。"""
    try:
        rules = load_rules()
        t = re.sub(r"\s+", "", str(text or ""))
        if not t:
            return True, "empty"
        if t in set(rules.get("discard_exact") or []):
            return True, "exact-greeting"
        core = re.sub(r"[，。！？、,.!?~～…]+", "", t)
        limit = int(rules.get("discard_max_chars") or 8)
        if core and len(core) <= limit and all(ch in _FILLER_CHARS for ch in core):
            return True, "filler-only"
        return False, ""
    except Exception:
        return False, ""


# --------------------------------------------------------------------------
# 裁决引擎（纯函数 + 应用两步，便于测试与回放）
# --------------------------------------------------------------------------

def fact_key(row) -> tuple:
    """冲突判定的键：主语 + 【体标记归一后的】谓语 + 宾语（宾语小写归一）。

    ★ 谓语必须归一（养过/养了→养，答应过→答应），否则同一条事实会因为
      "写哪个形式"不同而分裂成两个键，裁决（新的取代旧的）就失效。
      归一实现与评测台共用：maica/mtools/fact_json.py 的 normalize_predicate()。
    """
    from maica.mtools.fact_json import normalize_predicate, normalize_object
    obj = normalize_object(getattr(row, "object", None))
    return (getattr(row, "subject", "") or "",
            normalize_predicate(getattr(row, "predicate", "") or ""), obj)


def _source_rank(source: str, order: list) -> int:
    try:
        return order.index(source)
    except Exception:
        return len(order)  # 未知来源排最后


def near_same_text(a: str, b: str, ratio: float = 0.9) -> bool:
    """两句话是不是"几乎同一件事"（用于同键去重，纯确定性，不调模型）。

    两步：
      ① 去空白/标点，并剥掉不改变事实的程度词与语气词（真的/很/非常/平时/呀…）
      ② 剥完相等 → True；否则用 difflib 序列相似度 ≥ ratio → True

    ★ 只在【同键 + 同极性】的前提下使用（见 post_core_pipeliner 的前置去重）：
      同键已保证主语/谓语/宾语一致，极性保证态度一致，这里只判断"措辞是否近乎重复"。
    ★ 为什么不用"字符多重集重合度"：它会把「我今天去考试了」和「我昨天去考试了」
      判成 0.86 相似（今天/昨天 各占一字），而序列相似度是 0.71，能正确区分。
    """
    import re as _re
    from difflib import SequenceMatcher

    _FILLERS = ("真的", "非常", "特别", "其实", "平时", "最近", "有点", "稍微",
                "很", "挺", "也", "都", "还", "就",
                "呀", "啊", "呢", "吧", "哦", "嘛", "啦", "哈")

    def _k(s):
        t = _re.sub(r"[\s，。！？、,.!?~～…；;：:\"'“”‘’()（）\[\]【】]", "", str(s or ""))
        for w in _FILLERS:
            t = t.replace(w, "")
        return t

    ka, kb = _k(a), _k(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    return SequenceMatcher(None, ka, kb).ratio() >= ratio


def _row_ts(row) -> float:
    """裁决用时间戳：**优先 speak_time**，取不到才退回 updated_at。

    ★ 为什么必须优先 speak_time（2026-10-12 修）：
      `updated_at` 带 onupdate=func.now()，**任何 ORM 写入都会把它顶到当前时刻**
      —— 包括 `sync_player_facts()` 回写 vector_id 这种纯内部操作。
      用它排序，等于"谁最后被代码碰过谁就赢"，而不是"谁最后说的谁赢"。
      `speak_time` 在插入时写一次、之后绝不 auto-update（migration_8 就是为它加的）。
    ★ 回退到 updated_at 的原因：历史行（2026-10-12 之前入库的）speak_time 为空，
      不设回退会让它们全部并列成 0.0，反而失去顺序。
    """
    ts = getattr(row, "speak_time", None)
    if ts is None:
        ts = getattr(row, "updated_at", None)
    try:
        return ts.timestamp() if ts else 0.0
    except Exception:
        return 0.0


def resolve_conflicts(rows: Iterable) -> list[dict]:
    """对一组事实行做冲突裁决，返回动作列表（**不改数据**，纯函数）。

    排序依据（完全不含分数）：
      ① 来源优先级（player_direct ≈ savefile_field > savefile_derived > knowledge）
      ② 同源时，**speak_time** 新者优先（取不到才退回 updated_at）
      ③ 再平手时，id 大者优先（后写入的视为更晚）

    ★ 同键可能有多行（同一句话抽出两条事实时 fact_text 相同，但键不同；
      真正同键的多行来自"同一条事实被说了多次"）。此时只留 rank 最高的一行，
      **其余全部被它取代** —— 早先的实现是两两比较，会出现"两行互相取代"的
      无意义动作（胜负取决于 sort_key 的稳定性），现在改成一对多。
    """
    actions: list[dict] = []
    try:
        order = list(load_rules().get("source_priority") or _DEFAULT_RULES["source_priority"])
        groups: dict[tuple, list] = {}
        for row in rows:
            if getattr(row, "superseded_by", None):
                continue
            key = fact_key(row)
            if not any(key):
                continue
            groups.setdefault(key, []).append(row)

        for key, group in groups.items():
            if len(group) < 2:
                continue

            def sort_key(r):
                # 注意（2026-10-18）：persist_facts 目前只存 player_direct（唯一写入方是
                # post_core_pipeliner 的 _fact_ingest_bg），所以 source_rank 恒等 ——
                # 真正决定赢家的是后两项：speak_time 新者优先、再平手时 id 大者优先。
                # 第一项保留但恒等：将来若接入 savefile_* 晋升 fact 或历史事实导入
                # （表上 source 列默认值就是 savefile_generated），它会重新有意义。
                # ★ 2026-10-19：certainty 插在来源之后、时间之前 ——
                #   明确事实永远赢过推测（推测只能当"被取代者"，不能当"取代者"）。
                _cert = (getattr(r, "certainty", "") or "explicit").strip()
                return (_source_rank(getattr(r, "source", ""), order),
                        0 if _cert != "speculation" else 1,
                        -_row_ts(r),
                        -(getattr(r, "id", 0) or 0))

            ranked = sorted(group, key=sort_key)
            winner = ranked[0]
            for loser in ranked[1:]:
                same_source = getattr(loser, "source", "") == getattr(winner, "source", "")
                actions.append({
                    "winner": getattr(winner, "id", None),
                    "loser": getattr(loser, "id", None),
                    "key": key,
                    "reason": "同源，更新的取代较旧的" if same_source else "来源优先级更高",
                    "winner_text": getattr(winner, "fact_text", ""),
                    "loser_text": getattr(loser, "fact_text", ""),
                })
    except Exception:
        pass
    return actions


async def apply_conflicts(dbs, actions: list[dict]) -> int:
    """把裁决结果写回：**只设 `superseded_by`，不碰 `enabled`**（D12）。"""
    from maica.maica_utils import SqlPersistFact
    applied = 0
    try:
        for act in actions:
            if act.get("loser") is None or act.get("winner") is None:
                continue
            # 异步会话的 get 是协程，必须 await —— 漏掉会变成静默空操作
            row = await dbs.get(SqlPersistFact, act["loser"])
            if row is None or row.superseded_by:
                continue
            row.superseded_by = act["winner"]
            applied += 1
    except Exception:
        pass
    return applied

# --------------------------------------------------------------------------
# 配置与入库（P3-C）：`config.json` 的 facts 段，默认 dry_run=True（安全）
# --------------------------------------------------------------------------

_FACTS_DEFAULT = {"enabled": True, "dry_run": True,
                  # 推测类短期池：存多久、过期多久后硬删
                  "speculation_ttl_days": 7, "speculation_purge_days": 30}


def facts_settings() -> dict:
    """读取事实库开关；任何失败都回退默认（enabled + dry_run）。"""
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config.json")
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        section = raw.get("facts") if isinstance(raw, dict) else None
        cfg = dict(_FACTS_DEFAULT)
        if isinstance(section, dict):
            for key in _FACTS_DEFAULT:
                if key in section:
                    cfg[key] = section[key]
        return cfg
    except Exception:
        return dict(_FACTS_DEFAULT)


def _speculation_ttl_days() -> int:
    try:
        v = int(facts_settings().get("speculation_ttl_days") or 7)
        return v if v > 0 else 7
    except Exception:
        return 7


def _speculation_purge_days() -> int:
    try:
        v = int(facts_settings().get("speculation_purge_days") or 30)
        return v if v >= 0 else 30
    except Exception:
        return 30


def _utcnow():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


async def find_same_key(dbs, user_id: int, session_num: int, cand: dict) -> list:
    """找出与候选事实同键（主语+谓语+宾语）的现有行。"""
    from maica.maica_utils import SqlPersistFact
    import sqlalchemy
    try:
        # ★ 不能按 predicate/object 精确匹配：体标记（过/了/着）与切分差异
        #   会让同一条事实变成两个键。改为按【主体】取回，再用归一后的键在 Python 里比对。
        stmt = sqlalchemy.select(SqlPersistFact).where(
            SqlPersistFact.user_id == user_id,
            SqlPersistFact.chat_session_num == session_num,
            SqlPersistFact.subject == (cand.get("subject") or "player"),
        )
        from maica.mtools.fact_json import normalize_predicate, normalize_object
        want = (cand.get("subject") or "player",
                normalize_predicate(cand.get("predicate") or ""),
                normalize_object(cand.get("object")))
        return [r for r in (await dbs.scalars(stmt)).all() if fact_key(r) == want]
    except Exception:
        return []


async def insert_candidate(dbs, user_id: int, session_num: int, cand: dict,
                           source: str = "player_direct", speak_time=None):
    """把候选事实写成一行（调用方负责事务）。

    `speak_time`：这条事实被认为是在什么时候说出来的。
      调用方（post_core_pipeliner）传入**同一句解析出的所有候选共用的同一个 now**，
      这样"同一句话的两条事实"在裁决排序里完全同时，不会一个压另一个。
      不传时取当前 UTC 时间（保证字段永不为空，让旧排序路径不至于退化成 0.0）。
    """
    from maica.maica_utils import SqlPersistFact
    if speak_time is None:
        import datetime as _dt
        speak_time = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    row = SqlPersistFact(
        user_id=user_id,
        chat_session_num=session_num,
        subject=cand.get("subject") or "player",
        predicate=cand.get("predicate") or "",
        object=cand.get("object"),
        polarity=int(cand.get("polarity") or 1),
        fact_text=cand.get("fact_text") or "",
        kind=cand.get("kind") or "other",
        certainty=cand.get("certainty") or "explicit",
        source=cand.get("source") or source,
        enabled=True,
        time_expression=cand.get("time_expression"),
        time_source="explicit_text" if cand.get("time_expression") else "send_time",
        # ★ speak_time：这条事实【被认为是在什么时候说出来的】（2026-10-12 补）
        #   由后端在"确认要落库"那一刻传入 now；写一次，之后绝不 auto-update。
        #   用途：① 裁决排序（见 _row_ts）② 召回后连事实一起返回，让模型知道记忆新旧。
        #   注意：这里绝不能用 server_default=now —— 那会退化成"DB 写入时间"。
        speak_time=speak_time,
    )
    # ★ 2026-10-19：推测类进【短期池】—— 到期自动失活。
    #   TTL 从 speak_time 起算（不是 DB 写入时间），续期时由调用方刷新 speak_time。
    if (cand.get("certainty") or "").strip() == "speculation":
        import datetime as _dt
        base = speak_time or _utcnow()
        row.expires_at = base + _dt.timedelta(days=_speculation_ttl_days())
    dbs.add(row)
    return row
# --------------------------------------------------------------------------
# 实体代称与词表映射（开源可改，D-open：不把任何人物代称写死在代码里）
# --------------------------------------------------------------------------

_MAP_DEFAULT = {
    "entities": {
        "shared": ["我们", "咱俩", "两人", "一起"],
        "monika": ["莫妮卡", "莫妮", "小莫", "莫", "Monika", "monika", "moni", "你"],
        "player": ["{player_name}", "玩家", "我", "俺", "咱", "自己", "user"],
    },
}
_MAP_CACHE: dict = {}


def load_mappings() -> dict:
    """读取 advanced/mappings.json；任何失败都回退默认值。

    ★ 2026-10-18：原表里的 kinds / predicates / predicates_synonym 已删除 ——
      代码里没有任何地方读它们（谓语只受第四道否决的"子串校验"约束）。
      留着只会让人误以为系统在管谓语。实体代称表 entities 保留，resolve_entity 在用。
    """
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            "advanced", "mappings.json")
        mtime = os.path.getmtime(path)
        if _MAP_CACHE.get("mtime") == mtime:
            return _MAP_CACHE["maps"]
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        maps = dict(_MAP_DEFAULT)
        if isinstance(raw, dict):
            for key in ("entities",):
                if key in raw:
                    maps[key] = raw[key]
        _MAP_CACHE["mtime"] = mtime
        _MAP_CACHE["maps"] = maps
        return maps
    except Exception:
        return dict(_MAP_DEFAULT)


# 「自己」是反身代词：只有当它【处在主语位置】时才等于第一人称（2026-10-12 用户裁决）。
#   ✓ 「自己最近很累」        → 主语是自己 → player
#   ✗ 「开一家自己的甜品店」    → 定语，句子主语仍是"我" → 这条不该由"自己"命中
#   ✗ 「给自己搭配饮食」/「答应过自己」→ 与格/宾语位置 → 不算
# 实现上由 resolve_entity 做"主语位置"检查：别名必须出现在 text 的【开头】或
# 某个标点之后。而 resolve_entity 的输入是模型抽出的 subject_raw 字段本身，
# 所以实际等价于"只有当 自己 就是主语字段时才算"。
_SUBJECT_POS_ONLY = ("自己",)
_CLAUSE_SEP = "，,。.！!？?；;、：: \t\n"


def _in_subject_position(text: str, alias: str) -> bool:
    """别名是否处在【主语位置】：出现在句首或某个标点/空白之后。"""
    start = 0
    while True:
        k = text.find(alias, start)
        if k < 0:
            return False
        if k == 0 or text[k - 1] in _CLAUSE_SEP:
            return True
        start = k + 1


def resolve_entity(raw: str, player_name: str = "") -> str:
    """把代称映射成规范实体名（player / monika / shared / other）。

    ★ 不写死任何人名：全部来自 advanced/mappings.json，他人可自行扩充。
    匹配时按"叫法长度从长到短"，避免『我们』被『我』抢走。
    ★ 位置敏感词（见 _SUBJECT_POS_ONLY）：只有处在主语位置才命中。
      主要用于「自己」——它在宾语/定语里出现时（答应过自己 / 自己的甜品店）
      不得把整句判成玩家的自我事实。
    """
    try:
        maps = load_mappings().get("entities") or {}
        text = str(raw or "").strip()
        if player_name:
            text = text.replace("{player_name}", str(player_name))
        pairs = []
        for canon, aliases in maps.items():
            for alias in (aliases or []):
                a = str(alias).replace("{player_name}", str(player_name or ""))
                if a:
                    pairs.append((len(a), canon, a))
        for _, canon, alias in sorted(pairs, key=lambda x: -x[0]):
            if alias in text:
                if alias in _SUBJECT_POS_ONLY and not _in_subject_position(text, alias):
                    continue
                return canon
        return "other"
    except Exception:
        return "other"

# --------------------------------------------------------------------------
# 玩家事实 → 向量库（四类标签之一：player_direct）
# --------------------------------------------------------------------------

# 四类标签（2026-10-10 定）。persist_facts 只装 player_direct，
# 另外三类在 persist_items（见 maica_utils/persist_items.py）。
VECTOR_LABELS = ("knowledge", "player_direct", "savefile_derived", "savefile_field")

# ★★ 记忆的作用域（2026-10-18 用户决定）：事实与整句条目【跨会话通用】。
#   它们是"关于这个玩家"的记忆，不该因为换了个聊天窗口就消失。
#   所以 persist_facts / persist_items 统一写进 MEMORY_SESSION 这个作用域，
#   与当前对话会话号（0~9）无关。
#   仍然按会话隔离的只有：chat_session（对话原文）、summary_ledger（滚动总结）、
#   triggers（触发词）、persistents（存档快照，跟着上传走）。
MEMORY_SESSION = 0

# 迁移期遗留的旧叫法 → 归到哪一类
_LEGACY_SOURCE_MAP = {
    "savefile_generated": "savefile_derived",
}


def label_of_source(source: str) -> str:
    """事实行的 source 归到四类标签之一；未知一律按玩家亲口说的处理。"""
    s = (source or "").strip()
    if s in VECTOR_LABELS:
        return s
    return _LEGACY_SOURCE_MAP.get(s, "player_direct")


async def expire_speculations(user_id: int, session_num: int) -> dict:
    """短期池清理：到期的推测置 enabled=0；过期超过 purge 天直接删行（可审计窗口）。

    为什么是"懒清理"：插件是个随游戏起停的进程，没有常驻调度器；
    每次事实同步前顺手扫一遍，代价是一次索引查询，足够及时。
    """
    from maica.maica_utils import DatabaseUtils, SqlPersistFact
    import sqlalchemy
    now = _utcnow()
    purge_before = now - __import__("datetime").timedelta(days=_speculation_purge_days())
    disabled = purged = 0
    try:
        async with DatabaseUtils.SessionData() as dbs:
            rows = (await dbs.scalars(sqlalchemy.select(SqlPersistFact).where(
                SqlPersistFact.user_id == user_id,
                SqlPersistFact.chat_session_num == session_num,
                SqlPersistFact.certainty == "speculation",
            ))).all()
            targets_disable = [r for r in rows
                               if getattr(r, "enabled", True) and getattr(r, "expires_at", None)
                               and r.expires_at <= now]
            targets_purge = [r for r in rows
                             if getattr(r, "expires_at", None) and r.expires_at <= purge_before]
            if targets_disable:
                async with DatabaseUtils.SessionData() as dbs2:
                    async with dbs2.begin():
                        for r in targets_disable:
                            row = await dbs2.get(SqlPersistFact, r.id)
                            if row is not None and getattr(row, "enabled", True):
                                row.enabled = False
                                disabled += 1
            if targets_purge:
                async with DatabaseUtils.SessionData() as dbs3:
                    async with dbs3.begin():
                        for r in targets_purge:
                            row = await dbs3.get(SqlPersistFact, r.id)
                            if row is not None:
                                await dbs3.delete(row)
                                purged += 1
        if disabled or purged:
            try:
                import maica.maica_utils.debug_log as _dl
                _dl.dbg("FACT-SPEC-EXPIRE", "disabled=%d purged=%d" % (disabled, purged))
            except Exception:
                pass
        return {"disabled": disabled, "purged": purged}
    except Exception:
        return {}


async def sync_player_facts(sockets, user_id: int, session_num: int) -> dict:
    """把【当前生效的玩家事实】同步进向量库，并回写 vector_id 形成双向连接。

    ★ 两类分开索引（2026-10-19 起）：
        player_direct       明确事实（长期）
        player_speculation  推测事实（短期池，TTL 到期后自动失活）
      分开是为了让召回能把推测放在最低一层、只在没有明确事实时才出（见 session_late）。

    ★ 差集语义：被取代 / 被停用 / 已过期的行会自动从向量库消失。

    ★ 索引文本是【渲染后的结构化短句】，不是原话；时间不进索引（见 2026-10-18 裁决）。
    返回 {"total": n, "linked": m}；任何失败都返回空字典。
    """
    try:
        from maica.maica_utils import DatabaseUtils, SqlPersistFact
        from maica.maica_utils.fact_render import render_fact
        import sqlalchemy

        # ① 先清短期池：到期的推测不该再被索引（也顺手删掉很老的）
        await expire_speculations(user_id, session_num)

        now = _utcnow()
        async with DatabaseUtils.SessionData() as dbs:
            rows = (await dbs.scalars(sqlalchemy.select(SqlPersistFact).where(
                SqlPersistFact.user_id == user_id,
                SqlPersistFact.chat_session_num == session_num,
            ))).all()
            groups = {"player_direct": [], "player_speculation": []}
            for r in rows:
                if getattr(r, "superseded_by", None):
                    continue
                if not getattr(r, "enabled", True):
                    continue
                if label_of_source(getattr(r, "source", "")) != "player_direct":
                    continue
                is_spec = (getattr(r, "certainty", "") or "").strip() == "speculation"
                if is_spec:
                    exp = getattr(r, "expires_at", None)
                    if exp is not None and exp <= now:
                        continue          # 已过期：等 sweep 置停用，这里不索引
                text = (render_fact(r) or "").strip()
                if text:
                    groups["player_speculation" if is_spec else "player_direct"].append(
                        (r.id, text, r.vector_id))

        total = linked_total = 0
        # ★ 两类都要跑：即使某一类当前为空，也必须调用 sync_texts(set())，
        #   否则差集同步不会执行，已停用/已过期的行会永远留在索引里（2026-10-19 修）。
        for typ in ('player_direct', 'player_speculation'):
            picked = groups.get(typ) or []
            ref_map = {}
            collide = {}
            for rid, text, _ in sorted(picked, key=lambda x: x[0]):
                if text in ref_map:
                    collide.setdefault(text, [ref_map[text]]).append(str(rid))
                    continue
                ref_map[text] = str(rid)
            if collide:
                try:
                    import maica.maica_utils.debug_log as _dl
                    _dl.dbg("FACT-RENDER-COLLIDE", "%s: %d 组渲染文本撞车，取 id 最小者"
                            % (typ, len(collide)), payload=dict(list(collide.items())[:3]))
                except Exception:
                    pass
            total += len(ref_map)
            if sockets is None or not getattr(sockets, "is_vector_ready", False):
                continue
            vec_map = await sockets.vector_pool.sync_texts(
                embedding_conn=sockets.embedding_conn,
                data=set(ref_map),
                filters={"user_id": user_id, "chat_session_num": session_num, "type": typ},
                ref_ids=ref_map,
            )
            updates = [(rid, (vec_map or {}).get(text)) for rid, text, old in picked]
            updates = [(rid, vid) for rid, vid in updates if vid]
            if updates:
                async with DatabaseUtils.SessionData() as dbs2:
                    async with dbs2.begin():
                        for rid, vid in updates:
                            row = await dbs2.get(SqlPersistFact, rid)
                            if row is not None and row.vector_id != vid:
                                row.vector_id = vid
                                linked_total += 1
        return {"total": total, "linked": linked_total}
    except Exception as exc:
        try:
            import maica.maica_utils.debug_log as _dl
            _dl.dbg("FACT-VECTOR-ERROR", "%r" % (exc,))
        except Exception:
            pass
        return {}
