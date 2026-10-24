# -*- coding: utf-8 -*-
u"""mood_praise —— 「玩家在夸她吗」的规则判定（不调模型，微秒级）

为什么用规则而不是模型
----------------------
后端的小模型（r8）只负责抽事实，输出契约里没有"这是不是夸奖"这一项；
为一个标签再跑一次推理既慢又要改模型。而"被夸"的句子在中文里形态很固定：
    「你好可爱」「今天辛苦了，谢谢你」「我最喜欢你了」
所以用 词表 + 指向 + 否定 + 反问 四道检查就够了，错了也有兜底
（她回复里的 不满/生气 会撤销这次加成，见 monika_mood_engine）。

判定顺序（先命中先返回）
------------------------
    ① 词表里最长的赞美词（"好可爱" 优先于 "可爱"）
    ② 该词前面 3 个字内有 不/没/别…        → 不算（"不可爱""没那么好看"）
    ③ 句子以 吗/呢/？ 结尾               → 不算（"你可爱吗"）
    ④ 指向检查：句子里有 你/妳/莫妮卡/名字，或整句很短（≤8 字）且以赞美词开头
    ⑤ 该词前面 3 个字内是 她/他/它/这/那 且全句没有指向她 → 不算（"这游戏好可爱"）

词表在 lib\\mood_praise.txt（一行一个词，可带 `= 权重`）。文件缺失时用内置兜底表，
所以发布包里丢了它也不会崩。
"""

import io
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
WORDS_FILE = os.path.join(HERE, "mood_praise.txt")

# 内置兜底（文件读不到时用）
FALLBACK = {
    u"可爱": 8.0, u"好可爱": 9.0, u"漂亮": 8.0, u"好看": 8.0,
    u"温柔": 8.0, u"聪明": 8.0, u"厉害": 8.0, u"好听": 8.0,
    u"喜欢你": 10.0, u"爱你": 10.0, u"谢谢你": 6.0, u"辛苦了": 5.0,
}

DEFAULT_WEIGHT = 8.0

# 否定词：出现在赞美词之前 3 个字内就否决
_NEG = (u"不", u"没", u"别", u"未", u"无")
# 反问 / 疑问：句尾出现就不算
_QUESTION_TAIL = (u"吗", u"呢", u"么", u"?", u"？")
# 指向她的词（问句里"你"也是指向，所以这里只管"是不是在说她"）
_SELF = (u"你", u"妳", u"莫妮卡", u"小莫", u"monika", u"Monika", u"莫妮")
# 指向别人的开头字：如果赞美词紧跟在它们后面，且全句没有"你"，就不算
_OTHER = (u"她", u"他", u"它", u"这", u"那", u"别人", u"人家", u"游戏", u"角色")

_WINDOW = 3          # 否定/他指 往前看几个字
_SHORT_SENTENCE = 8  # 这么短的整句，默认就是在夸她

_CACHE = {"mtime": None, "words": None}


def load_words(path=None):
    """读词表；文件变了会自动重读。返回 {词: 权重}，按词长降序排。"""
    p = path or WORDS_FILE
    try:
        mtime = os.path.getmtime(p)
    except Exception:
        return _fallback()
    if _CACHE["mtime"] == mtime and _CACHE["words"]:
        return _CACHE["words"]
    words = {}
    try:
        with io.open(p, encoding="utf-8-sig") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if u"=" in s:
                    w, _, v = s.partition(u"=")
                    w = w.strip()
                    try:
                        weight = float(v.strip())
                    except Exception:
                        weight = DEFAULT_WEIGHT
                else:
                    w, weight = s, DEFAULT_WEIGHT
                if w:
                    words[w] = weight
    except Exception:
        return _fallback()
    if not words:
        return _fallback()
    _CACHE["mtime"] = mtime
    _CACHE["words"] = words
    return words


def _fallback():
    if not _CACHE["words"]:
        _CACHE["words"] = dict(FALLBACK)
    return _CACHE["words"]


def _ordered(words):
    """长词优先，避免"好可爱"只匹配到"可爱"。"""
    return sorted(words.keys(), key=lambda w: -len(w))


def _is_negated(text, pos):
    head = text[max(0, pos - _WINDOW):pos]
    return any(n in head for n in _NEG)


def _points_to_her(text, pos, word):
    low = text.lower()
    if any(s.lower() in low for s in _SELF):
        return True
    if len(text) <= _SHORT_SENTENCE and pos == 0:
        return True
    # 赞美词前面紧跟着"她/他/这/那"之类 → 说的是别人
    head = text[max(0, pos - _WINDOW):pos]
    if any(o in head for o in _OTHER):
        return False
    return False


def detect(text):
    """返回 (weight, word, reason)；不是夸奖时返回 (0.0, None, reason)。

    reason 只用于日志/面板，例如 'negated' / 'question' / 'other-target'。
    """
    if not text or not isinstance(text, str):
        try:
            text = u"%s" % (text,)
        except Exception:
            return 0.0, None, "bad-text"
    s = text.strip()
    if len(s) > 200:
        s = s[:200]
    if not s:
        return 0.0, None, "empty"
    if s.endswith(_QUESTION_TAIL):
        return 0.0, None, "question"
    words = load_words()
    for word in _ordered(words):
        pos = s.find(word)
        if pos < 0:
            continue
        if _is_negated(s, pos):
            return 0.0, None, "negated"
        if not _points_to_her(s, pos, word):
            return 0.0, None, "other-target"
        return float(words[word]), word, "ok"
    return 0.0, None, "no-word"


def detect_batch(texts):
    """给测试/离线评估用：返回 [(text, weight, word, reason)]。"""
    out = []
    for t in texts:
        w, word, why = detect(t)
        out.append((t, w, word, why))
    return out
