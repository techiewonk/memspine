"""PostgreSQL pgvector adapter for the :class:`VectorStore` port (RM-113 item 11).

The adapter receives the opened Postgres connector (``clients/postgres``) that the records
slot holds, plus a :class:`PgVectorProjection` (its owned table name, the embedding identity
and the index and provisioning settings). It owns the projection's DDL:
:meth:`PgVectorStore.ensure_table` (run by :meth:`PgVectorStore.start`) checks the ``vector``
extension, creates it only with the explicit ``create_extension`` opt-in, creates the schema,
table and indexes only with the explicit ``provision_projection`` opt-in, and otherwise
validates that the operator-provisioned table matches the embedding schema. Queries use
PostgreSQL's cosine distance operator and always apply namespace and embedder filters in the
same statement. The connection pool belongs to the connector; the adapter never opens or
closes it.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import Column, Index, MetaData, String, Table, delete, func, inspect, select, text
from sqlalchemy.dialects.postgresql import insert

from memspine.exceptions import ConfigError, MissingServiceError
from memspine.services.vector.base import VectorHit

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from memspine.clients.postgres import PostgresClient

__all__ = ["PROJECTION_SCHEMA", "PgVectorProjection", "PgVectorStore"]

#: The PostgreSQL schema that holds every owned pgvector projection table.
PROJECTION_SCHEMA = "memspine_vectors"


@dataclass(frozen=True, slots=True)
class PgVectorProjection:
    """One owned projection table: its name (scoped by owner, generation and embedding space),
    the vector dimension, the index settings and the two DDL opt-ins."""

    table_name: str
    dimension: int
    index: Literal["hnsw", "none"] = "hnsw"
    hnsw_m: int = 16
    hnsw_ef_construction: int = 64
    hnsw_ef_search: int = 40
    namespace_index: bool = True
    provision_projection: bool = False
    create_extension: bool = False

    def __post_init__(self) -> None:
        if self.dimension < 1:
            raise ValueError("pgvector requires a positive dimension")
        if self.create_extension and not self.provision_projection:
            raise ConfigError(
                "storage.vector.settings.create_extension requires explicit "
                "storage.vector.settings.provision_projection=true"
            )

    @property
    def use_hnsw(self) -> bool:
        return self.index == "hnsw"


def _vector_type() -> Any:
    try:
        from pgvector.sqlalchemy import VECTOR
    except ImportError:
        raise MissingServiceError("vector:pgvector", "postgres") from None
    return VECTOR


def projection_table(projection: PgVectorProjection) -> Table:
    """The SQLAlchemy definition of ``projection``'s table and indexes (no I/O)."""
    vector = _vector_type()
    name = projection.table_name
    table = Table(
        name,
        MetaData(schema=PROJECTION_SCHEMA),
        Column("record_id", String(512), primary_key=True),
        Column("namespace", String(512), nullable=False),
        Column("embedder_id", String(512), nullable=False),
        Column("embedding", vector(projection.dimension), nullable=False),
    )
    if projection.namespace_index:
        Index(f"ns_{name}", table.c.namespace)
    if projection.use_hnsw:
        Index(
            f"hnsw_{name}",
            table.c.embedding,
            postgresql_using="hnsw",
            postgresql_with={
                "m": projection.hnsw_m,
                "ef_construction": projection.hnsw_ef_construction,
            },
            postgresql_ops={"embedding": "vector_cosine_ops"},
        )
    return table


