# -*- coding: utf-8 -*-
"""玩家消息 → 结构化事实（P3-C 的提取接口）—— ★ 已接入本地小模型 r8（r8d_q8_0）。

链路：
    玩家这句话 → 独立进程的 OpenAI 兼容端点（MAICA_EXTRACT_ADDR，默认 :8091）
      → 模型吐一行扁平 JSON → fact_json.parse_facts() 解析（唯一解析来源）
      → 本模块的四道准入校验 → 交给 post_core_pipeliner 走漏斗/裁决/入库

契约（《需求与实现路径.md》§5）：
    输入：一条纯文本（玩家自己说的话）
    输出：0..n 个扁平 dict，字段与 persist_facts 对齐
    约束：不依赖上下文；判定"无信息 / 不自足 / 有歧义"时**宁可不输出**（D31）

本模块的四道准入校验（没有一道可以省）：
    ① has_info 必须为 true                    （模型自检层，漏斗第①层）
    ② self_contained 不能是 false             （D30：指代/省略 → 不落库）
    ③ certainty=='joke' 直接丢                （玩笑会污染长期记忆）
    ④ object_raw / predicate 必须是原句里就有的片段
       —— 这条是【后端硬校验】，用来挡模型抄错字（实测出现过
          「我喜欢听说唱」→ object_raw="说听唱" 的字符错）。
          例外：predicate == "是" 或 "有"（提示词允许"按需补"，原句里可以没有）。

任何失败都降级为"不产出事实"，绝不抛异常影响对话（D13/D31）。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time

# 提示词缓存：{路径: (mtime, 文本)}
_PROMPT_CACHE: dict = {}

# 允许补出来的谓语（提示词里写明"按需补"的那两个）
_PRED_MAYBE_ADDED = ("是", "有")

_MAX_TOKENS = 256
_DEFAULT_TIMEOUT = 30

# 提取器"还没就绪"的兜底（2026-10-18 补）。
# 实测：llama-server 冷启动头几秒回 503 {"message":"Loading model"}，
# 原来一次失败就放弃 → 那句话没有任何事实；玩家"重复说一件已经记过的事"时，
# 连"刷新 speak_time"也一起丢了（记忆时间永远停在第一次说的那天）。
# 现在：认出这是"没就绪"，在【后台任务里】等它就绪（最多 20 秒）再重试一次。
#   对话早已回复完毕，不占会话锁、不增加玩家等待；真失败仍按老样子放弃。
_NOTREADY_WAIT = 20.0
_NOTREADY_POLL = 0.5


# --------------------------------------------------------------------------
# 提示词：唯一来源，训练 / 推理 / 评测共用（找不到就返回 None，不静默换一份）
# --------------------------------------------------------------------------

def _backend_root() -> str:
    # 本文件在 <后端>/maica/mtools/fact_extract.py
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _prompt_candidates() -> list:
    from maica.maica_utils.gvars import G
    cands = []
    env = (getattr(G.A, "EXTRACT_PROMPT", "") or "").strip()
    if env:
        cands.append(env)
    root = _backend_root()
    cands.append(os.path.join(root, "system_prompt.txt"))
    # 测试期布局：<后端>/../../_tools/system_prompt.txt
    cands.append(os.path.normpath(os.path.join(root, "..", "..", "_tools", "system_prompt.txt")))
    return cands


def load_system_prompt() -> str | None:
    """读取唯一提示词；按 mtime 缓存。找不到返回 None（调用方将不提取）。"""
    for path in _prompt_candidates():
        try:
            if not path or not os.path.exists(path):
                continue
            mtime = os.path.getmtime(path)
            hit = _PROMPT_CACHE.get(path)
            if hit and hit[0] == mtime:
                return hit[1]
            text = io.open(path, encoding="utf-8").read().strip()
            if not text:
                continue
            _PROMPT_CACHE[path] = (mtime, text)
            return text
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------
# 硬校验
# --------------------------------------------------------------------------

def _norm(s) -> str:
    """去掉所有空白，用于"是不是原句片段"的比较（避免空格/换行造成误杀）。"""
    return re.sub(r"\s+", "", str(s or ""))


# 第二人称 → 莫妮卡。
# 玩家对莫妮卡说的原话里"你"指的是对话对象本身，r8 训练口径是"只记关于玩家的事"，
# 于是"我喜欢你写的诗"会被它判成"没信息"；换成第三人称客体就正常了。
# 实测（2026-10-17）：我喜欢你写的诗 → has_info=false；我喜欢莫妮卡写的诗 → 正例。
_MASK_WORDS = ("你们", "迷你")          # 换成名字会读坏的词，先保护起来
_SECOND_PERSON = ("你", "您", "妳")
_MONIKA_NAME = "莫妮卡"


def normalize_second_person(text: str, name: str = _MONIKA_NAME) -> str:
    """把句子里的第二人称换成莫妮卡，供模型判定与子串校验使用（不改 fact_text）。"""
    s = str(text or "")
    if not s:
        return s
    masks = {}
    for i, w in enumerate(_MASK_WORDS):
        if w in s:
            key = "\x00%d\x00" % i
            masks[key] = w
            s = s.replace(w, key)
    for p in _SECOND_PERSON:
        s = s.replace(p, name)
    for k, w in masks.items():
        s = s.replace(k, w)
    return s


def _is_substring(piece, text) -> bool:
    p, t = _norm(piece), _norm(text)
    return bool(p) and p in t


def _is_substring_alias(piece, text) -> bool:
    """子串校验，两侧都做第二人称归一后再比（模型把"你"写成"莫妮卡"时仍算命中）。"""
    if _is_substring(piece, text):
        return True
    return _is_substring(normalize_second_person(piece), normalize_second_person(text))


def _is_negative(raw_out: str) -> bool:
    """模型这一答是不是"没信息"。解析失败按负例处理（宁可漏不可错）。"""
    try:
        from maica.mtools.fact_json import parse_facts
        objs = parse_facts(raw_out or "")
    except Exception:
        return True
    if not objs:
        return True
    return all(o.get("has_info") is not True for o in objs)


def _veto_reason(cand: dict, text: str) -> str:
    """返回否决原因；空串表示通过。"""
    if cand.get("has_info") is not True:
        return "has_info-false"
    if cand.get("self_contained") is False:
        return "self_contained-false"
    if (cand.get("certainty") or "").strip() == "joke":
        return "certainty-joke"
    obj = cand.get("object_raw")
    if obj not in (None, ""):
        if not _is_substring_alias(obj, text):
            return "object-not-in-text(%r)" % (obj,)
    pred = (cand.get("predicate") or "").strip()
    if pred and pred not in _PRED_MAYBE_ADDED and not _is_substring_alias(pred, text):
        return "predicate-not-in-text(%r)" % (pred,)
    return ""


# --------------------------------------------------------------------------
# 「我的X是…」的中心语还原（2026-10-22）
# --------------------------------------------------------------------------
# 问题：r8 只看谓语，遇到「我的生日是五月二十号」只给 predicate="是"、
# object="五月二十号"，主语和"是"之间的中心语「生日」整段丢掉 ——
# 结构化事实变成"我是五月二十号"。后果不止是读着别扭：
#   · 同键（我, 是, 值）跟「我是学生」这类句子撞在同一个键上，
#     find_same_key / resolve_conflicts 会把两件不相干的事当成一件事互相覆盖；
#   · 召回索引虽然靠 render_fact 回退原句兜住了文本，但键和 kind 依旧是错的。
# 这里在入库前把它补回去：predicate 由「是」变成「的生日是」，
# 渲染出来正好是「我的生日是五月二十号」，键也从"是/值"变成"生日/值"。
# 只改谓语的头部，object 一个字不动（子串校验、fact_text 都不受影响）。
#
# 判据全部是原句上的确定性检查，不引词性分析：
#   ① 谓语必须【恰好】是链接词（是/为/就是/在/叫…），复合谓语不碰；
#   ② 用模型给的 object 在原句里定位，取它前面那段，必须以链接词结尾，
#      再往回找最后一个"我"，两者之间就是候选中心语；
#   ③ 中心语 1~4 字、不以 的/地/得/了/着/过 结尾（挡掉"我说的""上次说的"
#      这类"的"字短语）、不含代词和标点。
# 否定（polarity=-1）不fold：渲染侧会把"不"插在主语后（"我不的生日是…"），
# 那条路本来就回退原句，交给 fallback 反而正确。
_LINK_WORDS = ("是", "为", "就是", "也是", "不是", "在", "叫", "叫做", "名叫", "姓", "算是")
_HEAD_STOP = ("的", "地", "得", "了", "着", "过")
_HEAD_BAD = "我你他她它这那们了着过吗呢吧啊嘛，。,.!?！？、；;：:"
_MAX_HEAD = 4


def _fold_copula_head(predicate: str, object_: str, text: str) -> str:
    """把「我+的X+是/叫…」里的中心语 X 并回谓语；不符合形态就原样返回。"""
    pred = str(predicate or "").strip()
    obj = str(object_ or "").strip()
    if not pred or not obj or pred not in _LINK_WORDS:
        return pred
    if pred[0] in "不没未别":
        return pred
    t, o = _norm(text), _norm(obj)
    if not o:
        return pred
    # 用最后一次出现（宾语在谓语之后；同一句里前面可能先提过一次这个值）
    i = t.rfind(o)
    if i <= 0:
        return pred
    prefix = t[:i]
    if not prefix.endswith(pred):
        return pred
    core = prefix[: -len(pred)]
    j = core.rfind("我")
    if j < 0:
        return pred
    head, lead = core[j + 1:], ""
    if head.startswith("的"):
        lead, head = "的", head[1:]
    if not head or len(head) > _MAX_HEAD:
        return pred
    if head[-1] in _HEAD_STOP:
        return pred
    if any(ch in head for ch in _HEAD_BAD):
        return pred
    return lead + head + pred


def normalize_facts(objs: list, text: str, fact_text: str = None) -> list:
    """把模型输出的 dict 列表整理成 persist_facts 对齐的候选；不合格的直接丢。

    text       —— 送去模型的句子（已做第二人称归一），子串校验用它
    fact_text  —— 入库保存的原话；默认与 text 相同
    """
    out, seen = [], set()
    keep = fact_text if fact_text else text
    for cand in objs or []:
        if not isinstance(cand, dict):
            continue
        why = _veto_reason(cand, text)
        if why:
            try:
                import maica.maica_utils.debug_log as _dl
                _dl.dbg("FACT-REJECT", "reason=%s payload=%s" % (why, text))
            except Exception:
                pass
            continue
        obj = (cand.get("object_raw") or "").strip() or None
        pred = (cand.get("predicate") or "").strip()
        if not pred:
            continue
        polarity = -1 if str(cand.get("polarity")).strip() in ("-1", "-1.0") else 1
        # 「我的生日是X」→ predicate "是" 补成 "的生日是"（只对肯定句做，见 _fold_copula_head）
        if polarity == 1:
            folded = _fold_copula_head(pred, obj, text)
            if folded != pred:
                try:
                    import maica.maica_utils.debug_log as _dl
                    _dl.dbg("FACT-FOLD-HEAD", "%s -> %s" % (pred, folded), payload=text)
                except Exception:
                    pass
                pred = folded
        item = {
            "self_contained": True,
            "certainty": (cand.get("certainty") or "explicit").strip() or "explicit",
            "kind": (cand.get("kind") or "other").strip() or "other",
            "subject_raw": (cand.get("subject_raw") or "我").strip() or "我",
            "predicate": pred,
            "object_raw": obj,
            "polarity": polarity,
            "time_expression": (cand.get("time_expression") or None),
            "source": "player_direct",
            "fact_text": keep,
        }
        key = (item["subject_raw"], item["predicate"], item["object_raw"], item["polarity"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# --------------------------------------------------------------------------
# 调用端点
# --------------------------------------------------------------------------

# 这些特征说明"端点还没起来/模型还在加载"，不是调用本身出错
_NOTREADY_HINTS = (
    "503",
    "loading model",
    "loading",
    "unavailable",
    "apiconnectionerror",
    "connecterror",
    "connection refused",
    "connection error",
    "all connection attempts failed",
)


def _is_not_ready(exc) -> bool:
    """异常是不是"提取器还没就绪"（加载中 / 端口没监听 / 连接失败）。

    ★ 只对这类重试：真正的 400/422（请求格式错）重试多少次都一样，
      而且会把后台任务拖长，所以要区分开。
    """
    try:
        low = ("%r" % (exc,)).lower()
    except Exception:
        return False
    return any(hint in low for hint in _NOTREADY_HINTS)


def _health_url(conn) -> str:
    """从 OpenAI 兼容端点推出 llama-server 的 /health 地址。

    base_url 形如 http://127.0.0.1:8091/v1 → http://127.0.0.1:8091/health
    （llama-server 在模型加载期间对这个地址回 503，加载完回 200）
    """
    try:
        base = str(getattr(conn, "base_url", "") or "").strip().rstrip("/")
        if not base:
            return ""
        if base.endswith("/v1"):
            base = base[:-3]
        return base + "/health"
    except Exception:
        return ""


def _probe_health(url: str) -> bool:
    """同步探活（在线程里跑，1 秒超时）。任何异常都当"没就绪"。"""
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=1.0) as resp:
            return int(getattr(resp, "status", 200)) == 200
    except Exception:
        return False


async def _wait_ready(conn, deadline: float) -> bool:
    """等到 deadline 为止轮询 /health；取不到地址就简单等一会儿再试。"""
    url = _health_url(conn)
    if not url:
        await asyncio.sleep(1.0)
        return True
    while True:
        if await asyncio.to_thread(_probe_health, url):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_NOTREADY_POLL)


# 刚放弃过的时刻（单调时钟）。extract_facts 可能对同一句调两次 _ask_model
# （原句判负后再用第二人称归一化的句子补问），不设冷却会把 20 秒等到 40 秒。
_GAVE_UP_AT = 0.0
_GIVEUP_COOLDOWN = 5.0


def _dbg(kind: str, message: str, payload=None) -> None:
    try:
        import maica.maica_utils.debug_log as _dl
        if payload is None:
            _dl.dbg(kind, message)
        else:
            _dl.dbg(kind, message, payload=payload)
    except Exception:
        pass


async def _ask_model(fsc, system: str, text: str) -> str:
    """调提取器端点，返回模型原文。无端点/出错 → 空串。

    ★ 2026-10-18：端点回 503（模型加载中）/ 连接失败时，等它就绪再重试一次。
      这是"重复提及已有事实"最关键的窗口 —— 失败的话那句既不入库、也不刷新
      speak_time，记忆时间会永远停在第一次说的时候。
    """
    global _GAVE_UP_AT
    conn = getattr(fsc, "extract_conn", None) if fsc is not None else None
    if conn is None or getattr(conn, "client", None) is None:
        return ""
    try:
        from maica.maica_utils.gvars import G
        timeout = int(getattr(G.A, "OPENAI_TIMEOUT", "") or _DEFAULT_TIMEOUT) or _DEFAULT_TIMEOUT
    except Exception:
        timeout = _DEFAULT_TIMEOUT
    kwargs = {
        "model": conn.model_actual,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "max_tokens": _MAX_TOKENS,
        "stream": False,
    }

    async def _call():
        return await asyncio.wait_for(
            conn.client.chat.completions.create(**kwargs), timeout=timeout)

    try:
        resp = await _call()
    except Exception as exc:
        if not _is_not_ready(exc):
            _dbg("FACT-EXTRACT-ERROR", "%r" % (exc,), payload=text)
            return ""
        if _GAVE_UP_AT and (time.monotonic() - _GAVE_UP_AT) < _GIVEUP_COOLDOWN:
            # 刚等满 20 秒还是没就绪：同一句的补问、或紧跟着的下一条都不要再来一遍
            _dbg("FACT-EXTRACT-GIVEUP", "cooldown after recent giveup", payload=text)
            return ""
        _dbg("FACT-EXTRACT-NOTREADY", "%r" % (exc,), payload=text)
        started = time.monotonic()
        ready = await _wait_ready(conn, started + _NOTREADY_WAIT)
        _dbg("FACT-EXTRACT-RETRY" if ready else "FACT-EXTRACT-GIVEUP",
             "waited %.1fs (limit %.0fs)" % (time.monotonic() - started, _NOTREADY_WAIT),
             payload=text)
        if not ready:
            _GAVE_UP_AT = time.monotonic()
            return ""
        _GAVE_UP_AT = 0.0
        try:
            resp = await _call()
        except Exception as exc2:
            _dbg("FACT-EXTRACT-ERROR", "retry failed: %r" % (exc2,), payload=text)
            return ""

    try:
        msg = resp.choices[0].message
        return (getattr(msg, "content", None) or "").strip()
    except Exception as exc:
        _dbg("FACT-EXTRACT-ERROR", "bad response: %r" % (exc,), payload=text)
        return ""


# --------------------------------------------------------------------------
# 对外接口（唯一接线点，被 post_core_pipeliner 调用）
# --------------------------------------------------------------------------

async def extract_facts(text: str, fsc=None) -> list:
    """把一个玩家句子提纯成结构化事实。

    返回 list[dict]，字段与 persist_facts 对齐：
        self_contained / certainty / kind / subject_raw /
        predicate / object_raw / polarity / time_expression / source / fact_text
    """
    try:
        raw = (text or "").strip()
        if not raw:
            return []
        system = load_system_prompt()
        if not system:
            try:
                import maica.maica_utils.debug_log as _dl
                _dl.dbg("FACT-EXTRACT-NOPROMPT", "system_prompt.txt not found; extraction skipped")
            except Exception:
                pass
            return []
        # 两段式：先按原句问；只有原句判负、且句子里有第二人称时，才用归一化句补问一次。
        # 这样保证不会破坏原有正例（原句能抽出的不会再走第二遍）。
        out_text = await _ask_model(fsc, system, raw)
        sent_to_model = raw
        if (not out_text or _is_negative(out_text)) and any(p in raw for p in _SECOND_PERSON):
            alt = normalize_second_person(raw)
            alt_text = await _ask_model(fsc, system, alt)
            if alt_text and not _is_negative(alt_text):
                out_text, sent_to_model = alt_text, alt
        if not out_text:
            return []
        from maica.mtools.fact_json import parse_facts
        objs = parse_facts(out_text)
        try:
            import maica.maica_utils.debug_log as _dl
            _dl.dbg("FACT-EXTRACT-RAW", "objs=%d out=%s" % (
                len(objs), out_text.replace("\n", " ")[:180]), payload=raw)
        except Exception:
            pass
        # 子串校验用真正送进模型的那句（可能是归一化后的），fact_text 仍存原话
        return normalize_facts(objs, sent_to_model, fact_text=raw)
    except Exception as exc:
        try:
            import maica.maica_utils.debug_log as _dl
            _dl.dbg("FACT-EXTRACT-FATAL", "%r" % (exc,))
        except Exception:
            pass
        return []
