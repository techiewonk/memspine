"""LanceDB vector store — the scalable default (D-09), behind ``[lance]``.

Import-guarded: constructing it without the extra raises
:class:`MissingServiceError` naming the fix (D-10). One table per embedder id
keeps model swaps clean (a new embedder writes a new table).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from memspine.exceptions import MissingServiceError
from memspine.services.vector.base import VectorHit

__all__ = ["LanceDBVectorStore"]


def _table_name(embedder_id: str) -> str:
    return "memspine_" + re.sub(r"[^a-zA-Z0-9_]", "_", embedder_id)


class LanceDBVectorStore:
    def __init__(self, path: str | Path, embedder_id: str, dim: int) -> None:
        try:
            import lancedb  # noqa: F401
        except ImportError as exc:
            raise MissingServiceError("vector:lancedb", extra="lance") from exc
        self._path = str(path)
        self._embedder_id = embedder_id
        self._dim = dim
        self._db: Any = None
        self._table: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_table(self) -> Any:
        if self._table is None:
            async with self._lock:
                if self._table is None:
                    import lancedb
                    import pyarrow as pa

                    self._db = await asyncio.to_thread(lancedb.connect, self._path)
                    schema = pa.schema(
                        [
                            pa.field("record_id", pa.string()),
                            pa.field("namespace", pa.string()),
                            pa.field("vector", pa.list_(pa.float32(), self._dim)),
                        ]
                    )
                    name = _table_name(self._embedder_id)
                    existing = await asyncio.to_thread(self._db.table_names)
                    if name in existing:
                        self._table = await asyncio.to_thread(self._db.open_table, name)
                    else:
                        self._table = await asyncio.to_thread(
                            self._db.create_table, name, schema=schema
                        )
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

    async def query(self, namespace: str, vector: list[float], top_k: int = 8) -> list[VectorHit]:
        table = await self._ensure_table()

        def _search() -> list[dict[str, Any]]:
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