class PgVectorStore:
    """A generation and embedder scoped pgvector table over the lent Postgres connector."""

    def __init__(
        self,
        connector: PostgresClient,
        projection: PgVectorProjection,
        *,
        embedder_id: str,
    ) -> None:
        if not embedder_id:
            raise ValueError("pgvector requires an embedder identity")
        self._connector = connector
        self._projection = projection
        self._table = projection_table(projection)
        self._embedder_id = embedder_id
        self._dimension = projection.dimension
        self._hnsw_ef_search = projection.hnsw_ef_search
        self._use_hnsw = projection.use_hnsw

    # -- lifecycle -------------------------------------------------------------------------
    async def start(self) -> None:
        """Prepare the projection (:meth:`ensure_table`)."""
        await self.ensure_table()

    async def ensure_table(self) -> None:
        """Check the extension and provision or validate the owned table (module docstring).

        Raises :class:`ConfigError` when the extension is missing without the opt-in, when
        the table is missing without ``provision_projection``, or when an existing table does
        not match the embedding schema."""
        projection = self._projection
        async with self._connector.engine.begin() as conn:
            has_extension = bool(
                await conn.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
                )
            )
            if not has_extension and projection.create_extension:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                has_extension = True
            if not has_extension:
                raise ConfigError(
                    "the selected PostgreSQL database does not have the pgvector extension; "
                    "install it or explicitly set storage.vector.settings.create_extension=true"
                )
            if projection.provision_projection:
                await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {PROJECTION_SCHEMA}"))
        async with self._connector.engine.begin() as conn:
            if projection.provision_projection:
                await conn.run_sync(self._table.metadata.create_all)
                return
            await conn.run_sync(self._validate_existing)

    def _validate_existing(self, connection: Any) -> None:
        vector = _vector_type()
        name = self._projection.table_name
        inspector = inspect(connection)
        if not inspector.has_table(name, schema=PROJECTION_SCHEMA):
            raise ConfigError(
                "the owned pgvector projection table is missing; explicitly set "
                "storage.vector.settings.provision_projection=true during operator provisioning"
            )
        columns = inspector.get_columns(name, schema=PROJECTION_SCHEMA)
        by_name = {str(column["name"]): column["type"] for column in columns}
        embedding_type = by_name.get("embedding")
        if (
            not {"record_id", "namespace", "embedder_id", "embedding"}.issubset(by_name)
            or not isinstance(embedding_type, vector)
            or embedding_type.dim != self._dimension
        ):
            raise ConfigError(
                "the existing pgvector projection table does not match the selected "
                "embedding schema"
            )

    async def probe(self) -> bool:
        """A zero-row read of the owned table (the provider health probe)."""
        statement = select(self._table.c.record_id).limit(0)

        async def run(conn: AsyncConnection) -> bool:
            return (await conn.execute(statement)).returns_rows

        return await self._execute(run)

    # -- port ------------------------------------------------------------------------------
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
        await self._run(statement)

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
            .where(table.c.namespace == namespace, table.c.embedder_id == self._embedder_id)
            .order_by(distance.asc(), table.c.record_id.asc())
            .limit(top_k)
        )

        async def run(conn: AsyncConnection) -> list[VectorHit]:
            await self._set_ef_search(conn)
            rows = (await conn.execute(statement)).all()
            return [VectorHit(str(row.record_id), 1.0 - float(row.distance)) for row in rows]

        return await self._execute(run)

    async def search_rescore(
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[VectorHit]:
        """pgvector ranks by native cosine distance; no separate quantized stage is configured."""
        return await self.query(namespace, vector, embedder_id, top_k)

    async def query_many(
        self, namespace: str, vectors: list[list[float]], embedder_id: str, top_k: int = 8
    ) -> list[list[VectorHit]]:
        return [await self.query(namespace, vector, embedder_id, top_k) for vector in vectors]

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
            table.c.embedder_id == self._embedder_id,
            table.c.record_id.in_(record_ids),
        )

        async def run(conn: AsyncConnection) -> dict[str, float]:
            await self._set_ef_search(conn)
            return {
                str(row.record_id): 1.0 - float(row.distance)
                for row in (await conn.execute(statement)).all()
            }

        return await self._execute(run)

    async def delete(self, record_id: str) -> None:
        await self._run(delete(self._table).where(self._table.c.record_id == record_id))

    async def delete_all(self) -> None:
        await self._run(delete(self._table))

    async def drop_namespace(self, namespace: str) -> None:
        await self._run(delete(self._table).where(self._table.c.namespace == namespace))

    async def exists(self, record_id: str) -> bool:
        statement = (
            select(self._table.c.record_id).where(self._table.c.record_id == record_id).limit(1)
        )

        async def run(conn: AsyncConnection) -> bool:
            return (await conn.execute(statement)).first() is not None

        return await self._execute(run)

    async def history_absent(self, record_id: str) -> bool | None:
        """Prove a current row remains, but never claim physical history is absent.

        PostgreSQL MVCC, WAL, replicas and backups may retain deleted tuple data;
        this adapter does not own a safe scoped purge for those stores.
        """
        return False if await self.exists(record_id) else None

    async def purge_deleted(self) -> bool:
        """Physical erasure is not proven by DELETE or a no-op SQL query."""
        return False

    # -- internals -------------------------------------------------------------------------
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

    async def _set_ef_search(self, conn: AsyncConnection) -> None:
        if self._use_hnsw:
            # Transaction-local (``is_local``): the setting ends with this unit of work.
            await conn.execute(
                select(func.set_config("hnsw.ef_search", str(self._hnsw_ef_search), True))
            )

    async def _run(self, statement: Any) -> None:
        async def run(conn: AsyncConnection) -> None:
            await conn.execute(statement)

        await self._execute(run)

    async def _execute[T](self, action: Callable[[AsyncConnection], Awaitable[T]]) -> T:
        async with self._connector.engine.begin() as connection:
            return await action(connection)
