# -*- coding: utf-8 -*-
"""事实渲染层：把结构化事实拼成【人话短句】，供向量索引与召回注入共用。

为什么要有这一层（2026-10-18 用户裁决）：
    小模型现在【一句话只出一条事实】（system_prompt.txt：只留第一个含"我"的分句）。
    原先"整句原话进索引"的设计是为了"一句话多条事实必须一起被带出"，
    那个前提没了，整句索引只剩害处：
        · 索引里混着"你可以帮我看看吗"这种噪声，稀释向量；
        · 召回注入的是一整句 38 字，真正有用的是"写了一首诗"5 个字；
        · 时间信息全丢，莫妮卡分不清"上周说"和"刚说"。
    所以改成：SQLite 存结构（权威）→ 本模块渲染成短句 → 向量库存短句 → 召回时
    再拼上【谁 + 什么时候 + 说过】。

分工：
    render_fact(row)                        事实行/候选 dict → "我写了一首诗"
                                             （索引文本，**不含时间**：时间每次重提都会变，
                                               进了文本就要反复重嵌入，还会稀释相似度）
    format_recall_line(text, speak_time)     召回时拼前缀 → "小明三天前说过：我写了一首诗"

时间按台阶降精度：今天 / 昨天 / 前天 / N天前（≤30 天）/ N个月前（<12 个月）/ N年前。
后端在召回那一刻用 now 减 speak_time 现算。理由：莫妮卡要判断的是"这条记忆新旧"，
不是审计时间戳；绝对日期既占 prompt，又需要她自己跟今天做减法。
时区跟 mfocus 的取法一致（settings.basic.tz）。

渲染规则（全部确定性，不调模型）：
    ① 主语固定是"我"：抽取提示词只收第一人称（subject_raw 只写"我"），
       subject 不是 player 的老数据（规则解析期迁移进来的）一律回退原句。
    ② 时间词（time_expression，模型给的是原话）插在主语后：我 + 最近 + 在学Java。
    ③ 否定（polarity == -1）：状态/属性类用"不"（喜欢→不喜欢），动作/事件用"没"（看→没看）。
       "状态还是动作"没有任何词性分析：先看 `有/在` 前缀与 28 词白名单，
       查不到再看模型给的 `kind`（preference/attribute/relationship/commitment → 不，
       event → 没），最后默认"没"。详见 _negation()。
    ④ 谓语是"是"→ **回退原句的分句**：`是` 字句的中心语常落在主语里
       （"我的生日是2005年12月10日" → 拼出来会变成"我是2005年12月10日"，丢了"生日"；
        "我口味很杂" → "我是口味很杂" 不通），这类只能取原句里带宾语的那个分句。
    ⑤ 兜底：拼出来的片段必须能在原话里找到（谓语/宾语都在），否则回退原句里带宾语的分句。
       ★ 这条同时兜住了"模型把时间词塞进宾语"的情况（"大概我下次会带礼物来" →
         predicate=会, object=下次带礼物来，宾语在原话里不连续 → 回退，得到"我下次会带礼物来"）。
         万一某次宾语恰好连续、拼出来语序别扭，也只是语序问题，语义无损；
         根治要改抽取的切分，不在本层（用户 2026-10-18 决定不为此加规则）。

★ 不做的事：
    · 不写 SQLite（渲染是推导出来的，不落列；改规则后下次入库按差集自动重建索引）
    · 不动 fact_text（原话保留：审计、同键去重 near_same_text、插件展示都要它）
    · 不碰 knowledge / savefile_field / savefile_derived（那三类本来就是整句陈述，
      不是"谁说过"的话，也没有主谓宾可拼）
"""

from __future__ import annotations

import datetime
import re

__all__ = ["render_fact", "format_recall_line", "resolve_tz", "format_day", "relative_day_text"]

# 分句标点：取"包含宾语的那个分句"用
_CLAUSE_SPLIT = re.compile(r"[，,。.！!？?；;：:\n]")

