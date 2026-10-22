"""ADR-081: ``graph.provider: auto`` resolves once and is pinned in the projector binding.

A later different resolution (an optional extra installed afterwards) is reported as a
required provider change (``wiring.provider_drift`` plus a ``provider.auto_pin_drift``
warning), never applied silently. The ``sqlite_adjacency`` name is rejected with an error
naming ``sql_adjacency`` (no alias, ADR-068), and a binding recorded under the old name is
adopted under the new one. The pin runs on SQLite and, with ``MEMSPINE_TEST_PG_URL``, on
Postgres (auto on Postgres: ladybug when installed, else ``sql_adjacency``).
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import delete, select, update
from structlog.testing import capture_logs

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError
from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.providers.projection_bindings import (
    ProjectionTargetBinding,
    SqlProjectionBindingStore,
)
from memspine.providers.registry import GRAPH_STORES, UnknownProviderError
from memspine.providers.selection import ProviderSelectionError, select_provider
from memspine.runtime.container import Wiring, _graph_descriptor, resolve_graph_pin
from memspine.services.storage.sqlite.schema import metadata, provider_projector_bindings

PG_URL = os.environ.get("MEMSPINE_TEST_PG_URL")
_REAL_FIND_SPEC = importlib.util.find_spec


@pytest.fixture
def ladybug(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, bool]]:
    """Toggle whether the ``ladybug`` package looks installed (``state["installed"]``)."""
    state = {"installed": False}

    def find_spec(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "ladybug" and not state["installed"]:
            return None
        return _REAL_FIND_SPEC(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    yield state


# ------------------------------------------------------------------------------- rename
def test_old_graph_provider_name_is_rejected_with_the_new_name() -> None:
    with pytest.raises(ProviderSelectionError, match="renamed to 'sql_adjacency'"):
        select_provider("graph", "sqlite_adjacency")
    with pytest.raises(UnknownProviderError, match="renamed to 'sql_adjacency'"):
        GRAPH_STORES.get("sqlite_adjacency")
    assert GRAPH_STORES.find("sqlite_adjacency") is None  # no alias
    assert GRAPH_STORES.get("sql_adjacency").ref.name == "sql_adjacency"


async def test_engine_refuses_the_old_graph_provider_name(tmp_path: Path) -> None:
    eng = _engine(tmp_path, graph={"provider": "sqlite_adjacency"})
    with pytest.raises(ConfigError, match="renamed to 'sql_adjacency'"):
        await eng.start()
    await eng.stop()


# ------------------------------------------------------------------------------- engine e2e
def _engine(path: Path, **overrides: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "provider": "sqlite",
            "settings": {"path": str(path / "pin.db"), "busy_timeout_s": 5.0},
        },
        embedding={"provider": "hash", "settings": {}},
        memories={"semantic": {"enabled": True}, "associative": {"enabled": True}},
        **overrides,
    )


async def _bound(path: Path) -> str | None:
    from memspine.clients.sqlite import SQLiteClient, SQLiteSpec

    client = SQLiteClient(SQLiteSpec(str(path / "pin.db")))
    await client.open()
    try:
        async with client.engine.connect() as conn:
            value = await conn.scalar(
                select(provider_projector_bindings.c.provider).where(
                    provider_projector_bindings.c.projector_name == "graph"
                )
            )
    finally:
        await client.close()
    return None if value is None else str(value)


async def test_auto_is_pinned_across_an_extra_install(
    tmp_path: Path, ladybug: dict[str, bool]
) -> None:
    first = _engine(tmp_path)
    await first.start()
    try:
        assert first.describe()["graph"] == "SqlAdjacencyGraph"
        assert first._wiring.graph_pin == "sql_adjacency"
        assert first._wiring.provider_drift == {}
    finally:
        await first.stop()
    assert await _bound(tmp_path) == "graph:sql_adjacency:v1"  # the resolution is recorded

    ladybug["installed"] = True  # the [graph] extra appears later
    second = _engine(tmp_path)
    with capture_logs() as logs:
        await second.start()
    try:
        assert second.describe()["graph"] == "SqlAdjacencyGraph"  # never switched silently
        assert second._wiring.provider_drift == {"graph": ("sql_adjacency", "ladybug")}
        drift = [e for e in logs if e["event"] == "provider.auto_pin_drift"]
        assert drift and drift[0]["log_level"] == "warning"
        assert (drift[0]["pinned"], drift[0]["resolved"]) == ("sql_adjacency", "ladybug")
    finally:
        await second.stop()
    assert await _bound(tmp_path) == "graph:sql_adjacency:v1"


async def test_explicit_provider_change_is_blocked_without_a_verified_switch(
    tmp_path: Path, ladybug: dict[str, bool]
) -> None:
    first = _engine(tmp_path)
    await first.start()
    await first.write("alpha one")
    await first.stop()
    ladybug["installed"] = True
    explicit = _engine(tmp_path, graph={"provider": "ladybug"})
    with pytest.raises(ConfigError, match="verified projection switch"):
        await explicit.start()
    await explicit.stop()


async def test_a_binding_recorded_under_the_old_name_is_adopted(
    tmp_path: Path, ladybug: dict[str, bool]
) -> None:
    first = _engine(tmp_path)
    await first.start()
    await first.stop()
    from memspine.clients.sqlite import SQLiteClient, SQLiteSpec

    client = SQLiteClient(SQLiteSpec(str(tmp_path / "pin.db")))
    await client.open()
    async with client.engine.begin() as conn:  # what a pre-rename database holds
        await conn.execute(
            update(provider_projector_bindings)
            .where(provider_projector_bindings.c.projector_name == "graph")
            .values(provider="graph:sqlite_adjacency:v1", target_fingerprint="0" * 64)
        )
    await client.close()
    for graph in ({}, {"graph": {"provider": "sql_adjacency"}}):
        eng = _engine(tmp_path, **graph)
        await eng.start()
        try:
            assert eng.describe()["graph"] == "SqlAdjacencyGraph"
        finally:
            await eng.stop()
        assert await _bound(tmp_path) == "graph:sql_adjacency:v1"


# ------------------------------------------------------------------------------- per dialect
@pytest.fixture(params=["sqlite", "postgres"])
async def sql(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Any]:
    if request.param == "postgres":
        if not PG_URL or importlib.util.find_spec("psycopg") is None:
            pytest.skip("graph auto pin on Postgres: set MEMSPINE_TEST_PG_URL")
        from memspine.clients.postgres import PostgresClient, PostgresSpec

        pg = PostgresClient(PostgresSpec(PG_URL))
        await pg.open()
        async with pg.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            await conn.execute(delete(provider_projector_bindings))
        try:
            yield SimpleNamespace(engine=pg.engine, capabilities=frozenset({"sql", "sql.postgres"}))
        finally:
            async with pg.engine.begin() as conn:
                await conn.execute(delete(provider_projector_bindings))
            await pg.close()
        return
    from memspine.clients.sqlite import SQLiteClient, SQLiteSpec

    client = SQLiteClient(SQLiteSpec(str(tmp_path / "pin-unit.db")))
    await client.open()
    async with client.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield SimpleNamespace(engine=client.engine, capabilities=frozenset({"sql", "sql.sqlite"}))
    await client.close()


def _config(**overrides: Any) -> MemspineConfig:
    runtime = {
        "provider_services": {
            "health_timeout_s": 2.0,
            "close_timeout_s": 2.0,
            "startup_timeout_s": None,
        }
    }
    return MemspineConfig(runtime=runtime, **overrides)


def _wiring(sql: Any) -> Wiring:
    return Wiring(
        ProviderLifecycleManager(close_timeout_s=2.0), 2.0, None, owner_id="dep-1", sql=sql
    )


async def test_resolve_graph_pin_per_dialect(sql: Any, ladybug: dict[str, bool]) -> None:
    config = _config()
    assert config.graph.provider == "auto"
    wiring = _wiring(sql)
    # no binding yet: today's resolution (no ladybug -> sql_adjacency, SQLite and Postgres)
    assert await resolve_graph_pin(config, wiring) == "sql_adjacency"
    assert _graph_descriptor(config, wiring).ref.name == "sql_adjacency"
    await SqlProjectionBindingStore(sql.engine).ensure(
        ProjectionTargetBinding("dep-1", "graph", "graph:sql_adjacency:v1", "f" * 64)
    )
    ladybug["installed"] = True
    later = _wiring(sql)
    assert await resolve_graph_pin(config, later) == "sql_adjacency"  # from the binding
    assert _graph_descriptor(config, later).ref.name == "sql_adjacency"
    assert later.provider_drift == {"graph": ("sql_adjacency", "ladybug")}
    # a fresh deployment (no binding) resolves to ladybug once it is installed
    fresh = Wiring(
        ProviderLifecycleManager(close_timeout_s=2.0), 2.0, None, owner_id="dep-2", sql=sql
    )
    assert await resolve_graph_pin(config, fresh) == "ladybug"
    assert fresh.provider_drift == {}
    # an explicit provider is never pinned
    explicit = _config(graph={"provider": "sql_adjacency"})
    assert await resolve_graph_pin(explicit, _wiring(sql)) is None


async def test_pinned_provider_that_is_no_longer_installed_fails_clearly(
    sql: Any, ladybug: dict[str, bool]
) -> None:
    from memspine.exceptions import MissingServiceError

    await SqlProjectionBindingStore(sql.engine).ensure(
        ProjectionTargetBinding("dep-1", "graph", "graph:ladybug:v1", "e" * 64)
    )
    wiring = _wiring(sql)
    assert await resolve_graph_pin(_config(), wiring) == "ladybug"
    with pytest.raises(MissingServiceError):  # pinned, not silently replaced
        _graph_descriptor(_config(), wiring)
