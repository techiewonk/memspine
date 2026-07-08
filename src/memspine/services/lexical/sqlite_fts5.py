"""Zero-dep lexical default (D-25): SQLite FTS5 virtual table, BM25 ranking.

The core-install lexical leg. Content is indexed under its namespace in an FTS5
virtual table (``memory_fts``); ``search`` filters by namespace and ranks with
SQLite's built-in ``bm25()``. A rebuildable projection like the vector store —
``clear()`` truncates and the projector re-indexes from the log.

If the SQLite build lacks the FTS5 module (rare — it has shipped compiled-in
since 3.9), the store degrades to a plain table + case-insensitive ``LIKE``
scan and logs the downgrade once (the D-25 ILIKE fallback). Index/delete/clear
DDL is identical on both paths; only ``search`` differs.

User queries are treated strictly as DATA: on the FTS5 path each whitespace
term is quoted (a phrase, internal quotes doubled) and OR-joined, so no query
can inject MATCH grammar (AND/OR/NEAR/``*``/``:``/parentheses) or escape the
namespace filter. The LIKE path binds terms as parameters.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from memspine.clients.sqlite import SQLiteClient
from memspine.config.constants import (
    LEXICAL_CACHE_MAX_ENTRIES,
    LEXICAL_LIKE_SCAN_MAX_ROWS,
    MAX_LEXICAL_QUERY_CHARS,
    MAX_LEXICAL_QUERY_TERMS,
)
from memspine.core.records import MemoryRecord
from memspine.observability.logging import get_logger
from memspine.services.lexical.base import LexicalHit
from memspine.services.storage.sqlite.schema import LEXICAL_FTS_TABLE

__all__ = ["SQLiteFTS5Lexical", "fts5_available", "sanitize_fts5_query", "strip_control_chars"]

_log = get_logger(__name__)

# C0 control chars break the FTS5 MATCH bind (a NUL truncates SQLite's C string
# mid-value → OperationalError) and carry no lexical signal. Drop them, keeping
# tab/newline/CR as ordinary whitespace separators. Applied to BOTH indexed
# content and query text so the lexical store accepts exactly what the record
# and vector projectors accept — a NUL in content can never poison the projector
# chain, and a NUL in a query can never crash search().
_CONTROL_TABLE = {
    codepoint: None for codepoint in (*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F)
}


def strip_control_chars(text: str) -> str:
    """Remove NUL and other C0 control characters (keeping tab/newline/CR)."""
    return text.translate(_CONTROL_TABLE)


def sanitize_fts5_query(query: str) -> str:
    """Turn arbitrary user text into a safe FTS5 MATCH expression.

    Control chars are stripped first (a NUL would crash the MATCH bind). Each
    whitespace-delimited term then becomes a double-quoted phrase (embedded
    quotes doubled) and the phrases are OR-joined for recall. Terms with no
    alphanumeric character are dropped (a bare ``"!!!"`` phrase is meaningless
    to the tokenizer and risks an empty-phrase parse); the term count is capped
    at ``MAX_LEXICAL_QUERY_TERMS`` so an adversarial query cannot make the OR
    parse super-linear. Returns ``""`` when the query carries no usable term —
    the caller then skips the MATCH entirely.
    """
    terms: list[str] = []
    for token in strip_control_chars(query).split():
        if len(terms) >= MAX_LEXICAL_QUERY_TERMS:
            break
        if not any(ch.isalnum() for ch in token):
            continue
        terms.append('"' + token.replace('"', '""') + '"')
    return " OR ".join(terms)


async def fts5_available(client: SQLiteClient) -> bool:
    """Probe the SQLite build for the FTS5 module by creating a throwaway
    temp virtual table. Reliable regardless of what ``memory_fts`` already is
    (a plain ``CREATE VIRTUAL TABLE IF NOT EXISTS`` would no-op against an
    existing table and never surface a missing module)."""
    async with client.engine.begin() as conn:
        try:
            await conn.exec_driver_sql("CREATE VIRTUAL TABLE temp.__fts5_probe USING fts5(x)")
        except OperationalError:
            return False
        await conn.exec_driver_sql("DROP TABLE temp.__fts5_probe")
    return True


class SQLiteFTS5Lexical:
    """FTS5-backed lexical store over an injected SQLiteClient (D-24)."""

    def __init__(self, client: SQLiteClient) -> None:
        self._client = client
        self._fts5: bool | None = None
        self._lock = asyncio.Lock()
        # D-42 §5 LRU corpus cache: bounded (namespace, query, top_k) -> hits.
        # Invalidated on every mutation (index/delete/clear) — the corpus moved.
        self._cache: OrderedDict[tuple[str, str, int], list[LexicalHit]] = OrderedDict()
        # Monotonic corpus generation, bumped on every mutation. A search that
        # started under one generation must NOT write its result into the cache
        # if a concurrent index/delete/clear moved the corpus while it awaited
        # the DB — otherwise the stale result is served indefinitely (the write's
        # _invalidate already ran before the search's cache-write).
        self._generation = 0

    async def _ensure(self) -> bool:
        """Create ``memory_fts`` (FTS5 vtable, or plain fallback) once and cache
        which path this build takes. Returns whether FTS5 is available."""
        if self._fts5 is not None:
            return self._fts5
        async with self._lock:
            if self._fts5 is not None:
                return self._fts5
            available = await fts5_available(self._client)
            async with self._client.engine.begin() as conn:
                if available:
                    await conn.exec_driver_sql(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS {LEXICAL_FTS_TABLE} "
                        "USING fts5(record_id UNINDEXED, namespace UNINDEXED, content)"
                    )
                else:
                    await conn.exec_driver_sql(
                        f"CREATE TABLE IF NOT EXISTS {LEXICAL_FTS_TABLE} "
                        "(record_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, "
                        "content TEXT NOT NULL)"
                    )
            if not available:
                _log.warning(
                    "lexical.fts5_unavailable",
                    detail="SQLite build lacks FTS5 — degraded to a LIKE scan (D-25)",
                )
            self._fts5 = available
            return available

    def _invalidate(self) -> None:
        self._cache.clear()
        self._generation += 1

    async def index(self, record: MemoryRecord) -> None:
        await self._ensure()
        # Strip control chars from the FTS projection of the content: a NUL would
        # truncate the SQLite bind and raise mid-projector-chain (a poison pill
        # that permanently blocks catch_up/rebuild). The record store keeps the
        # content verbatim — only its lexical projection is cleaned.
        content = strip_control_chars(record.content)
        # Delete-then-insert keys the upsert on record_id (FTS5's implicit rowid
        # would otherwise duplicate) and is idempotent under replay.
        async with self._client.engine.begin() as conn:
            await conn.execute(
                text(f"DELETE FROM {LEXICAL_FTS_TABLE} WHERE record_id = :rid"),
                {"rid": record.record_id},
            )
            await conn.execute(
                text(
                    f"INSERT INTO {LEXICAL_FTS_TABLE} (record_id, namespace, content) "
                    "VALUES (:rid, :ns, :content)"
                ),
                {"rid": record.record_id, "ns": record.namespace, "content": content},
            )
        self._invalidate()

    async def index_many(self, records: Sequence[MemoryRecord]) -> None:
        """Index each record in turn — a sequential convenience, NOT an atomic
        batch: every record commits in its own transaction (via :meth:`index`),
        so a mid-sequence failure leaves the earlier records indexed. Idempotent
        under replay like :meth:`index`."""
        for record in records:
            await self.index(record)

    async def search(self, namespace: str, query: str, top_k: int = 8) -> list[LexicalHit]:
        available = await self._ensure()
        # Bound the query BEFORE it becomes a cache key (an oversized raw string
        # would otherwise be retained) and before it reaches the parser/LIKE scan.
        query = strip_control_chars(query)[:MAX_LEXICAL_QUERY_CHARS]
        key = (namespace, query, top_k)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        generation = self._generation
        hits = (
            await self._search_fts5(namespace, query, top_k)
            if available
            else await self._search_like(namespace, query, top_k)
        )
        # Only cache if no mutation raced us while we awaited the DB — otherwise
        # this now-stale result would land in the freshly-invalidated cache.
        if generation == self._generation:
            self._cache[key] = hits
            self._cache.move_to_end(key)
            while len(self._cache) > LEXICAL_CACHE_MAX_ENTRIES:
                self._cache.popitem(last=False)
        return hits

    async def _search_fts5(self, namespace: str, query: str, top_k: int) -> list[LexicalHit]:
        match = sanitize_fts5_query(query)
        if not match:
            return []
        # bm25() returns ascending (most-relevant most-negative); negate so
        # higher = better and order by the raw rank so best comes first.
        stmt = text(
            f"SELECT record_id, bm25({LEXICAL_FTS_TABLE}) AS rank "
            f"FROM {LEXICAL_FTS_TABLE} "
            f"WHERE {LEXICAL_FTS_TABLE} MATCH :match AND namespace = :ns "
            "ORDER BY rank LIMIT :k"
        )
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt, {"match": match, "ns": namespace, "k": top_k})).all()
        return [LexicalHit(record_id=row[0], score=-float(row[1])) for row in rows]

    async def _search_like(self, namespace: str, query: str, top_k: int) -> list[LexicalHit]:
        terms = [token.lower() for token in query.split() if any(c.isalnum() for c in token)]
        # Cap the term count (each term is a per-row substring scan) and the rows
        # scanned — the degraded path must never become an accidental DoS.
        terms = terms[:MAX_LEXICAL_QUERY_TERMS]
        if not terms:
            return []
        stmt = text(
            f"SELECT record_id, content FROM {LEXICAL_FTS_TABLE} WHERE namespace = :ns LIMIT :cap"
        )
        async with self._client.engine.connect() as conn:
            rows = (
                await conn.execute(stmt, {"ns": namespace, "cap": LEXICAL_LIKE_SCAN_MAX_ROWS})
            ).all()
        scored: list[LexicalHit] = []
        for record_id, content in rows:
            body = str(content).lower()
            occurrences = sum(body.count(term) for term in terms)
            if occurrences:
                scored.append(LexicalHit(record_id=str(record_id), score=float(occurrences)))
        # Deterministic: score desc, then record_id asc (stable tie-break).
        scored.sort(key=lambda hit: (-hit.score, hit.record_id))
        return scored[:top_k]

    async def delete(self, record_id: str) -> None:
        await self._ensure()
        async with self._client.engine.begin() as conn:
            await conn.execute(
                text(f"DELETE FROM {LEXICAL_FTS_TABLE} WHERE record_id = :rid"),
                {"rid": record_id},
            )
        self._invalidate()

    async def exists(self, record_id: str) -> bool:
        """M7 ``forget --verify`` support: is this record still in the FTS index?
        ``memory_fts`` holds raw content, so erasure is unproven until this is
        checked — the verifier must not report clean without inspecting it."""
        await self._ensure()
        stmt = text(f"SELECT 1 FROM {LEXICAL_FTS_TABLE} WHERE record_id = :rid LIMIT 1")
        async with self._client.engine.connect() as conn:
            return (await conn.execute(stmt, {"rid": record_id})).first() is not None

    async def clear(self) -> None:
        await self._ensure()
        async with self._client.engine.begin() as conn:
            await conn.execute(text(f"DELETE FROM {LEXICAL_FTS_TABLE}"))
        self._invalidate()

    async def close(self) -> None:
        # The SQLite client is owned and closed by the engine lifecycle (D-24);
        # nothing store-local to release. Drop the cache to be tidy.
        self._invalidate()
