# -*- coding: utf-8 -*-
"""MAICA 开发者模式 —— 全链路调试日志（P4）

设计约束（务必遵守）：
1. 只用标准库；绝不引入依赖
2. 绝不抛异常 —— 任何失败都静默降级，绝不影响业务
3. 密钥永不打印 —— 所有输出统一经过 redact()
4. 完整提示词是【独立开关】，默认关闭（含玩家私人内容）
5. 配置来自后端根目录的 config.json（缺失或损坏 → 用默认值）

日志形态（每行一条，便于 grep）：
    [2026-10-08T10:12:33.123] BOOT      env loaded ...
    [--] TURN-IN   stage2 recv (312 bytes) payload=...
    [--] SCHEMA    validate cls=PersSelectionResults len=204 raw=...
    [--] VEC-SEARCH hits=3 filters=user_id = 2 AND ... hits=[...]
    [--] VEC-SYNC  scope=... added=2 deleted=1
    [--] LLM       model=deepseek-flash stream=False text_len=... prompt=...
"""

from __future__ import annotations

import datetime
import json
import os
import re
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))          # 后端根目录（与 .env 同级）
_CONFIG_NAME = "config.json"

_DEFAULTS = {
    "enabled": False,          # 总开关
    "chain_log": True,         # 全链路事件
    "content_log": True,       # 玩家文本 / 模型输出片段
    "prompt_log": False,       # 完整提示词（默认关）
    "console": False,          # 是否同时打到控制台
    "log_file": "debug-chain.log",
    "max_bytes": 8 * 1024 * 1024,
    "preview_chars": 600,
}

_SECRETS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.=]{8,}"),
    re.compile(r"(?i)(api[_-]?key|apikey|token|secret|password|passwd)\s*[\"']?\s*[:=]\s*[\"']?[^\s\"',}\]]{4,}"),
    re.compile(r"[A-Za-z0-9_\-]{48,}"),
)

_lock = threading.Lock()
_cache = {"mtime": None, "cfg": None}


def root() -> str:
    """后端根目录（与 .env / config.json 同级）。"""
    return _ROOT


def config_path() -> str:
    return os.path.join(_ROOT, _CONFIG_NAME)


def _load() -> dict:
    """读取配置；失败一律回退默认值，绝不抛异常。"""
    try:
        path = config_path()
        mtime = os.path.getmtime(path)
        if _cache["mtime"] == mtime and isinstance(_cache["cfg"], dict):
            return _cache["cfg"]
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        cfg = dict(_DEFAULTS)
        section = raw.get("debug") if isinstance(raw, dict) else None
        if isinstance(section, dict):
            for key in list(_DEFAULTS):
                if key in section:
                    cfg[key] = section[key]
        _cache["mtime"] = mtime
        _cache["cfg"] = cfg
        return cfg
    except Exception:
        return dict(_DEFAULTS)


def effective() -> dict:
    return dict(_load())


def enabled() -> bool:
    try:
        return bool(_load().get("enabled"))
    except Exception:
        return False


def prompt_enabled() -> bool:
    try:
        return bool(_load().get("enabled")) and bool(_load().get("prompt_log"))
    except Exception:
        return False


def content_enabled() -> bool:
    try:
        return bool(_load().get("enabled")) and bool(_load().get("content_log"))
    except Exception:
        return False


def redact(value) -> str:
    """把密钥类内容统一抹掉。任何输入都不抛异常。"""
    try:
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, default=str)
        for pattern in _SECRETS:
            value = pattern.sub("<已隐藏>", value)
        return value
    except Exception:
        return "<redact-failed>"


def _preview(value, limit: int = None) -> str:
    try:
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", errors="replace")
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        limit = int(limit or _load().get("preview_chars") or 600)
        text = redact(text)
        if len(text) > limit:
            return text[:limit] + "…<共 %d 字>" % len(text)
        return text
    except Exception:
        return "<preview-failed>"


def _rotate(path: str, max_bytes: int) -> None:
    try:
        if max_bytes > 0 and os.path.exists(path) and os.path.getsize(path) > max_bytes:
            backup = path + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(path, backup)
    except Exception:
        pass


def _write(kind: str, message: str) -> None:
    """落盘；任何失败都静默忽略。"""
    try:
        cfg = _load()
        if not cfg.get("enabled") or not cfg.get("chain_log"):
            return
        stamp = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        # ★ 2026-10-18：kind 原来被截到 10 个字符，于是
        #     FACT-EXTRACT-RAW / FACT-EXTRACT-ERROR 都变成 "FACT-EXTRA"，
        #     FACT-DUPLICATE 变成 "FACT-DUPLIC" —— 正好把需要区分的事件糊成一条。
        #   现在不截断，列宽 16（短 kind 仍然对齐）。
        line = "[%s] %-16s %s" % (stamp, str(kind), redact(str(message)))
        path = os.path.join(_ROOT, str(cfg.get("log_file") or "debug-chain.log"))
        with _lock:
            _rotate(path, int(cfg.get("max_bytes") or 0))
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        if cfg.get("console"):
            try:
                print(line)
            except Exception:
                pass
    except Exception:
        pass


def dbg(kind: str, message: str, **fields) -> None:
    """记一条链路事件。fields 里以 _full 结尾的键只在提示词开关打开时输出。"""
    try:
        if not enabled():
            return
        parts = [str(message)]
        for key, value in fields.items():
            if key in ("raw", "payload", "prompt", "response", "hits"):
                if not content_enabled() and key != "hits":
                    continue
            if key == "prompt" and not prompt_enabled():
                parts.append("prompt=<完整提示词开关未开>")
                continue
            parts.append("%s=%s" % (key, _preview(value)))
        _write(kind, "  ".join(parts))
    except Exception:
        pass


def dbg_llm(model: str, kwargs: dict) -> None:
    """所有大模型调用的必经点。密钥永不进入日志（kwargs 里有 client 时不打印它）。"""
    try:
        if not enabled():
            return
        data = dict(kwargs or {})
        data.pop("client", None)
        data.pop("timeout", None)
        stream = bool(data.get("stream"))
        text_schema = data.get("text")
        payload = {}
        for key, value in data.items():
            if key in ("stream", "text"):
                continue
            payload[key] = value
        msg = "model=%s stream=%s" % (model, stream)
        if text_schema:
            try:
                msg += " schema=%s" % (str(text_schema)[:80])
            except Exception:
                pass
        if prompt_enabled():
            dbg("LLM", msg, prompt=payload)
        else:
            short = {}
            for key, value in payload.items():
                short[key] = _preview(value, 160)
            dbg("LLM", msg + "  (完整提示词开关未开，仅摘要)", **short)
    except Exception:
        pass


def dbg_response(model: str, response) -> None:
    try:
        if not enabled():
            return
        text = None
        for attr in ("output_text", "content", "text"):
            value = getattr(response, attr, None)
            if isinstance(value, str) and value:
                text = value
                break
        dbg("LLM-OUT", "model=%s type=%s" % (model, type(response).__name__),
            response=(text if text is not None else "<非文本响应/流式>"))
    except Exception:
        pass
