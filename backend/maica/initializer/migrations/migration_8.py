"""
Add `speak_time` to persist_facts.

`speak_time` 记录【这条事实被认为是在什么时候说出来的】：
由后端在「小模型转完 JSON → 后端做完最后处理 → 确认要落库」那一刻传入 now。
它不是 DB 写入时间，也绝不 auto-update。

为什么不复用 updated_at：
  updated_at 带 onupdate=func.now()，任何 ORM 写入都会把它顶到当前时刻
  —— 包括回写 vector_id 这类纯内部操作。
  所以 updated_at 表达的是"这一行最后一次被代码碰过的时刻"，
  不能用来表达"玩家最后一次说这句话的时间"。
"""

import asyncio
from typing import *
from sqlalchemy import inspect, text
from maica.maica_utils import *
from .base import register_migration

target_version = "1.3.004.post4"

TABLE = "persist_facts"
COLUMN = "speak_time"


async def _column_exists(conn, table: str, column: str) -> bool:
    cols = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_columns(table))
    return any(c.get("name") == column for c in cols)


async def migrate():
    try:
        async with DatabaseUtils.engine_data.begin() as conn:
            if await _column_exists(conn, TABLE, COLUMN):
                sync_messenger(
                    info=f"[migration-8] {TABLE}.{COLUMN} already exists, skipping...",
                    type=MsgType.DEBUG,
                )
                return

            if conn.dialect.name == "mysql":
                ddl = f"ALTER TABLE `{TABLE}` ADD COLUMN `{COLUMN}` DATETIME NULL DEFAULT NULL"
            else:
                ddl = f"ALTER TABLE `{TABLE}` ADD COLUMN `{COLUMN}` DATETIME"

            await conn.execute(text(ddl))
            sync_messenger(
                info=f"[migration-8] Added {TABLE}.{COLUMN}",
                type=MsgType.PRIM_LOG,
            )
    except Exception as e:
        raise MaicaDbWarning(f"Couldn't add {TABLE}.{COLUMN}: {str(e)}, maybe manually done already?") from e


register_migration(target_version, migrate)

if __name__ == "__main__":
    from maica import init
    init()
    asyncio.run(migrate())
