"""Standalone Tantivy lexical adapter — the **default, core** BM25 leg (D-25).

Tantivy is a core dependency (v0.2): it backs the default hybrid retrieval leg
on every storage backend, since it owns its own index and never touches the
transactional store. It satisfies the
:class:`~memspine.services.lexical.base.LexicalStore` port, so the
:class:`~memspine.services.lexical.projector.LexicalProjector` drives it
identically and it is a fully rebuildable projection (:meth:`clear` truncates so
a rebuild replays from seq 0). Server-scale deployments swap in the
``[opensearch]`` provider (:mod:`memspine.services.lexical.opensearch`).

tantivy-py is **synchronous** — it owns an on-disk (or in-RAM) index directory,
a schema, a single ``IndexWriter`` (holding the index-wide writer lock) and a
snapshot ``Searcher``. Every one of those calls is wrapped in
:func:`asyncio.to_thread` so the async port contract holds, and all mutations
are serialized through one :class:`asyncio.Lock` (tantivy permits exactly one
writer at a time). One long-lived writer is reused across commits — allocating
its heap once — and is finalized in :meth:`close`.

Schema (D-25): ``record_id`` (stored + ``raw``-indexed, for exact upsert/delete
and result retrieval), ``namespace`` (stored + ``raw``-indexed, for the exact
term filter), ``content`` (``default``-tokenized, not stored — the BM25 body).

Namespace isolation: every search ANDs a
``Must`` term query on the ``namespace`` field with the content query, so a hit
can never come from another namespace. User text is treated strictly as DATA —
the query is NEVER handed to Tantivy's query parser (which would let
``namespace:other`` cross the filter and let malformed syntax raise). Instead it
is tokenized here into content-field term queries exactly as the ``default``
tokenizer splits indexed text (alphanumeric runs, lower-cased), OR-joined, and
ANDed under the namespace filter. Control chars are stripped and the query is
length/term-bounded before it ever reaches the index.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memspine.config.constants import (
    MAX_LEXICAL_QUERY_CHARS,
    MAX_LEXICAL_QUERY_TERMS,
    TANTIVY_WRITER_HEAP_BYTES,
)
from memspine.core.records import MemoryRecord
from memspine.observability.logging import get_logger
from memspine.services.lexical.base import LexicalHit

if TYPE_CHECKING:
    pass

__all__ = ["TantivyLexical", "strip_control_chars", "tokenize_content"]

# C0 control chars (keeping tab/newline/CR) + DEL. A NUL in content can never
# poison the projector chain, and a NUL in a query can never crash search().
_CONTROL_TABLE = {
    codepoint: None for codepoint in (*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F)
}


def strip_control_chars(text: str) -> str:
    """Remove NUL and other C0 control characters (keeping tab/newline/CR)."""
    return text.translate(_CONTROL_TABLE)


_log = get_logger(__name__)


def tokenize_content(query: str) -> list[str]:
    """Split user text into content-field terms the way Tantivy's ``default``
    tokenizer splits indexed content: maximal runs of alphanumeric characters,
    lower-cased. Term queries match indexed tokens verbatim (no tokenizer is
    applied to a term query's value), so the query must be lowered here to hit
    the lower-cased index. The term count is capped at ``MAX_LEXICAL_QUERY_TERMS``
    so an adversarial query cannot fan out into an unbounded boolean query.
    """
    terms: list[str] = []
    current: list[str] = []
    for ch in query:
        if ch.isalnum():
            current.append(ch.lower())
        elif current:
            terms.append("".join(current))
            current = []
            if len(terms) >= MAX_LEXICAL_QUERY_TERMS:
                return terms
    if current and len(terms) < MAX_LEXICAL_QUERY_TERMS:
        terms.append("".join(current))
    return terms


class TantivyLexical:
    """Tantivy-backed lexical store (D-25) — the default hybrid leg, **core**.

    ``index_path=None`` builds an in-RAM index (``:memory:`` parity — same
    lifetime as an in-memory event log, no ghost segments across runs); a path
    opens/creates an on-disk index directory. Backend-independent: it never
    touches the transactional store, so hybrid works the same on SQLite and
    Postgres.
    """

    def __init__(
        self, index_path: str | Path | None = None, heap_bytes: int = TANTIVY_WRITER_HEAP_BYTES
    ) -> None:
        # tantivy is a CORE dependency (v0.2): it backs the default hybrid leg on
        # every backend, so a failed import is a broken install, not a missing
        # optional extra — surface it plainly rather than as a D-10 "pip install".
        import tantivy

        self._path = str(index_path) if index_path is not None else None
        self._heap = heap_bytes
        # Schema building is pure in-memory work — safe in __init__. Opening the
        # index (filesystem I/O for the on-disk case) is deferred to _ensure so
        # it runs off the event loop.
        builder = tantivy.SchemaBuilder()
        builder.add_text_field("record_id", stored=True, tokenizer_name="raw")
        builder.add_text_field("namespace", stored=True, tokenizer_name="raw")
        builder.add_text_field("content", stored=False, tokenizer_name="default")
        self._schema = builder.build()
        self._index: Any = None
        self._writer: Any = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> None:
        """Open the index and its single long-lived writer once, off the loop."""
        if self._index is not None:
            return
        async with self._lock:
            if self._index is not None:
                return
            await asyncio.to_thread(self._open)

    def _open(self) -> None:
        import tantivy

        if self._path is None:
            self._index = tantivy.Index(self._schema)
        else:
            Path(self._path).mkdir(parents=True, exist_ok=True)
            self._index = tantivy.Index(self._schema, path=self._path)
        # One writer for the store's life: commit() is reusable across mutations,
        # so the heap is allocated once (wait_merging_threads, which consumes the
        # writer, is deferred to close()).
        self._writer = self._index.writer(self._heap, 1)

    def _add_doc(self, record_id: str, namespace: str, content: str) -> None:
        import tantivy

        # Delete-then-add keys the upsert on record_id: the delete carries a lower
        # opstamp than the add, so a re-index replaces the row instead of
        # duplicating it (idempotent under replay/catch-up).
        self._writer.delete_documents_by_term("record_id", record_id)
        doc = tantivy.Document()
        doc.add_text("record_id", record_id)
        doc.add_text("namespace", namespace)
        doc.add_text("content", content)
        self._writer.add_document(doc)

    async def index(self, record: MemoryRecord) -> None:
        await self._ensure()
        # Strip control chars from the lexical projection (a NUL is a bind hazard
        # elsewhere and carries no lexical signal); the record store keeps the
        # content verbatim, so a NUL can never poison the projector chain.
        content = strip_control_chars(record.content)
        rid, ns = record.record_id, record.namespace
        async with self._lock:
            await asyncio.to_thread(self._commit_index, rid, ns, content)

    def _commit_index(self, record_id: str, namespace: str, content: str) -> None:
        self._add_doc(record_id, namespace, content)
        self._writer.commit()
        self._index.reload()

    async def index_many(self, records: Sequence[MemoryRecord]) -> None:
        """Index a sequence under a SINGLE commit + reload (per the port's batch
        convenience). Still idempotent under replay via the per-record upsert."""
        await self._ensure()
        cleaned = [
            (record.record_id, record.namespace, strip_control_chars(record.content))
            for record in records
        ]
        if not cleaned:
            return
        async with self._lock:
            await asyncio.to_thread(self._commit_index_many, cleaned)

    def _commit_index_many(self, cleaned: list[tuple[str, str, str]]) -> None:
        for rid, ns, content in cleaned:
            self._add_doc(rid, ns, content)
        self._writer.commit()
        self._index.reload()

    async def search(self, namespace: str, query: str, top_k: int = 8) -> list[LexicalHit]:
        await self._ensure()
        # Bound the query before it reaches the tokenizer/index (DoS guard).
        terms = tokenize_content(strip_control_chars(query)[:MAX_LEXICAL_QUERY_CHARS])
        if not terms:
            return []
        return await asyncio.to_thread(self._search, namespace, terms, top_k)

    def _search(self, namespace: str, terms: list[str], top_k: int) -> list[LexicalHit]:
        import tantivy

        searcher = self._index.searcher()
        # Namespace filter (Must) ANDed with an OR over the content terms (Should).
        # The user query never touches parse_query, so it cannot reference the
        # namespace field or break match syntax — isolation is structural.
        namespace_query = tantivy.Query.term_query(self._schema, "namespace", namespace)
        content_query = tantivy.Query.boolean_query(
            [
                (tantivy.Occur.Should, tantivy.Query.term_query(self._schema, "content", term))
                for term in terms
            ]
        )
        query = tantivy.Query.boolean_query(
            [(tantivy.Occur.Must, namespace_query), (tantivy.Occur.Must, content_query)]
        )
        result = searcher.search(query, top_k)
        hits: list[LexicalHit] = []
        for score, address in result.hits:
            # Tantivy BM25 scores are higher = better, matching the LexicalHit
            # contract directly (no negation, unlike SQLite's bm25()).
            record_id = searcher.doc(address).get_first("record_id")
            hits.append(LexicalHit(record_id=str(record_id), score=float(score)))
        return hits

    async def delete(self, record_id: str) -> None:
        await self._ensure()
        async with self._lock:
            await asyncio.to_thread(self._commit_delete, record_id)

    def _commit_delete(self, record_id: str) -> None:
        self._writer.delete_documents_by_term("record_id", record_id)
        self._writer.commit()
        self._index.reload()

    async def exists(self, record_id: str) -> bool:
        """M7 ``forget --verify`` support: is this record still indexed? The
        index stores raw content, so erasure is unproven until this is checked."""
        await self._ensure()
        return await asyncio.to_thread(self._exists, record_id)

    def _exists(self, record_id: str) -> bool:
        import tantivy

        searcher = self._index.searcher()
        query = tantivy.Query.term_query(self._schema, "record_id", record_id)
        return bool(searcher.search(query, 1).hits)

    async def clear(self) -> None:
        await self._ensure()
        async with self._lock:
            await asyncio.to_thread(self._commit_clear)

    def _commit_clear(self) -> None:
        self._writer.delete_all_documents()
        self._writer.commit()
        self._index.reload()

    async def close(self) -> None:
        if self._writer is None:
            return
        async with self._lock:
            if self._writer is None:
                return
            writer, self._writer = self._writer, None
            # Join merge threads so an on-disk index leaves no dangling temp
            # segments (this consumes the writer — the store is done).
            await asyncio.to_thread(writer.wait_merging_threads)
