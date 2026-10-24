
import asyncio
import os
import orjson

from typing import *

from maica.maica_utils import *


# ---------------------------------------------------------------------------
# 知识入库（knowledge）
#
# ★ 2026-10-18 拆出来的：原来这段只在 GenericModelHelper._ainit() 里跑，而那个类只在
#   MAICA_MCORE_GENERIC=1 时才创建 -> 默认配置下 knowledge 从来入库（实测库里 0 行）。
#   知识入库和"核心模型提示词模式"没有任何关系，不该被那个开关绑架。
# ---------------------------------------------------------------------------

async def sync_knowledge(csc, force: bool = False) -> dict:
    """把代码里硬编码的莫妮卡资料写进 persist_items（→ 向量库 type='knowledge'）。

    · 幂等：sync_items 是差集同步，重复调用不会堆行
    · ★ 不做"库里有就跳过"的短路：SQLite 有行、向量库被删空的情况必须能自愈，
      所以每次都走一遍 sync_items（空跑只有一次 select + 一次向量表扫描）
    · csc 需要 is_vector_ready；不满足时返回 {} 并记一条 debug
    """
    try:
        if not getattr(csc, "is_vector_ready", False):
            sync_messenger(info="[knowledge] vector_db not ready, skip sync", type=MsgType.DEBUG)
            return {}
    except Exception:
        return {}

    from maica.maica_utils.session_early import static_knowledge
    from maica.maica_utils.persist_items import sync_items, count_items

    if not force:
        try:
            existing = await count_items(-1, 0, "knowledge")
        except Exception:
            existing = 0
    else:
        existing = -1

    _lang = 'zh'
    try:
        _lang = csc.maica_settings.basic.target_lang or 'zh'
    except Exception:
        pass

    try:
        ds_set = static_knowledge(target_lang=_lang)
    except Exception as exc:
        sync_messenger(info=f"[knowledge] Failed to load static knowledge: {exc}", type=MsgType.WARN)
        return {}

    if not ds_set:
        sync_messenger(info="[knowledge] No knowledge content, skipping", type=MsgType.DEBUG)
        return {}

    try:
        _res = await sync_items(csc, -1, 0, "knowledge", ds_set)
        sync_messenger(info=f"[knowledge] synced (sqlite had {existing}): {_res}", type=MsgType.DEBUG)
        return _res or {}
    except Exception as exc:
        sync_messenger(info=f"[knowledge] sync failed: {exc}", type=MsgType.WARN)
        return {}


async def ensure_knowledge(csc) -> dict:
    """兜底入口：库里有就跳过，没有就补一次（会话开始时调用，覆盖启动时序问题）。"""
    return await sync_knowledge(csc, force=False)


class GenericModelHelper(AsyncCreator):
    """Main toolbox."""

    def __init__(self, csc: ConnSocketsContainer):
        """This is not per-user, so we use csc."""
        self.csc = csc
        if not csc.is_vector_ready:
            raise MaicaDbWarning("vector_db is not usable")

    async def _ainit(self):
        # This class is only instantialized once in the entire lc, so no need to further optimize
        # What about we just use user = -1 to store dataset vectors

        def _json_to_friendly(obj: list | dict):
            if isinstance(obj, list):
                msg_obj = obj

            elif isinstance(obj, dict):
                for possible_key in ('messages', 'conversations'):
                    if (
                        possible_key in obj
                        and isinstance(obj[possible_key], list)
                    ):
                        msg_obj = obj[possible_key]
                        break
                else:
                    raise MaicaDbWarning(f"No matching messages in object: {str(obj)}")
                
            else:
                raise MaicaDbWarning(f"Object is not list or dict: {str(obj)}")
            
            if not msg_obj:
                raise MaicaDbWarning("Processed object is empty")
            
            def translate_kv(d: dict):
                # Compat ms-swift format
                if "from" in d:
                    d["role"] = d.pop("from")
                if "value" in d:
                    d["content"] = d.pop("value")

            for i in msg_obj:
                translate_kv(i)

            # Now we have standard conversation objs, form readable conversations
            tlist = []
            for i in msg_obj:
                if i["role"] in ('user', 'assistant'):
                    tlist.append(f"{i["role"]}: {i["content"]}")
            
            text = "; ".join(tlist)
            return text
        
        def json_to_friendly(*args, **kwargs):
            try:
                return _json_to_friendly(*args, **kwargs)
            except Exception as e:
                sync_messenger(info=f"Error processing line: {str(e)}, skipping...")

        try:
            base_path = get_inner_path('mtools/zsco')
            ds_set = set()
            with os.scandir(base_path) as ds_file_entries:
                for entry in ds_file_entries:
                    if entry.is_file() and entry.name.endswith('.jsonl'):
                        with open(entry.path, 'r', encoding='utf-8') as file:
                            ds_set.update(
                                json_to_friendly(orjson.loads(line))
                                for line in file
                                if line.strip()
                            )
        
            sync_messenger(info=f"[maica-zsco] Loaded dataset lines for generic: {len(ds_set)}", type=MsgType.DEBUG)

        except Exception as e:
            sync_messenger(info=f"[maica-zsco] Datasets may not exist: {str(e)}, ignoring and continuing", type=MsgType.DEBUG)

        # 数据来源：代码里【硬编码的莫妮卡资料】（静态、全局一份），已抽成模块级函数。
        # 这里保留调用只是保持 generic 路径行为不变；真正的启动入口在 maica_ws。
        try:
            await sync_knowledge(self.csc)
        except Exception as exc:
            sync_messenger(info=f"[maica-zsco] knowledge sync skipped: {exc}", type=MsgType.WARN)

        if not ds_set:
            sync_messenger(info="[maica-zsco] No dataset content, skipping", type=MsgType.DEBUG)
            return

        from maica.maica_utils.persist_items import sync_items
        _res = await sync_items(self.csc, -1, 0, "knowledge", ds_set)
        sync_messenger(info=f"[maica-zsco] dataset synced: {_res}", type=MsgType.DEBUG)

    async def search(self, query: str):
        vector_pool = self.csc.vector_pool

        res_set = await vector_pool.search(
            embedding_conn=self.csc.embedding_conn,
            data=[query],
            filters={
                "user_id": -1,
                "chat_session_num": 0,
                "type": "knowledge",
            },
            similarity_min=0,
        )

        return res_set
