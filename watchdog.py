# -*- coding: utf-8 -*-
u"""MAICA_Mood · 孤儿服务看门狗（Python 3，只用标准库）

为什么需要它：三个本地服务（嵌入 / 后端 / 提取器）是游戏进程的子进程，Windows 上
父进程被强杀时子进程不会跟着死。游戏正常退出有 _quit 钩子兜底；但任务管理器强杀 /
崩溃不会走钩子，于是它们会一直占着端口和内存（提取器 ~1.7 GB）。插件被删掉后，
下一次启动也没人清理它们 —— 只能重启电脑。

这个进程就是补那一刀：
    看门狗 = python watchdog.py --parent-pid <游戏pid> --ports 5100,6100,8090,8091
                                  --root <插件目录> [--interval 3] [--log <日志>]
  · 每 interval 秒检查父进程（游戏）还在不在；
  · 父进程没了 → 按端口找到监听者，只杀 exe 路径在 <插件目录> 下的那个；
  · 杀完（或本来就没人监听）→ 自己退出。

为什么它自己不会被强杀带走：它就是"活着等父进程死"的孤儿，父进程死了它反而有活干。
插件目录被删也没关系 —— 脚本已经在内存里跑着了。

用法上它由插件在拉起服务时顺便启动，正常退出时插件会主动把它停掉（见 stop_watchdog）。
"""

import argparse
import ctypes
import os
import subprocess
import sys
import time

SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x102


def log(msg, path=None):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        print(line)
    except Exception:
        pass
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def parent_alive(pid):
    """父进程（游戏）还活着吗。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(SYNCHRONIZE, False, pid)
    if not h:
        return False          # 打不开 = 进程已不存在
    try:
        return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        k32.CloseHandle(h)


def _run(cmd):
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if not isinstance(out.stdout, str):
            return out.stdout.decode("mbcs", "replace")
        return out.stdout
    except Exception:
        return ""


def listening_pids(port):
    """监听该端口的所有 PID。"""
    pids = []
    out = _run(["netstat", "-ano", "-p", "TCP"])
    needle = ":%d" % int(port)
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        if not parts[1].endswith(needle):
            continue
        if parts[-1].isdigit() and int(parts[-1]) > 4:
            pids.append(int(parts[-1]))
    return pids


def exe_of(pid):
    """进程的可执行文件路径；取不到返回空串。wmic 在 Win11 24H2 起默认不在，用 PS 兜底。"""
    out = _run(["wmic", "process", "where", "ProcessId=%d" % int(pid),
                "get", "ExecutablePath", "/value"])
    for line in out.splitlines():
        line = line.strip()
        if line.lower().startswith("executablepath="):
            path = line.split("=", 1)[1].strip()
            if path:
                return path
    out = _run(["powershell", "-NoProfile", "-Command",
                "(Get-Process -Id %d -ErrorAction Stop).Path" % int(pid)])
    return out.strip()


def kill(pid):
    try:
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def cleanup(ports, root, log_path=None, parent_pid=None):
    """父进程没了：只杀"exe 在我们插件目录下"的监听者。"""
    root = (root or "").lower().rstrip("\\/")
    killed = []
    for port in ports:
        for pid in listening_pids(port):
            if parent_pid and pid == int(parent_pid):
                continue
            exe = exe_of(pid)
            if not exe:
                log("port %d: pid %d exe unknown -> skip" % (port, pid), log_path)
                continue
            if root and not exe.lower().replace("/", "\\").startswith(root.replace("/", "\\")):
                log("port %d: pid %d is %s (not ours) -> skip" % (port, pid, exe), log_path)
                continue
            if kill(pid):
                killed.append((port, pid))
                log("port %d: killed orphan pid %d (%s)" % (port, pid, exe), log_path)
    return killed


def main(argv=None):
    ap = argparse.ArgumentParser(description="MAICA_Mood orphan-service watchdog")
    ap.add_argument("--parent-pid", type=int, required=True,
                    help="游戏进程 pid（父进程）")
    ap.add_argument("--ports", default="",
                    help="逗号分隔的端口，父进程死后按端口清理")
    ap.add_argument("--root", default="",
                    help="插件目录；只杀 exe 路径在它下面的进程")
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--log", default="")
    ap.add_argument("--noop", action="store_true",
                    help="只观察不杀（调试用）")
    args = ap.parse_args(argv)

    ports = [int(x) for x in (args.ports or "").replace(" ", "").split(",") if x.isdigit()]
    log("started: parent=%d ports=%s root=%s interval=%.1f"
        % (args.parent_pid, ports, args.root, args.interval), args.log)
    if not ports:
        log("no ports given -> nothing to clean, exit", args.log)
        return 0

    while parent_alive(args.parent_pid):
        time.sleep(max(0.5, args.interval))

    log("parent %d is gone" % args.parent_pid, args.log)
    if args.noop:
        time.sleep(0.1)
        return 0
    killed = cleanup(ports, args.root, args.log, args.parent_pid)
    log("done, killed=%r" % (killed,), args.log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
