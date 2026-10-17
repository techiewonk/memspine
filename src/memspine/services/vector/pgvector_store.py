"""PostgreSQL pgvector adapter for the :class:`VectorStore` port.

The adapter receives a provider-owned table definition and the selected
embedding identity. It uses PostgreSQL's cosine distance operator and always
applies namespace and embedder filters in the same query. The lifecycle and SQL
engine remain owned by the storage provider.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from sqlalchemy import Table, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from memspine.exceptions import ConfigError
from memspine.providers.async_utils import run_blocking_drained
from memspine.services.vector.base import VectorHit

__all__ = ["PgVectorStore"]


class PgVectorStore:
    """A generation and embedder scoped pgvector table."""

    def __init__(
        self,
        engine: Engine,
        table: Table,
        *,
        embedder_id: str,
        dimension: int,
        hnsw_ef_search: int,
        use_hnsw: bool,
    ) -> None:
        if not embedder_id or dimension < 1:
            raise ValueError("pgvector requires an embedder identity and positive dimension")
        self._engine = engine
        self._table = table
        self._embedder_id = embedder_id
        self._dimension = dimension
        self._hnsw_ef_search = hnsw_ef_search
        self._use_hnsw = use_hnsw

    async def upsert(
        self, record_id: str, namespace: str, embedder_id: str, vector: list[float]
    ) -> None:
        self._validate_write(embedder_id, vector)
        table = self._table
        statement = insert(table).values(
            record_id=record_id,
            namespace=namespace,
            embedder_id=embedder_id,
            embedding=vector,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[table.c.record_id],
            set_={
                "namespace": statement.excluded.namespace,
                "embedder_id": statement.excluded.embedder_id,
                "embedding": statement.excluded.embedding,
            },
        )
        await run_blocking_drained(self._execute, lambda conn: conn.execute(statement))

    async def query(
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[VectorHit]:
        self._validate_query(embedder_id, vector)
        if top_k <= 0:
            return []
        table = self._table
        distance = table.c.embedding.cosine_distance(vector)
        statement = (
            select(table.c.record_id, distance.label("distance"))
            .where(table.c.namespace == namespace, table.c.embedder_id == embedder_id)
            .order_by(distance.asc(), table.c.record_id.asc())
            .limit(top_k)
        )

        def run(conn: Any) -> list[VectorHit]:
            if self._use_hnsw:
                conn.execute(
                    select(func.set_config("hnsw.ef_search", str(self._hnsw_ef_search), True))
                )
            rows = conn.execute(statement).all()
            return [
                VectorHit(str(row.record_id), 1.0 - float(row.distance)) for row in rows
            ]

        return await run_blocking_drained(self._execute, run)

    async def search_rescore(
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[VectorHit]:
        """pgvector ranks by native cosine distance; no separate quantized stage is configured."""
        return await self.query(namespace, vector, embedder_id, top_k)

    async def query_many(
        self, namespace: str, vectors: list[list[float]], embedder_id: str, top_k: int = 8
    ) -> list[list[VectorHit]]:
        return [
            await self.query(namespace, vector, embedder_id, top_k) for vector in vectors
        ]

    async def scores_for(
        self, namespace: str, vector: list[float], embedder_id: str, record_ids: list[str]
    ) -> dict[str, float]:
        self._validate_query(embedder_id, vector)
        if not record_ids:
            return {}
        table = self._table
        distance = table.c.embedding.cosine_distance(vector)
        statement = select(table.c.record_id, distance.label("distance")).where(
            table.c.namespace == namespace,
            table.c.embedder_id == embedder_id,
            table.c.record_id.in_(record_ids),
        )

        def run(conn: Any) -> dict[str, float]:
            if self._use_hnsw:
                conn.execute(
                    select(func.set_config("hnsw.ef_search", str(self._hnsw_ef_search), True))
                )
            return {
                str(row.record_id): 1.0 - float(row.distance)
                for row in conn.execute(statement).all()
            }

        return await run_blocking_drained(self._execute, run)

    async def delete(self, record_id: str) -> None:
        statement = delete(self._table).where(self._table.c.record_id == record_id)
        await run_blocking_drained(self._execute, lambda conn: conn.execute(statement))

    async def delete_all(self) -> None:
        await run_blocking_drained(self._execute, lambda conn: conn.execute(delete(self._table)))

    async def drop_namespace(self, namespace: str) -> None:
        statement = delete(self._table).where(self._table.c.namespace == namespace)
        await run_blocking_drained(self._execute, lambda conn: conn.execute(statement))

    async def exists(self, record_id: str) -> bool:
        statement = select(self._table.c.record_id).where(self._table.c.record_id == record_id).limit(1)
        return await run_blocking_drained(
            self._execute, lambda conn: conn.execute(statement).first() is not None
        )

    async def history_absent(self, record_id: str) -> bool | None:
        """Prove a current row remains, but never claim physical history is absent.

        PostgreSQL MVCC, WAL, replicas and backups may retain deleted tuple data;
        this adapter does not own a safe scoped purge for those stores.
        """
        return False if await self.exists(record_id) else None

    async def purge_deleted(self) -> bool:
        """Physical erasure is not proven by DELETE or a no-op SQL query."""
        return False

    def _validate_write(self, embedder_id: str, vector: list[float]) -> None:
        self._validate_query(embedder_id, vector)

    def _validate_query(self, embedder_id: str, vector: list[float]) -> None:
        if embedder_id != self._embedder_id:
            raise ConfigError("pgvector request embedder does not match the opened vector space")
        if len(vector) != self._dimension:
            raise ConfigError(
                f"pgvector vector dimension mismatch: expected {self._dimension}, received {len(vector)}"
            )
        if any(isinstance(value, bool) or not math.isfinite(value) for value in vector):
            raise ConfigError("pgvector vectors must contain only finite numeric values")
        if not any(value != 0 for value in vector):
            raise ConfigError("pgvector cosine vectors must have nonzero magnitude")

    def _execute[T](self, action: Callable[[Any], T]) -> T:
        with self._engine.begin() as connection:
            return action(connection)
