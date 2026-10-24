#!/usr/bin/env python3
# -*- coding: utf-8 -*-
u"""MAICA_Mood · 问卷网页（本地小服务，Python 3）

为什么要有它：Ren'Py 6.99 的游戏进程（py2）既没有 sqlite3 也没有 HTTP 服务，
而且游戏内的输入框不能输入中文。所以问卷/记忆展示都放到系统浏览器里做：
插件点按钮 → 起这个服务 → `webbrowser` 打开 http://127.0.0.1:<port>/?t=<token>。

约定：
  · 只监听 127.0.0.1；每次启动一个随机 token，所有接口都要校验
  · 题目从 <插件>/questionnaire_questions.txt 读（`题号 | 模板{空} | 提示`）
  · 答案写 <backend>/data/questionnaire.db（和 maica.db 同目录 → 更新保留、卸载随 data 删）
  · 记忆列表只读打开 <backend>/data/maica.db（后端是唯一写者，这里绝不写）
  · 空闲 10 分钟或父进程（游戏）消失 → 自己退出

用法：
  python webui_server.py --port 8099 --token XXX \
      --questions questionnaire_questions.txt \
      --db data/questionnaire.db --facts-db data/maica.db \
      --last-json logs/webui_last.json --parent-pid 1234 [--backend-http 6100]
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import urllib.request

# ---------------------------------------------------------------------------
# 校验规则（用户 2026-10-22 定）：填空 10 字以内、不能有标点符号；空 = 跳过
# ---------------------------------------------------------------------------
MAX_BLANK = 10
# 中英文标点 + 空白：一律不接受（全角空格也在这里）
PUNCT = set(u"，。、；：！？…—～·“”‘’（）《》【】〈〉「」『』〔〕"
            u",.;:!?-~\"'()[]{}<>/\\|`@#$%^&*+=_ \t\r\n\u3000")
PLACEHOLDER = u"{空}"


def parse_questions(path):
    """读题目文件 → [{qid, prefix, suffix, hint}]；坏行跳过。"""
    out = []
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = f.read().splitlines()
    except Exception as e:
        return [], u"读不到题目文件：%s" % e
    for lineno, line in enumerate(raw, 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.split("|")]
        if len(parts) < 2:
            continue
        qid = parts[0]
        tpl = parts[1]
        hint = parts[2] if len(parts) > 2 else ""
        if tpl.count(PLACEHOLDER) != 1:
            continue
        prefix, suffix = tpl.split(PLACEHOLDER)
        out.append({"qid": qid, "prefix": prefix, "suffix": suffix, "hint": hint})
    return out, None


def check_value(v):
    """返回错误信息，None = 合法。"""
    v = (v or u"").strip()
    if not v:
        return None                      # 空 = 跳过
    if len(v) > MAX_BLANK:
        return u"最多 %d 字（现在 %d 字）" % (MAX_BLANK, len(v))
    for ch in v:
        if ch in PUNCT:
            return u"不能有标点符号或空格（发现了「%s」）" % ch
    return None


# ---------------------------------------------------------------------------
# 数据层
# ---------------------------------------------------------------------------
def db_connect(path, readonly=False):
    if readonly:
        uri = "file:%s?mode=ro" % path.replace("\\", "/")
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
    else:
        conn = sqlite3.connect(path, timeout=3.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
    conn.row_factory = sqlite3.Row
    return conn


def db_init(path):
    conn = db_connect(path)
    try:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS answers("
            " qid TEXT PRIMARY KEY, value TEXT, sentence TEXT,"
            " content_hash TEXT, imported_hash TEXT, updated_at TEXT,"
            " fact_ids TEXT, removed INTEGER DEFAULT 0);"
            "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);"
        )
        # ★ 2026-10-22：老库补两列（fact_ids = 这一题上次导入产生的行；removed = 已撤回）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(answers)")}
        if "fact_ids" not in cols:
            conn.execute("ALTER TABLE answers ADD COLUMN fact_ids TEXT")
        if "removed" not in cols:
            conn.execute("ALTER TABLE answers ADD COLUMN removed INTEGER DEFAULT 0")
        conn.commit()
    finally:
        conn.close()


def load_answers(path):
    try:
        conn = db_connect(path, readonly=True)
    except Exception:
        return {}
    try:
        return {r["qid"]: r["value"] for r in conn.execute("SELECT qid, value FROM answers")}
    except Exception:
        return {}
    finally:
        conn.close()


def save_answers(path, questions, values):
    """写库。返回 (saved, skipped, errors)。

    ★ 2026-10-22：清空一题不再直接 DELETE 行，而是留下"墓碑"（removed=1，
    value/sentence/content_hash 全空，fact_ids 保留）——后端下次导入时据此把这一题
    以前产生的事实删掉。直接删行会把 fact_ids 一起丢掉，旧事实就永远留在记忆里。
    """
    saved, skipped, errors = 0, 0, []
    conn = db_connect(path)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        for q in questions:
            qid = q["qid"]
            # 只处理这次真的提交了的题：没出现在 payload 里的题保持原样，
            # 免得部分提交把别的答案抹掉
            if qid not in values:
                continue
            raw = (values.get(qid) or u"").strip()
            err = check_value(raw)
            if err:
                errors.append({"qid": qid, "msg": err})
                continue
            if not raw:
                cur = conn.execute(
                    "UPDATE answers SET value='', sentence='', content_hash='',"
                    " removed=1, updated_at=? WHERE qid=?", (now, qid))
                if cur.rowcount:
                    skipped += 1          # 记作"清空"，页面上和"留空"一起数
                continue
            sentence = q["prefix"] + raw + q["suffix"]
            # ★ hash 只算填空内容：以后改题干措辞不会把老答案当成"新内容"重新入库
            import hashlib
            h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
            conn.execute(
                "INSERT INTO answers(qid, value, sentence, content_hash, updated_at,"
                " removed) VALUES(?,?,?,?,?,0)"
                " ON CONFLICT(qid) DO UPDATE SET"
                "   value=excluded.value, sentence=excluded.sentence,"
                "   content_hash=excluded.content_hash, updated_at=excluded.updated_at,"
                "   removed=0",
                (qid, raw, sentence, h, now),
            )
            saved += 1
        conn.commit()
    finally:
        conn.close()
    return saved, skipped, errors


def list_facts(path, limit=500):
    """只读列出玩家的明确事实（source=player_direct、未停用、未被取代）。"""
    try:
        conn = db_connect(path, readonly=True)
    except Exception as e:
        return [], u"读不到记忆库：%s" % e
    try:
        # ★ 2026-10-22 修：主键列名是 fact_id（SQLAlchemy 里叫 id），不是 id ——
        #   写成 id 会 "no such column: id"，整页只剩一句查询失败。
        rows = conn.execute(
            "SELECT fact_id, fact_text, polarity, speak_time FROM persist_facts"
            " WHERE source='player_direct'"
            "   AND (superseded_by IS NULL) AND (enabled IS NULL OR enabled=1)"
            " ORDER BY fact_id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r["fact_id"], "fact_text": r["fact_text"],
                 "polarity": r["polarity"], "time": r["speak_time"]} for r in rows], None
    except Exception as e:
        return [], u"查询失败：%s" % e
    finally:
        conn.close()



DELETE_HINT = u"需要先进入天堂树林进行一次对话才能删除"
RENDER_HINT = u"进入天堂树林以显示库中原句"
# 保存后的提示（2026-10-22）：导入是在后台按轮次跑的，不写清楚玩家会以为没生效
SAVE_HINT = (u"切回游戏、在本地后端节点里随便说一句话，她就会把这些记下来；"
             u"全部导入大约需要 1 分钟，期间可以正常聊天。")


def backend_status(backend_http, timeout=1.0):
    """探测后端与用户状态。

    返回 (state, can_delete, reason)，state ∈ {"ok", "no_user", "down"}
      ok      : 后端在跑且知道用户是谁 → 可以删
      no_user : 后端在跑但还没拿到用户（还没聊过）→ 只读
      down    : 后端没在跑 → 只读
    """
    if not backend_http:
        return "down", False, DELETE_HINT
    try:
        url = "http://127.0.0.1:%d/mm/health" % int(backend_http)
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
        content = data.get("content") if isinstance(data.get("content"), dict) else {}
        if content.get("user"):
            return "ok", True, ""
        return "no_user", False, DELETE_HINT
    except Exception:
        return "down", False, DELETE_HINT


def backend_facts(backend_http, timeout=2.0):
    """从后端取"她实际会召回的那句短句"；后端没跑/没用户 → None（回退直读库）。"""
    if not backend_http:
        return None
    try:
        url = "http://127.0.0.1:%d/mm/facts" % int(backend_http)
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
        if not data.get("success"):
            return None
        content = data.get("content")
        return content if isinstance(content, list) else None
    except Exception:
        return None


def backend_delete_fact(backend_http, fid):
    """删除请求转发给后端（后端是 maica.db 的唯一写者）。"""
    if not backend_http:
        return False, u"本地后端没在运行（当前可能不在本地节点）"
    url = "http://127.0.0.1:%d/mm/facts?id=%d" % (int(backend_http), int(fid))
    try:
        req = urllib.request.Request(url, method="DELETE")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
        if data.get("success"):
            return True, ""
        return False, data.get("exception") or u"后端拒绝了删除"
    except Exception as e:
        return False, u"连不上本地后端：%s" % (e,)


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
_CSS = """
:root { color-scheme: dark; }
body { margin:0; background:#15161a; color:#e8e8ea;
       font:15px/1.7 "Microsoft YaHei","PingFang SC",system-ui,sans-serif; }
