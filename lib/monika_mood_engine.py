# -*- coding: utf-8 -*-
r"""monika_mood_engine —— 莫妮卡心情状态机（阶段 1：只算，不改按钮）

Why this file exists
--------------------
MAICA already extracts emotion keywords from Monika's own replies
(emotion_analyze_v2.EmoSelector.analyze -> emote_kws), but throws them away,
keeping only the emote codes. This module consumes those keywords and turns
them into a scalar mood that can later be injected into the prompt.

★ The relativised mapping table (the whole point of this version)
-----------------------------------------------------------------
Measured over the real logs (2026-10-06, ~700 tags):

    微笑 509 | 思考 58 | 笑 33 | 眨眼 25 | 开心 24 | 得意 12 | 轻松 10
    惊讶 5 | 担心 4 | 害羞 4 | 生气 4 | 沉重 3 | 急切 2 | 严肃 1 | 惊喜 1

微笑 alone is ~73% of ALL tags. So an absolute mapping ("微笑 = +3..+6",
as in the old design note) would push the mood to its ceiling and keep it
there regardless of the conversation.

Therefore 微笑 is treated as the BASELINE (0), and every other keyword is a
deviation from it. "She is smiling as usual" == mood unchanged.

Sentiment polarity comes from MAS's own table (json_exporter.emotion_sentiment),
so the signs are not invented here. The magnitudes are deliberately asymmetric
(negative slightly stronger than positive) so a session does not ratchet upward
just because positive tags are more frequent.

Threading
---------
update_keywords() is called from the websocket thread (maica.add_ana ->
MoodStatus.analyze). This class therefore holds ONLY plain Python data and
never touches renpy.store / persistent. Writing a float attribute is atomic
under the GIL, so reading it from the main thread is safe.
"""

import os
import sys

try:                                    # py2 builtin; py3 has str only
    _TEXT = unicode                     # noqa: F821
except NameError:
    _TEXT = str

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# mood_words 是开发期的可选词表（不随插件分发）。插件里没有它就用法下面的内置兜底表，
# 所以【不能】依赖任何开发机路径 —— 只有 import 成功才用，失败即降级。
try:
    import mood_words as _mw
    MOOD_LABEL = _mw.mood_label
    _TIER_BASE = dict(_mw.TIER_BASE)
