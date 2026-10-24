# -*- coding: utf-8 -*-
"""从模型输出里解析结构化事实 —— 【唯一解析来源】

约定（训练 / 推理 / 评测三处必须一致）：
    每个 JSON 对象**独占一行**；并列句拆成多条时输出**多行**；单条时只输出一行。
    同时兼容：整体 JSON 数组、```json 围栏、对象之间用空行隔开。

为什么要有这个文件：
    并列句必须能"一句话出多条"。如果三处各写一份解析，就会出现
    "训练用数组、评测只认单个对象"这种口径不一致 —— 所以这里做唯一实现，
    评测台（_tools/eval_extract.py）直接引用本文件的 parse_facts()。

被谁用：
    - 后端接入小模型时：把模型原始输出交给 parse_facts()，得到 list[dict]
    - 评测台：load_parser() 会按路径加载本文件
"""

from __future__ import annotations

import json
import re


def _scan_objects(text: str) -> list:
    """按括号深度逐个抠出 JSON 对象（字符串内的花括号不会误判）。"""
    objs, depth, start = [], 0, None
    in_str, esc = False, False
    for i, ch in enumerate(text or ""):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            objs.append(obj)
                    except Exception:
                        pass
                    start = None
    return objs


def parse_facts(text: str) -> list:
    """把模型输出解析成 list[dict]（每行一个对象；兼容数组与围栏）。"""
    try:
        t = (text or "").strip()
        if t.startswith("```"):
            t = t.strip("`").strip()
            if t[:4].lower() == "json":
                t = t[4:].lstrip()
        if t.startswith("["):
            try:
                arr = json.loads(t)
                if isinstance(arr, list):
                    return [x for x in arr if isinstance(x, dict)]
            except Exception:
                pass
        return _scan_objects(t)
    except Exception:
        return []


def normalize_predicate(p: str) -> str:
    """谓语体标记归一：养过/养了 → 养；答应过 → 答应；去过 → 去。

    用途：冲突裁决的键用它，fact_text 保留原样。
    ★ 不归一就会出问题："我去过日本" 与 "我去日本" 会变成两个不同的键，
      裁决（新的取代旧的）就失效了。
    """
    return re.sub(r"(过|了|着)$", "", (p or "").strip())


def normalize_object(o: str) -> str:
    """宾语归一（**只用于冲突键**，落库的 object 保留原样）。

    ★ 2026-10-17 补：体标记也可能落在宾语尾部 —— 实测
        「我喜欢猫」      → object_raw="猫"
        「我不喜欢猫了」  → object_raw="猫了"
      按原样做键会得到两个不同的键 → 极性翻转（这正是最需要裁决的情形）**不会取代**。
      去掉宾语尾部的 了/过/着 后两者同键，新的正确取代旧的。
    """
    s = (o or "").strip().lower()
    # ★ 2026-10-18 补：句尾语气词也可能被模型带进宾语（实测「我喜欢猫」→「猫呀」，
    #   于是同一条事实因为一个语气词变成两个键，同键去重/裁决全部失效）。
    #   这里只影响【键】，落库的 object 仍然是原样。
    s = re.sub(r"[呀啊呢吧哦嘛啦哈哟诶欸么嘛]+$", "", s)
    return re.sub(r"(过|了|着)$", "", s)

