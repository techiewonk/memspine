"""LanceDB vector store — sole vector backend (D-09, ADR-021).

Consumes an injected :class:`LanceDBClient` (D-24) and the live embedder: the
table schema is created lazily on first use, reading ``embedder.dim`` *after*
the first real embedding has corrected any guessed dimension — never a baked
startup guess. One table per embedder id keeps model swaps clean.
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from typing import Any

from memspine.clients.lancedb import LanceDBClient
from memspine.config import constants
from memspine.observability.logging import get_logger
from memspine.services.embedding.base import EmbeddingService
from memspine.services.vector.base import VectorHit

__all__ = ["LanceDBVectorStore"]

_log = get_logger(__name__)


def _table_name(embedder_id: str) -> str:
    return "memspine_" + re.sub(r"[^a-zA-Z0-9_]", "_", embedder_id)


def _record_ids(table: Any) -> set[str]:
    """Every ``record_id`` in a table (empty for a new in-memory table)."""
    if int(table.count_rows()) == 0:
        return set()
    column = table.to_arrow().column("record_id")
    return {str(value) for value in column.to_pylist()}


class LanceDBVectorStore:
    def __init__(
        self,
        client: LanceDBClient,
        embedder: EmbeddingService,
        *,
        quantization: str | None = None,
        matryoshka_dim: int | None = None,
        oversample: int = constants.RESCORE_OVERSAMPLE,
        compact_every: int | None = None,
        exclusive: bool = False,
        namespace_index: bool = False,
        table_suffix: str = "",
    ) -> None:
        """``compact_every``: merge the table's fragments after that many
        upserts. Each single-row upsert adds a fragment, and a flat query opens
        every fragment, so without compaction each write makes the next query
        slower. Compaction keeps the rows and their order, so results do not
        change. It is skipped when quantization or Matryoshka is active, where
        it would also fold new rows into the ANN index and change which rows
        are searched exactly. ``None`` never compacts: the engine passes it for
        a table other engines may write concurrently.

        ``exclusive``: no other writer touches the table (an in-memory table is
        private to its connection). The store then tracks the ids it holds and
        appends a new id directly instead of running the merge join, which
        writes the same row in the same place. Its compaction also drops every
        table version but the latest (ADR-053): a ``memory://`` store keeps
        each version's files alive, and LanceDB's writer leaves every one of
        them holding a 5 MB upload buffer, so without the cleanup an in-memory
        table commits ~15 MB per write and never gives it back. Deletes count
        towards the same compaction cadence as upserts."""
        if quantization not in (None, "int8", "binary"):
            raise ValueError(f"unknown quantization {quantization!r} (valid: int8, binary, None)")
        self._client = client
        self._embedder = embedder
        # E4 (ADR-020 §6): quantization/Matryoshka drive whether search_rescore
        # builds a native ANN index; when both are None it is byte-identical to
        # query() (the profile="simple" guard). Matryoshka prefix truncation is
        # NOT applied at this layer — the per-embedder table stores full-dim
        # vectors and LanceDB's compressed IVF sub-index already provides the
        # "cheap prefilter → exact refine" two-stage natively; an embedder that
        # truly emits truncated vectors would present a smaller ``dim`` upstream
        # and thus a smaller table (see search_rescore).
        self._quantization = quantization
        self._matryoshka_dim = matryoshka_dim
        self._oversample = max(1, oversample)
        self._compact_every = compact_every if compact_every and compact_every > 0 else None
        self._upserts_since_compact = 0
        #: I1 (isolation review): keep a BITMAP scalar index on ``namespace`` so the
        #: per-user prefilter reads one user's rows instead of scanning the column.
        self._namespace_index = namespace_index
        #: I4: a per-namespace table is named ``<embedder table><table_suffix>``.
        self._table_suffix = table_suffix
        self._writes_since_ns_index = 0
        self._ns_index_disabled = False
        # Ids present in an exclusive table (None: not exclusive, always merge).
        self._ids: set[str] | None = set() if exclusive else None
        self._table: Any = None
        # Guards table creation and every write this store makes to the table, so
        # the erasure purge never interleaves with this process's own upserts.
        self._lock = asyncio.Lock()
        # Ids deleted since the last successful purge: their vectors may still
        # sit in older table versions and in rewritten-but-unreclaimed files.
        self._unpurged: set[str] = set()
        # ANN index lifecycle (built lazily on first active search_rescore).
        self._index_lock = asyncio.Lock()
        self._index_ready = False  # a usable vector ANN index is confirmed present
        self._index_disabled = False  # sticky: a real create failure → flat forever
        self._deferred_logged = False  # one-shot log while below the row threshold

    @property
    def _rescore_active(self) -> bool:
        """The native two-stage path is live only when quantization or Matryoshka
        was declared; otherwise search_rescore is exactly query()."""
        return self._quantization is not None or self._matryoshka_dim is not None

    async def _ensure_table(self) -> Any:
        if self._table is None:
            async with self._lock:
                if self._table is None:
                    import pyarrow as pa

                    db = self._client.db
                    # Read dim NOW (post-first-embed) — not a startup guess.
                    schema = pa.schema(
                        [
                            pa.field("record_id", pa.string()),
                            pa.field("namespace", pa.string()),
                            pa.field("vector", pa.list_(pa.float32(), self._embedder.dim)),
                        ]
                    )
                    name = _table_name(self._embedder.embedder_id) + self._table_suffix
                    # ``exist_ok=True`` makes create-or-open one atomic call on
                    # LanceDB's side: a check-then-act (table_names() then
                    # create_table()) races two engines opening the same file
                    # concurrently (D-45/D0.1 concurrent-engines contract) —
                    # both can observe "absent" and both call create_table,
                    # and the loser raises "Table already exists" instead of
                    # just opening it.
                    table = await asyncio.to_thread(
                        db.create_table, name, schema=schema, exist_ok=True
                    )
                    if self._ids is not None:
                        self._ids = await asyncio.to_thread(_record_ids, table)
                    self._table = table
        return self._table

    async def upsert(
        self, record_id: str, namespace: str, embedder_id: str, vector: list[float]
    ) -> None:
        table = await self._ensure_table()
        row = [{"record_id": record_id, "namespace": namespace, "vector": vector}]
        async with self._lock:
            if self._ids is not None and record_id not in self._ids:
                # Exclusive table, unseen id: the merge would take its insert branch.
                await asyncio.to_thread(table.add, row)
                self._ids.add(record_id)
            else:
                await asyncio.to_thread(
                    lambda: (
                        table.merge_insert("record_id")
                        .when_matched_update_all()
                        .when_not_matched_insert_all()
                        .execute(row)
                    )
                )
            await self._maybe_compact(table)
            await self._maybe_index_namespace(table)

    async def _maybe_index_namespace(self, table: Any) -> None:
        """I1: (re)build the ``namespace`` BITMAP index every
        ``NAMESPACE_INDEX_EVERY`` writes once the table holds at least
        ``NAMESPACE_INDEX_MIN_ROWS`` rows. Rows written since the last build are
        still found: LanceDB scans the unindexed tail. An optimisation only: a
        failure is logged, never raised, and disables further attempts."""
        if not self._namespace_index or self._ns_index_disabled:
            return
        self._writes_since_ns_index += 1
        if self._writes_since_ns_index < constants.NAMESPACE_INDEX_EVERY:
            return
        self._writes_since_ns_index = 0
        try:
            rows = int(await asyncio.to_thread(table.count_rows))
            if rows < constants.NAMESPACE_INDEX_MIN_ROWS:
                return
            await asyncio.to_thread(
                lambda: table.create_scalar_index("namespace", index_type="BITMAP", replace=True)
            )
        except Exception as exc:
            self._ns_index_disabled = True
            _log.warning("vector.lance_namespace_index_failed", error=str(exc))

    async def _maybe_compact(self, table: Any) -> None:
        """Merge fragments every ``compact_every`` writes (see ``__init__``).

        An exclusive table also removes its older versions in the same call."""
        if self._compact_every is None or self._rescore_active:
            return
        self._upserts_since_compact += 1
        if self._upserts_since_compact < self._compact_every:
            return
        self._upserts_since_compact = 0
        cleanup = timedelta(0) if self._ids is not None else None
        try:
            await asyncio.to_thread(lambda: table.optimize(cleanup_older_than=cleanup))
        except Exception as exc:
            # Compaction is an optimisation only: a failure leaves the rows
            # exactly as they were, so it must never fail the write.
            _log.warning("vector.lance_compact_failed", error=str(exc))

    async def query(
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[VectorHit]:
        # The table is per-embedder (name derives from embedder_id), so the
        # embedder scoping the port requires is structural here.
        table = await self._ensure_table()

        def _search() -> list[dict[str, Any]]:
            # namespace grammar (core.namespace) admits no quotes — safe filter.
            result: list[dict[str, Any]] = (
                table.search(vector)
                .where(f"namespace = '{namespace}'", prefilter=True)
                .metric("cosine")
                .limit(top_k)
                .to_list()
            )
            return result

        rows = await asyncio.to_thread(_search)
        # LanceDB returns cosine _distance_; similarity = 1 - distance.
        return [
            VectorHit(record_id=row["record_id"], score=1.0 - float(row["_distance"]))
            for row in rows
        ]

    async def query_many(
        self, namespace: str, vectors: list[list[float]], embedder_id: str, top_k: int = 8
    ) -> list[list[VectorHit]]:
        """#63: :meth:`query` for many vectors in one flat scan.

        LanceDB runs a multi-vector search as one pass, tagging each row with its
        ``query_index``; the hits of each query are returned in that query's slot,
        best first, exactly as :meth:`query` ranks them."""
        if not vectors:
            return []
        table = await self._ensure_table()

        def _search() -> list[dict[str, Any]]:
            # namespace grammar (core.namespace) admits no quotes — safe filter.
            result: list[dict[str, Any]] = (
                table.search(vectors if len(vectors) > 1 else vectors[0])
                .where(f"namespace = '{namespace}'", prefilter=True)
                .metric("cosine")
                .limit(top_k)
                .to_list()
            )
            return result

        rows = await asyncio.to_thread(_search)
        out: list[list[VectorHit]] = [[] for _ in vectors]
        for row in rows:
            index = int(row.get("query_index") or 0)
            out[index].append(
                VectorHit(record_id=row["record_id"], score=1.0 - float(row["_distance"]))
            )
        for hits in out:
            hits.sort(key=lambda hit: -hit.score)  # stable: LanceDB order kept on ties
        return out

    def _create_index(self, table: Any) -> None:
        """Build the native ANN index whose compressed sub-index realizes E4.

        int8 → IVF_HNSW_SQ (scalar quantization: each dim to int8, HNSW graph
        over the IVF cells); binary / Matryoshka-only → IVF_PQ (product
        quantization). Both are queried with ``nprobes`` + ``refine_factor`` so
        the search hits the compressed index then re-ranks the oversampled window
        by exact vector distance — LanceDB's native two-stage rescore. The
        distance type matches the ``cosine`` metric used by query()/search."""
        from lancedb.index import IvfHnswSq, IvfPq

        if self._quantization == "int8":
            table.create_index("vector", config=IvfHnswSq(distance_type="cosine"))
        else:
            table.create_index("vector", config=IvfPq(distance_type="cosine"))

    async def _ensure_index(self, table: Any) -> bool:
        """Lazily ensure a queryable ANN index exists; return whether one is
        usable. Created once when the corpus clears ``LANCE_ANN_MIN_ROWS`` (IVF/PQ
        k-means training needs that many rows), reused thereafter — never rebuilt
        per query. Below the threshold: no index, skip-log once, return False so
        the caller runs a flat exact query (retried as the corpus grows). A real
        ``create_index`` failure sticky-disables the ANN path for the process."""
        if self._index_ready:
            return True
        if self._index_disabled:
            return False
        async with self._index_lock:
            if self._index_ready:
                return True
            if self._index_disabled:
                return False

            def _existing_vector_index() -> bool:
                # A persisted table may already carry the index from a prior run.
                indices = table.list_indices()
                return any("vector" in getattr(idx, "columns", []) for idx in indices)

            if await asyncio.to_thread(_existing_vector_index):
                self._index_ready = True
                return True

            rows = await asyncio.to_thread(table.count_rows)
            if rows < constants.LANCE_ANN_MIN_ROWS:
                if not self._deferred_logged:
                    _log.info(
                        "vector.lance_index_deferred",
                        rows=rows,
                        min_rows=constants.LANCE_ANN_MIN_ROWS,
                        detail="too few rows to train an ANN index — flat exact query",
                    )
                    self._deferred_logged = True
                return False
            try:
                await asyncio.to_thread(self._create_index, table)
            except Exception as exc:
                # Sticky-disable (ADR-020 §5 precedent): a genuine training/build
                # failure won't fix itself, so stop retrying an expensive create
                # per query and degrade to flat exact search for the process.
                _log.warning("vector.lance_index_failed", error=str(exc))
                self._index_disabled = True
                return False
            self._index_ready = True
            return True

    async def search_rescore(
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[VectorHit]:
        """E4 (ADR-020 §6): LanceDB owns quantization natively, so the two-stage
        "quantized prefilter → exact rescore" is realized by its ANN index rather
        than the SQLite store's pure-Python codes. When quantization/Matryoshka is
        active this ensures a compressed IVF index exists (IVF_HNSW_SQ for int8,
        IVF_PQ otherwise) and queries it with ``nprobes`` + ``refine_factor =
        RESCORE_OVERSAMPLE`` — searching the compressed index then re-ranking the
        oversampled candidate window by exact vector distance. Below the index's
        row threshold (or after a build failure) it degrades to a flat exact
        query, skip-logged once. When inactive it is exactly query() — the
        byte-identical guard that keeps ``profile="simple"`` unperturbed."""
        if not self._rescore_active:
            return await self.query(namespace, vector, embedder_id, top_k)
        table = await self._ensure_table()
        if not await self._ensure_index(table):
            return await self.query(namespace, vector, embedder_id, top_k)

        def _search() -> list[dict[str, Any]]:
            # namespace grammar (core.namespace) admits no quotes — safe filter.
            result: list[dict[str, Any]] = (
                table.search(vector)
                .where(f"namespace = '{namespace}'", prefilter=True)
                .metric("cosine")
                .nprobes(constants.LANCE_NPROBES)
                .refine_factor(self._oversample)
                .limit(top_k)
                .to_list()
            )
            return result

        rows = await asyncio.to_thread(_search)
        # Same scoring as query(): LanceDB returns cosine _distance_; sim = 1 - d.
        return [
            VectorHit(record_id=row["record_id"], score=1.0 - float(row["_distance"]))
            for row in rows
        ]

    async def delete(self, record_id: str) -> None:
        table = await self._ensure_table()
        escaped = record_id.replace("'", "''")
        async with self._lock:
            await asyncio.to_thread(table.delete, f"record_id = '{escaped}'")
            self._unpurged.add(record_id)
            if self._ids is not None:
                self._ids.discard(record_id)
            await self._maybe_compact(table)
            await self._maybe_index_namespace(table)

    async def delete_all(self) -> None:
        table = await self._ensure_table()
        async with self._lock:
            await asyncio.to_thread(table.delete, "record_id IS NOT NULL")
            if self._ids is not None:
                self._ids.clear()

    async def purge_deleted(self) -> bool:
        """M7 erasure: physically remove the vectors of deleted rows.

        A LanceDB delete only writes a deletion file: the row's bytes stay in its
        data file, and every older table version still returns it on checkout.
        This rewrites every live row (a no-op update, so each old fragment ends
        fully deleted and is dropped) and then removes every version but the
        latest with the files only they referenced. Rows, ids and vectors are
        unchanged; the cost is one pass over the table.

        Runs under the store's write lock, so no write of this process
        interleaves. Another engine writing the same file-backed table can make
        the rewrite fail on a commit conflict; files of its in-flight commit are
        kept (``delete_unverified`` stays off). Returns False when the purge did
        not complete; :meth:`history_absent` then keeps reporting residue."""
        if not self._unpurged:
            return True
        table = await self._ensure_table()
        async with self._lock:
            pending = set(self._unpurged)

            def _purge() -> None:
                if int(table.count_rows()) > 0:
                    table.update(
                        where="record_id IS NOT NULL", values_sql={"namespace": "namespace"}
                    )
                table.optimize(cleanup_older_than=timedelta(0))

            try:
                await asyncio.to_thread(_purge)
            except Exception as exc:
                _log.warning("vector.lance_purge_failed", error=str(exc), pending=len(pending))
                return False
            self._unpurged -= pending
        return True

    async def history_absent(self, record_id: str) -> bool | None:
        """M7 ``forget --verify``: True when no retained table version holds a row
        for ``record_id``; False when one does, or when this process deleted the
        row and has not purged since; None when there are too many versions to
        scan (``LANCE_VERIFY_MAX_VERSIONS``), which the engine reports unproven.

        Not covered: a deleted row whose pre-delete versions were removed by a
        routine cleanup in an earlier process without a purge; its bytes can
        stay in a data file until compaction rewrites that fragment."""
        if record_id in self._unpurged:
            return False
        await self._ensure_table()
        escaped = record_id.replace("'", "''")
        name = _table_name(self._embedder.embedder_id) + self._table_suffix

        def _scan() -> bool | None:
            # A separate handle: checkout() would pin the shared one to the past.
            handle = self._client.db.open_table(name)
            versions = handle.list_versions()
            if len(versions) > constants.LANCE_VERIFY_MAX_VERSIONS:
                return None
            for version in versions:
                handle.checkout(version["version"])
                if int(handle.count_rows(f"record_id = '{escaped}'")) > 0:
                    return False
            return True

        return await asyncio.to_thread(_scan)

    async def exists(self, record_id: str) -> bool:
        """M7 ``forget --verify`` support: is a row still present? Without this
        the erasure verifier cannot prove the default backend is clean."""
        table = await self._ensure_table()
        escaped = record_id.replace("'", "''")

        def _count() -> int:
            return int(table.count_rows(f"record_id = '{escaped}'"))

        return await asyncio.to_thread(_count) > 0
