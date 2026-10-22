"""Standalone Tantivy lexical adapter — the **default, core** BM25 leg (D-25).

Tantivy is a core dependency (v0.2): it backs the default hybrid retrieval leg
on every storage backend, since it owns its own index and never touches the
transactional store. It satisfies the
:class:`~memspine.services.lexical.base.LexicalStore` port, so the
:class:`~memspine.services.lexical.projector.LexicalProjector` drives it
identically and it is a fully rebuildable projection (:meth:`clear` truncates so
a rebuild replays from seq 0).

tantivy-py is **synchronous** — it owns an on-disk (or in-RAM) index directory,
a schema, a single ``IndexWriter`` (holding the index-wide writer lock) and a
snapshot ``Searcher``. Every one of those calls is wrapped in
:func:`asyncio.to_thread` so the async port contract holds, and all mutations
are serialized through one :class:`threading.Lock` taken on the worker thread
(tantivy permits exactly one writer at a time). The lock is not bound to an event
loop, so one on-disk index is shared by every Engine in the process, whatever loop
each runs on (ADR-079: a single-writer store is opened once per path). One long-lived writer is reused across commits — allocating
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
import contextvars
import itertools
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from memspine.config.constants import (
    MAX_LEXICAL_QUERY_CHARS,
    MAX_LEXICAL_QUERY_TERMS,
    TANTIVY_COMMIT_BACKOFF_S,
    TANTIVY_COMMIT_RETRIES,
    TANTIVY_WRITER_HEAP_BYTES,
)
from memspine.observability.logging import get_logger
from memspine.services.lexical.base import LexicalHit
from memspine.store.records import MemoryRecord

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

#: N58: the registered name of the English (stop words + stemmer) analyzer.
ENGLISH_TOKENIZER = "memspine_english"

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


def _is_sharing_violation(exc: Exception) -> bool:
    """A Windows "Access is denied" (error 5) from Tantivy, in any of its wordings:
    ``An IO error occurred: 'Access is denied. (os error 5)'`` on a rename, or
    ``Failed to open file for write: 'IoError { ... code: 5, kind: PermissionDenied
    ... }'`` on a segment file."""
    text = str(exc)
    return "os error 5" in text or ("PermissionDenied" in text and "code: 5" in text)


def _commit_with_retry(writer: Any) -> None:
    """Commit, retrying a Windows sharing violation a few times.

    On Windows a commit renames ``meta.json``; while another handle in the process
    (the reader reload, an indexer or scanner) still has it open, the rename fails
    with ``Access is denied (os error 5)``. The commit is safe to retry: nothing was
    published. Any other error, or the last attempt's, is raised unchanged."""
    import time

    for attempt in range(TANTIVY_COMMIT_RETRIES):
        try:
            writer.commit()
            return
        except ValueError as exc:
            if not _is_sharing_violation(exc) or attempt == TANTIVY_COMMIT_RETRIES - 1:
                raise
            time.sleep(TANTIVY_COMMIT_BACKOFF_S * (2**attempt))


def _wait_merging_threads(writer: Any) -> None:
    """Join the writer's merge threads at close, tolerating a writer still borrowed.

    A writer call whose awaiting task was cancelled keeps running on its worker thread after the
    store lock is released; joining then raises ``RuntimeError: Already borrowed`` (PyO3). Retry a
    few times while that call finishes; if it is still busy, skip the join (the writer is dropped,
    so at worst an on-disk index keeps a temp segment until the next open merges it)."""
    import time

    for attempt in range(TANTIVY_COMMIT_RETRIES):
        try:
            writer.wait_merging_threads()
            return
        except RuntimeError as exc:
            if "already borrowed" not in str(exc).lower():
                raise
            if attempt == TANTIVY_COMMIT_RETRIES - 1:
                _log.warning("lexical.close.writer_busy", attempts=attempt + 1)
                return
            time.sleep(TANTIVY_COMMIT_BACKOFF_S * (2**attempt))


