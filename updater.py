# -*- coding: utf-8 -*-
u"""MAICA_Mood · 更新器（Python 3，只用标准库）

它做四件事，顺序不能变：

  ① 检查游戏有没有在跑 —— 游戏开着时 `.rpy`/`.py` 可能被占用，硬覆盖只会留下
     半个版本。在跑就退出，让玩家先关游戏（`--force` 可跳过，风险自负）。
  ② 停掉可能残留的本地服务（按端口，只杀 exe 在插件目录下的）—— 释放文件锁。
  ③ **备份记忆库**：`backend\\data\\maica.db`（和 questionnaire.db / forum_flarum_db.db）
     复制到 `backend\\data\\_backup\\<名字>-<时间>.db`，只保留最近 5 份。
  ④ 解压更新包 → 逐文件覆盖，但**永远不碰**这些：
        backend\\data\\      ← 玩家的记忆（事实库 / 问卷答案 / 向量库）
        backend\\.env        ← 本机路径现生成的
        logs\\               ← 日志
        ports.json           ← 玩家这边顺延过的端口
        .mm_*                ← 插件运行期标记（节点标记、状态、卸载握手）
        dev_*                ← 开发期开关
        *.pyc / __pycache__  ← 无用的编译产物
        *.rpyc               ← 不覆盖，直接删（Ren'Py 会重新编译，留着反而跑旧代码）
     最后把新的 `VERSION` 写上、写一份 `logs\\update.log` 和
     `.mm_update_result.json`（bat 和玩家都能看到结果）。

更新包格式：一个 zip，里面的路径相对插件根目录，例如::

    MAICA_Mood.rpy
    launcher.py
    VERSION
    backend/maica/maica_http.py
    ...

可选带 `manifest.json`（`{"version": "1.0.2"}`）；没有就用 zip 里的 VERSION。

用法::

    runtime\\Scripts\\python.exe updater.py --zip 更新包.zip [--root <插件目录>]
                                        [--dry-run] [--force] [--no-backup]

退出码：0 成功 / 1 参数错 / 2 游戏在跑 / 3 包不对 / 4 覆盖出错
"""

from __future__ import print_function

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

# ---------------------------------------------------------------------------
#  会被保护起来、永远不从更新包覆盖的路径（相对插件根，posix 写法、小写比较）
# ---------------------------------------------------------------------------
PROTECT_PREFIX = (
    "backend/data/",
    "logs/",
)
PROTECT_EXACT = (
    "backend/.env",
    "backend/maica/.initialized",   # 迁移标记：带了会让新库跳过建表
    "ports.json",
    "version",          # 只由本程序按新包内容改写，不直接拷
)
PROTECT_PATTERNS = (
    r"^\.mm_",          # .mm_state.json / .mm_node_ours / .mm_uninstall_*
    r"^dev_",           # dev_bypass_affection / dev_affection.txt
    r"^提示词",          # 玩家自己写的提示词（提示词.txt / 提示词-附加.txt / 提示词-写信.txt）
    r"(^|/)__pycache__/",
    r"\.pyc$",
    r"\.rpyc$",         # 不覆盖，改成删除
)

BACKUP_NAMES = ("maica.db", "questionnaire.db", "forum_flarum_db.db")
BACKUP_KEEP = 5

# 更新包必须带的东西（防止玩家拿错 zip）
REQUIRED_NEW = ("MAICA_Mood.rpy",)
REQUIRED_AFTER = ("MAICA_Mood.rpy", "launcher.py", "backend/maica/maica_http.py")


def log(msg, log_path=None):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        print(line)
    except Exception:
        pass
    if log_path:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def is_protected(rel):
    """rel 是 posix 风格、相对插件根的路径（小写已由调用方处理）。"""
    if not rel:
        return True
    if rel.endswith("/"):
        return True
    for p in PROTECT_PREFIX:
        if rel.startswith(p):
            return True
    if rel in PROTECT_EXACT:
        return True
    for pat in PROTECT_PATTERNS:
        if re.search(pat, rel):
            return True
    return False


