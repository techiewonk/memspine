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
from memspine.services.embedding.base import EmbeddingService
from memspine.services.vector.base import VectorHit

__all__ = ["LanceDBVectorStore"]


def _table_name(embedder_id: str) -> str:
    return "memspine_" + re.sub(r"[^a-zA-Z0-9_]", "_", embedder_id)


class LanceDBVectorStore:
    def __init__(self, client: LanceDBClient, embedder: EmbeddingService) -> None:
        self._client = client
        self._embedder = embedder
        self._table: Any = None
        self._lock = asyncio.Lock()

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

    async def delete(self, record_id: str) -> None:
        table = await self._ensure_table()
        await asyncio.to_thread(table.delete, f"record_id = '{record_id}'")

    async def delete_all(self) -> None:
        table = await self._ensure_table()
        await asyncio.to_thread(table.delete, "record_id IS NOT NULL")
