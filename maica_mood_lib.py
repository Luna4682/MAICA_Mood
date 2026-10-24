# -*- coding: utf-8 -*-
u"""MAICA 心情插件 · 运行库（Python 2.7 / 3 双兼容）

纯逻辑，不碰 renpy.store / persistent（那些在 MAICA_Mood.rpy 里做）。
四件事：节点注册 / 心情收词 / 会话门槛判断 / 掩掉官方 status 撞名误判。
不修改任何官方文件。
"""

import re
import os
import sys
import json
import time
import socket
import subprocess
import binascii

NODE_ID = 9998
NODE_NAME = u"MAICA_Mood 本地后端"
NODE_DESC = u"本地自建后端 · 自定义人设与心情系统"
NODE_MODEL = u"deepseek-flash"

# ★ 好感门槛（2026-10-18 用户指定）：官方 README「提示」第 1 条 ——
#   "完整的功能只会在好感度达到 100 后开放"。我们的节点必须遵守同一门槛：
#   好感 < 100 时节点不出现在列表里；已经选中但好感掉下来时自动切回官方节点。
AFFECTION_MIN = 100

# ★ 提取器空闲多久后自动停掉（秒）。2026-10-18 用户定的规格：
#   进入聊天 → 启动；退出聊天 → 关闭；聊天中连续 2 分钟没对话 → 停止。
#   提取器是 Q8 模型常驻约 1.7 GB，不聊天时没必要占着。
EXTRACT_IDLE_STOP = 120.0

# ---------------------------------------------------------------------------
# 端口：唯一真实来源 = <插件>\ports.json（安装器生成，玩家可改）
# 2026-10-17：原先 ws/http 写死在 WS_URL/HTTP_URL、提取器写死在 EXTRACTOR_PORT、
# 嵌入服务写死在 embed_server.py、后端写死在 .env —— 玩家改一处没用，必须改 5~6 处。
# 现在全部从这里发散；后端 .env 也由本模块在启动前【现生成】。
# ---------------------------------------------------------------------------
PORTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ports.json")
DEFAULT_PORTS = {"ws": 5100, "http": 6100, "embed": 8090, "extract": 8091}
_PORTS_CACHE = {}

SESSION_OURS = 10240         # 我们在自己节点上用的窗口（2026-10-20 定；100 轮实测 7.2K token 就够）
SESSION_LEGACY = 16384       # 旧版本插件写过的值：遇到就迁到 SESSION_OURS / 还原官方
SESSION_MIN = SESSION_OURS   # 兼容旧名（历史代码按它做下限判断）
SESSION_HARD_MAX = 28672     # 官方硬上限（前端 maica.py:40 + 后端 pydantic le=28672）
OFFICIAL_SESSION_LEN = 8192  # 官方默认值：原值丢失时用它兜底还原

BAD_TASK_CLASS = "HistoryStatusHandler"
TAG_RE = re.compile(u"\\[([^\\[\\]]{1,12})\\]")


def load_ports():
    """读 ports.json；缺失/损坏 → 用默认值（并写回一份，方便玩家知道有这文件）。"""
    try:
        mtime = os.path.getmtime(PORTS_FILE)
        hit = _PORTS_CACHE.get("v")
        if hit and _PORTS_CACHE.get("m") == mtime:
            return dict(hit)
        with open(PORTS_FILE, "r") as f:
            raw = json.load(f)
        ports = dict(DEFAULT_PORTS)
        for k in DEFAULT_PORTS:
            v = raw.get(k)
            if isinstance(v, int) and 1 <= v <= 65535:
                ports[k] = v
        _PORTS_CACHE["m"] = mtime
        _PORTS_CACHE["v"] = ports
        return dict(ports)
    except Exception:
        ports = dict(DEFAULT_PORTS)
        try:
            save_ports(ports)
        except Exception:
            pass
        return ports


def save_ports(ports):
    """写 ports.json（保留玩家手改过的其它键）。"""
    try:
        cur = {}
        if os.path.isfile(PORTS_FILE):
            try:
                with open(PORTS_FILE, "r") as f:
                    cur = json.load(f) or {}
            except Exception:
                cur = {}
        for k in DEFAULT_PORTS:
            if k in ports:
                cur[k] = int(ports[k])
        with open(PORTS_FILE, "w") as f:
            json.dump(cur, f, indent=2, sort_keys=True)
        _PORTS_CACHE.clear()
        return True
    except Exception:
        return False