# ---------------------------------------------------------------------------
#  环境检查
# ---------------------------------------------------------------------------
def game_running(root):
    """游戏进程还在跑吗（按 exe 路径判断，只看游戏根目录下的）。

    插件在 <游戏根>\\game\\Submods\\MAICA_Mood，所以游戏根 = root\\..\\..\\..
    """
    if os.name != "nt":
        return False
    try:
        base = os.path.abspath(os.path.join(root, "..", "..", ".."))
    except Exception:
        return False
    cmd = [
        "powershell", "-NoProfile", "-Command",
        "Get-Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Path -and "
        "([System.IO.Path]::GetDirectoryName($_.Path) -eq '%s') } | "
        "Select-Object -First 1 -ExpandProperty ProcessName" % base.replace("'", "''"),
    ]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=20)
        name = (out.stdout or b"").decode("mbcs", "replace").strip()
        return bool(name)
    except Exception:
        return False


def _run(cmd):
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=20)
        if not isinstance(out.stdout, str):
            return out.stdout.decode("mbcs", "replace")
        return out.stdout
    except Exception:
        return ""


def _pid_listening(port):
    out = _run(["netstat", "-ano", "-p", "TCP"])
    want = (":%d" % int(port)) + " "
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(":%d" % int(port)) and parts[-1].isdigit():
            return int(parts[-1])
    return 0


def _exe_of(pid):
    out = _run(["powershell", "-NoProfile", "-Command",
                "(Get-Process -Id %d -ErrorAction Stop).Path" % int(pid)])
    return out.strip()


def stop_services(root):
    """把 exe 在插件目录下的监听者杀掉（按 ports.json 的四个端口）。"""
    killed = []
    ports = {"ws": 5100, "http": 6100, "embed": 8090, "extract": 8091}
    try:
        with open(os.path.join(root, "ports.json"), "r", encoding="utf-8") as f:
            raw = json.load(f) or {}
        for k in ports:
            v = raw.get(k)
            if isinstance(v, int) and 1 <= v <= 65535:
                ports[k] = v
    except Exception:
        pass
    root_low = root.lower().replace("/", "\\")
    for port in ports.values():
        pid = _pid_listening(port)
        if not pid or pid <= 4:
            continue
        exe = _exe_of(pid)
        if not exe or not exe.lower().replace("/", "\\").startswith(root_low):
            continue
        if _run(["taskkill", "/PID", str(pid), "/F"]):
            killed.append((port, pid))
    return killed


def backup_data(root, old_version, log_path=None):
    """备份记忆库到 backend\\data\\_backup\\<名字>-<旧版本>-<时间>.db。"""
    data = os.path.join(root, "backend", "data")
    if not os.path.isdir(data):
        return []
    dst_dir = os.path.join(data, "_backup")
    try:
        os.makedirs(dst_dir, exist_ok=True)
    except Exception:
        return []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    done = []
    for name in BACKUP_NAMES:
        src = os.path.join(data, name)
        if not os.path.isfile(src):
            continue
        stem, ext = os.path.splitext(name)
        dst = os.path.join(dst_dir, "%s-%s-%s%s" % (stem, old_version or "old", stamp, ext))
        try:
            shutil.copy2(src, dst)
            done.append(dst)
        except Exception as e:
            log("backup failed for %s: %r" % (name, e), log_path)
    # 只留最近 N 份（每种名字各算）
    for name in BACKUP_NAMES:
        stem = os.path.splitext(name)[0]
        try:
            items = [f for f in os.listdir(dst_dir)
                     if f.startswith(stem + "-") and f.endswith(os.path.splitext(name)[1])]
            items.sort(reverse=True)
            for f in items[BACKUP_KEEP:]:
                os.remove(os.path.join(dst_dir, f))
        except Exception:
            pass
    return done


# ---------------------------------------------------------------------------
#  更新包
# ---------------------------------------------------------------------------
def read_zip_version(zf):
    for cand in ("manifest.json", "VERSION"):
        try:
            raw = zf.read(cand).decode("utf-8-sig").strip()
        except KeyError:
            continue
        except Exception:
            continue
        if not raw:
            continue
        if cand == "manifest.json":
            try:
                return str((json.loads(raw) or {}).get("version") or "").strip()
            except Exception:
                continue
        return raw.splitlines()[0].strip()
    return ""