.wrap { max-width:720px; margin:0 auto; padding:28px 20px 80px; }
h1 { font-size:19px; margin:0 0 4px; color:#ff9ecb; }
.sub { color:#8d8f99; font-size:13px; margin-bottom:22px; }
.card { background:#1d1f25; border:1px solid #2a2d35; border-radius:12px;
        padding:16px 18px; margin-bottom:12px; }
.q { color:#c9cbd4; }
.q b { color:#ff9ecb; font-weight:600; margin-right:10px; }
.line { margin-top:10px; font-size:17px; color:#f2f2f5; }
input[type=text] { background:#12131a; border:1px solid #3a3f4b; color:#fff;
    border-radius:7px; padding:6px 10px; font:inherit; width:9em; text-align:center; }
input[type=text]:focus { outline:none; border-color:#ff9ecb; }
.hint { color:#6f727c; font-size:12px; margin-top:6px; }
.err { color:#ff6b6b; font-size:12px; margin-top:6px; }
.bar { position:fixed; left:0; right:0; bottom:0; background:#1b1d22;
       border-top:1px solid #2a2d35; padding:12px 20px; display:flex;
       gap:12px; align-items:center; justify-content:center; }
button { background:#ff9ecb; color:#17181c; border:0; border-radius:8px;
         padding:9px 22px; font:inherit; font-weight:600; cursor:pointer; }
button.ghost { background:#2a2d35; color:#d8d9de; }
button:disabled { opacity:.45; cursor:default; }
.tbl { width:100%; border-collapse:collapse; font-size:14px; }
.tbl td { border-bottom:1px solid #26282f; padding:9px 6px; vertical-align:top; }
.tbl td.id { color:#6f727c; width:52px; }
.empty { color:#8d8f99; padding:26px 0; text-align:center; }
a { color:#8fb8ff; }
.ok { color:#7ee2a8; }
.status { color:#8d8f99; font-size:12.5px; margin-bottom:10px; }
.hintline { color:#8d8f99; font-size:12.5px; line-height:1.6; }
.orig { color:#7a7d88; font-size:12.5px; margin-top:4px; }
.warn { color:#ffc46b; background:#2a2216; border:1px solid #4a3a1e;
        border-radius:8px; padding:10px 14px; margin-bottom:12px; font-size:14px; }
"""

_JS = """
async function save(btn) {
  btn.disabled = true;
  const data = {answers:{}};
  document.querySelectorAll('input[data-qid]').forEach(el => { data.answers[el.dataset.qid] = el.value; });
  try {
    const r = await fetch('/api/save?t=' + encodeURIComponent(TOKEN), {
      method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(data)});
    const j = await r.json();
    document.querySelectorAll('.err').forEach(e => e.remove());
    if (j.errors && j.errors.length) {
      for (const e of j.errors) {
        const box = document.querySelector('[data-box="' + e.qid + '"]');
        if (box) { const d = document.createElement('div'); d.className='err'; d.textContent=e.msg; box.appendChild(d); }
      }
      btn.disabled = false;
      return;
    }
    document.getElementById('status').innerHTML =
      '<span class="ok">已保存 ' + j.saved + ' 条，留空/清空 ' + j.skipped + ' 条。</span>' +
      (j.hint ? '<br><span class="hintline">' + j.hint + '</span>' : '');
  } catch (err) {
    document.getElementById('status').textContent = '保存失败：' + err;
    btn.disabled = false;
  }
}
"""



_JS_FACTS = """
function showHint() {
  const box = document.getElementById('delerr');
  if (box) box.textContent = HINT;
}

async function delFact(id, btn) {
  if (!confirm('删掉这条吗？她会立刻忘掉它。')) return;
  btn.disabled = true;
  const box = document.getElementById('delerr');
  if (box) box.textContent = '';
  try {
    const r = await fetch('/api/facts/' + id + '?t=' + encodeURIComponent(TOKEN), {method:'DELETE'});
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.error) {
      if (box) box.textContent = '删除失败：' + (j.error || ('HTTP ' + r.status));
      btn.disabled = false;
      return;
    }
    location.reload();
  } catch (e) {
    if (box) box.textContent = '删除失败：' + e;
    btn.disabled = false;
  }
}
"""


def page_fill(questions, answers, token):
    rows = []
    for q in questions:
        qid = q["qid"]
        val = answers.get(qid, u"")
        hint = (u'<div class="hint">%s</div>' % q["hint"]) if q["hint"] else u""
        rows.append(
            u'<div class="card" data-box="%s">'
            u'<div class="q"><b>%s</b></div>'
            u'<div class="line">%s<input type="text" data-qid="%s" value="%s" maxlength="%d">%s</div>'
            u'%s</div>' % (qid, q["qid"], q["prefix"], qid, val.replace('"', "&quot;"),
                           MAX_BLANK, q["suffix"], hint)
        )
    body = u"\n".join(rows) if rows else u'<div class="empty">题目文件是空的。</div>'
    return (u'<!doctype html><html lang="zh"><meta charset="utf-8">'
            u'<meta name="viewport" content="width=device-width,initial-scale=1">'
            u'<title>快速填写信息</title><style>%s</style><body><div class="wrap">'
            u'<h1>快速填写信息</h1>'
            u'<div class="sub">每空 10 字以内、不要标点；不想答的留空。<br>'
            u'填完点下面的保存，然后关掉这个页面回到游戏。</div>'
            u'%s</div>'
            u'<div class="bar"><span id="status"></span>'
            u'<button onclick="save(this)">保存</button>'
            u'<button class="ghost" onclick="window.close()">关闭</button></div>'
            u'<script>const TOKEN="%s";%s</script></body></html>'
            % (_CSS, body, token, _JS))


def page_facts(facts, err, token, can_delete=True, reason=u"", state="ok"):
    if err:
        return _simple(u"已记录的信息", u'<div class="empty">%s</div>' % err)

    label = {"ok": u"后端运行中 · 已识别用户",
             "no_user": u"后端运行中 · 还没识别到用户",
             "down": u"后端未运行（只读）"}.get(state, u"")
    status = (u'<div class="status">%s</div>' % _esc(label)) if label else u""

    if not facts:
        body = u'<div class="empty">目前库中还没有已记录的信息。</div>'
        if state != "ok":
            body += (u'<div class="empty" style="padding-top:0">先切到「本地后端节点」，'
                     u'进天堂树林和她说一句话，再点下面的「刷新」。</div>')
        return _simple(u"已记录的信息", status + body +
                       u'<div style="text-align:center"><button class="ghost" '
                       u'onclick="location.reload()">刷新</button></div>')

    rows = []
    for f in facts:
        neg = u"（否定）" if int(f["polarity"] or 1) < 0 else u""
        rendered = (f.get("render") or "").strip()
        original = (f.get("fact_text") or "").strip()
        if rendered and rendered != original:
            main_line = (u'<div>%s</div><div class="orig">原话：%s</div>'
                         % (_esc(rendered), _esc(original)))
        elif rendered:
            main_line = u'<div>%s</div>' % _esc(rendered)
        else:
            main_line = (u'<div>%s</div><div class="orig">%s</div>'
                         % (_esc(original), _esc(RENDER_HINT)))
        btn = (u'<button class="del" onclick="delFact(%s, this)">删除</button>' % int(f["id"]))
        if not can_delete:
            btn = (u'<button class="del" disabled title="%s" '
                   u'onclick="showHint()">删除</button>' % _esc(reason))
        rows.append(u'<tr><td class="id">#%s</td><td>%s%s</td>'
                    u'<td class="act">%s</td></tr>'
                    % (f["id"], main_line, neg, btn))

    note = (u'<div class="warn">%s</div>' % _esc(reason)) if (not can_delete and reason) else u""
    inner = (status + note +
             u'<div class="sub">共 %d 条（只显示你亲口说过的明确事实）。'
             u'删除后她会立刻忘掉这条；以后再聊到，她还会重新记住。</div>'
             u'<div class="card"><table class="tbl">%s</table></div>'
             u'<div class="err" id="delerr"></div>'
             u'<div style="text-align:center;margin-top:14px"><button class="ghost" '
             u'onclick="location.reload()">刷新</button></div>'
             % (len(facts), u"".join(rows)))
    return _simple(u"已记录的信息", inner, token=token,
                   extra_js=u'const HINT="%s";' % _esc(reason) + _JS_FACTS)

def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _simple(title, inner, token="", extra_js=""):
    tail = u''
    if extra_js:
        tail = u'<script>const TOKEN="%s";%s</script>' % (token, extra_js)
    return (u'<!doctype html><html lang="zh"><meta charset="utf-8">'
            u'<meta name="viewport" content="width=device-width,initial-scale=1">'
            u'<title>%s</title><style>%s</style><body><div class="wrap">'
            u'<h1>%s</h1>%s</div>'
            u'<div class="bar"><button class="ghost" onclick="window.close()">关闭</button></div>'
            u'%s</body></html>' % (title, _CSS, title, inner, tail))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "MAICAMoodWebUI/1.0"

    def log_message(self, fmt, *args):        # 安静点，别刷日志
        if self.server.log_path:
            try:
                with open(self.server.log_path, "a", encoding="utf-8") as f:
                    f.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
            except Exception:
                pass

    # -- helpers --
    def _tok_ok(self, qs):
        return (qs.get("t") or [""])[0] == self.server.token

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _touch(self):
        self.server.last_active = time.time()

    # -- routes --
    def do_GET(self):
        self._touch()
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/api/health":
            return self._json(200, {"ok": True, "pid": os.getpid()})
        if not self._tok_ok(qs):
            return self._send(403, u"token 无效 —— 请从游戏里重新点按钮打开。")
        if u.path in ("/", "/index.html"):
            qs_list, err = parse_questions(self.server.questions_path)
            if err:
                return self._send(500, err)
            answers = load_answers(self.server.db_path)
            return self._send(200, page_fill(qs_list, answers, self.server.token))
        if u.path == "/facts":
            facts, err = list_facts(self.server.facts_db)
            state, can_delete, reason = backend_status(self.server.backend_http)
            if can_delete:
                rich = backend_facts(self.server.backend_http)
                if rich is not None:
                    facts, err = rich, None
            return self._send(200, page_facts(facts, err, self.server.token,
                                              can_delete=can_delete, reason=reason,
                                              state=state))
        if u.path == "/api/facts":
            facts, err = list_facts(self.server.facts_db)
            return self._json(200, {"facts": facts, "error": err})
        return self._send(404, u"没有这个页面。")

    def do_DELETE(self):
        self._touch()
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._tok_ok(qs):
            return self._json(403, {"error": "bad token"})
        m = re.match(r"^/api/facts/(\d+)$", u.path)
        if not m:
            return self._json(404, {"error": "no such endpoint"})
        ok, msg = backend_delete_fact(self.server.backend_http, int(m.group(1)))
        if ok:
            return self._json(200, {"deleted": int(m.group(1))})
        return self._json(409, {"error": msg})

    def do_POST(self):
        self._touch()
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not self._tok_ok(qs):
            return self._json(403, {"error": "bad token"})
        if u.path != "/api/save":
            return self._json(404, {"error": "no such endpoint"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception as e:
            return self._json(400, {"error": "bad json: %s" % e})
        values = payload.get("answers") or {}
        qs_list, err = parse_questions(self.server.questions_path)
        if err:
            return self._json(500, {"error": err})
        saved, skipped, errors = save_answers(self.server.db_path, qs_list, values)
        result = {"saved": saved, "skipped": skipped, "errors": errors,
                  "hint": SAVE_HINT, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            with open(self.server.last_json, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False)
        except Exception:
            pass
        return self._json(200, result)


def _parent_alive(pid):
    if not pid:
        return True
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000, False, int(pid))   # SYNCHRONIZE
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(h, 0) == 0x102     # WAIT_TIMEOUT
    finally:
        k32.CloseHandle(h)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--token", required=True)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--facts-db", required=True)
    ap.add_argument("--last-json", default="")
    ap.add_argument("--log", default="")
    ap.add_argument("--backend-http", type=int, default=0)
    ap.add_argument("--parent-pid", type=int, default=0)
    ap.add_argument("--idle-exit", type=float, default=600.0)
    args = ap.parse_args(argv)

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    db_init(args.db)

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.stderr.write("port busy: %s\n" % e)
        return 2
    httpd.token = args.token
    httpd.questions_path = args.questions
    httpd.db_path = args.db
    httpd.facts_db = args.facts_db
    httpd.backend_http = args.backend_http
    httpd.last_json = args.last_json
    httpd.log_path = args.log or ""
    httpd.last_active = time.time()

    def guard():
        while True:
            time.sleep(20)
            if not _parent_alive(args.parent_pid):
                break
            if time.time() - httpd.last_active > args.idle_exit:
                break
        try:
            httpd.shutdown()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=guard, daemon=True).start()
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
