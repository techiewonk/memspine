"""Standalone Tantivy lexical adapter — the **default, core** BM25 leg (D-25).

Tantivy is a core dependency (v0.2): it backs the default hybrid retrieval leg
on every storage backend, since it owns its own index and never touches the
transactional store. It satisfies the
:class:`~memspine.services.lexical.base.LexicalStore` port, so the
:class:`~memspine.services.lexical.projector.LexicalProjector` drives it
identically and it is a fully rebuildable projection (:meth:`clear` truncates so
a rebuild replays from seq 0).

tantivy-py is **synchronous**. The index, its schema, the single ``IndexWriter``
(holding the index-wide writer lock) and the writer's threading lock belong to an
injected :class:`~memspine.clients.tantivy.TantivyConnector` (RM-113); this adapter
never opens an index itself. Every native call is wrapped in :func:`asyncio.to_thread`
so the async port contract holds, and all mutations are serialized through the
connector's lock taken on the worker thread. The connector opens lazily on first use
(or in the RM-74 warm-up) and is closed by its owner (the factory), which commits any
held documents first; :meth:`TantivyLexical.close` only commits what is held.

Schema (D-25, :data:`LEXICAL_FIELDS`): ``record_id`` (stored + ``raw``-indexed, for exact
upsert/delete and result retrieval), ``namespace`` (stored + ``raw``-indexed, for the exact
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
import contextvars
import itertools
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from memspine.clients.tantivy import ENGLISH_TOKENIZER, TantivyConnector, TantivyField, TantivySpec
from memspine.config.constants import MAX_LEXICAL_QUERY_CHARS, MAX_LEXICAL_QUERY_TERMS
from memspine.services.lexical.base import LexicalHit
from memspine.store.records import MemoryRecord

__all__ = [
    "LEXICAL_FIELDS",
    "TantivyLexical",
    "strip_control_chars",
    "tantivy_spec",
    "tokenize_content",
]

# C0 control chars (keeping tab/newline/CR) + DEL. A NUL in content can never
# poison the projector chain, and a NUL in a query can never crash search().
_CONTROL_TABLE = {
    codepoint: None for codepoint in (*range(0x00, 0x09), 0x0B, 0x0C, *range(0x0E, 0x20), 0x7F)
}


def strip_control_chars(text: str) -> str:
    """Remove NUL and other C0 control characters (keeping tab/newline/CR)."""
    return text.translate(_CONTROL_TABLE)


#: The stored, ``raw``-indexed fields of the lexical index (D-25); ``content`` follows the
#: analyzer (see :func:`tantivy_spec`).
LEXICAL_FIELDS: tuple[TantivyField, ...] = (
    TantivyField("record_id", stored=True, tokenizer="raw"),
    TantivyField("namespace", stored=True, tokenizer="raw"),
)


def tantivy_spec(
    path: str | Path | None = None, *, analyzer: Literal["default", "english"] = "default"
) -> TantivySpec:
    """The connector spec of a lexical index at ``path`` (``None`` = in RAM). N58:
    ``english`` = stop words + Snowball stemmer, on the indexed content and the query terms."""
    content = ENGLISH_TOKENIZER if analyzer == "english" else "default"
    return TantivySpec(
        None if path is None else str(path),
        (*LEXICAL_FIELDS, TantivyField("content", stored=False, tokenizer=content)),
        analyzer,
    )


#: RM-72: open ``defer_commits`` batches of the calling context, per store token (depth).
#: Deferral belongs to the caller (one Engine's projection batch), never to a store handle
#: that the registry shares between Engines (ADR-079).
_DEFERRING: contextvars.ContextVar[Mapping[int, int]] = contextvars.ContextVar(
    "memspine_tantivy_deferring", default=MappingProxyType({})
)
_STORE_TOKENS = itertools.count(1)


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

    Wraps an injected :class:`~memspine.clients.tantivy.TantivyConnector` built from
    :func:`tantivy_spec` (an in-RAM index for ``:memory:`` parity, or an on-disk
    directory). Backend-independent: it never touches the transactional store, so
    hybrid works the same on SQLite and Postgres.
    """

    def __init__(self, connector: TantivyConnector, date_tokens: bool = False) -> None:
        self._conn = connector
        #: N30 (write side): index each record's date words with its text.
        self._date_tokens = date_tokens
        # Batched ingest (see defer_commits): added documents wait for one
        # commit, which every read and flush() performs first. Whether a call defers
        # is the caller's context (_DEFERRING), not shared state of this handle.
        self._token = next(_STORE_TOKENS)

    @property
    def connector(self) -> TantivyConnector:
        return self._conn

    async def _ensure(self) -> None:
        """Open the connector's index and writer once (lazily, off the loop)."""
        if not self._conn.is_open:
            await self._conn.open()

    def _add_doc(self, record_id: str, namespace: str, content: str) -> None:
        import tantivy

        writer = self._conn.writer
        # Delete-then-add keys the upsert on record_id: the delete carries a lower
        # opstamp than the add, so a re-index replaces the row instead of
        # duplicating it (idempotent under replay/catch-up).
        writer.delete_documents_by_term("record_id", record_id)
        doc = tantivy.Document()
        doc.add_text("record_id", record_id)
        doc.add_text("namespace", namespace)
        doc.add_text("content", content)
        writer.add_document(doc)

    async def index(self, record: MemoryRecord) -> None:
        await self._ensure()
        # Strip control chars from the lexical projection (a NUL is a bind hazard
        # elsewhere and carries no lexical signal); the record store keeps the
        # content verbatim, so a NUL can never poison the projector chain.
        content = self._body(record)
        defer = self._deferring()
        await asyncio.to_thread(self._index_one, record.record_id, record.namespace, content, defer)

    def _index_one(self, record_id: str, namespace: str, content: str, defer: bool) -> None:
        with self._conn.lock:
            self._add_doc(record_id, namespace, content)
            if defer:
                self._conn.uncommitted = True
            else:
                # Commits every held document too: committing early is always safe.
                self._conn.commit()

    def _deferring(self) -> bool:
        return _DEFERRING.get().get(self._token, 0) > 0

    def defer_commits(self) -> None:
        """Hold the commit of documents this caller indexes until :meth:`flush` or a read.

        A commit (and reload) per document dominates the cost of indexing one
        turn. Searches and ``exists`` commit pending documents first, and
        deletes commit them together with the delete in the writer's order,
        so every read sees exactly what per-document commits would show.

        RM-72: the hold is scoped to the calling context (the task running one
        Engine's projection batch and the tasks it starts), not to this handle. A
        handle shared by several Engines (ADR-079) therefore commits another
        caller's write before it returns, so that caller never checkpoints ahead of
        the index, and one caller's :meth:`flush` never ends another's batch."""
        held = dict(_DEFERRING.get())
        held[self._token] = held.get(self._token, 0) + 1
        _DEFERRING.set(held)

    async def flush(self) -> None:
        """Commit held documents and end this caller's :meth:`defer_commits` hold.

        Every held document is committed, whoever indexed it (early commits are safe)."""
        held = dict(_DEFERRING.get())
        depth = held.pop(self._token, 0)
        if depth > 1:
            held[self._token] = depth - 1
        _DEFERRING.set(held)
        await self._commit_pending()

    async def _commit_pending(self) -> None:
        if not self._conn.uncommitted:
            return
        await asyncio.to_thread(self._commit_if_pending)

    def _commit_if_pending(self) -> None:
        with self._conn.lock:
            if self._conn.uncommitted:
                self._conn.commit()

    async def index_many(self, records: Sequence[MemoryRecord]) -> None:
        """Index a sequence under a SINGLE commit + reload (per the port's batch
        convenience). Still idempotent under replay via the per-record upsert."""
        await self._ensure()
        cleaned = [(record.record_id, record.namespace, self._body(record)) for record in records]
        if not cleaned:
            return
        await asyncio.to_thread(self._commit_index_many, cleaned)

    def _commit_index_many(self, cleaned: list[tuple[str, str, str]]) -> None:
        with self._conn.lock:
            for rid, ns, content in cleaned:
                self._add_doc(rid, ns, content)
            self._conn.commit()

    async def warm(self) -> None:
        """ADR-079 / RM-74 warm hook: open the index and its searcher with one tiny query."""
        await self.search("__memspine_warmup__", "memspine", top_k=1)

    async def search(self, namespace: str, query: str, top_k: int = 8) -> list[LexicalHit]:
        await self._ensure()
        # Bound the query before it reaches the tokenizer/index (DoS guard).
        terms = self._terms(strip_control_chars(query)[:MAX_LEXICAL_QUERY_CHARS])
        if not terms:
            return []
        await self._commit_pending()
        return await asyncio.to_thread(self._search, namespace, terms, top_k)

    def _body(self, record: MemoryRecord) -> str:
        """The indexed text: the content, plus its date words under N30."""
        content = strip_control_chars(record.content)
        if not self._date_tokens:
            return content
        from memspine.models.encoders.query.temporal_query import date_words

        return f"{content} {date_words(record)}"

    def _terms(self, query: str) -> list[str]:
        """Query terms, split exactly as the content field's analyzer splits text."""
        analyzer = self._conn.analyzer
        if analyzer is None:
            return tokenize_content(query)
        terms = list(dict.fromkeys(analyzer.analyze(query)))
        return terms[:MAX_LEXICAL_QUERY_TERMS]

    def _search(self, namespace: str, terms: list[str], top_k: int) -> list[LexicalHit]:
        import tantivy

        schema = self._conn.schema
        searcher = self._conn.index.searcher()
        # Namespace filter (Must) ANDed with an OR over the content terms (Should).
        # The user query never touches parse_query, so it cannot reference the
        # namespace field or break match syntax — isolation is structural.
        namespace_query = tantivy.Query.term_query(schema, "namespace", namespace)
        content_query = tantivy.Query.boolean_query(
            [
                (tantivy.Occur.Should, tantivy.Query.term_query(schema, "content", term))
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
        await asyncio.to_thread(self._commit_delete, record_id)

    def _commit_delete(self, record_id: str) -> None:
        with self._conn.lock:
            self._conn.writer.delete_documents_by_term("record_id", record_id)
            self._conn.commit()

    async def exists(self, record_id: str) -> bool:
        """M7 ``forget --verify`` support: is this record still indexed? The
        index stores raw content, so erasure is unproven until this is checked."""
        await self._ensure()
        await self._commit_pending()
        return await asyncio.to_thread(self._exists, record_id)

    def _exists(self, record_id: str) -> bool:
        import tantivy

        searcher = self._conn.index.searcher()
        query = tantivy.Query.term_query(self._conn.schema, "record_id", record_id)
        return bool(searcher.search(query, 1).hits)

    async def clear(self) -> None:
        await self._ensure()
        await asyncio.to_thread(self._commit_clear)

    def _commit_clear(self) -> None:
        with self._conn.lock:
            self._conn.writer.delete_all_documents()
            self._conn.commit()

    async def close(self) -> None:
        """Commit documents still held (never dropped); the connector's owner closes it."""
        if self._conn.is_open:
            await self._commit_pending()