def port_free(port):
    """端口是否可 bind（没被别的程序占）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", int(port))) != 0
    except Exception:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass


def _port_owned_by_us(port):
    """该端口上的监听进程是不是我们插件目录里的（= 上次崩溃留下的残留服务）。"""
    pid = _pid_listening(port)
    if not pid:
        return False
    exe = _exe_of(pid)
    return bool(exe) and exe.lower().startswith(_HERE.lower())


def pick_free_ports(base=None, avoid=None):
    """逐个探测：被【别的程序】占了就顺延；占用者是我们自己的残留服务则保留。

    返回 (ports, changed)；changed = [(key, 旧端口, 新端口), ...]
    """
    src = base or load_ports()
    ports = {}
    for k in ("ws", "http", "embed", "extract"):
        try:
            ports[k] = int(src.get(k) or DEFAULT_PORTS[k])
        except Exception:
            ports[k] = DEFAULT_PORTS[k]
    avoid = set(int(x) for x in (avoid or ()))
    changed = []
    used = set()
    for k in ("ws", "http", "embed", "extract"):
        old = ports[k]
        p = old
        n = 0
        while True:
            free = port_free(p)
            ours = (not free) and _port_owned_by_us(p)
            if (free or ours) and (p not in used) and (p not in avoid):
                break
            p += 1
            n += 1
            if n > 200:
                break
        if p != old:
            changed.append((k, old, p))
        ports[k] = p
        used.add(p)
    return ports, changed


def ensure_ports():
    """拉起服务之前调用：端口被别的程序占用就顺延，并把结果写回 ports.json。

    ★ 2026-10-21（待办 #3）：`pick_free_ports` 以前写好了却没人调用，
      `start_backend` 撞到占用直接返回 `port-busy` —— 三个服务一个都起不来，
      玩家只看到"连不上"，界面上没有任何提示。现在改成：
        · 占用者是我们自己的残留服务 → 不动（交给 kill_own_on_ports 收拾）
        · 占用者是别的程序         → 顺延到下一个空闲端口
      端口只有 ports.json 一个真实来源：后端 .env 由它现生成，前端节点条目
      的 ws/http 也从它取，所以改一处、全都跟着改。

    返回 (ports, changed) —— changed = [(key, 旧, 新), ...]
    """
    ports, changed = pick_free_ports()
    if changed:
        save_ports(ports)
    return ports, changed


def ws_url(ports=None):
    return "ws://127.0.0.1:%d" % (ports or load_ports())["ws"]


def http_url(ports=None):
    return "http://127.0.0.1:%d" % (ports or load_ports())["http"]


def node_entry():
    """追加到官方节点表里的条目，字段与官方 _fakelocalprovider 一致。

    ★ 端口从 ports.json 取（每次 get_servers 都会重新注入 → 改端口不用重启游戏）。
    """
    p = load_ports()
    return {
        "id": NODE_ID,
        "name": NODE_NAME,
        "description": NODE_DESC,
        "isOfficial": False,
        "portalPage": "",
        "servingModel": NODE_MODEL,
        "modelLink": "",
        "wsInterface": ws_url(p),
        "httpInterface": http_url(p),
    }


def install_node(manager_class, allow=None):
    """包 get_provider（连接路径）与 get_servers（界面读列表的路径），保证节点始终在表里。

    官方 get_provider() 会重建 self._servers，而且刷新可能是异步的，所以只在
    get_provider 里追加并不安全：界面走的是 get_servers()，两个入口都要补。幂等。

    allow: 可选 callable，返回 False 时【不把我们的节点放进表里】（门槛用）。
           官方设计：完整功能在好感 100 后开放（README「提示」第 1 条），
           所以没到门槛时节点应该像官方那样"还没解锁"。
    """
    if getattr(manager_class, "_mm_node_patched", False):
        return True
    orig_get_provider = manager_class.get_provider
    orig_get_servers = manager_class.get_servers

    def _allowed():
        if allow is None:
            return True
        try:
            return bool(allow())
        except Exception:
            return False

    def _ensure_entry(self):
        try:
            if getattr(self, "_servers", None) is None:
                self._servers = []
            ok = _allowed()
            # 门槛没过：把我们的条目【移除】（好感回落时也要跟着消失）
            if not ok:
                self._servers = [s for s in self._servers
                                 if not _is_our_entry(s)]
                return
            want = node_entry()
            for i, s in enumerate(self._servers):
                if _is_our_entry(s):
                    # ★ 2026-10-21：端口顺延后节点条目要跟着换（否则前端还指着
                    #   旧端口，服务起来了也连不上）。字段没变就什么都不做。
                    if s.get("wsInterface") != want["wsInterface"] or \
                            s.get("httpInterface") != want["httpInterface"]:
                        self._servers[i] = want
                    return
            self._servers.append(want)
        except Exception:
            pass

    def get_provider(self):
        ok = orig_get_provider(self)
        _ensure_entry(self)
        return ok

    def get_servers(self):
        _ensure_entry(self)
        return orig_get_servers(self)

    # ★ 2026-10-24：前端 1.9.5 会绕过 get_provider/get_servers 直接走
    #   _get_server_by_id()（get_server_info / get_wssurl / get_api_url 都调它）。
    #   __init__ 里 _servers 只有官方列表 + 9999；刷新失败时更是只剩
    #   [_isfailedresponse, _fakelocalprovider]。这时候查 9998 会落到
    #   "ERROR: 无法获取节点信息"，界面就显示「获取节点失败 / 服务器可能离线」。
    #   所以这里也补一层：查表前先把我们的条目放回去。
    orig_get_server_by_id = getattr(manager_class, "_get_server_by_id", None)
    if orig_get_server_by_id is not None:
        def _get_server_by_id(self, server_id):
            try:
                _ensure_entry(self)
            except Exception:
                pass
            return orig_get_server_by_id(self, server_id)

        manager_class._get_server_by_id = _get_server_by_id

    manager_class.get_provider = get_provider
    manager_class.get_servers = get_servers
    manager_class._mm_node_patched = True
    return True


def _is_our_entry(entry):
    try:
        return int(entry.get("id")) == NODE_ID
    except Exception:
        return False


def harvest_tags(text, known_words=None):
    """从回复原文里取情绪标签（[微笑] 这种），只保留引擎认识的词。"""
    if not text:
        return []
    try:
        if not isinstance(text, type(u"")):
            text = text.decode("utf-8", "replace")
    except Exception:
        text = u"%s" % (text,)
    out = []
    seen = set()
    for m in TAG_RE.finditer(text):
        w = m.group(1)
        if known_words is not None and w not in known_words:
            continue
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def wanted_session_len(provider_id, current, saved):
    """按当前节点决定 session_len_limit。

    返回 (新值, 是否由我们持有改动, 应保存的原值)

    ★ 2026-10-20：我们的窗口从 16384 降到 10240。
      实测依据：4,045 字提示词 + 100 轮 1.0 长会话只用到 7.2K token，
      10240（警告线 6827）大约第 45 轮才裁剪，而 16384 的 ~120 轮实际用不到，
      只会把无关历史留在上下文里。
    ★ 旧值 16384 视为"我们写过的"：在自己节点上迁到 10240，切回官方时还原。

    ★ 2026-10-17 修：`saved` 丢失时（persistent 写失败 / 旧版本没存）必须退回官方默认值，
      否则玩家会永久卡在自己设的值 —— 删了插件也没人还原。官方默认 8192。
    """
    try:
        pid = int(provider_id)
    except Exception:
        pid = None
    try:
        cur = int(current)
    except Exception:
        cur = 0

    ours = (SESSION_OURS, SESSION_LEGACY)

    if pid == NODE_ID:
        # 玩家的原始值：有存档就用存档；没有就看当前值是不是我们写的
        if saved is not None:
            keep = int(saved)
        elif cur in ours:
            keep = OFFICIAL_SESSION_LEN
        else:
            keep = cur
        if cur != SESSION_OURS:
            return SESSION_OURS, True, keep
        return cur, True, keep

    # 官方节点（或任何不是我们的节点）
    if saved is not None:
        return int(saved), False, None
    if cur in ours:
        return OFFICIAL_SESSION_LEN, False, None
    return cur, False, None


def install_failed_mask(manager_class):
    """包一层 MaicaTaskManager.is_task_failed，把 HistoryStatusHandler 排除。

    官方 bug：HistoryStatusHandler 写 self.status = TOKEN_MAX_EXCEEDED(=2)，
    而 MaicaTask 用 status == 2 表示 ERROR，于是它被永久判为失败任务，
    is_failed() 恒为真。这里是运行时掩码，不改官方文件；正确改法应提给上游。
    """
    if getattr(manager_class, "_mm_failmask_patched", False):
        return True
    orig = manager_class.is_task_failed

    def is_task_failed(self):
        failed = orig(self)
        try:
            return [t for t in failed if t.__class__.__name__ != BAD_TASK_CLASS]
        except Exception:
            return failed

    manager_class.is_task_failed = is_task_failed
    manager_class._mm_failmask_patched = True
    return True


# ============================================================================
#  事实提取器（r8 小模型）—— 模型与运行时都在【插件本体】里，由插件启停
#
#  为什么放插件里：玩家本地要有这个模型，不能依赖开发机路径；
#  插件目录本身就是随包分发的，所以 assets 放这里最稳。
#
#  目录结构（相对本文件所在目录）：
#    extractor/
#      fact-r8-q6_k.gguf          Q6_K 量化（978 条实测与 Q8 持平：漏检 15.5% vs 17.9%，误检 0.70% vs 0.58%；Q4 召回塌到 30.8%，禁用）
#      bin-vulkan/llama-server.exe  优先（有 GPU 时）
#      bin-cpu/llama-server.exe     兜底（无 Vulkan / GPU 时）
#      extractor.log                 运行时日志（插件写）
#
#  与后端的分工：后端只认 http://127.0.0.1:8091/v1（.env 的 MAICA_EXTRACT_ADDR）；
#  路径全部由插件算，后端不需要知道插件在哪。
# ============================================================================

EXTRACTOR_PORT = 8091
EXTRACTOR_ALIAS = "r8"
EXTRACTOR_MODEL = "fact-r8-q6_k.gguf"
CREATE_NO_WINDOW = 0x08000000   # Python 2.7 的 subprocess 里没有这个常量，用原值

# ★ 2026-10-21：Python 2.7（Ren'Py 6.99 用的就是它）没有 subprocess.DEVNULL，
#   直接写它会抛 AttributeError —— 而调用点大多包在 try/except 里，
#   表现是"静默什么都不做"：kill_own_on_ports 从来没真的杀过进程，
#   _exe_of 的 PowerShell 兜底也从来没跑过。这里按版本取。
try:
    DEVNULL = subprocess.DEVNULL
except AttributeError:
    DEVNULL = open(os.devnull, "wb")

_HERE = os.path.dirname(os.path.abspath(__file__))


def plugin_root():
    """插件根目录（本文件所在目录）。"""
    return _HERE


# ---------------------------------------------------------------------------
# 卸载握手（插件 ⇄ <插件>\卸载.bat）
#
# 为什么要有握手文件：session_len_limit 与采样参数都存在 Ren'Py 的 persistent
# 里，插件目录一删，就没有任何代码能再把它们改回官方值 —— 只能靠玩家手动改
# 二进制 persistent，等于没救。所以卸载顺序必须是：
#     ① 卸载脚本创建 .mm_uninstall_request，并启动（或等待）游戏
#     ② 插件看到请求 → 切回官方节点、还原参数、停服务 → 写 .mm_uninstall_done
#     ③ 脚本确认 .mm_uninstall_done 在 → 才删目录
# bat 读不了 persistent（pickle），只认这些普通文件：
#     .mm_uninstall_request  卸载意图（脚本创建，插件删除）
#     .mm_uninstall_done     还原完成凭据（插件写，脚本删）
#     .mm_node_ours          存在 = 当前选中的是我们的节点（诊断/兼容用）
#     .mm_state.json         诊断用（provider_id / limit / 时间）
# ---------------------------------------------------------------------------
NODE_MARKER = ".mm_node_ours"
UNINSTALL_FLAG = ".mm_uninstall_request"
UNINSTALL_DONE = ".mm_uninstall_done"
STATE_JSON = ".mm_state.json"


def marker_path(name=NODE_MARKER):
    return os.path.join(_HERE, name)


def node_marker_exists():
    """True = 当前仍选着我们的节点（bat 看到就不肯直接删）。"""
    return os.path.isfile(marker_path())


def uninstall_requested():
    """bat 放下的卸载请求：插件下次启动时自动切回官方节点。"""
    return os.path.isfile(marker_path(UNINSTALL_FLAG))


def clear_uninstall_request():
    try:
        os.unlink(marker_path(UNINSTALL_FLAG))
        return True
    except Exception:
        return False


def mark_uninstall_done():
    """写好还原凭据 —— 卸载脚本只认这个文件，不看 persistent。"""
    try:
        with open(marker_path(UNINSTALL_DONE), "w") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
        return True
    except Exception:
        return False


def official_fallback_pid():
    """官方默认节点 id —— 与前端 header.rpy:237 的默认值一致。"""
    return 1 if os.name == "nt" else 2


def set_node_marker(ours, provider_id=None, session_len=None):
    """写/删 .mm_node_ours，并刷新 .mm_state.json。返回 True = 状态有变化。"""
    p = marker_path()
    changed = bool(os.path.isfile(p)) != bool(ours)
    try:
        if ours:
            if not os.path.isfile(p):
                with open(p, "w") as f:
                    f.write("1")
        elif os.path.isfile(p):
            os.unlink(p)
    except Exception:
        pass
    try:
        with open(marker_path(STATE_JSON), "w") as f:
            json.dump({"node_ours": bool(ours),
                       "provider_id": provider_id,
                       "session_len_limit": session_len,
                       "updated": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    except Exception:
        pass
    return changed


def extractor_root():
    """提取器资产目录：环境变量优先，否则=本文件旁边的 extractor/。"""
    env = os.environ.get("MAICA_MOOD_EXTRACTOR_DIR")
    if env and os.path.isdir(env):
        return env
    return os.path.join(_HERE, "extractor")


def logs_dir():
    """插件日志目录 = <插件>\\logs\\（★ 不再写游戏根，卸载=删一个文件夹就干净）。

    2026-10-17 改：原先写 `renpy.config.basedir\\mood-plugin.log`，
    实测（MAS-clean）会在游戏根留下 48 KB 残留，卸载脚本删不掉。
    """
    d = os.path.join(_HERE, "logs")
    try:
        if not os.path.isdir(d):
            os.makedirs(d)
    except Exception:
        pass
    return d


def log_path(name=u"mood-plugin.log"):
    return os.path.join(logs_dir(), name)


def extractor_model_path():
    return os.path.join(extractor_root(), EXTRACTOR_MODEL)


def extractor_binaries():
    """按优先级返回候选 exe：GPU(Vulkan) → CPU。"""
    root = extractor_root()
    out = []
    for sub in ("bin-vulkan", "bin-cpu"):
        exe = os.path.join(root, sub, "llama-server.exe")
        if os.path.isfile(exe):
            out.append(exe)
    return out


def _url_open(url, timeout):
    if sys.version_info[0] >= 3:
        import urllib.request as _u
    else:
        import urllib2 as _u
    return _u.urlopen(url, timeout=timeout)


def extractor_alive(port=None, timeout=0.6):
    """探测提取器是否真的可用（GET /health 且返回 ok）。"""
    port = port or load_ports()["extract"]
    try:
        r = _url_open("http://127.0.0.1:%d/health" % port, timeout)
        try:
            body = r.read()
        finally:
            try:
                r.close()
            except Exception:
                pass
        if not isinstance(body, type(b"")):
            body = u"%s" % (body,)
            return "ok" in body
        return b"ok" in body
    except Exception:
        return False


def _port_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) != 0
    except Exception:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass


def _wait_gone(proc, port=None, timeout=2.0):
    """terminate() 之后等进程真的退出、端口真的空出来。

    ★ 2026-10-21：Windows 的 terminate 是 TerminateProcess（硬杀，不同步），
      返回时进程/端口往往还没释放。玩家"切走官方→马上切回来"时会撞上
      start_backend 的 port-busy，然后要等 15 秒重试。这里最多等 timeout 秒。
    只在服务线程里调用（后台），不要在游戏主线程里等。
    """
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        alive = False
        try:
            alive = proc is not None and proc.poll() is None
        except Exception:
            alive = False
        free = (port is None) or _port_free(port)
        if (not alive) and free:
            return True
        time.sleep(0.1)
    return False


def _spawn(exe, port, log_path=None):
    """起一个 llama-server；返回 Popen 或 None。"""
    import subprocess
    root = extractor_root()
    model = extractor_model_path()
    if not (os.path.isfile(exe) and os.path.isfile(model)):
        return None
    args = [exe, "-m", model,
            "--host", "127.0.0.1", "--port", str(port),
            "-c", "2048", "-np", "1", "--alias", EXTRACTOR_ALIAS]
    if "vulkan" in os.path.basename(os.path.dirname(exe)).lower():
        args += ["-ngl", "99"]
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = CREATE_NO_WINDOW
    out = None
    try:
        if log_path:
            out = open(log_path, "ab")
        else:
            out = open(os.devnull, "wb")
        devnull = open(os.devnull, "rb")
        return subprocess.Popen(args, cwd=os.path.dirname(exe),
                                stdout=out, stderr=out, stdin=devnull, **kw)
    except Exception:
        try:
            if out:
                out.close()
        except Exception:
            pass
        return None


def extractor_bundle_present():
    """本包是否带本地抽取器（模型 + 至少一个可执行目录）。

    ★ 2026-10-24：lite 包不含 extractor\，此时抽取改走服务商（见 MAICA_Mood.rpy 的
      _mm_sync_api_settings）。完整的 1.6 GB 包行为不变。
    """
    try:
        if not os.path.isfile(extractor_model_path()):
            return False
        return bool(extractor_binaries())
    except Exception:
        return False


def start_extractor(port=None, wait_s=20.0, log_path=None, state=None):
    """启动提取器（已在跑就直接返回 True）。返回 (是否可用, 说明)。

    ★ state 非空时会把 Popen 存进 state["extractor_proc"]，否则 stop_extractor 永远关不掉
      （2026-10-17 修：原先只管启动不管记账，切节点/退出时进程会残留）。
    """
    port = port or load_ports()["extract"]
    if extractor_alive(port):
        return True, "already-running"
    exes = extractor_binaries()
    if not exes:
        return False, "no-binary"
    if not os.path.isfile(extractor_model_path()):
        return False, "no-model"
    if not _port_free(port):
        return False, "port-busy:%d" % port
    for exe in exes:
        p = _spawn(exe, port, log_path)
        if p is None:
            continue
        deadline = time.time() + wait_s
        while time.time() < deadline:
            time.sleep(0.5)
            if extractor_alive(port):
                if state is not None:
                    state["extractor_proc"] = p
                return True, os.path.basename(os.path.dirname(exe))
            if p.poll() is not None:
                break
        try:
            p.terminate()
        except Exception:
            pass
    return False, "spawn-failed"


def stop_extractor(state=None, port=None, wait=False):
    """停掉本插件起的提取器进程（只杀自己起的那个）。"""
    proc = (state or {}).get("extractor_proc")
    if proc is None:
        return False
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
    if wait:
        try:
            _wait_gone(proc, port or load_ports()["extract"])
        except Exception:
            pass
    try:
        (state or {}).pop("extractor_proc", None)
    except Exception:
        pass
    return True


def extractor_supervise(want, state, log_path=None, port=None):
    """每 tick 调一次的状态机：我们的节点 → 确保在跑；切走 → 关掉（省 1.5GB 内存）。

    state 是插件传进来的可变 dict，本函数会写入：
        extractor_proc / extractor_tried_at / extractor_last_msg
    """
    state = state if state is not None else {}
    port = port or load_ports()["extract"]
    if want:
        if extractor_alive(port):
            if state.get("extractor_proc") is None:
                state["extractor_last_msg"] = "external"
            return "running"
        last = state.get("extractor_tried_at") or 0.0
        if time.time() - last < 15.0:      # 失败后别每 tick 都重试
            return state.get("extractor_last_msg") or "waiting"
        state["extractor_tried_at"] = time.time()
        ok, msg = start_extractor(port=port, log_path=log_path, state=state)
        state["extractor_last_msg"] = msg
        if not ok:
            state["extractor_proc"] = None
        return msg
    else:
        if state.get("extractor_proc") is not None:
            stop_extractor(state)
            state["extractor_last_msg"] = "stopped"
            return "stopped"
        return "idle"


# ============================================================================
#  嵌入服务（向量召回）—— 资产在 <插件>\embedding\，用 <插件>\runtime 的 Python 起
# ============================================================================

EMBED_SCRIPT = os.path.join("embedding", "embed_server.py")


def runtime_python():
    """插件自带的 Python（没有就退回 subprocess 的默认解释器）。"""
    exe = os.path.join(_HERE, "runtime", "Scripts", "python.exe")
    return exe if os.path.isfile(exe) else None


def ensure_runtime():
    """★ 让自带的 Python 只依赖插件自己 —— 自愈 pyvenv.cfg。

    背景（2026-10-17 实测踩到）：venv 的 pyvenv.cfg 里 `home`/`executable` 是**绝对路径**。
    插件被拷到新位置（玩家机器、或换个游戏目录）后，`runtime\\Scripts\\python.exe` 仍然
    去原位置找 python-runtime —— 源目录一删，后端、嵌入服务、提取器全部起不来。

    修法：每次插件加载时检查一遍，路径不对就地改写。这样"整个文件夹拷到哪都能跑"，
    不需要安装器参与，也不怕玩家自己搬家。

    返回 True=已是正确状态或修复成功；False=没有自带运行时。
    """
    rt = os.path.join(_HERE, "runtime")
    home = os.path.join(rt, "python-runtime")
    exe = os.path.join(home, "python.exe")
    cfg = os.path.join(rt, "pyvenv.cfg")
    if not (os.path.isdir(rt) and os.path.isfile(exe)):
        return False

    want = [
        "home = %s" % home,
        "include-system-site-packages = false",
    ]
    try:
        cur = {}
        if os.path.isfile(cfg):
            with open(cfg, "r") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.split("=", 1)
                        cur[k.strip().lower()] = v.strip()
        # 已指向自己就不动（避免每次启动都写盘）
        if (cur.get("home", "").lower() == home.lower()
                and cur.get("executable", "").lower() == exe.lower()):
            return True
        version = cur.get("version") or "3.12.10"
        lines = want + [
            "version = %s" % version,
            "executable = %s" % exe,
            "command = python -m venv %s" % rt,
        ]
        with open(cfg, "w") as f:
            f.write("\n".join(lines) + "\n")
        return True
    except Exception:
        return False


def _probe_health(port, timeout=0.6):
    """GET /health 且返回 ok（嵌入服务与提取器同款探活）。"""
    try:
        r = _url_open("http://127.0.0.1:%d/health" % port, timeout)
        try:
            body = r.read()
        finally:
            try:
                r.close()
            except Exception:
                pass
        return (b"ok" in body) if isinstance(body, type(b"")) else ("ok" in (u"%s" % (body,)))
    except Exception:
        return False


def embedding_alive(port=None):
    return _probe_health(port or load_ports()["embed"])


def start_embedding(port=None, wait_s=30.0, log_path=None, state=None):
    """启动嵌入服务。返回 (是否可用, 说明)。"""
    import subprocess
    port = port or load_ports()["embed"]
    if embedding_alive(port):
        return True, "already-running"
    py = runtime_python()
    script = os.path.join(_HERE, EMBED_SCRIPT)
    if py is None:
        return False, "no-runtime"
    if not os.path.isfile(script):
        return False, "no-script"
    if not _port_free(port):
        return False, "port-busy:%d" % port
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = CREATE_NO_WINDOW
    env = dict(os.environ)
    env["MAICA_EMBED_PORT"] = str(port)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    out = None
    try:
        out = open(log_path, "ab") if log_path else open(os.devnull, "wb")
        devnull = open(os.devnull, "rb")
        p = subprocess.Popen([py, script, "--port", str(port)],
                             cwd=os.path.join(_HERE, "embedding"),
                             stdout=out, stderr=out, stdin=devnull, env=env, **kw)
    except Exception:
        try:
            if out:
                out.close()
        except Exception:
            pass
        return False, "spawn-failed"
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(0.5)
        if embedding_alive(port):
            if state is not None:
                state["embedding_proc"] = p
            return True, "started"
        if p.poll() is not None:
            break
    try:
        p.terminate()
    except Exception:
        pass
    return False, "spawn-failed"


def stop_embedding(state=None, port=None, wait=False):
    proc = (state or {}).get("embedding_proc")
    if proc is None:
        return False
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
    if wait:
        try:
            _wait_gone(proc, port or load_ports()["embed"])
        except Exception:
            pass
    try:
        (state or {}).pop("embedding_proc", None)
    except Exception:
        pass
    return True


def embedding_supervise(want, state, log_path=None, port=None):
    state = state if state is not None else {}
    port = port or load_ports()["embed"]
    if want:
        if embedding_alive(port):
            return "running"
        last = state.get("embedding_tried_at") or 0.0
        if time.time() - last < 15.0:
            return state.get("embedding_last_msg") or "waiting"
        state["embedding_tried_at"] = time.time()
        ok, msg = start_embedding(port=port, log_path=log_path, state=state)
        state["embedding_last_msg"] = msg
        return msg
    else:
        if state.get("embedding_proc") is not None:
            stop_embedding(state)
            return "stopped"
        return "idle"


# ============================================================================
#  后端 —— 资产在 <插件>\backend\，用 <插件>\runtime 的 Python 起
#  ★ .env 不随包发：每次启动前按 ports.json + 本机路径【现生成】
# ============================================================================

BACKEND_SUBDIR = "backend"
BACKEND_ENV_TMPL = ".env.template"
BACKEND_ENV = ".env"


def backend_root():
    return os.path.join(_HERE, BACKEND_SUBDIR)


def backend_alive(port=None, timeout=0.8):
    """后端 HTTP 探活（官方先例：HTTP 端口活着就说明服务在）。"""
    port = port or load_ports()["http"]
    try:
        r = _url_open("http://127.0.0.1:%d/defaults" % port, timeout)
        try:
            r.read()
        finally:
            try:
                r.close()
            except Exception:
                pass
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 前端设置 → 后端 .env（2026-10-21，取代"安装器"）
#
# 玩家在前端「设置 → API Keys」里填的那几项，由官方 MAICA_ServerSubmod 在
# init -5 注册（maica_core_url / maica_core_key / maica_core_model …），存在
# %APPDATA%\RenPy\<存档>\api_keys.json。官方自己也是把它们 set_env 给后端的
# （MAICA_ServerSubmod\main.rpy:125-137），我们照抄同一套映射：
#     maica_core_url/key/model    → MAICA_MCORE_ADDR/KEY/CHOICE
#     maica_mfocus_url/key/model  → MAICA_MFOCUS_ADDR/KEY/CHOICE
#     maica_mvista_addr/key/choice→ MAICA_MVISTA_ADDR/KEY/CHOICE
#     maica_mnerve_*              → MAICA_MNERVE_*
#     maica_proxy_addr            → MAICA_PROXY_ADDR
#     maica_tp_apis               → MAICA_TP_APIS
#
# 为什么不做安装器：这份配置前端已经能填、官方节点也在用；插件在生成 .env 时
# 读一次就行 —— 玩家填一次，两个节点共用，换 key 也不用重装。
# ★ 所以 .env.template 里不许有任何真 key（那是要把 key 发给所有玩家）。
# ---------------------------------------------------------------------------
ENV_OVERRIDE_KEYS = (
    "MAICA_MCORE_ADDR", "MAICA_MCORE_KEY", "MAICA_MCORE_CHOICE",
    "MAICA_MFOCUS_ADDR", "MAICA_MFOCUS_KEY", "MAICA_MFOCUS_CHOICE",
    "MAICA_MVISTA_ADDR", "MAICA_MVISTA_KEY", "MAICA_MVISTA_CHOICE",
    "MAICA_MNERVE_ADDR", "MAICA_MNERVE_KEY", "MAICA_MNERVE_CHOICE",
    "MAICA_PROXY_ADDR", "MAICA_TP_APIS",
    # ★ 2026-10-24：lite 包（无 extractor\）要把抽取指向服务商 —— 这三个必须在白名单里，
    #   否则 set_env_overrides 会静默丢掉，.env 里仍是 127.0.0.1:8091（没有进程在听）。
    "MAICA_EXTRACT_ADDR", "MAICA_EXTRACT_KEY", "MAICA_EXTRACT_CHOICE",
)

_ENV_OVERRIDES = {}


def set_env_overrides(mapping):
    """主线程每次 tick 调用；空值不覆盖模板默认。返回真正生效的 dict。"""
    global _ENV_OVERRIDES
    clean = {}
    for k in ENV_OVERRIDE_KEYS:
        v = (mapping or {}).get(k)
        if v is None:
            continue
        v = str(v).strip()
        if v:
            clean[k] = v
    _ENV_OVERRIDES = clean
    return dict(clean)


def env_overrides():
    return dict(_ENV_OVERRIDES)


def _normalize_base_url(url):
    """`https://api.deepseek.com` → `https://api.deepseek.com/v1`（只补裸主机）。

    玩家在前端填的地址一般已经带 /v1（官方节点也这么用），所以只在
    "scheme://host" 后面什么都没有时才补，不猜其它路径。
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return u
    if "://" not in u:
        return u
    scheme, rest = u.split("://", 1)
    if "/" not in rest:
        return u + "/v1"
    return u