except Exception:
    MOOD_LABEL = None
    _TIER_BASE = {
        "love": 60, "enamored": 45, "aff": 30, "happy": 15,
        "normal": 0, "upset": -30, "distressed": -60, "broken": -90,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. 档位：好感度 -> 静息心情（慢变基线）
#    边界与 mood_words.TIER_RANGE 一致（已实测：64 -> happy -> +15）
# ─────────────────────────────────────────────────────────────────────────────

def tier_of(affection):
    """Map an affection value to a tier name."""
    try:
        a = int(affection)
    except Exception:
        return "normal"
    if a >= 1000:
        return "love"
    if a >= 400:
        return "enamored"
    if a >= 100:
        return "aff"
    if a >= 30:
        return "happy"
    if a >= -29:
        return "normal"
    if a >= -99:
        return "upset"
    return "distressed"


def baseline_of(affection):
    """Resting mood for an affection value (the slow baseline)."""
    return _TIER_BASE.get(tier_of(affection), 0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. ★ 相对化映射表：情绪词 -> 偏离量
#    微笑 = 0（基线）；数值为"相对于她平常那种微笑"的偏离
#    覆盖 emotion_selector 的全部 28 个键 + 归一变体"很开心"
#    符号取自 json_exporter.emotion_sentiment
# ─────────────────────────────────────────────────────────────────────────────

KEYWORD_DELTA = {
    # ── 基线：她 73% 的时候就是这个表情 ───────────────────────────────
    u"微笑":  +0.0,

    # ── 中性（sentiment 0）：有话说但不表达情绪倾向 ──────────────────
    u"思考":  +0.0,
    u"眨眼":  +0.0,
    u"严肃":  +0.0,
    u"凝视":  +0.0,
    u"惊讶":  +0.0,
    u"脸红":  +0.0,
    u"意味深长": +0.0,

    # ── 轻度正向：比平常那个微笑再多一点 ─────────────────────────────
    u"笑":    +3.0,
    u"开心":  +8.0,
    u"很开心": +8.0,
    u"轻松":  +3.5,
    u"得意":  +2.5,
    u"可爱":  +2.0,
    u"激动":  +6.0,

    # ── 强烈正向：权重刻意压低，避免心情被持续推高 ───────────────────
    u"惊喜":  +5.0,
    u"感动":  +6.0,
    u"憧憬":  +2.5,
    u"宠爱":  +3.0,

    # ── 轻负向 ────────────────────────────────────────────────────────
    u"害羞": -1.5,
    u"尴尬": -2.5,
    u"担心": -5.0,
    u"急切": -4.0,

    # ── 负向：微笑才是同一场景里的常态，负向必须更有分量 ─────────────
    u"不满": -8.0,
    u"厌恶": -10.0,
    u"伤心": -15.0,
    u"沉重": -10.0,
    u"害怕": -10.0,
    u"生气": -15.0,
}

# 未知关键词（上游新增情绪词）不应该被当成正向 —— 按中性处理并记录
UNKNOWN_DELTA = 0.0


class MonikaMood(object):
    """一个标量心情，显示时夹取到 -100..+100。

    Structure:  mood = baseline(affection) + deviation

    真正被对话推动的是 deviation(dev)，范围由 MAX_DEV_UP/MAX_DEV_DOWN 限定
    （当前 +40 / -40）。心情显示的硬夹取是 LIMIT/FLOOR（+100 / -100）。

    ★ 2026-10-22 改（用户反馈"数值总被限制在某个区间、对回复影响不大"）：
      根因不是范围，而是**后端按绝对心情分档**：好感 100+ 时基线 +30，
      dev 只有 ±40 → 心情永远在 [-10, +70]，"心情不好"那一档物理上到不了，
      "心情好"要 dev>10 才触发，于是 90% 的轮次都是"平静 = 没有指令"。
      现在的做法：
        ① 【按 dev 分档】5 档（high/up/flat/down/low），任何好感档位都够得着；
        ② 【推力加大】开心/感动这类从 4.5 提到 8，衰减 0.8→0.85（多留一轮）；
        ③ 【连击】连续两轮同向再 ±3，让"越聊越开心/越聊越糟"出现；
        ④ 【事件冲击】praise（被夸）走 add_praise()，+8~+10 直接记在 impulse 上，
           一轮就能换档；带防刷（同词 5 轮一次、10 轮最多 2 次）；
        ⑤ 【跨会话】export_state()/import_state()，离线按 24 小时半衰期回落。
    """

    DECAY = 0.85          # 偏离量每轮的惯性（1-DECAY 为回归速度）
    GAIN = 1.0            # 表值即每轮推力（2026-10-07 起）
    MAX_DEV_UP = 40.0
    MAX_DEV_DOWN = -40.0
    LIMIT = 100.0
    FLOOR = -100.0

    # 连击：同向连续 N 轮 → 额外推力
    STREAK_MIN = 2
    STREAK_BONUS = 3.0

    # 防刷：同一个词 N 轮内只算一次；WINDOW 轮内最多 CAP 次
    PRAISE_SAME_WINDOW = 5
    PRAISE_CAP = 2
    PRAISE_CAP_WINDOW = 10

    # 5 档（按 dev 判定）+ 给模型的具体行为指令
    BUCKETS = (
        (20.0, "high", u"你此刻心情很好：话可以多一点、主动起话头、愿意逗他，可以撒娇。"),
        (8.0, "up", u"你此刻心情不错：比平时稍主动一点，语气轻快一点。"),
        (-8.0, "flat", u"你此刻心情平静。"),
        (-20.0, "down", u"你此刻有点低落：句子短一些、少提问；他问你时正常回答。"),
    )
    BUCKET_LOW = ("low", u"你此刻心情很低：话很短、不主动开新话题；但不要用沉默惩罚他。")
    BUCKET_TAIL = u"（这是状态不是话题：别说出「心情」这两个字，直接体现在语气里。）"

    def __init__(self, affection=0):
        self.affection = affection
        self.deviation = 0.0
        self._delta = 0.0
        self._impulse = 0.0      # 事件冲击（被夸等），每轮结算后清零
        self._seen = []          # 本轮收到的情绪词（诊断用）
        self._unknown = []       # 上游新增、本表未覆盖的词
        self._praise_hist = []   # [(turn, word)]，防刷用
        self._reason = u""       # 最近一次冲击的原因（面板显示）
        self._pos_streak = 0
        self._neg_streak = 0
        self.turns = 0

    # ---- 输入 ---------------------------------------------------------------
    def set_affection(self, affection):
        self.affection = affection

    def baseline(self):
        return float(baseline_of(self.affection))

    def mood(self):
        return self.baseline() + self.deviation

    def update_keywords(self, keywords):
        """Buffer one reply's emotion keywords. Safe to call many times per turn."""
        if not keywords:
            return
        for kw in keywords:
            if kw is None:
                continue
            if not isinstance(kw, _TEXT):
                kw = _TEXT(kw)
            self._seen.append(kw)
            if kw in KEYWORD_DELTA:
                self._delta += KEYWORD_DELTA[kw]
            else:
                self._delta += UNKNOWN_DELTA
                if kw not in self._unknown:
                    self._unknown.append(kw)

    def add_praise(self, word, weight):
        """玩家夸了她。带防刷；返回是否真的计入。"""
        if not word:
            return False
        keep = []
        for t, w in self._praise_hist:
            if self.turns - t < self.PRAISE_CAP_WINDOW:
                keep.append((t, w))
        self._praise_hist = keep
        for t, w in keep:
            if w == word and self.turns - t < self.PRAISE_SAME_WINDOW:
                return False
        if len(keep) >= self.PRAISE_CAP:
            return False
        self._praise_hist.append((self.turns, word))
        try:
            self._impulse += float(weight)
        except Exception:
            self._impulse += 8.0
        self._reason = u"被夸「%s」" % word
        return True

    def add_impulse(self, delta, reason=u""):
        """其它事件冲击（礼物/道歉/冷落…），与情绪词分开记。"""
        try:
            self._impulse += float(delta)
        except Exception:
            return
        if reason:
            self._reason = reason

    # ---- 每轮结算（关键：一轮只算一次，避免流式重复计数）------------------------
    def on_turn(self):
        """Settle one conversation turn: decay the deviation, then apply this turn's pushes."""
        self.turns += 1
        raw = self._delta * self.GAIN + self._impulse

        # 连击：连续同向再加一把力
        if self._delta > 0:
            self._pos_streak += 1
            self._neg_streak = 0
        elif self._delta < 0:
            self._neg_streak += 1
            self._pos_streak = 0
        bonus = 0.0
        if self._pos_streak >= self.STREAK_MIN:
            bonus = self.STREAK_BONUS
        elif self._neg_streak >= self.STREAK_MIN:
            bonus = -self.STREAK_BONUS

        self.deviation = self.deviation * self.DECAY + raw + bonus
        if self.deviation > self.MAX_DEV_UP:
            self.deviation = self.MAX_DEV_UP
        elif self.deviation < self.MAX_DEV_DOWN:
            self.deviation = self.MAX_DEV_DOWN
        self._delta = 0.0
        self._impulse = 0.0
        self._seen = []
        return self.mood()

    def reset_turn_buffer(self):
        """Drop buffered keywords without advancing the state (aborted turns)."""
        self._delta = 0.0
        self._seen = []

    # ---- 输出 ---------------------------------------------------------------
    def value(self):
        v = self.mood()
        if v > self.LIMIT:
            v = self.LIMIT
        elif v < self.FLOOR:
            v = self.FLOOR
        return int(round(v))

    def bucket(self):
        """按偏离量分档 → (key, 给模型的行为指令)。"""
        d = self.deviation
        for limit, key, text in self.BUCKETS:
            if d >= limit:
                return key, text + self.BUCKET_TAIL
        return self.BUCKET_LOW[0], self.BUCKET_LOW[1] + self.BUCKET_TAIL

    def reason(self):
        return self._reason

    # 面板文案（按 dev 五档给，和注入提示词的那五档一一对应）
    BUCKET_LABELS = {
        "high": u"很有兴致、话变多了",
        "up": u"心情不错、放松",
        "flat": u"平静，正常聊天",
        "down": u"有点低落、话变少了",
        "low": u"很低落，不太想说话",
    }

    def label(self):
        """界面文案：按【档位】给，不按绝对心情。

        ★ 2026-10-22 改：以前按绝对心情分（>=70/>=20/…），好感 100+ 时基线 +30，
        面板永远停在"心情不错、放松"，玩家看不出心情在动。现在 dev 一变档，
        面板文案就跟着变，和注入提示词的档位完全一致。
        """
        key = self.bucket()[0]
        text = self.BUCKET_LABELS.get(key)
        if text:
            return text
        if MOOD_LABEL:
            try:
                return MOOD_LABEL(self.mood())
            except Exception:
                pass
        return u"\u5e73\u9759\uff0c\u6b63\u5e38\u804a\u5929"

    # ---- 跨会话 -------------------------------------------------------------
    def export_state(self):
        import time as _t
        return {"dev": round(self.deviation, 3), "turns": self.turns, "at": _t.time()}

    def import_state(self, d):
        """恢复上次的偏离；按离线时长衰减（半衰期 24 小时）。"""
        if not isinstance(d, dict):
            return
        try:
            dev = float(d.get("dev") or 0.0)
            at = float(d.get("at") or 0.0)
        except Exception:
            return
        if not dev:
            return
        import time as _t
        hours = max(0.0, (_t.time() - at) / 3600.0) if at else 0.0
        factor = 0.5 ** (hours / 24.0)
        self.deviation = max(self.MAX_DEV_DOWN, min(self.MAX_DEV_UP, dev * factor))

    def describe(self):
        key, _text = self.bucket()
        return {
            "mood": self.value(),
            "mood_label": self.label(),
            "tier": tier_of(self.affection),
            "baseline": self.baseline(),
            "deviation": round(self.deviation, 2),
            "bucket": key,
            "reason": self._reason,
            "turns": self.turns,
            "unknown_keywords": list(self._unknown),
        }

    def __repr__(self):
        return "<MonikaMood %+d (base %.0f dev %+.1f bucket=%s) turns=%d>" % (
            self.value(), self.baseline(), self.deviation, self.bucket()[0], self.turns)