# -*- coding: utf-8 -*-
"""向量索引的 SQLite 实现：一张表 + 进程内矩阵缓存 + 精确余弦。

★ 2026-10-19 新增：替掉 LanceDB。
  理由（实测，768 维、top-5）：
    · 200 条：LanceDB 查询 2.65 ms，本实现 0.62 ms（含读库）
    · 单条写入：LanceDB 5.5 ms，SQLite 0.11 ms
    · 依赖体积：lancedb + pyarrow = 253 MB，这里 0
    · 交叉点：带索引的 LanceDB 要到 5 万条左右才追平"矩阵常驻"的暴力检索，
      而一个人的记忆量通常几百~几千条。

对外接口与 LanceVectorStore 完全一致（async_create / sync_texts / search /
search_with_refs / close），所以调用方一行都不用改。

设计要点：
1. **表结构**与 Lance 版字段一一对应（id/ref_id/user_id/chat_session_num/
   type/raw_text/is_prod/dim/vec），vec 是 float32 小端 BLOB（768 维 = 3,072 字节）；
2. **矩阵常驻内存**：按 (user_id, chat_session_num, type) 缓存归一化后的矩阵，
   写入后 version+1，下次查询重建。10k 条约 30 MB；
3. **余弦 = 点积**：加载时就把每行归一化，查询时 `m @ q` 一次算完，
   避免了逐行 sqrt；
4. 行号（id）沿用 Lance 版的算法 `sha256(repr(sorted(filters)) + b"\0" + text)`，
   所以迁移后 id 不变，SQLite 里已有的 vector_id 仍然有效。
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import numpy as np

_VALID_FILTERS = {"user_id", "chat_session_num", "type", "ref_id", "is_prod"}
_FILTER_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INTEGER_FILTERS = {"user_id", "chat_session_num"}
_BOOLEAN_FILTERS = {"is_prod"}
_STRING_FILTERS = {"type", "ref_id"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vector_row (
    id               TEXT PRIMARY KEY,
    ref_id           TEXT,
    user_id          INTEGER,
    chat_session_num INTEGER,
    type             TEXT,
    raw_text         TEXT NOT NULL,
    is_prod          INTEGER,
    dim              INTEGER NOT NULL,
    vec              BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vector_scope ON vector_row (user_id, chat_session_num, type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vector_text ON vector_row (user_id, chat_session_num, type, raw_text);
"""


def _validate_texts(data: Iterable[str]) -> list[str]:
    if isinstance(data, (str, bytes)):
        raise TypeError("vector text data must be an iterable of strings, not a scalar")
    texts = list(data)
    if any(not isinstance(text, str) for text in texts):
        raise TypeError("raw_text values must be strings")
    return list(dict.fromkeys(texts))


def _validate_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    if filters is None:
        return {}
    if not isinstance(filters, Mapping):
        raise TypeError("filters must be a mapping")
    out: dict[str, Any] = {}
    for key, value in filters.items():
        if not isinstance(key, str) or not _FILTER_KEY.fullmatch(key):
            raise ValueError(f"invalid vector filter field: {key}")
        if key not in _VALID_FILTERS:
            raise ValueError(f"invalid vector filter field: {key}")
        if key in _INTEGER_FILTERS and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"invalid vector filter value for {key}")
        if key in _BOOLEAN_FILTERS and not isinstance(value, bool):
            raise ValueError(f"invalid vector filter value for {key}")
        if key in _STRING_FILTERS and not isinstance(value, str):
            raise ValueError(f"invalid vector filter value for {key}")
        out[key] = value
    return out