def _env_quote(v):
    try:
        return json.dumps(v)
    except Exception:
        return '"' + str(v).replace('"', '\\"') + '"'


def _set_env_value(text, key, value):
    """把 .env 文本里 key 的值换成 value。

    模板里的值可能跨行、带转义引号（MAICA_TP_APIS / MAICA_MCORE_EXTRA 就是），
    所以按"引号个数配平"吃掉续行，而不是只换第一行。
    """
    lines = text.split("\n")
    out = []
    i = 0
    pat = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    quote_re = re.compile(r'(?<!\\)"')
    while i < len(lines):
        line = lines[i]
        m = pat.match(line)
        if m:
            prefix = line[:m.end()]
            nq = len(quote_re.findall(line))
            while nq % 2 == 1 and i + 1 < len(lines):
                i += 1
                nq += len(quote_re.findall(lines[i]))
            out.append(prefix + " " + _env_quote(value))
            i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
#  玩家可改的提示词（2026-10-22）
# ---------------------------------------------------------------------------
# 为什么要有它：提示词在后端 .env 里是 JSON 转义的一整行，玩家没法改；
# 而 backend\.env 是插件每次拉起后端前【现生成】的，直接改会被覆盖。
# 所以给三个纯文本入口（放插件目录，UTF-8，改完重启游戏生效）：
#     提示词.txt        整个替换聊天人设（高级：换一套完全不同的写法）
#     提示词-附加.txt   追加到聊天人设末尾（常用：只加几条自己的要求）
#     提示词-写信.txt   替换写信那份人设
# 都不存在 = 用随包默认；删掉文件即恢复默认。
_PROMPT_FULL_FILES = ("提示词.txt", "prompt.txt")
_PROMPT_EXTRA_FILES = ("提示词-附加.txt", "prompt-extra.txt")
_PROMPT_WRITE_FILES = ("提示词-写信.txt", "prompt-write.txt")

