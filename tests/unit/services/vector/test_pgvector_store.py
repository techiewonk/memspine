"""RM-113 item 11: the pgvector adapter owns its DDL and takes the Postgres connector.

The ``pgvector`` package is optional (``[postgres]``) and the PostgreSQL service is not
available to unit tests, so ``pgvector.sqlalchemy`` is stubbed with a minimal ``VECTOR`` type
and the connector with a recording fake; the real round trip is
``tests/integration/providers/test_pgvector_contract.py`` (``MEMSPINE_TEST_LIVE_POSTGRES_URL``).
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Float
from sqlalchemy.types import UserDefinedType

from memspine.exceptions import ConfigError
from memspine.services.vector.pgvector_store import (
    PROJECTION_SCHEMA,
    PgVectorProjection,
    PgVectorStore,
)


class _Vector(UserDefinedType[Any]):
    cache_ok = True

    def __init__(self, dim: int | None = None) -> None:
        self.dim = dim

    def get_col_spec(self, **_: Any) -> str:
        return f"VECTOR({self.dim})"

    class comparator_factory(UserDefinedType.Comparator[Any]):
        def cosine_distance(self, other: Any) -> Any:
            return self.op("<=>", return_type=Float())(other)


@pytest.fixture(autouse=True)
def _pgvector_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    package = ModuleType("pgvector")
    package.__path__ = []  # type: ignore[attr-defined]
    module = ModuleType("pgvector.sqlalchemy")
    module.VECTOR = _Vector  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pgvector", package)
    monkeypatch.setitem(sys.modules, "pgvector.sqlalchemy", module)


class _Conn:
    def __init__(self, db: _Db) -> None:
        self._db = db

    async def scalar(self, statement: Any) -> bool:
        self._db.statements.append(str(statement))
        return self._db.has_extension

    async def execute(self, statement: Any) -> Any:
        sql = str(statement)
        self._db.statements.append(sql)
        if "CREATE EXTENSION" in sql:
            self._db.has_extension = True
        return SimpleNamespace(returns_rows=True, all=list, first=lambda: None)

    async def run_sync(self, fn: Callable[[Any], Any]) -> Any:
        name = getattr(fn, "__name__", repr(fn))
        self._db.statements.append(f"run_sync:{name}")
        if name == "_validate_existing" and not self._db.table_exists:
            raise ConfigError("the owned pgvector projection table is missing")
        return None


class _Db:
    def __init__(self, *, has_extension: bool, table_exists: bool = False) -> None:
        self.has_extension = has_extension
        self.table_exists = table_exists
        self.statements: list[str] = []
        self.transactions = 0


class _Connector:
    """The PostgresClient surface the adapter uses: ``engine.begin()``; never closed."""

    def __init__(self, db: _Db) -> None:
        self._db = db
        self.closed = False
        connector = self

        class _Engine:
            @asynccontextmanager
            async def begin(self) -> AsyncIterator[_Conn]:
                connector._db.transactions += 1
                yield _Conn(connector._db)

        self.engine = _Engine()

    async def close(self) -> None:  # pragma: no cover - the adapter must never call it
        self.closed = True


def _store(db: _Db, **projection: Any) -> PgVectorStore:
    return PgVectorStore(
        _Connector(db),  # type: ignore[arg-type]
        PgVectorProjection(table_name="vectors_abc", dimension=4, **projection),
        embedder_id="hash:4",
    )


async def test_missing_extension_without_opt_in_fails_before_any_ddl() -> None:
    db = _Db(has_extension=False)
    with pytest.raises(ConfigError, match="pgvector extension"):
        await _store(db).start()
    assert not [s for s in db.statements if s.startswith(("CREATE", "run_sync"))]


async def test_create_extension_requires_provision_projection() -> None:
    with pytest.raises(ConfigError, match="provision_projection"):
        PgVectorProjection(table_name="t", dimension=4, create_extension=True)


async def test_provisioning_opt_ins_create_extension_schema_and_table() -> None:
    db = _Db(has_extension=False)
    store = _store(db, provision_projection=True, create_extension=True)
    await store.start()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in db.statements
    assert f"CREATE SCHEMA IF NOT EXISTS {PROJECTION_SCHEMA}" in db.statements
    assert "run_sync:create_all" in db.statements
    assert "run_sync:_validate_existing" not in db.statements
    table = store._table
    assert table.schema == PROJECTION_SCHEMA and table.name == "vectors_abc"
    assert {index.name for index in table.indexes} == {"ns_vectors_abc", "hnsw_vectors_abc"}


async def test_without_provisioning_the_existing_table_is_validated_not_created() -> None:
    db = _Db(has_extension=True, table_exists=True)
    store = _store(db, index="none", namespace_index=False)
    await store.start()
    assert "run_sync:_validate_existing" in db.statements
    assert not [s for s in db.statements if s.startswith("CREATE")]
    assert "run_sync:create_all" not in db.statements
    assert not store._table.indexes
    with pytest.raises(ConfigError, match="missing"):
        await _store(_Db(has_extension=True, table_exists=False)).start()


async def test_queries_run_on_the_lent_connector_and_never_close_it() -> None:
    db = _Db(has_extension=True, table_exists=True)
    store = _store(db)
    connector = store._connector
    await store.start()
    before = db.transactions
    assert await store.query("ns", [1.0, 0.0, 0.0, 0.0], "hash:4") == []
    assert await store.probe()
    await store.delete_all()
    assert db.transactions == before + 3
    assert any("hnsw.ef_search" in s or "set_config" in s for s in db.statements)
    assert not connector.closed  # type: ignore[attr-defined]


async def test_factory_lends_the_storage_connector_and_runs_ensure_table() -> None:
    from memspine.config.schema.providers import PgvectorSettings
    from memspine.providers.builtin.pgvector import PgvectorBuilder

    db = _Db(has_extension=False)
    connector = _Connector(db)
    borrowed: list[str] = []

    class Resources:
        def borrow(self, resource_id: str) -> Any:
            borrowed.append(resource_id)
            return SimpleNamespace(resource_id=resource_id)

    ctx = SimpleNamespace(
        sql=SimpleNamespace(capabilities={"sql.postgres"}, connector=connector),
        shared=SimpleNamespace(owner_id="deployment", resources=Resources()),
        embedding=SimpleNamespace(embedder_id="hash:4", dim=4),
        versioned_spaces=False,
        quantization=None,
        matryoshka_dim=None,
        generation_id="active",
        profile_meta=None,
    )
    built = await PgvectorBuilder().build(
        PgvectorSettings(provision_projection=True, create_extension=True),
        ctx,  # type: ignore[arg-type]
    )
    assert built.service._connector is connector
    assert borrowed == ["sql"]  # no private sync lease or engine any more
    assert [ref.resource_id for ref in built.resources] == ["sql"]
    assert "run_sync:create_all" in db.statements
    assert built.health_probe is not None and await built.health_probe()


async def test_factory_without_a_connector_fails_clearly() -> None:
    from memspine.config.schema.providers import PgvectorSettings
    from memspine.providers.builtin.pgvector import PgvectorBuilder

    ctx = SimpleNamespace(
        sql=SimpleNamespace(capabilities={"sql.postgres"}),
        shared=SimpleNamespace(owner_id="deployment"),
        embedding=SimpleNamespace(embedder_id="hash:4", dim=4),
        versioned_spaces=False,
        quantization=None,
        matryoshka_dim=None,
    )
    with pytest.raises(ConfigError, match="Postgres connector"):
        await PgvectorBuilder().build(PgvectorSettings(), ctx)  # type: ignore[arg-type]
