"""Zero-dep vector fallback: float32 blobs in SQLite, brute-force cosine.

Correct and dependency-free for the ``simple`` profile and tests; LanceDB
(``[lance]``) is the scalable default (D-09). Vectors are L2-normalized at
upsert so query-time scoring is a dot product.
"""

from __future__ import annotations

import math
import struct

from sqlalchemy import delete, select

from memspine.clients.sqlite import SQLiteClient
from memspine.services.storage.sqlite.schema import memory_embeddings
from memspine.services.vector.base import VectorHit

__all__ = ["SQLiteVectorStore"]


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(raw: bytes) -> list[float]:
    count = len(raw) // 4
    return list(struct.unpack(f"<{count}f", raw))


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:
        return vector
    return [component / norm for component in vector]


class SQLiteVectorStore:
    def __init__(self, client: SQLiteClient) -> None:
        self._client = client

    async def upsert(
        self, record_id: str, namespace: str, embedder_id: str, vector: list[float]
    ) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        normalized = _normalize(vector)
        values = {
            "namespace": namespace,
            "embedder_id": embedder_id,
            "dim": len(normalized),
            "vector": _pack(normalized),
        }
        stmt = sqlite_insert(memory_embeddings).values(record_id=record_id, **values)
        stmt = stmt.on_conflict_do_update(index_elements=["record_id"], set_=values)
        async with self._client.engine.begin() as conn:
            await conn.execute(stmt)

    async def query(self, namespace: str, vector: list[float], top_k: int = 8) -> list[VectorHit]:
        query_vec = _normalize(vector)
        stmt = select(memory_embeddings.c.record_id, memory_embeddings.c.vector).where(
            memory_embeddings.c.namespace == namespace
        )
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        hits = [
            VectorHit(
                record_id=row[0],
                score=sum(a * b for a, b in zip(query_vec, _unpack(row[1]), strict=False)),
            )
            for row in rows
        ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:top_k]

    async def delete(self, record_id: str) -> None:
        async with self._client.engine.begin() as conn:
            await conn.execute(
                delete(memory_embeddings).where(memory_embeddings.c.record_id == record_id)
            )

    async def delete_all(self) -> None:
        async with self._client.engine.begin() as conn:
            await conn.execute(delete(memory_embeddings))