_PROMPT_KEY_RE = r'(?ms)^%s\s*=\s*"(?:[^"\\]|\\.)*"'


def _to_like(sample, s):
    """让 s 和 sample 同为 bytes(py2) / str(py3)，避免 py2 的隐式解码炸掉。"""
    if isinstance(sample, bytes) and not isinstance(s, bytes):
        return s.encode("utf-8")
    if (not isinstance(sample, bytes)) and isinstance(s, bytes):
        return s.decode("utf-8")
    return s


def _read_text_file(names):
    """按顺序取第一个存在的文件 → (内容, 路径)；都没有 → (None, None)。"""
    for n in names:
        p = os.path.join(_HERE, n)
        if os.path.isfile(p):
            try:
                import io
                with io.open(p, "r", encoding="utf-8-sig") as f:
                    s = f.read()
                s = s.strip()
                return (s or None), p
            except Exception:
                return None, p
    return None, None


def _pat_for(text, key):
    """py2 的 .env 文本是 bytes，py3 测试里是 str —— 各自的 pattern 要同类型。"""
    pat = _PROMPT_KEY_RE % key
    return pat.encode("ascii") if isinstance(text, bytes) else pat


def _prompt_env_get(text, key):
    m = re.search(_pat_for(text, key), text)
    if not m:
        return u""
    try:
        whole = m.group(0)
        sep = b"=" if isinstance(whole, bytes) else "="
        lit = whole.split(sep, 1)[1].strip()
        return json.loads(lit)
    except Exception:
        return u""