# 谓语是这些词（或以下列词开头）时，"否定"用"不"，其余动作/事件用"没"
_STATIVE = {
    "喜欢", "爱", "讨厌", "想", "要", "会", "能", "可以", "需要", "习惯",
    "怕", "懂", "明白", "记得", "知道", "擅长", "介意", "在乎", "在意",
    "觉得", "认为", "相信", "属于", "适合", "支持", "反对", "接受",
    "是", "有", "在",
}

# 分句句首的话语标记：挑中分句后剥掉（否则会出现"没错我口味很杂"）
_DISCOURSE = (
    "没错", "其实", "说实话", "老实说", "老实讲", "大概", "可能", "也许",
    "不过", "但是", "然后", "而且", "所以", "因为", "总之", "另外", "还有",
    "嗯", "唉", "诶", "哎",
)

_PLAYER_NAME_FALLBACK = "{player_name}"


def _row(row, name, default=None):
    """事实行可能是 ORM 对象，也可能是候选 dict（评测台）。"""
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _norm(s: str) -> str:
    """比较用：去掉所有空白（模型输出里常带空格）。"""
    return re.sub(r"\s+", "", str(s or ""))


def _clause_of(fact_text: str, object_: str = "", predicate: str = "") -> str:
    """取原话里【带宾语】的那个分句（找不到就用带谓语/第一非空分句）。

    用于 `是` 字句与兜底：整句本身就是人话，只需要把噪声分句切掉。
    """
    text = str(fact_text or "").strip()
    if not text:
        return ""
    parts = [p.strip() for p in _CLAUSE_SPLIT.split(text)]
    parts = [p for p in parts if p]
    if not parts:
        return text
    obj, pred = _norm(object_), _norm(predicate)
    for want in (obj, pred):
        if not want:
            continue
        for part in parts:
            if want in _norm(part):
                return _strip_discourse(part, want)
    return _strip_discourse(parts[0], obj)


def _strip_discourse(clause: str, want: str = "") -> str:
    """剥掉分句句首的话语标记；剥完还得含 want（或 want 为空），否则原样返回。"""
    out = clause
    for _ in range(3):
        hit = None
        for marker in _DISCOURSE:
            if out.startswith(marker) and len(out) > len(marker):
                hit = marker
                break
        if not hit:
            break
        rest = out[len(hit):].lstrip(" ，,、")
        if not rest:
            break
        if want and want not in _norm(rest):
            break
        out = rest
    return out


def _negation(predicate: str, kind: str = "") -> str:
    """选否定词：不 / 没（没有任何词性分析，是"查表 + 兜底"）。

    顺序（先命中先返回）：
      ① 谓语为空，或以 不/没/无/别 开头 → 返回空（模型已经把否定写进谓语，不叠加）
      ② 谓语以"有"开头 → "没"（我不能说"我不有宗教信仰"）
      ③ 谓语以"在"开头且不止一个字 → "没"（"在学" → 没在学）
      ④ 谓语在 _STATIVE 表里 → "不"（喜欢/讨厌/是/会/能/想/觉得/知道…）
      ⑤ 落到 kind（模型给的语义标签）：
           preference / attribute / relationship / commitment → "不"（"我不吃素"）
           event                                            → "没"（"我没看网课"）
      ⑥ 都没有 → "没"（动作/事件默认值，"我没加班" / "我没看" 都通）

    为什么需要 ⑤：中文里"没"对动作通顺、对属性/习惯不通顺（"我没吃素"✗），
    但属性谓语的词表是开集（吃素/抽烟/信教/住校…），4 个字一张表永远补不全；
    模型本来就要给 kind，用它兜比继续堆词表靠谱。
    ④ 放在 ⑤ 前面的理由：谓语是直接观测到的东西，"kind 说 event 但谓语是喜欢"
    这种自相矛盾时以谓语为准。
    """
    pred = str(predicate or "").strip()
    if not pred or pred[0] in "不没无别":
        return ""
    if pred.startswith("有"):
        return "没"
    if pred.startswith("在") and len(pred) > 1:
        return "没"
    if pred in _STATIVE:
        return "不"
    k = str(kind or "").strip().lower()
    if k in ("preference", "attribute", "relationship", "commitment"):
        return "不"
    return "没"