def zip_entries(zf):
    """[(rel_posix_lower, rel_original, info)]，跳过目录。"""
    out = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        while name.startswith("./"):
            name = name[2:]
        if not name or name.startswith("/"):
            continue
        out.append((name.lower(), name, info))
    return out


def read_old_version(root):
    p = os.path.join(root, "VERSION")
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            v = (f.read() or "").strip().splitlines()
        return v[0].strip() if v else ""
    except Exception:
        return ""


def apply_zip(root, zip_path, new_version, dry_run=False, log_path=None):
    """把 zip 里的文件覆盖到 root，返回 (copied, skipped, failed)。"""
    copied = skipped = failed = 0
    with zipfile.ZipFile(zip_path) as zf:
        entries = zip_entries(zf)
        if not any(rel_low.endswith("maica_mood.rpy") for rel_low, _, _ in entries):
            raise ValueError("更新包里没有 MAICA_Mood.rpy —— 这不像插件更新包")
        for rel_low, rel, info in entries:
            if is_protected(rel_low):
                skipped += 1
                continue
            dst = os.path.join(root, *rel.split("/"))
            if dry_run:
                log("would copy %s (%d B)" % (rel, info.file_size), log_path)
                copied += 1
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                tmp = dst + ".mm_new"
                with zf.open(info) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.replace(tmp, dst)
                copied += 1
            except Exception as e:
                failed += 1
                log("copy failed: %s -> %r" % (rel, e), log_path)
                try:
                    if os.path.exists(dst + ".mm_new"):
                        os.remove(dst + ".mm_new")
                except Exception:
                    pass
        try:
            manifest = zf.read("manifest.json")
            if not dry_run:
                with open(os.path.join(root, "manifest.json"), "wb") as f:
                    f.write(manifest)
        except Exception:
            pass
    if new_version and not dry_run:
        try:
            with open(os.path.join(root, "VERSION"), "w", encoding="utf-8") as f:
                f.write(new_version + "\n")
        except Exception as e:
            log("write VERSION failed: %r" % (e,), log_path)
    return copied, skipped, failed