def _prompt_env_set(text, key, value):
    raw = json.dumps(value, ensure_ascii=False)
    if isinstance(raw, bytes):                 # py2 的 json 有时直接给 bytes
        raw = raw.decode("utf-8")
    repl_u = u"%s = %s" % (key, raw)
    repl = _to_like(text, repl_u)
    pat = _pat_for(text, key)
    if re.search(pat, text):
        return re.sub(pat, lambda m: repl, text, count=1)
    return text + _to_like(text, u"\n" + repl_u + u"\n")


# 提示词的真源就在插件包里：backend\prompts\*.txt（纯文本，改完部署即可生效）。
# 下面四个文件如果存在，会覆盖 .env 模板里的同名字段（模板只是"没有 txt 时的兜底"）。
# 顺序：模板 → backend\prompts\*.txt → 玩家自己的 提示词*.txt
PROMPT_DIR_FILES = (
    ("MAICA_PROMPT_ZC", "chat_zh.txt"),
    ("MAICA_PROMPT_EC", "chat_en.txt"),
    ("MAICA_PROMPT_ZW", "write_zh.txt"),
    ("MAICA_PROMPT_EW", "write_en.txt"),
)


def prompts_dir():
    return os.path.join(backend_root(), "prompts")


def read_prompt_file(name):
    """读 backend/prompts/<name>（UTF-8，保留首尾之间的空白，只去掉结尾换行）。"""
    p = os.path.join(prompts_dir(), name)
    if not os.path.isfile(p):
        return None
    try:
        import io
        with io.open(p, "r", encoding="utf-8-sig") as f:
            s = f.read()
    except Exception:
        return None
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s.strip("\n") or None


def apply_prompt_files(text):
    """把 backend/prompts/*.txt 叠到 .env 文本上（插件包内的提示词真源）。"""
    if not text:
        return text
    for key, name in PROMPT_DIR_FILES:
        s = read_prompt_file(name)
        if s:
            text = _prompt_env_set(text, key, s)
    return text

def apply_prompt_overrides(text):
    """把插件目录里的 提示词*.txt 叠加到生成好的 .env 文本上（没有文件就原样返回）。"""
    if not text:
        return text
    full, _p1 = _read_text_file(_PROMPT_FULL_FILES)
    extra, _p2 = _read_text_file(_PROMPT_EXTRA_FILES)
    if full:
        text = _prompt_env_set(text, "MAICA_PROMPT_ZC", full)
    elif extra:
        base = _prompt_env_get(text, "MAICA_PROMPT_ZC")
        text = _prompt_env_set(text, "MAICA_PROMPT_ZC",
                               (base + u"\n\n" + extra) if base else extra)
    wfull, _p3 = _read_text_file(_PROMPT_WRITE_FILES)
    if wfull:
        text = _prompt_env_set(text, "MAICA_PROMPT_ZW", wfull)
    return text

