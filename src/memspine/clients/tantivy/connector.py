"""Tantivy index connector (RM-113 P1 #10): index open, directory creation, the single
writer (index-wide writer lock), commit-and-reload and close.

tantivy-py is synchronous. The connector holds one ``threading.Lock`` that serialises
opening and every writer call (tantivy permits exactly one writer at a time); the adapter
(``services/lexical/tantivy.py``) takes :attr:`TantivyConnector.lock` on a worker thread
around its writer calls. The lock is not bound to an event loop, so one on-disk index is
shared by every Engine in the process, whatever loop each runs on (ADR-079: a single-writer
store is opened once per path). One long-lived writer is reused across commits — allocating
its heap once — and is finalized in :meth:`TantivyConnector.close`, which commits documents
still held by a ``defer_commits`` batch (RM-72) before joining the merge threads.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from memspine.clients._common.deadlines import probe
from memspine.clients.base import ConnectorIdentity, Locality, Scope
from memspine.exceptions import StorageError

__all__ = [
    "ENGLISH_TOKENIZER",
    "TANTIVY_COMMIT_BACKOFF_S",
    "TANTIVY_COMMIT_RETRIES",
    "TANTIVY_WRITER_HEAP_BYTES",
    "TantivyConnector",
    "TantivyField",
    "TantivySpec",
    "commit_with_retry",
    "wait_merging_threads",
]

#: Per-thread heap the single long-lived IndexWriter buffers into before a commit flushes to
#: a segment. tantivy requires a floor around 15 MB per writer thread; one writer serves the
#: whole index (mutations are serialized), so this is allocated once, not per write.
TANTIVY_WRITER_HEAP_BYTES = 15_000_000
#: Commit on Windows: retries and base backoff (s, doubled each try) for the "Access is
#: denied (os error 5)" sharing violation when ``meta.json`` is renamed while another handle
#: holds it (file-backed indexes only).
TANTIVY_COMMIT_RETRIES = 6
TANTIVY_COMMIT_BACKOFF_S = 0.05
#: N58: the registered name of the English (stop words + stemmer) analyzer.
ENGLISH_TOKENIZER = "memspine_english"

Analyzer = Literal["default", "english"]
_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TantivyField:
    """One text field of the index schema."""

    name: str
    stored: bool
    tokenizer: str


@dataclass(frozen=True, slots=True)
class TantivySpec:
    """An index directory (``None`` = an in-RAM index private to its connector), its text
    fields, the analyzer registered as :data:`ENGLISH_TOKENIZER` and the writer heap."""

    path: str | None
    fields: tuple[TantivyField, ...]
    analyzer: Analyzer = "default"
    heap_bytes: int = TANTIVY_WRITER_HEAP_BYTES

    def __post_init__(self) -> None:
        if self.analyzer not in ("default", "english"):
            raise ValueError(f"unknown lexical analyzer {self.analyzer!r} (default | english)")
        if self.path is not None:
            object.__setattr__(self, "path", str(self.path))


def _is_sharing_violation(exc: Exception) -> bool:
    """A Windows "Access is denied" (error 5) from Tantivy, in any of its wordings:
    ``An IO error occurred: 'Access is denied. (os error 5)'`` on a rename, or
    ``Failed to open file for write: 'IoError { ... code: 5, kind: PermissionDenied
    ... }'`` on a segment file."""
    text = str(exc)
    return "os error 5" in text or ("PermissionDenied" in text and "code: 5" in text)


def commit_with_retry(writer: Any) -> None:
    """Commit, retrying a Windows sharing violation a few times.

    On Windows a commit renames ``meta.json``; while another handle in the process
    (the reader reload, an indexer or scanner) still has it open, the rename fails
    with ``Access is denied (os error 5)``. The commit is safe to retry: nothing was
    published. Any other error, or the last attempt's, is raised unchanged."""
    for attempt in range(TANTIVY_COMMIT_RETRIES):
        try:
            writer.commit()
            return
        except ValueError as exc:
            if not _is_sharing_violation(exc) or attempt == TANTIVY_COMMIT_RETRIES - 1:
                raise
            time.sleep(TANTIVY_COMMIT_BACKOFF_S * (2**attempt))


def wait_merging_threads(writer: Any) -> bool:
    """Join the writer's merge threads at close, tolerating a writer still borrowed.

    A writer call whose awaiting task was cancelled keeps running on its worker thread after
    the lock is released; joining then raises ``RuntimeError: Already borrowed`` (PyO3).
    Retry a few times while that call finishes; if it is still busy, skip the join and return
    False (the writer is dropped, so at worst an on-disk index keeps a temp segment until the
    next open merges it)."""
    for attempt in range(TANTIVY_COMMIT_RETRIES):
        try:
            writer.wait_merging_threads()
            return True
        except RuntimeError as exc:
            if "already borrowed" not in str(exc).lower():
                raise
            if attempt == TANTIVY_COMMIT_RETRIES - 1:
                return False
            time.sleep(TANTIVY_COMMIT_BACKOFF_S * (2**attempt))
    return False  # pragma: no cover - the loop always returns