def clean_compiled(root, dry_run=False, log_path=None):
    """删掉旧的 .rpyc / .pyc / __pycache__（留着会让游戏跑旧代码）。

    ★ 只清插件自己的代码，跳过 runtime/、extractor/、embedding/ 这些自带资产：
      它们的 __pycache__ 是 Python 运行时自己生成的，删了只是让下次启动慢一点，
      却要多走几十万个文件（实测 224 个目录，全是 runtime\\python-runtime\\Lib 下的）。
    """
    n = 0
    skip_top = ("runtime", "extractor", "embedding")
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root).replace("\\", "/").lower()
        if rel == ".":
            rel = ""
        low = rel
        if "/backend/data" in low or low.startswith("backend/data"):
            dirnames[:] = []
            continue
        if rel.split("/")[0] in skip_top:
            dirnames[:] = []
            continue
        if "__pycache__" in dirnames:
            target = os.path.join(dirpath, "__pycache__")
            if dry_run:
                log("would remove %s" % target, log_path)
            else:
                shutil.rmtree(target, ignore_errors=True)
            dirnames.remove("__pycache__")
            n += 1
        for fn in filenames:
            if fn.endswith((".rpyc", ".pyc")):
                p = os.path.join(dirpath, fn)
                if dry_run:
                    log("would remove %s" % p, log_path)
                else:
                    try:
                        os.remove(p)
                    except Exception:
                        pass
                n += 1
    return n


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="MAICA_Mood updater")
    ap.add_argument("--zip", required=True, help="更新包（本地路径或 http(s) 地址）")
    ap.add_argument("--root", default="", help="插件目录（默认本文件所在目录）")
    ap.add_argument("--dry-run", action="store_true", help="只看会做什么，不改文件")
    ap.add_argument("--force", action="store_true", help="游戏还在跑也继续（危险）")
    ap.add_argument("--no-backup", action="store_true", help="跳过记忆库备份")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root or os.path.dirname(os.path.abspath(__file__)))
    logs = os.path.join(root, "logs")
    try:
        os.makedirs(logs, exist_ok=True)
    except Exception:
        pass
    log_path = os.path.join(logs, "update.log")
    result_path = os.path.join(root, ".mm_update_result.json")
    result = {"ok": False, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
              "root": root, "from": read_old_version(root), "to": "", "backup": [],
              "copied": 0, "skipped": 0, "failed": 0, "cleaned": 0, "error": ""}

    def done(code, err=""):
        result["error"] = err
        try:
            with open(result_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return code

    log("=" * 60, log_path)
    log("updater start: root=%s dry_run=%s force=%s" % (root, args.dry_run, args.force), log_path)

    if not os.path.isfile(os.path.join(root, "MAICA_Mood.rpy")):
        log("[error] 这里不像插件目录（没有 MAICA_Mood.rpy）: %s" % root, log_path)
        return done(1, "not-a-plugin-dir")

    # 更新包：本地路径或 http(s)
    zip_path = args.zip
    tmp_zip = None
    if zip_path.lower().startswith(("http://", "https://")):
        try:
            import urllib.request
            fd, tmp_zip = tempfile.mkstemp(suffix=".zip", prefix="mm_update_")
            os.close(fd)
            log("downloading %s ..." % zip_path, log_path)
            urllib.request.urlretrieve(zip_path, tmp_zip)
            zip_path = tmp_zip
        except Exception as e:
            log("[error] download failed: %r" % (e,), log_path)
            return done(3, "download-failed: %r" % (e,))
    if not os.path.isfile(zip_path):
        log("[error] 更新包不存在: %s" % zip_path, log_path)
        return done(3, "zip-not-found")

    try:
        with zipfile.ZipFile(zip_path) as zf:
            new_version = read_zip_version(zf)
    except Exception as e:
        log("[error] 打不开更新包: %r" % (e,), log_path)
        return done(3, "bad-zip: %r" % (e,))
    result["to"] = new_version
    log("version: %s -> %s" % (result["from"] or "?", new_version or "?"), log_path)

    if not args.dry_run:
        if game_running(root) and not args.force:
            log("[error] 游戏还在运行，请先退出游戏（或加 --force）", log_path)
            return done(2, "game-running")
        killed = stop_services(root)
        if killed:
            log("stopped leftover services: %r" % (killed,), log_path)

        if not args.no_backup:
            result["backup"] = backup_data(root, result["from"], log_path)
            for b in result["backup"]:
                log("backup -> %s" % b, log_path)

    try:
        copied, skipped, failed = apply_zip(root, zip_path, new_version,
                                            dry_run=args.dry_run, log_path=log_path)
    except ValueError as e:                 # 包内容不对（不是插件包）
        log("[error] 更新包不对: %r" % (e,), log_path)
        return done(3, "bad-package: %r" % (e,))
    except Exception as e:
        log("[error] 覆盖失败: %r" % (e,), log_path)
        return done(4, "apply-failed: %r" % (e,))
    result["copied"], result["skipped"], result["failed"] = copied, skipped, failed
    result["cleaned"] = clean_compiled(root, dry_run=args.dry_run, log_path=log_path)

    missing = [p for p in REQUIRED_AFTER
               if not os.path.isfile(os.path.join(root, *p.split("/")))]
    if missing and not args.dry_run:
        log("[error] 覆盖后缺少关键文件: %r" % (missing,), log_path)
        return done(4, "missing-after: %r" % (missing,))

    result["ok"] = failed == 0
    if tmp_zip:
        try:
            os.remove(tmp_zip)
        except Exception:
            pass
    log("done: copied=%d skipped=%d failed=%d cleaned=%d%s"
        % (copied, skipped, failed, result["cleaned"],
           " (dry-run)" if args.dry_run else ""), log_path)
    return done(0 if result["ok"] else 4, "" if result["ok"] else "some-files-failed")


if __name__ == "__main__":
    sys.exit(main())