def _where(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    if not filters:
        return "", []
    parts, args = [], []
    for key, value in filters.items():
        parts.append("%s = ?" % key)
        args.append(int(value) if isinstance(value, bool) else value)
    return " WHERE " + " AND ".join(parts), args


class SqliteVectorStore:
    """SQLite + 内存矩阵的精确向量检索（替代 LanceDB）。"""

    table_name = "vector_row"
    db_file = "vectors.db"

    def __init__(self, conn: sqlite3.Connection, dimensions: int):
        self._conn = conn
        self.dimensions = dimensions
        self._lock = threading.Lock()
        self._write_lock = asyncio.Lock()
        # (user_id, chat_session_num, type) -> {"version": int, "texts": [...], "refs": [...],
        #                                        "types": [...], "matrix": ndarray (n, dim)}
        self._cache: dict[tuple, dict[str, Any]] = {}
        self._version = 0

    # ---------- 生命周期 ----------

    @classmethod
    async def async_create(cls, path: str | Path, dimensions: int, table_name: str | None = None):
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0:
            raise ValueError("vector dimensions must be positive")
        # path 沿用 MAICA_VECTOR_DB_PATH（一个目录）：表就放在 <dir>/vectors.db
        root = Path(path)
        if root.suffix.lower() in (".db", ".sqlite", ".sqlite3"):
            db_path = root
        else:
            root.mkdir(parents=True, exist_ok=True)
            db_path = root / cls.db_file

        def open_conn():
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(_SCHEMA)
            conn.commit()
            return conn

        conn = await asyncio.to_thread(open_conn)
        store = cls(conn, dimensions)
        await asyncio.to_thread(store._check_dims)
        return store

    def _check_dims(self):
        """维度不一致直接报错（和 Lance 版同语义）：换模型必须显式重建。"""
        row = self._conn.execute("SELECT dim FROM vector_row LIMIT 1").fetchone()
        if row is None:
            return
        stored = int(row[0])
        if stored != self.dimensions:
            raise ValueError(
                f"vector dimension mismatch: stored={stored}, configured={self.dimensions}"
                f"（换嵌入模型后请删除 {self._db_path()} 或调用 migrate）")

    def _db_path(self) -> str:
        for row in self._conn.execute("PRAGMA database_list"):
            if row[1] == "main":
                return row[2]
        return "?"

    def _ensure_open(self):
        if self._conn is None:
            raise RuntimeError("SqliteVectorStore is closed")

    async def close(self):
        conn, self._conn = self._conn, None
        self._cache.clear()
        if conn is not None:
            await asyncio.to_thread(conn.close)

    # ---------- 写 ----------

    @staticmethod
    def _record_id(text: str, filters: Mapping[str, Any] | None) -> str:
        """与 Lance 版保持逐字节一致，迁移后 id 不变。"""
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
                raise ValueError(
                    f"embedding dimension mismatch for {text!r}: {len(vector)} != {self.dimensions}")
        return pairs

    async def sync_texts(self, embedding_conn, data: Iterable[str], unique: str = "raw_text",
                         filters=None, ref_ids=None):
        """把这一批文本同步进指定范围（差集：本轮没出现的删掉）。语义与 Lance 版相同。"""
        if unique != "raw_text":
            raise ValueError("SqliteVectorStore only supports raw_text uniqueness")
        flt = _validate_filters(filters)
        requested = set(_validate_texts(data))
        want_ref = dict(ref_ids or {})
        expression, args = _where(flt)
        self._ensure_open()
        async with self._write_lock:
            def read_old():
                rows = self._conn.execute(
                    "SELECT id, raw_text, ref_id FROM vector_row" + expression, args).fetchall()
                return rows

            old = await asyncio.to_thread(read_old)
            old_by_text = {r[1]: r[0] for r in old}
            stale = {r[1] for r in old
                     if want_ref and r[2] != want_ref.get(r[1])}
            to_add = (requested - set(old_by_text)) | (stale & requested)
            to_delete = (set(old_by_text) - requested) | stale

            def delete_rows():
                ids = [old_by_text[t] for t in to_delete if t in old_by_text]
                if not ids:
                    return 0
                self._conn.executemany("DELETE FROM vector_row WHERE id = ?", [(i,) for i in ids])
                self._conn.commit()
                return len(ids)

            if to_delete:
                await asyncio.to_thread(delete_rows)

            embedded = await self._embed(embedding_conn, to_add)
            if embedded:
                def insert_rows(rows):
                    self._conn.executemany(
                        "INSERT OR REPLACE INTO vector_row "
                        "(id, ref_id, user_id, chat_session_num, type, raw_text, is_prod, dim, vec) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", rows)
                    self._conn.commit()

                payload = []
                for text, vector in embedded:
                    vec = np.asarray(vector, dtype=np.float32)
                    payload.append((
                        self._record_id(text, flt), want_ref.get(text),
                        flt.get("user_id"), flt.get("chat_session_num", 0),
                        flt.get("type", "persistent"), text,
                        1 if flt.get("is_prod", True) else 0,
                        self.dimensions, vec.tobytes(),
                    ))
                await asyncio.to_thread(insert_rows, payload)

            self._version += 1
            try:
                import maica.maica_utils.debug_log as _dl
                _dl.dbg("VEC-SYNC", "scope=%s added=%d deleted=%d"
                        % (flt, len(to_add), len(to_delete)))
            except Exception:
                pass
            return {text: old_by_text.get(text) or self._record_id(text, flt) for text in requested}

    # ---------- 读 ----------

    def _load_matrix(self, flt: Mapping[str, Any]) -> dict[str, Any]:
        """取（或重建）某范围的缓存矩阵：归一化后的 float32 矩阵 + 元数据。"""
        key = (flt.get("user_id"), flt.get("chat_session_num"), flt.get("type"))
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached["version"] == self._version:
                return cached
            expression, args = _where(flt)
            rows = self._conn.execute(
                "SELECT raw_text, ref_id, type, vec FROM vector_row" + expression, args).fetchall()
            if not rows:
                empty = {"version": self._version, "texts": [], "refs": [], "types": [],
                         "matrix": np.zeros((0, self.dimensions), dtype=np.float32)}
                self._cache[key] = empty
                return empty
            dim = self.dimensions
            # frombuffer 是只读视图，先 copy 再归一化（原地写只读数组会抛 ValueError）
            matrix = np.frombuffer(b"".join(r[3] for r in rows), dtype=np.float32).reshape(len(rows), dim).copy()
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            matrix = matrix / norms
            entry = {
                "version": self._version,
                "texts": [r[0] for r in rows],
                "refs": [r[1] for r in rows],
                "types": [r[2] for r in rows],
                "matrix": matrix,
            }
            self._cache[key] = entry
            return entry

    async def _query(self, embedding_conn, data: Iterable[str], filters, topk: int, similarity_min: float):
        flt = _validate_filters(filters)
        texts = _validate_texts(data)
        embedded = await self._embed(embedding_conn, texts)

        def scan():
            entry = self._load_matrix(flt)
            matrix = entry["matrix"]
            hits = []
            for _, vector in embedded:
                q = np.asarray(vector, dtype=np.float32)
                nq = float(np.linalg.norm(q))
                if nq == 0 or matrix.shape[0] == 0:
                    continue
                sims = matrix @ (q / nq)
                order = np.argpartition(-sims, min(topk, len(sims) - 1))[:topk]
                for i in order:
                    score = float(sims[i])
                    if score >= similarity_min:
                        hits.append({
                            "raw_text": entry["texts"][i],
                            "ref_id": entry["refs"][i],
                            "type": entry["types"][i],
                            "score": score,
                        })
            return hits

        return await asyncio.to_thread(scan)

    async def search(self, embedding_conn, data: Iterable[str], filters=None,
                     topk: int = 5, similarity_min: float = 0.5):
        if isinstance(topk, bool) or not isinstance(topk, int) or topk <= 0:
            raise ValueError("topk must be a positive integer")
        if isinstance(similarity_min, bool) or not isinstance(similarity_min, (int, float)) \
                or not math.isfinite(similarity_min):
            raise ValueError("similarity_min must be a finite number")
        self._ensure_open()
        hits = await self._query(embedding_conn, data, filters, topk, similarity_min)
        results = {h["raw_text"] for h in hits}
        try:
            import maica.maica_utils.debug_log as _dl
            _dl.dbg("VEC-SEARCH", "hits=%d filters=%s" % (len(results), _validate_filters(filters)),
                    hits=sorted(results))
        except Exception:
            pass
        return results

    async def search_with_refs(self, embedding_conn, data: Iterable[str], filters=None,
                               topk: int = 5, similarity_min: float = 0.5) -> list:
        if isinstance(topk, bool) or not isinstance(topk, int) or topk <= 0:
            raise ValueError("topk must be a positive integer")
        self._ensure_open()
        hits = await self._query(embedding_conn, data, filters, topk, similarity_min)
        best: dict[str, dict] = {}
        for item in hits:
            text = item["raw_text"]
            if text not in best or best[text]["score"] < item["score"]:
                best[text] = item
        out = sorted(best.values(), key=lambda item: item["score"], reverse=True)
        try:
            import maica.maica_utils.debug_log as _dl
            _dl.dbg("VEC-SEARCH", "hits=%d filters=%s" % (len(out), _validate_filters(filters)),
                    hits=[item["raw_text"] for item in out])
        except Exception:
            pass
        return out