def render_backend_env(ports=None, data_dir=None):
    """按 .env.template 生成 .env 文本（替换 {PLUGIN}/{WS}/{HTTP}/{EMBED}/{EXTRACT}）。

    ★ 2026-10-21：再叠一层"前端设置"（见 set_env_overrides）。玩家在游戏里
      填的 key / 地址 / 模型名从 api_keys.json 读出来，覆盖模板里的默认值 ——
      这样模板不必、也不该带任何真 key。
    """
    ports = ports or load_ports()
    root = backend_root()
    tpl = os.path.join(root, BACKEND_ENV_TMPL)
    if not os.path.isfile(tpl):
        return None
    try:
        with open(tpl, "r") as f:
            text = f.read()
    except Exception:
        return None
    data = data_dir or os.path.join(root, "data").replace("\\", "/")
    repl = {
        "{PLUGIN}": _HERE.replace("\\", "/"),
        "{DATA}": data.replace("\\", "/"),
        "{WS}": str(ports["ws"]),
        "{HTTP}": str(ports["http"]),
        "{EMBED}": str(ports["embed"]),
        "{EXTRACT}": str(ports["extract"]),
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    for k, v in (env_overrides() or {}).items():
        if k.endswith("_ADDR"):
            v = _normalize_base_url(v)
        text = _set_env_value(text, k, v)
    text = apply_prompt_files(text)
    return apply_prompt_overrides(text)


def write_backend_env(ports=None):
    """把生成好的 .env 落到 backend\\.env。返回是否成功。"""
    text = render_backend_env(ports)
    if text is None:
        return False
    try:
        with open(os.path.join(backend_root(), BACKEND_ENV), "w") as f:
            f.write(text)
        return True
    except Exception:
        return False


def backend_has_key(ports=None):
    """生成的 .env 里有没有可用的 API Key。

    ★ 2026-10-22：后端缺 key 会在启动时直接抛 OpenAIError 退出，
      表现是前端"连不上服务器"、日志里只有一段 traceback。
      这里按 .env 的实际内容判断（模板 + 前端设置叠加之后），
      没有就干脆不起后端，让上层给玩家一句能看懂的话。
    """
    try:
        text = render_backend_env(ports)
        if not text:
            return False
        import re as _re
        m = _re.search(r'^\s*MAICA_MCORE_KEY\s*=\s*"(.*)"\s*$', text, _re.M)
        return bool(m and m.group(1).strip())
    except Exception:
        return True          # 判断失败时不要挡住启动


def start_backend(ports=None, wait_s=90.0, log_path=None, state=None):
    """生成 .env 后启动后端。返回 (是否可用, 说明)。

    ★ 2026-10-17 修两处：
      ① 必须先建 backend\\data\\ —— 发行包里不带 data，缺它 sqlite 会直接
         `unable to open database file`（实测踩到）；
      ② 首次启动要跑 8 个迁移，wait 默认放宽到 90 秒。
    """
    import subprocess
    ports = ports or load_ports()
    if backend_alive(ports["http"]):
        return True, "already-running"
    py = runtime_python()
    root = backend_root()
    if py is None:
        return False, "no-runtime"
    if not os.path.isdir(root):
        return False, "no-backend"
    if not write_backend_env(ports):
        return False, "no-env-template"
    # ★ 2026-10-22：没有 API Key 就别起后端 —— 后端会在 import 阶段
    #   openai.OpenAIError: Missing credentials 直接退出，前端只会看到
    #   "连不上服务器"。这里提前拦下，交给上层提示。
    if not backend_has_key(ports):
        return False, "no-api-key"
    # data\ 必须存在（DB / 向量库都落在这里；发行包不带它）
    try:
        d = os.path.join(root, "data")
        if not os.path.isdir(d):
            os.makedirs(d)
    except Exception:
        return False, "no-data-dir"
    if not _port_free(ports["ws"]) or not _port_free(ports["http"]):
        return False, "port-busy"
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = CREATE_NO_WINDOW
    env = dict(os.environ)
    env["PYTHONPATH"] = root
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    out = None
    try:
        out = open(log_path, "ab") if log_path else open(os.devnull, "wb")
        devnull = open(os.devnull, "rb")
        p = subprocess.Popen([py, "-X", "utf8", "-m", "maica.maica_starter", "-e", ".env"],
                             cwd=root, stdout=out, stderr=out, stdin=devnull, env=env, **kw)
    except Exception:
        try:
            if out:
                out.close()
        except Exception:
            pass
        return False, "spawn-failed"
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(1.0)
        if backend_alive(ports["http"]):
            if state is not None:
                state["backend_proc"] = p
            return True, "started"
        if p.poll() is not None:
            break
    return False, "spawn-timeout"


def stop_backend(state=None, ports=None, wait=False):
    proc = (state or {}).get("backend_proc")
    if proc is None:
        return False
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
    if wait:
        try:
            _wait_gone(proc, (ports or load_ports())["http"])
        except Exception:
            pass
    try:
        (state or {}).pop("backend_proc", None)
    except Exception:
        pass
    return True


def backend_supervise(want, state, log_path=None, ports=None):
    """每 tick：我们的节点 → 保证 嵌入服务 + 后端 都在跑；切走 → 关掉。

    顺序很重要：嵌入服务先起（后端启动时会连它），再起后端。
    """
    state = state if state is not None else {}
    ports = ports or load_ports()
    if not want:
        msg = []
        if state.get("backend_proc") is not None:
            stop_backend(state)
            msg.append("backend-stopped")
        if state.get("embedding_proc") is not None:
            stop_embedding(state)
            msg.append("embed-stopped")
        return ",".join(msg) if msg else "idle"

    last = state.get("backend_tried_at") or 0.0
    if time.time() - last < 15.0 and not backend_alive(ports["http"]):
        return state.get("backend_last_msg") or "waiting"
    state["backend_tried_at"] = time.time()

    if not embedding_alive(ports["embed"]):
        state["embedding_last_msg"] = start_embedding(
            ports["embed"], log_path=log_path, state=state)[1]
    ok, msg = start_backend(ports, log_path=log_path, state=state)
    state["backend_last_msg"] = msg
    return msg if ok else (msg + "／embed:" + str(state.get("embedding_last_msg")))


# ============================================================================
#  问卷网页服务（2026-10-22）
#
#  Ren'Py 6.99 的 py2 既没有 sqlite3 也没有 HTTP 服务，游戏内输入框又不能输入中文。
#  所以问卷和记忆展示都交给这个 py3 小服务，游戏里只负责"起服务 + 打开浏览器"：
#      插件点按钮 → start_webui() → webbrowser 打开 http://127.0.0.1:<port>/?t=<token>
#  它自己会在空闲 10 分钟或游戏进程消失时退出（父进程检查），退出游戏时插件也会主动停。
# ============================================================================
WEBUI_SCRIPT = "webui_server.py"
WEBUI_BASE_PORT = 8099
WEBUI_QUESTIONS = "questionnaire_questions.txt"


def webui_alive(state=None):
    proc = (state or {}).get("proc")
    try:
        return proc is not None and proc.poll() is None
    except Exception:
        return False


def webui_stop(state=None):
    """停掉网页服务（退出游戏时调用）。"""
    proc = (state or {}).get("proc")
    if proc is None:
        return False
    try:
        if proc.poll() is None:
            proc.terminate()
            deadline = time.time() + 3.0
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
    except Exception:
        pass
    for k in ("proc", "port", "token"):
        try:
            (state or {}).pop(k, None)
        except Exception:
            pass
    return True


def _webui_pick_port(start=WEBUI_BASE_PORT, avoid=None):
    avoid = set(int(x) for x in (avoid or ()))
    for p in range(int(start), int(start) + 40):
        if p in avoid:
            continue
        if _port_free(p):
            return p
    return None


def start_webui(ports=None, log_path=None, last_json=None, state=None, parent_pid=None):
    """起问卷网页服务；已在跑就直接复用。

    返回 (ok, msg, info)，info = {"port": int, "token": str}
    """
    state = state if state is not None else {}
    if webui_alive(state):
        return True, "already-running", {"port": state.get("port"), "token": state.get("token")}
    py = runtime_python()
    script = os.path.join(_HERE, WEBUI_SCRIPT)
    if py is None:
        return False, "no-runtime", {}
    if not os.path.isfile(script):
        return False, "no-script", {}
    ports = ports or load_ports()
    port = _webui_pick_port(avoid=ports.values())
    if port is None:
        return False, "no-free-port", {}
    token = binascii.hexlify(os.urandom(8)).decode("ascii")
    data_dir = os.path.join(backend_root(), "data")
    try:
        if not os.path.isdir(data_dir):
            os.makedirs(data_dir)
    except Exception:
        pass
    cmd = [py, "-X", "utf8", script,
           "--port", str(port),
           "--token", token,
           "--questions", os.path.join(_HERE, WEBUI_QUESTIONS),
           "--db", os.path.join(data_dir, "questionnaire.db"),
           "--facts-db", os.path.join(data_dir, "maica.db"),
           "--backend-http", str(int(ports.get("http") or 0)),
           "--last-json", last_json or "",
           "--log", log_path or "",
           "--parent-pid", str(int(parent_pid if parent_pid is not None else os.getpid())),
           "--idle-exit", "600"]
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = CREATE_NO_WINDOW
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    devnull = None
    try:
        devnull = open(os.devnull, "wb")
        devnull_in = open(os.devnull, "rb")
        proc = subprocess.Popen(cmd, cwd=_HERE, stdout=devnull, stderr=devnull,
                                stdin=devnull_in, env=env, **kw)
    except Exception:
        try:
            if devnull:
                devnull.close()
        except Exception:
            pass
        return False, "spawn-failed", {}
    state["proc"] = proc
    state["port"] = port
    state["token"] = token
    # 等服务真的开始 listen（给它 5 秒）
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if proc.poll() is not None:
            state.pop("proc", None)
            return False, "exited", {}
        if not _port_free(port):
            return True, "started", {"port": port, "token": token}
        time.sleep(0.1)
    return True, "started(not-yet-listening)", {"port": port, "token": token}

def stop_all(state=None, ports=None, wait=False):
    """退出/切节点：把三个进程都关掉（只关本插件起的）。

    wait=True 时每个进程都等到真的退出、端口释放（只给后台服务线程用；
    退出游戏的主线程路径不要等，避免关游戏卡几秒）。
    """
    done = []
    ports = ports or load_ports()
    for fn, key in ((stop_backend, "backend_proc"),
                    (stop_embedding, "embedding_proc"),
                    (stop_extractor, "extractor_proc"),
                    (stop_watchdog, "watchdog_proc")):
        try:
            if (state or {}).get(key) is None:
                continue
            if key == "backend_proc":
                ok = fn(state, ports=ports, wait=wait)
            elif key == "embedding_proc":
                ok = fn(state, port=ports["embed"], wait=wait)
            elif key == "extractor_proc":
                ok = fn(state, port=ports["extract"], wait=wait)
            else:
                ok = fn(state)
            if ok:
                done.append(key)
        except Exception:
            pass
    return done


# ============================================================================
#  孤儿服务看门狗（2026-10-21，待办 #9）
#
#  Windows 上父进程被强杀时子进程不会跟着死：三个服务会一直占着端口和内存
#  （提取器 ~1.7 GB）。正常退出有 _quit 钩子；强杀/崩溃没有。插件被删掉后，
#  下次启动也没人清理 —— 只能重启电脑。
#
#  做法：插件拉起服务时顺便起一个 python watchdog.py，它唯一的活是
#  "等游戏进程死 → 按端口清掉 exe 在插件目录下的监听者 → 自己退出"。
#  它自己本来就是个孤儿，所以父进程死后它反而有活干；插件目录被删也不影响
#  （脚本已经在内存里）。
#  ★ 正常退出时 stop_all 会主动把它停掉，不留常驻进程。
# ============================================================================
WATCHDOG_SCRIPT = "watchdog.py"


def watchdog_alive(state=None):
    proc = (state or {}).get("watchdog_proc")
    try:
        return proc is not None and proc.poll() is None
    except Exception:
        return False


def start_watchdog(parent_pid=None, ports=None, log_path=None, state=None):
    """拉起看门狗。返回 (是否在跑, 说明)。"""
    if watchdog_alive(state):
        return True, "already-running"
    py = runtime_python()
    script = os.path.join(_HERE, WATCHDOG_SCRIPT)
    if py is None:
        return False, "no-runtime"
    if not os.path.isfile(script):
        return False, "no-script"
    ports = ports or load_ports()
    pid = int(parent_pid if parent_pid is not None else os.getpid())
    port_arg = ",".join(str(ports[k]) for k in ("ws", "http", "embed", "extract"))
    cmd = [py, "-X", "utf8", script,
           "--parent-pid", str(pid),
           "--ports", port_arg,
           "--root", _HERE,
           "--interval", "3"]
    if log_path:
        cmd += ["--log", log_path]
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = CREATE_NO_WINDOW
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    out = None
    try:
        out = open(os.devnull, "wb")
        devnull = open(os.devnull, "rb")
        p = subprocess.Popen(cmd, cwd=_HERE, stdout=out, stderr=out,
                             stdin=devnull, env=env, **kw)
    except Exception:
        try:
            if out:
                out.close()
        except Exception:
            pass
        return False, "spawn-failed"
    if state is not None:
        state["watchdog_proc"] = p
    return True, "started"


def stop_watchdog(state=None):
    """停掉看门狗（正常退出/切节点时调用）。"""
    proc = (state or {}).get("watchdog_proc")
    if proc is None:
        return False
    try:
        if proc.poll() is None:
            proc.terminate()
            # py2.7 的 Popen.wait 没有 timeout，自己等
            deadline = time.time() + 3.0
            while time.time() < deadline and proc.poll() is None:
                time.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
    except Exception:
        pass
    try:
        (state or {}).pop("watchdog_proc", None)
    except Exception:
        pass
    return True


# ============================================================================
#  残留进程清理（按端口 + exe 路径，只杀我们插件目录下的）
#
#  背景（2026-10-18 实测）：正常退出游戏后三个服务仍然在跑（父进程已消失 = 孤儿）。
#  原因是 quit 钩子里 stop_all 传错了 dict、且先停线程再停子进程。
#  这里提供"按端口兜底"，两处用：
#    ① quit 钩子里，作为 stop_all 之后的第二道保险
#    ② 插件启动时，清掉上一次崩溃/强杀留下的孤儿
# ============================================================================

def _pid_listening(port):
    """返回监听该端口的 PID；取不到返回 None。"""
    try:
        if os.name == "nt":
            out = subprocess.check_output(["netstat", "-ano", "-p", "TCP"],
                                          stderr=subprocess.STDOUT)
            if not isinstance(out, str):
                out = out.decode("mbcs", "replace")
            needle = ":%d" % int(port)
            for line in out.splitlines():
                if "LISTENING" not in line:
                    continue
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(needle):
                    pid = parts[-1]
                    if pid.isdigit() and int(pid) > 4:
                        return int(pid)
        else:
            out = subprocess.check_output(["lsof", "-ti", "tcp:%d" % int(port)])
            txt = out.decode("utf-8", "replace").strip().splitlines()
            if txt and txt[0].isdigit():
                return int(txt[0])
    except Exception:
        pass
    return None


def _exe_of(pid):
    """返回该 PID 的可执行文件路径；取不到返回空串。

    Windows 先试 wmic；★ 2026-10-21：Win11 24H2 起默认不装 wmic，
    失败时再用 PowerShell 问一次 —— 否则 _port_owned_by_us 会把自家残留
    服务误判成"别人的程序"。
    """
    if os.name != "nt":
        try:
            return os.path.realpath("/proc/%d/exe" % int(pid))
        except Exception:
            return ""
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where", "ProcessId=%d" % int(pid),
             "get", "ExecutablePath", "/value"],
            stderr=subprocess.STDOUT)
        if not isinstance(out, str):
            out = out.decode("mbcs", "replace")
        for line in out.splitlines():
            line = line.strip()
            if line.lower().startswith("executablepath="):
                path = line.split("=", 1)[1].strip()
                if path:
                    return path
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process -Id %d -ErrorAction Stop).Path" % int(pid)],
            stderr=DEVNULL)
        if not isinstance(out, str):
            out = out.decode("mbcs", "replace")
        return out.strip()
    except Exception:
        return ""