def _traceable(predicate: str, object_: str, fact_text: str) -> bool:
    """拼出来的谓语/宾语必须能在原话里找到 —— 否则模型改写了字段，回退原句。"""
    text = _norm(fact_text)
    if not text:
        return False
    pred, obj = _norm(predicate), _norm(object_)
    if pred and pred not in text:
        return False
    if obj and obj not in text:
        return False
    return bool(pred or obj)


def render_fact(row) -> str:
    """事实行 → 结构化短句（索引与召回共用的"人话版本"）。

    退化情况一律回退 `fact_text`（原话），绝不返回空串：
      · subject 不是 player（规则迁移期的老数据）
      · 谓宾都空
      · 谓语是"是"（中心语在主语里，拼不出来）
      · 谓宾在原话里找不到（模型改写过字段）
    """
    try:
        fact_text = str(_row(row, "fact_text") or "").strip()
        subject = str(_row(row, "subject") or "player").strip()
        predicate = str(_row(row, "predicate") or "").strip()
        object_ = str(_row(row, "object") or "").strip()
        time_expr = str(_row(row, "time_expression") or "").strip()
        try:
            polarity = int(_row(row, "polarity", 1) or 1)
        except Exception:
            polarity = 1

        def _fallback():
            return _clause_of(fact_text, object_, predicate) or fact_text

        if subject and subject != "player":
            return _fallback()
        if not predicate and not object_:
            return _fallback()
        if predicate.startswith("是"):
            return _fallback()
        if not _traceable(predicate, object_, fact_text):
            return _fallback()

        neg = _negation(predicate, _row(row, "kind", "")) if polarity == -1 else ""
        return "我" + time_expr + neg + predicate + object_
    except Exception:
        try:
            return str(_row(row, "fact_text") or "")
        except Exception:
            return ""


def resolve_tz(tz, target_lang=None) -> str:
    """把 settings.basic.tz 归一成可用的时区名（与 mfocus 的取法一致）。"""
    value = (tz or "").strip()
    if not value or value in ("auto",):
        value = (target_lang or "zh").strip()
    if value in ("zh",):
        return "Asia/Shanghai"
    if value in ("en",):
        return "America/Indiana/Vincennes"
    return value


def _to_local(speak_time, tz=None):
    """naive UTC（库里就是 naive UTC）→ 本地时间。失败就按 UTC。"""
    if speak_time is None:
        return None
    try:
        dt = speak_time
        if isinstance(dt, str):
            # 库里存的是 naive UTC 字符串（sqlite3 直接读出来就是 str），解析一下
            dt = datetime.datetime.fromisoformat(dt.strip())
        if not isinstance(dt, datetime.datetime):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        if tz:
            try:
                from dateutil.tz import gettz
                zone = gettz(str(tz))
                if zone is not None:
                    dt = dt.astimezone(zone)
            except Exception:
                pass
        return dt
    except Exception:
        return None


def format_day(speak_time, tz=None) -> str:
    """绝对日期 2026-10-18 —— 只用于日志/调试，召回不用它。"""
    dt = _to_local(speak_time, tz)
    return dt.strftime("%Y-%m-%d") if dt else ""


