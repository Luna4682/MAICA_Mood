
import asyncio
import datetime

from typing import *


from sqlalchemy import (
    UniqueConstraint,
    JSON,
    Boolean,
    DateTime,
    String,
    Text,
    inspect,
)
from sqlalchemy.sql import (
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
)
from sqlalchemy.ext.mutable import (
    MutableDict,
    MutableList,
)

class OrmToDictMixin():
    def model_to_dict(self):
        return {
            c.key: getattr(self, c.key)
            for c in inspect(self).mapper.column_attrs
        }

class SqlBaseAuth(DeclarativeBase, OrmToDictMixin):
    pass

class SqlBaseData(DeclarativeBase, OrmToDictMixin):
    pass

class SqlUser(SqlBaseAuth):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    nickname: Mapped[Optional[str]] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(150), unique=True)
    is_email_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    password: Mapped[str] = mapped_column(String(100))
    suspended_until: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime)

class SqlAccountStatus(SqlBaseData):
    __tablename__ = "account_status"

    id: Mapped[int] = mapped_column("user_id", primary_key=True, autoincrement=False)
    status: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON),
        default=dict,
    )
    preferences: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON),
        default=dict,
    )

class SqlChatSession(SqlBaseData):
    __tablename__ = "chat_session"
    __table_args__ = (
        UniqueConstraint("user_id", "chat_session_num", name="uq_chat_session_user_session"),
    )

    id: Mapped[int] = mapped_column("chat_session_id", primary_key=True)
    user_id: Mapped[int]
    chat_session_num: Mapped[int]
    content: Mapped[Optional[str]] = mapped_column(Text)

class SqlCropArchived(SqlBaseData):
    __tablename__ = "crop_archived"

    id: Mapped[int] = mapped_column("archive_id", primary_key=True)
    chat_session_id: Mapped[int]
    content: Mapped[Optional[str]] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

class SqlCsessionArchived(SqlBaseData):
    __tablename__ = "csession_archived"

    id: Mapped[int] = mapped_column("archive_id", primary_key=True)
    chat_session_id: Mapped[int]
    content: Mapped[Optional[str]] = mapped_column(Text)

class SqlMsCache(SqlBaseData):
    __tablename__ = "ms_cache"
    __table_args__ = (
        UniqueConstraint("hash", name="uq_hash"),
    )

    id: Mapped[int] = mapped_column("spire_id", primary_key=True)
    user_id: Mapped[int]
    hash: Mapped[str] = mapped_column(String(255))
    prompt: Mapped[Optional[str]] = mapped_column(Text)
    content: Mapped[Optional[str]] = mapped_column(Text)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
    )

class SqlMvMeta(SqlBaseData):
    __tablename__ = "mv_meta"

    id: Mapped[int] = mapped_column("vista_id", primary_key=True)
    user_id: Mapped[int]
    uuid: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
    )

class SqlPersistent(SqlBaseData):
    __tablename__ = "persistents"
    __table_args__ = (
        UniqueConstraint("user_id", "chat_session_num", name="uq_persistents_user_session"),
    )

    id: Mapped[int] = mapped_column("persistent_id", primary_key=True)
    user_id: Mapped[int]
    chat_session_num: Mapped[int]
    content: Mapped[Optional[str]] = mapped_column(Text)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
    )

class SqlPersistFact(SqlBaseData):
    """结构化事实库（P3 骨架）。权威数据在 SQLite；向量库只按 fact_text 建索引。

    ★ 本表为骨架：现有条目由规则解析迁入（占位结构化），小模型上线后应整表重建。
    """
    __tablename__ = "persist_facts"

    id: Mapped[int] = mapped_column("fact_id", primary_key=True)
    user_id: Mapped[int]
    chat_session_num: Mapped[int]

    # 结构（由小模型给出，扁平；后端只做映射与拼装）
    subject: Mapped[str] = mapped_column(String(32), default="player")
    predicate: Mapped[str] = mapped_column(String(128), default="")
    object: Mapped[Optional[str]] = mapped_column(String(512))
    polarity: Mapped[int] = mapped_column(default=1)

    fact_text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32), default="other")

    # 准入与来源
    certainty: Mapped[str] = mapped_column(String(32), default="explicit")
    source: Mapped[str] = mapped_column(String(32), default="savefile_generated")
    confidence: Mapped[Optional[float]]
    enabled: Mapped[bool] = mapped_column(default=True)
    superseded_by: Mapped[Optional[int]]

    # 时间
    time_expression: Mapped[Optional[str]] = mapped_column(String(128))
    time_source: Mapped[str] = mapped_column(String(16), default="send_time")
    # ★ speak_time：这条事实【被认为是在什么时候说出来的】。
    #   由后端在"小模型转完 JSON → 后端做完最后处理 → 确认要落库"那一刻传入 now，
    #   不是 DB 写入时间，也【绝不 auto-update】。
    #   用途：召回后连同事实一起返回给模型，让模型知道每条记忆的新旧。
    #   与 updated_at 的区别：updated_at 会被任何内部写入（如回写 vector_id）顶掉，
    #   所以不能用它表达"玩家最后一次说这句话的时间"。
    speak_time: Mapped[Optional[datetime.datetime]]
    first_seen_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    expires_at: Mapped[Optional[datetime.datetime]]

    vector_id: Mapped[Optional[str]] = mapped_column(String(64))


class SqlPersistItem(SqlBaseData):
    """非玩家事实的条目库：knowledge / savefile_derived / savefile_field。

    ★ 为什么不塞进 persist_facts：那三类是【整句文本】，没有主谓宾，而事实表
      靠"主语+谓语+宾语"做冲突裁决，混进去会让裁决键变形。所以分两张表：
          persist_facts  只装 player_direct 的结构化事实
          persist_items  装另外三类的整句文本
    权威都在 SQLite；向量库只按 raw_text 建索引，并通过 ref_id 指回本表。
    """
    __tablename__ = "persist_items"

    id: Mapped[int] = mapped_column("item_id", primary_key=True)
    user_id: Mapped[int]
    chat_session_num: Mapped[int]

    # 来源标签：knowledge / savefile_derived / savefile_field
    source: Mapped[str] = mapped_column(String(32), default="knowledge")
    raw_text: Mapped[str] = mapped_column(Text)

    enabled: Mapped[bool] = mapped_column(default=True)
    superseded_by: Mapped[Optional[int]]

    first_seen_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    # 指回向量库那一行（双向连接的另一半）
    vector_id: Mapped[Optional[str]] = mapped_column(String(64))


class SqlSummaryLedger(SqlBaseData):
    """总结流水：只增不删，每次总结追加一行（向量库据此保住每一代总结）。"""
    __tablename__ = "summary_ledger"

    id: Mapped[int] = mapped_column("summary_id", primary_key=True)
    user_id: Mapped[int]
    chat_session_num: Mapped[int]
    content: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        server_default=func.now(),
    )

class SqlTrigger(SqlBaseData):
    __tablename__ = "triggers"
    __table_args__ = (
        UniqueConstraint("user_id", "chat_session_num", name="uq_triggers_user_session"),
    )

    id: Mapped[int] = mapped_column("trigger_id", primary_key=True)
    user_id: Mapped[int]
    chat_session_num: Mapped[int]
    content: Mapped[Optional[str]] = mapped_column(Text)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
    )