def kill_own_on_ports(ports=None):
    """扫 ports.json 里的四个端口：占用者如果在我们插件目录下 → 杀掉。

    返回被杀掉的 [(port, pid), ...]。只动我们自己的 exe，不碰官方后端。
    """
    ports = ports or load_ports()
    root = _HERE.lower()
    killed = []
    for key in ("ws", "http", "embed", "extract"):
        port = ports.get(key)
        if not port:
            continue
        pid = _pid_listening(port)
        if not pid:
            continue
        exe = _exe_of(pid)
        if not exe:
            continue
        if exe.lower().startswith(root):
            try:
                subprocess.call(["taskkill", "/PID", str(pid), "/F"],
                                stdout=DEVNULL, stderr=DEVNULL)
                killed.append((port, pid))
            except Exception:
                pass
    return killed


# ============================================================================
#  后台服务线程 —— ★ 关键：绝不能在 Ren'Py 主线程里等启动
#
#  为什么要有这个：`config.periodic_callbacks` 里的函数跑在【游戏主线程】。
#  启动后端最长要等 60 秒（首次还要建表），在主线程里 time.sleep 等 = 游戏卡死。
#  所以主线程只负责"设目标状态 + 读状态"，真正的探活/拉起/重试全在这个 daemon 线程里。
#
#  状态字段（主线程只读）：
#      want          主线程写：当前节点是不是我们的
#      stop          主线程写：退出时置 True
#      embed / backend / extract   各服务最近一次结果（running / started / spawn-failed / idle …）
#      backend_ready 后端 HTTP 是否活着（主线程据此触发一次前端重连）
#      text          一行摘要，用于日志去重
# ============================================================================

