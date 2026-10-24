# -*- coding: utf-8 -*-
u"""本地三件服务的启动器（开发期手工用；正式玩法由插件自己启停）。

读 插件目录/ports.json（唯一真实来源），依次起：
    嵌入服务(:embed) → 后端(:ws/:http) → 提取器(:extract)
并按同一份 ports.json 现生成 backend\\.env。

用法：
    python launcher.py            # 起三件，打印状态，Ctrl+C 停
    python launcher.py --stop     # 只停（按端口杀，不按进程名）
    python launcher.py --status   # 只看状态
"""
from __future__ import print_function

import io
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import maica_mood_lib as L  # noqa: E402


def status(ports):
    return {
        "embed(%d)" % ports["embed"]: L.embedding_alive(ports["embed"]),
        "backend(%d/%d)" % (ports["ws"], ports["http"]): L.backend_alive(ports["http"]),
        "extract(%d)" % ports["extract"]: L.extractor_alive(ports["extract"]),
    }


def show(ports):
    for k, v in status(ports).items():
        print("   %-18s %s" % (k, "OK" if v else "-"))


def stop_by_ports(ports):
    """按端口精确杀（不按进程名：官方后端也可能叫 python.exe）。"""
    import subprocess
    killed = []
    for key in ("embed", "extract", "ws", "http"):
        p = ports[key]
        try:
            out = subprocess.check_output(
                ["netstat", "-ano"], stderr=subprocess.STDOUT)
            if not isinstance(out, str):
                out = out.decode("mbcs", "replace")
            for line in out.splitlines():
                if "LISTENING" in line and (":%d " % p) in line:
                    pid = line.split()[-1]
                    if pid.isdigit() and int(pid) > 4:
                        subprocess.call(["taskkill", "/PID", pid, "/F"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        killed.append("%s:%s" % (key, pid))
        except Exception:
            pass
    return killed


def main():
    ports = L.load_ports()
    print("ports.json -> ws=%(ws)d http=%(http)d embed=%(embed)d extract=%(extract)d" % ports)

    if "--status" in sys.argv:
        show(ports)
        return 0

    if "--stop" in sys.argv:
        k = stop_by_ports(ports)
        print("stopped: %s" % (", ".join(k) if k else "(nothing)"))
        show(ports)
        return 0

    logf = L.log_path
    state = {}
    print("starting embedding ...")
    print("   ", L.embedding_supervise(True, state, log_path=logf(u"mood-embed.log"), port=ports["embed"]))
    print("starting backend ...")
    print("   ", L.backend_supervise(True, state, log_path=logf(u"mood-backend.log"), ports=ports))
    print("starting extractor ...")
    print("   ", L.extractor_supervise(True, state, log_path=logf(u"mood-extractor.log"), port=ports["extract"]))
    print("")
    show(ports)
    print("")
    print("Ctrl+C to stop all three.")
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        print("")
    done = L.stop_all(state)
    print("stopped: %s" % (", ".join(done) if done else "(nothing)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