class TantivyLexical:
    """Tantivy-backed lexical store (D-25) — the default hybrid leg, **core**.

    ``index_path=None`` builds an in-RAM index (``:memory:`` parity — same
    lifetime as an in-memory event log, no ghost segments across runs); a path
    opens/creates an on-disk index directory. Backend-independent: it never
    touches the transactional store, so hybrid works the same on SQLite and
    Postgres.
    """

    def __init__(
        self,
        index_path: str | Path | None = None,
        heap_bytes: int = TANTIVY_WRITER_HEAP_BYTES,
        analyzer: str = "default",
        date_tokens: bool = False,
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
        #: N30 (write side): index each record's date words with its text.
        self._date_tokens = date_tokens
        #: N58: ``english`` = stop words + Snowball stemmer, applied to the indexed
        #: content and to the query terms alike (``_terms``).
        self._analyzer: Any = None
        content_tokenizer = "default"
        if analyzer == "english":
            self._analyzer = (
                tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
                .filter(tantivy.Filter.remove_long(40))
                .filter(tantivy.Filter.lowercase())
                .filter(tantivy.Filter.stopword("english"))
                .filter(tantivy.Filter.stemmer("english"))
                .build()
            )
            content_tokenizer = ENGLISH_TOKENIZER
        elif analyzer != "default":
            raise ValueError(f"unknown lexical analyzer {analyzer!r} (default | english)")
        builder.add_text_field("content", stored=False, tokenizer_name=content_tokenizer)
        self._schema = builder.build()
        self._index: Any = None
        self._writer: Any = None
        #: Serialises opening and every writer call; taken on worker threads only.
        self._lock = threading.Lock()
        # Batched ingest (see defer_commits): added documents wait for one
        # commit, which every read and flush() performs first. Whether a call defers
        # is the caller's context (_DEFERRING), not shared state of this handle.
        self._token = next(_STORE_TOKENS)
        self._uncommitted = False
        #: Set by close(): an open still running on a worker thread (a cancelled RM-74
        #: warm-up) must not take the writer lock after the store was closed.
        self._closed = False

    async def _ensure(self) -> None:
        """Open the index and its single long-lived writer once, off the loop."""
        if self._index is not None:
            return
        await asyncio.to_thread(self._open)

    def _open(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("lexical index is closed")
            if self._index is None:
                self._open_unlocked()

    def _open_unlocked(self) -> None:
        import tantivy

        if self._path is None:
            index = tantivy.Index(self._schema)
        else:
            Path(self._path).mkdir(parents=True, exist_ok=True)
            index = tantivy.Index(self._schema, path=self._path)
        if self._analyzer is not None:
            index.register_tokenizer(ENGLISH_TOKENIZER, self._analyzer)
        # One writer for the store's life: commit() is reusable across mutations,
        # so the heap is allocated once (wait_merging_threads, which consumes the
        # writer, is deferred to close()). ``_index`` is published last: ``_ensure``
        # skips the lock when it is set, so a concurrent first use (an RM-74 warm-up
        # racing a write) must never see an index without its writer.
        self._writer = index.writer(self._heap, 1)
        self._index = index

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
        content = self._body(record)
        defer = self._deferring()
        await asyncio.to_thread(self._index_one, record.record_id, record.namespace, content, defer)

    def _index_one(self, record_id: str, namespace: str, content: str, defer: bool) -> None:
        with self._lock:
            if defer:
                self._add_doc(record_id, namespace, content)
                self._uncommitted = True
            else:
                # Commits every held document too: committing early is always safe.
                self._commit_index(record_id, namespace, content)
                self._uncommitted = False

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
        if not self._uncommitted:
            return
        await asyncio.to_thread(self._commit_if_pending)

    def _commit_if_pending(self) -> None:
        with self._lock:
            if self._uncommitted:
                self._commit()
                self._uncommitted = False

    def _commit(self) -> None:
        _commit_with_retry(self._writer)
        self._index.reload()

    def _commit_index(self, record_id: str, namespace: str, content: str) -> None:
        self._add_doc(record_id, namespace, content)
        _commit_with_retry(self._writer)
        self._index.reload()

    async def index_many(self, records: Sequence[MemoryRecord]) -> None:
        """Index a sequence under a SINGLE commit + reload (per the port's batch
        convenience). Still idempotent under replay via the per-record upsert."""
        await self._ensure()
        cleaned = [(record.record_id, record.namespace, self._body(record)) for record in records]
        if not cleaned:
            return
        await asyncio.to_thread(self._commit_index_many, cleaned)

    def _commit_index_many(self, cleaned: list[tuple[str, str, str]]) -> None:
        with self._lock:
            for rid, ns, content in cleaned:
                self._add_doc(rid, ns, content)
            _commit_with_retry(self._writer)
            self._index.reload()
            self._uncommitted = False

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
        if self._analyzer is None:
            return tokenize_content(query)
        terms = list(dict.fromkeys(self._analyzer.analyze(query)))
        return terms[:MAX_LEXICAL_QUERY_TERMS]

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
        await asyncio.to_thread(self._commit_delete, record_id)

    def _commit_delete(self, record_id: str) -> None:
        with self._lock:
            self._writer.delete_documents_by_term("record_id", record_id)
            _commit_with_retry(self._writer)
            self._index.reload()
            self._uncommitted = False

    async def exists(self, record_id: str) -> bool:
        """M7 ``forget --verify`` support: is this record still indexed? The
        index stores raw content, so erasure is unproven until this is checked."""
        await self._ensure()
        await self._commit_pending()
        return await asyncio.to_thread(self._exists, record_id)

    def _exists(self, record_id: str) -> bool:
        import tantivy

        searcher = self._index.searcher()
        query = tantivy.Query.term_query(self._schema, "record_id", record_id)
        return bool(searcher.search(query, 1).hits)

    async def clear(self) -> None:
        await self._ensure()
        await asyncio.to_thread(self._commit_clear)

    def _commit_clear(self) -> None:
        with self._lock:
            self._writer.delete_all_documents()
            _commit_with_retry(self._writer)
            self._index.reload()
            self._uncommitted = False

    async def close(self) -> None:
        if self._closed:
            return
        await asyncio.to_thread(self._close)

    def _close(self) -> None:
        with self._lock:
            self._closed = True
            if self._writer is None:
                return
            if self._uncommitted:
                # Documents held by defer_commits are committed, never dropped.
                self._commit()
                self._uncommitted = False
            writer, self._writer = self._writer, None
            # Join merge threads so an on-disk index leaves no dangling temp
            # segments (this consumes the writer — the store is done).
            _wait_merging_threads(writer)