class TantivyConnector:
    """One Tantivy index and its single writer; a :class:`~memspine.clients.base.Connector`.

    tantivy is a core dependency (v0.2): a failed import is a broken install, not a missing
    optional extra, so it surfaces plainly. Opening is idempotent and safe from several
    threads; :meth:`open_blocking` is the worker-thread form."""

    kind: ClassVar[str] = "lexical.tantivy"
    scope: ClassVar[Scope] = "thread"

    def __init__(self, spec: TantivySpec) -> None:
        import tantivy

        self._spec = spec
        # Schema building is pure in-memory work; opening the index (filesystem I/O for the
        # on-disk case) is deferred to open() so it runs off the event loop.
        builder = tantivy.SchemaBuilder()
        for text_field in spec.fields:
            builder.add_text_field(
                text_field.name, stored=text_field.stored, tokenizer_name=text_field.tokenizer
            )
        self._schema = builder.build()
        self._analyzer: Any = None
        if spec.analyzer == "english":
            self._analyzer = (
                tantivy.TextAnalyzerBuilder(tantivy.Tokenizer.simple())
                .filter(tantivy.Filter.remove_long(40))
                .filter(tantivy.Filter.lowercase())
                .filter(tantivy.Filter.stopword("english"))
                .filter(tantivy.Filter.stemmer("english"))
                .build()
            )
        self._index: Any = None
        self._writer: Any = None
        #: Serialises opening and every writer call; taken on worker threads only.
        self.lock = threading.Lock()
        #: Documents added but not committed (an adapter's ``defer_commits`` batch). Read and
        #: written under :attr:`lock`; committed by :meth:`close` so nothing held is dropped.
        self.uncommitted = False
        #: Set by close(): an open still running on a worker thread (a cancelled RM-74
        #: warm-up) must not take the writer after the index was closed.
        self._closed = False

    # -- connector protocol ----------------------------------------------------------------
    @classmethod
    def identity(cls, spec: TantivySpec) -> ConnectorIdentity:
        """An on-disk index by canonical path and analyzer; an in-RAM one is private."""
        if spec.path is None:
            return ConnectorIdentity(cls.kind, {"path": None, "private": uuid.uuid4().hex})
        path = os.path.normcase(str(Path(spec.path).expanduser().resolve(strict=False)))
        return ConnectorIdentity(
            cls.kind,
            {"path": path},
            knobs={
                "analyzer": spec.analyzer,
                "fields": [(f.name, f.stored, f.tokenizer) for f in spec.fields],
            },
            canonical={"kind": "tantivy", "path": path},
        )

    @classmethod
    def locality(cls, spec: TantivySpec) -> Locality:
        return "process" if spec.path is None else "host"

    @property
    def spec(self) -> TantivySpec:
        return self._spec

    @property
    def handle(self) -> Any:
        return self.index

    @property
    def schema(self) -> Any:
        return self._schema

    @property
    def analyzer(self) -> Any:
        """The registered English analyzer, or None for the ``default`` tokenizer."""
        return self._analyzer

    @property
    def is_open(self) -> bool:
        return self._index is not None

    @property
    def index(self) -> Any:
        if self._index is None:
            raise StorageError("Tantivy index is not open — call open() first")
        return self._index

    @property
    def writer(self) -> Any:
        """The single writer; call only while holding :attr:`lock`."""
        if self._writer is None:
            raise StorageError("Tantivy index is not open — call open() first")
        return self._writer

    async def open(self) -> None:
        if self._index is not None:
            return
        await asyncio.to_thread(self.open_blocking)

    def open_blocking(self) -> None:
        """:meth:`open` on the calling (worker) thread."""
        with self.lock:
            if self._closed:
                raise RuntimeError("lexical index is closed")
            if self._index is None:
                self._open_unlocked()

    def _open_unlocked(self) -> None:
        import tantivy

        path = self._spec.path
        if path is None:
            index = tantivy.Index(self._schema)
        else:
            Path(path).mkdir(parents=True, exist_ok=True)
            index = tantivy.Index(self._schema, path=path)
        if self._analyzer is not None:
            index.register_tokenizer(ENGLISH_TOKENIZER, self._analyzer)
        # One writer for the index's life: commit() is reusable across mutations, so the heap
        # is allocated once (wait_merging_threads, which consumes the writer, is deferred to
        # close()). ``_index`` is published last: ``open`` skips the lock when it is set, so a
        # concurrent first use (an RM-74 warm-up racing a write) never sees an index without
        # its writer.
        self._writer = index.writer(self._spec.heap_bytes, 1)
        self._index = index

    def commit(self) -> None:
        """Commit the writer and reload the reader; call only while holding :attr:`lock`."""
        commit_with_retry(self.writer)
        self.index.reload()
        self.uncommitted = False

    async def health(self, deadline_s: float) -> bool:
        """Open a searcher on the index (a reader round trip; no write)."""
        index = self._index
        if index is None:
            return False

        async def check() -> bool:
            searcher = await asyncio.to_thread(index.searcher)
            return int(searcher.num_docs) >= 0

        return await probe(deadline_s, check)

    async def close(self) -> None:
        if self._closed:
            return
        await asyncio.to_thread(self._close)

    def _close(self) -> None:
        with self.lock:
            self._closed = True
            if self._writer is None:
                return
            if self.uncommitted:
                # Documents held by defer_commits are committed, never dropped.
                self.commit()
            writer, self._writer = self._writer, None
            # Join merge threads so an on-disk index leaves no dangling temp segments
            # (this consumes the writer — the index is done).
            if not wait_merging_threads(writer):
                _log.warning("lexical.close.writer_busy")
