"""Embedded LanceDB storage for derived MAICA retrieval vectors."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from pathlib import Path
from collections.abc import Iterable, Mapping
from typing import Any

import lancedb
import pyarrow as pa


_FILTER_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SCHEMA_FIELDS = {
    "id",
    "ref_id",
    "user_id",
    "chat_session_num",
    "type",
    "raw_text",
    "is_prod",
    "vector",
}
# ★ ref_id 指回 SQLite 里那一行（权威）。向量库只当索引，检索时据此回表取文本。
_FILTER_FIELDS = _SCHEMA_FIELDS - {"id", "raw_text", "vector"}
_INTEGER_FILTERS = {"user_id", "chat_session_num"}
_BOOLEAN_FILTERS = {"is_prod"}
_STRING_FILTERS = {"type", "ref_id"}


def _table_schema(dimensions: int) -> "pa.Schema":
    """向量库表结构。唯一一份定义，首次创建与重建都用它。"""
    return pa.schema([
        pa.field("id", pa.string(), nullable=False),
        pa.field("ref_id", pa.string()),
        pa.field("user_id", pa.int64()),
        pa.field("chat_session_num", pa.int64()),
        pa.field("type", pa.string()),
        pa.field("raw_text", pa.string(), nullable=False),
        pa.field("is_prod", pa.bool_()),
        pa.field("vector", pa.list_(pa.float32(), dimensions), nullable=False),
    ])


def _filter_expr(filters: Mapping[str, Any] | None) -> str | None:
    if filters is None:
        return None
    if not isinstance(filters, Mapping):
        raise TypeError("filters must be a mapping")
    if not filters:
        return None
    clauses = []
    for key, value in filters.items():
        if not isinstance(key, str) or not _FILTER_KEY.fullmatch(key):
            raise ValueError(f"invalid vector filter field: {key}")
        if key not in _FILTER_FIELDS:
            raise ValueError(f"invalid vector filter field: {key}")
        if key in _INTEGER_FILTERS and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"invalid vector filter value for {key}")
        if key in _BOOLEAN_FILTERS and not isinstance(value, bool):
            raise ValueError(f"invalid vector filter value for {key}")
        if key in _STRING_FILTERS and not isinstance(value, str):
            raise ValueError(f"invalid vector filter value for {key}")
        if isinstance(value, bool):
            literal = "true" if value else "false"
        elif isinstance(value, (int, float)):
            literal = str(value)
        elif isinstance(value, str):
            literal = "'" + value.replace("'", "''") + "'"
        else:
            raise ValueError(f"unsupported vector filter value for {key}")
        clauses.append(f"{key} = {literal}")
    return " AND ".join(clauses)


def _validate_texts(data: Iterable[str]) -> list[str]:
    if isinstance(data, (str, bytes)):
        raise TypeError("vector text data must be an iterable of strings, not a scalar")
    texts = list(data)
    if any(not isinstance(text, str) for text in texts):
        raise TypeError("raw_text values must be strings")
    return list(dict.fromkeys(texts))


class LanceVectorStore:
    table_name = "maica_rag"

    def __init__(self, db, table, dimensions: int):
        self.db = db
        self.table = table
        self.dimensions = dimensions
        self._write_lock = asyncio.Lock()

    @classmethod
    async def async_create(cls, path: str | Path, dimensions: int, table_name: str | None = None):
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0:
            raise ValueError("vector dimensions must be positive")
        name = table_name or cls.table_name

        def open_store():
            db = lancedb.connect(str(Path(path)))
            names = db.list_tables().tables or []
            if name in names:
                table = db.open_table(name)
                try:
                    vector_type = table.schema.field("vector").type
                except KeyError as exc:
                    raise ValueError("vector schema is missing the vector field") from exc
                actual = vector_type.list_size if pa.types.is_fixed_size_list(vector_type) else None
                if actual != dimensions:
                    raise ValueError(
                        f"vector dimension mismatch: stored={actual}, configured={dimensions}"
                    )
                if vector_type.value_type != pa.float32():
                    raise ValueError(
                        f"vector schema mismatch: stored={vector_type}, configured=float32[{dimensions}]"
                    )
                missing_fields = _SCHEMA_FIELDS - set(table.schema.names)
                if missing_fields:
                    # 向量库只是索引，随时可以从 SQLite（权威）重建，所以字段对不上
                    # 时直接重建，而不是报错把后端卡住。内容稍后由 SQLite 重新同步回来。
                    try:
                        from maica.maica_utils.maica_utils import sync_messenger, MsgType
                        sync_messenger(
                            info=f"[vector] schema missing {sorted(missing_fields)}; rebuilding index table",
                            type=MsgType.WARN,
                        )
                    except Exception:
                        pass
                    db.drop_table(name)
                    table = db.create_table(name, schema=_table_schema(dimensions))
            else:
                table = db.create_table(name, schema=_table_schema(dimensions))
            return db, table

        db, table = await asyncio.to_thread(open_store)
        return cls(db, table, dimensions)

    @staticmethod
    def _record_id(text: str, filters: Mapping[str, Any] | None) -> str:
        scope = repr(sorted((filters or {}).items())).encode("utf-8")
        return hashlib.sha256(scope + b"\0" + text.encode("utf-8")).hexdigest()

    async def _embed(self, embedding_conn, data: Iterable[str]):
        texts = _validate_texts(data)
        if not texts:
            return []
        response = await embedding_conn.make_embedding(input=texts)
        pairs = list(zip(texts, (item.embedding for item in response.data), strict=True))
        for text, vector in pairs:
            if len(vector) != self.dimensions:
                raise ValueError(f"embedding dimension mismatch for {text!r}: {len(vector)} != {self.dimensions}")
        return pairs

    def _ensure_open(self):
        if self.table is None or self.db is None:
            raise RuntimeError("LanceVectorStore is closed")

    async def sync_texts(self, embedding_conn, data: Iterable[str], unique: str = "raw_text", filters=None, ref_ids=None):
        """把这一批文本同步进指定范围（差集：本轮没出现的删掉）。

        ref_ids: 可选的 {文本: SQLite 行号}，写进向量行的 ref_id 列，
                 这样检索命中后能回 SQLite 取权威文本。
        返回: {文本: 向量行号}，供调用方把 vector_id 写回 SQLite，形成双向连接。
        """
        if unique != "raw_text":
            raise ValueError("LanceVectorStore only supports raw_text uniqueness")
        requested = set(_validate_texts(data))
        want_ref = dict(ref_ids or {})
        expression = _filter_expr(filters)
        self._ensure_open()
        async with self._write_lock:
            def read_old():
                query = self.table.search().select(["id", "raw_text", "ref_id"])
                if expression:
                    query = query.where(expression)
                return query.to_arrow().to_pylist()

            old = await asyncio.to_thread(read_old)
            old_by_text = {}
            stale = set()
            for row in old:
                text = row["raw_text"]
                old_by_text[text] = row["id"]
                if want_ref and row.get("ref_id") != want_ref.get(text):
                    # SQLite 那边换了行号（例如重建过表），这条索引的指针就过期了，
                    # 删掉重加一次，保证 ref_id 指得准。
                    stale.add(text)
            to_add = (requested - set(old_by_text)) | (stale & requested)
            to_delete = (set(old_by_text) - requested) | stale
            if to_delete:
                ids = [old_by_text[text] for text in to_delete if text in old_by_text]
                if ids:
                    await asyncio.to_thread(self.table.delete, "id IN (" + ",".join(f"'{item}'" for item in ids) + ")")
            embedded = await self._embed(embedding_conn, to_add)
            if embedded:
                rows = []
                for text, vector in embedded:
                    row = {
                        "id": self._record_id(text, filters),
                        "ref_id": want_ref.get(text),
                        "raw_text": text,
                        "vector": [float(item) for item in vector],
                        "user_id": (filters or {}).get("user_id"),
                        "chat_session_num": (filters or {}).get("chat_session_num", 0),
                        "type": (filters or {}).get("type", "persistent"),
                        "is_prod": (filters or {}).get("is_prod", True),
                    }
                    rows.append(row)
                await asyncio.to_thread(self.table.add, rows)
            try:
                import maica.maica_utils.debug_log as _dl
                _dl.dbg("VEC-SYNC", "scope=%s added=%d deleted=%d" % (expression, len(to_add), len(to_delete)))
            except Exception:
                pass
            result = {}
            for text in requested:
                result[text] = old_by_text.get(text) or self._record_id(text, filters)
            return result

    async def search(self, embedding_conn, data: Iterable[str], filters=None, topk: int = 5, similarity_min: float = 0.5):
        if isinstance(topk, bool) or not isinstance(topk, int) or topk <= 0:
            raise ValueError("topk must be a positive integer")
        if isinstance(similarity_min, bool) or not isinstance(similarity_min, (int, float)) or not math.isfinite(similarity_min):
            raise ValueError("similarity_min must be a finite number")
        self._ensure_open()
        texts = _validate_texts(data)
        expression = _filter_expr(filters)
        embedded = await self._embed(embedding_conn, texts)
        results: set[str] = set()
        for _, vector in embedded:
            def query():
                search = self.table.search(vector, vector_column_name="vector").distance_type("cosine").limit(topk)
                if expression:
                    search = search.where(expression)
                return search.to_list()

            rows = await asyncio.to_thread(query)
            results.update(row["raw_text"] for row in rows if 1.0 - float(row["_distance"]) >= similarity_min)
        try:
            import maica.maica_utils.debug_log as _dl
            _dl.dbg("VEC-SEARCH", "hits=%d filters=%s" % (len(results), expression), hits=sorted(results))
        except Exception:
            pass
        return results

    async def search_with_refs(self, embedding_conn, data: Iterable[str], filters=None, topk: int = 5, similarity_min: float = 0.5) -> list:
        """跟 search 一样，但返回带 ref_id 与相似度的明细，供"回 SQLite 取权威文本"用。

        返回 [{raw_text, ref_id, score, type}, ...]，按相似度从高到低。
        """
        if isinstance(topk, bool) or not isinstance(topk, int) or topk <= 0:
            raise ValueError("topk must be a positive integer")
        self._ensure_open()
        texts = _validate_texts(data)
        expression = _filter_expr(filters)
        embedded = await self._embed(embedding_conn, texts)
        seen = {}
        for _, vector in embedded:
            def query():
                search = self.table.search(vector, vector_column_name="vector").distance_type("cosine").limit(topk)
                if expression:
                    search = search.where(expression)
                return search.to_list()

            rows = await asyncio.to_thread(query)
            for row in rows:
                score = 1.0 - float(row["_distance"])
                if score < similarity_min:
                    continue
                text = row["raw_text"]
                if text not in seen or seen[text]["score"] < score:
                    seen[text] = {
                        "raw_text": text,
                        "ref_id": row.get("ref_id"),
                        "score": score,
                        "type": row.get("type"),
                    }
        out = sorted(seen.values(), key=lambda item: item["score"], reverse=True)
        try:
            import maica.maica_utils.debug_log as _dl
            _dl.dbg("VEC-SEARCH", "hits=%d filters=%s" % (len(out), expression),
                    hits=[item["raw_text"] for item in out])
        except Exception:
            pass
        return out

    async def close(self):
        db = self.db
        self.table = None
        self.db = None
        connection = getattr(db, "_conn", None) if db is not None else None
        close = getattr(connection, "close", None)
        if callable(close):
            await asyncio.to_thread(close)