def relative_day_text(speak_time, tz=None, target_lang=None, now=None) -> str:
    """相对时间：今天 / 昨天 / 前天 / N天前 / N个月前 / N年前。

    精度是按【台阶】降的（用户 2026-10-18 定）：
        0 天        今天
        1 / 2 天    昨天 / 前天
        3 ~ 30 天   N天前
        31 天 ~ 11 个月   N个月前（按日历月，dateutil.relativedelta）
        ≥ 12 个月   N年前

    · now 不传就取当前时刻（后端时钟）；两侧都换到同一时区，按【日历日】相减，
      所以"昨晚 23:50"在今天 00:10 看是"昨天"，不是"0天前"。
    · speak_time 在未来（时钟偏了）→ 当"今天"，不输出负数。
    · 月/年用日历差（dateutil.relativedelta）+ 余数 ≥15 天进一：
      纯除法（days/30）会在 31/30/28 天的月份上偏；纯日历取整又会把
      "60 天前"（1 个月 29 天）算成 1 个月。两者结合：日历月 + 半个月进位。
    · 边界兜底：超过 30 天但日历差算出 0 个月 → 当 1 个月，避免"0个月前"。
    · 满 12 个月即算一年（"12个月前"读起来不如"1年前"）。
    """
    dt = _to_local(speak_time, tz)
    if dt is None:
        return ""
    try:
        base = now or datetime.datetime.now(datetime.timezone.utc)
        if isinstance(base, str):
            base = datetime.datetime.fromisoformat(base.strip())
        if base.tzinfo is None:
            base = base.replace(tzinfo=datetime.timezone.utc)
        if tz:
            try:
                from dateutil.tz import gettz
                zone = gettz(str(tz))
                if zone is not None:
                    base = base.astimezone(zone)
            except Exception:
                pass
        days = (base.date() - dt.date()).days
        months = 0
        if days > 30:
            from dateutil.relativedelta import relativedelta
            rd = relativedelta(base.date(), dt.date())
            months = rd.years * 12 + rd.months
            if rd.days >= 15:
                months += 1
            if months <= 0:
                months = 1
    except Exception:
        return ""
    if days <= 0:
        days = 0
    en = str(target_lang or "zh").startswith("en")

    if months >= 12:
        years = months // 12
        return ("%d year%s ago" % (years, "" if years == 1 else "s")) if en else ("%d年前" % years)
    if months > 0:
        return ("%d month%s ago" % (months, "" if months == 1 else "s")) if en else ("%d个月前" % months)

    if en:
        if days == 0:
            return "today"
        if days == 1:
            return "yesterday"
        if days == 2:
            return "the day before yesterday"
        return "%d days ago" % days
    if days == 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days == 2:
        return "前天"
    return "%d天前" % days


def format_recall_line(render: str, speak_time=None, pname: str = "", tz=None,
                       target_lang=None, now=None, weak: bool = False) -> str:
    """召回注入的一行：`小明三天前说过：我写了一首诗`。

    · 时间在【召回时】用 now 现算（按台阶降精度：天 → 月 → 年），不存库：
      同一条事实昨天看是"昨天"，今天看就是"前天"，存下来的相对时间会立刻过期。
    · pname 取不到（存档访问关闭）时退回 `{player_name}`：与另外三层的写法一致，
      由上层按需替换；总比写死"玩家"强。
    · speak_time 为空（2026-10-12 之前入库的老行）→ 不加时间前缀。
    · weak=True 用于推测类（短期池）：`小明好像说过：我可能有老年痴呆` ——
      语气必须弱，否则"可能/好像"的东西会被当成确定事实注入。
    """
    text = str(render or "").strip()
    if not text:
        return ""
    who = str(pname or "").strip() or _PLAYER_NAME_FALLBACK
    when = relative_day_text(speak_time, tz, target_lang, now)
    en = str(target_lang or "zh").startswith("en")
    verb = ("好像说过" if not en else "might have said") if weak else ("说过" if not en else "said")
    if when:
        return ("%s said %s: %s" % (who, when, text)) if en else ("%s%s%s：%s" % (who, when, verb, text))
    return ("%s said: %s" % (who, text)) if en else ("%s%s：%s" % (who, verb, text))
