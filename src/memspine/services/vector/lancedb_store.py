"""LanceDB vector store — the scalable default (D-09), behind ``[lance]``.

Consumes an injected :class:`LanceDBClient` (D-24) and the live embedder: the
table schema is created lazily on first use, reading ``embedder.dim`` *after*
the first real embedding has corrected any guessed dimension — never a baked
startup guess. One table per embedder id keeps model swaps clean.
"""

from __future__ import annotations

import asyncio
import re
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


class LanceDBVectorStore:
    def __init__(
        self,
        client: LanceDBClient,
        embedder: EmbeddingService,
        *,
        quantization: str | None = None,
        matryoshka_dim: int | None = None,
        oversample: int = constants.RESCORE_OVERSAMPLE,
    ) -> None:
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
        self._table: Any = None
        self._lock = asyncio.Lock()
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
                    name = _table_name(self._embedder.embedder_id)
                    existing = await asyncio.to_thread(db.table_names)
                    if name in existing:
                        self._table = await asyncio.to_thread(db.open_table, name)
                    else:
                        self._table = await asyncio.to_thread(db.create_table, name, schema=schema)
        return self._table

    async def upsert(
        self, record_id: str, namespace: str, embedder_id: str, vector: list[float]
    ) -> None:
        table = await self._ensure_table()
        await asyncio.to_thread(
            lambda: (
                table.merge_insert("record_id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute([{"record_id": record_id, "namespace": namespace, "vector": vector}])
            )
        )

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
        await asyncio.to_thread(table.delete, f"record_id = '{record_id}'")

    async def delete_all(self) -> None:
        table = await self._ensure_table()
        await asyncio.to_thread(table.delete, "record_id IS NOT NULL")

    async def exists(self, record_id: str) -> bool:
        """M7 ``forget --verify`` support: is a row still present? Without this
        the erasure verifier cannot prove the default backend is clean."""
        table = await self._ensure_table()
        escaped = record_id.replace("'", "''")

        def _count() -> int:
            return int(table.count_rows(f"record_id = '{escaped}'"))

        return await asyncio.to_thread(_count) > 0