def start_service_thread(ports=None, log_paths=None, interval=1.5, retry_every=15.0,
                         extract_idle_stop=None):
    """启动后台守护线程，返回状态 dict。主线程通过修改 d["want"] / d["stop"] 控制。

    ★ 2026-10-18：提取器改为【按聊天生命周期启停】——
        进入聊天（st["chat_active"]=True）→ 启动
        退出聊天                        → 停止
        聊天中连续 extract_idle_stop 秒没对话 → 停止（默认 120 秒）
      嵌入服务与后端仍然只跟 st["want"]（选中我们的节点）走。
      这样不聊天时内存从 ~2.9 GB 降到 ~1.2 GB。
    """
    import threading

    ports = ports or load_ports()
    log_paths = log_paths or {}
    if extract_idle_stop is None:
        extract_idle_stop = EXTRACT_IDLE_STOP
    ensure_runtime()          # ★ 起服务前先确保自带 Python 指向自己（拷到新位置也能跑）
    st = {
        "want": False, "stop": False, "thread": None,
        "chat_active": False, "last_chat_at": 0.0,
        "embed": "idle", "backend": "idle", "extract": "idle",
        "backend_ready": False, "text": "idle", "procs": {},
    }
    tries = {"embed": 0.0, "backend": 0.0, "extract": 0.0}
    busy_hits = {"embed": 0, "backend": 0, "extract": 0}

    def _after_fail(name, msg):
        """端口冲突是"等一会儿就好"的失败：2 秒后再试，别干等 15 秒。

        ★ 2026-10-21：切走官方再马上切回来时，旧进程可能还没释放端口；
          以前要等 retry_every（15 秒）才重试，表现为"切回来半天连不上"。
          最多快速重试 5 次，之后回落到正常间隔（避免真有别的程序占着时空转）。
        """
        try:
            text = str(msg or "")
        except Exception:
            return
        if text.startswith("port-busy"):
            busy_hits[name] = busy_hits.get(name, 0) + 1
            if busy_hits[name] <= 5:
                tries[name] = time.time() - retry_every + 2.0
            return
        busy_hits[name] = 0

    def loop():
        # ★ 2026-10-22：所有"启动时的一次性重活"都挪到这个后台线程里做。
        #   它们原来是主线程第一个 tick 里跑的（netstat / wmic / powershell 子进程，
        #   冷启动时能卡好几秒），正好撞上 Ren'Py 开局的性能自检
        #   （5 帧 / 0.333 秒），于是玩家每次启动都看到"性能影响警告"。
        #   实测日志：Frame drawn at 0.069s → 4.023s，中间 4 秒就是这些子进程。
        boot_done = False
        while not st["stop"]:
            try:
                now = time.time()
                if not boot_done:
                    boot_done = True
                    try:
                        killed = kill_own_on_ports(ports)
                        if killed:
                            st["stale_killed"] = list(killed)
                    except Exception:
                        pass
                    try:
                        moved_ports, moved = ensure_ports()
                        if moved:
                            ports.clear()
                            ports.update(moved_ports)
                            st["ports_moved"] = list(moved)
                    except Exception:
                        pass
                if st["want"]:
                    # ⓪ 看门狗：游戏被强杀时替我们收尸（待办 #9）。
                    #   必须在服务起来之前挂上，否则"刚起完服务就被强杀"这一段没人管。
                    try:
                        if not watchdog_alive(st["procs"]):
                            ok_wd, msg_wd = start_watchdog(
                                parent_pid=os.getpid(), ports=ports,
                                log_path=log_paths.get("watchdog"),
                                state=st["procs"])
                            st["watchdog"] = msg_wd if ok_wd else ("fail:" + msg_wd)
                    except Exception:
                        st["watchdog"] = "except"
                    # ① 嵌入服务
                    if embedding_alive(ports["embed"]):
                        st["embed"] = "running"
                    elif now - tries["embed"] >= retry_every:
                        tries["embed"] = now
                        st["embed"] = start_embedding(
                            ports["embed"], log_path=log_paths.get("embed"),
                            state=st["procs"])[1]
                        _after_fail("embed", st["embed"])
                    # ② 后端（先确保嵌入在跑）
                    if not backend_alive(ports["http"]):
                        if st["embed"] == "running" and now - tries["backend"] >= retry_every:
                            tries["backend"] = now
                            st["backend"] = start_backend(
                                ports, log_path=log_paths.get("backend"),
                                state=st["procs"])[1]
                            _after_fail("backend", st["backend"])
                    else:
                        st["backend"] = "running"
                    st["backend_ready"] = backend_alive(ports["http"])
                    # ③ 提取器：按聊天生命周期启停
                    # ★ 2026-10-24：lite 包不带 extractor\ → 不启动本地提取器，
                    #   抽取走服务商（.env 的 MAICA_EXTRACT_* 由 MAICA_Mood.rpy 指过去）
                    _has_local_ex = extractor_bundle_present()
                    _last = float(st.get("last_chat_at") or 0.0)
                    _idle = (now - _last) if _last else 1e9
                    if not _has_local_ex:
                        if (st["procs"] or {}).get("extractor_proc") is not None:
                            stop_extractor(st["procs"])
                        st["extract"] = "remote(no-local-model)"
                    else:
                        _want_ex = bool(st.get("chat_active")) and _idle <= float(extract_idle_stop)
                        if not _want_ex:
                            if (st["procs"] or {}).get("extractor_proc") is not None or \
                                    extractor_alive(ports["extract"]):
                                stop_extractor(st["procs"])
                                st["extract"] = "stopped(no-chat)" if not st.get("chat_active") \
                                    else "stopped(idle)"
                            else:
                                st["extract"] = "idle(no-chat)" if not st.get("chat_active") \
                                    else "idle(idle)"
                        elif extractor_alive(ports["extract"]):
                            st["extract"] = "running"
                        elif now - tries["extract"] >= retry_every:
                            tries["extract"] = now
                            st["extract"] = start_extractor(
                                port=ports["extract"], log_path=log_paths.get("extract"),
                                state=st["procs"])[1]
                            _after_fail("extract", st["extract"])
                else:
                    if st["procs"]:
                        # wait=True：等进程真的退出、端口释放，玩家马上切回来也不撞 port-busy
                        stop_all(st["procs"], ports=ports, wait=True)
                        st["procs"] = {}
                    st["embed"] = st["backend"] = st["extract"] = "idle"
                    st["backend_ready"] = False
                st["text"] = "embed:%s backend:%s extract:%s" % (
                    st["embed"], st["backend"], st["extract"])
            except Exception:
                st["text"] = "loop-error"
            time.sleep(interval)

    t = threading.Thread(target=loop)
    t.daemon = True
    t.start()
    st["thread"] = t
    return st
