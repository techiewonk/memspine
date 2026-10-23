from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from memspine import Engine
from memspine.exceptions import ConfigError
from memspine.providers.changes import ChangeContext, ChangeResult
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.control import ProviderChangeRuntime
from memspine.providers.health import ProbeStatus
from memspine.providers.identity import DEPLOYMENT_METADATA
from memspine.providers.lifecycle import LifecycleState


def _engine(path: Path) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(path), "busy_timeout_s": 5.0},
            },
            "cache": {"provider": "memory", "settings": {}},
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"semantic": {"enabled": True}},
    )


class _RevisionGuard:
    async def current_revision(self, logical_service_id: str) -> str:
        return "source-r1"

    async def current_target_fingerprint(self, config_id: str) -> str:
        return "target-fp"


class _ProjectionStrategy:
    async def execute(self, context: ChangeContext) -> None:
        return None

    async def verify(self, context: ChangeContext) -> ChangeResult:
        raise AssertionError("startup cleanup must not verify a new operation")

    async def activate(self, context: ChangeContext, result: ChangeResult) -> None:
        raise AssertionError("startup cleanup must not activate a new operation")

    async def cleanup(self, context: ChangeContext, result: ChangeResult) -> ChangeResult:
        raise AssertionError("no cleanup candidates are expected")


def _cleanup_runtime() -> ProviderChangeRuntime:
    return ProviderChangeRuntime(
        revision_guard=_RevisionGuard(),
        strategies={("graph", "projection_rebuild"): _ProjectionStrategy()},
        plan_ttl_s=3600.0,
    )


def _cleanup_engine(path: Path, *, provider_change_runtime: ProviderChangeRuntime | None) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(path), "busy_timeout_s": 5.0},
            },
            "cache": {"provider": "memory", "settings": {}},
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"semantic": {"enabled": True}},
        runtime={
            "service_switches": {
                "projections": {"cleanup_interval_s": 3600.0, "cleanup_batch_size": 2}
            }
        },
        provider_change_runtime=provider_change_runtime,
    )


@pytest.mark.asyncio
async def test_development_identity_is_stable_across_engine_reopen(tmp_path: Path) -> None:
    database = tmp_path / "engine-identity.db"
    first = _engine(database)
    await first.start()
    assert first._sql is not None
    async with first._sql.engine.connect() as connection:
        identity = await connection.scalar(
            select(DEPLOYMENT_METADATA.c.deployment_id).where(DEPLOYMENT_METADATA.c.id == 1)
        )
    await first.stop()

    second = _engine(database)
    await second.start()
    assert second._sql is not None
    async with second._sql.engine.connect() as connection:
        reopened = await connection.scalar(
            select(DEPLOYMENT_METADATA.c.deployment_id).where(DEPLOYMENT_METADATA.c.id == 1)
        )
    await second.stop()
    assert isinstance(identity, str) and identity
    assert reopened == identity


@pytest.mark.asyncio
async def test_engine_readiness_requires_required_graph_probe(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "graph-health.db")
    await engine.start()

    async def unhealthy() -> bool:
        return False

    engine._provider_results["graph.primary"] = BuildResult(
        service=object(),
        provider=ProviderRef("graph", "neo4j"),
        resources=(),
        effective_capabilities=frozenset({"graph"}),
        health_probe=unhealthy,
    )
    try:
        report = await engine.readiness()
        assert not report.ready
        graph = next(item for item in report.health.providers if item.logical_id == "graph.primary")
        assert graph.status is ProbeStatus.UNHEALTHY
        assert any("graph.primary" in reason for reason in report.reasons)
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_primary_graph_and_lexical_health_results_keep_distinct_ids(tmp_path: Path) -> None:
    engine = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            },
            "cache": {"provider": "memory", "settings": {}},
            "graph": {"provider": "sql_adjacency", "settings": {}},
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"associative": {"enabled": True}},
    )
    await engine.start()
    try:
        assert engine._provider_results["graph.primary"].provider.category == "graph"
        assert engine._provider_results["lexical"].provider.category == "lexical"
        health = await engine.provider_health()
        assert {item.logical_id for item in health.providers} >= {"graph.primary", "lexical"}
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_startup_failure_closes_already_built_providers(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "startup-cleanup.db")
    engine._overrides.setdefault("storage", {})["graph"] = {
        "provider": "sql_adjacency",
        "settings": {},
    }

    with pytest.raises(ConfigError, match="graph configured but memories.associative not enabled"):
        await engine.start()

    manager = engine._provider_resources
    assert manager is not None
    assert manager.states
    assert all(state is LifecycleState.STOPPED for state in manager.states.values())


@pytest.mark.asyncio
async def test_cleanup_schedule_requires_injected_strategy_before_provider_start(
    tmp_path: Path,
) -> None:
    engine = _cleanup_engine(
        tmp_path / "cleanup-missing-injection.db", provider_change_runtime=None
    )
    with pytest.raises(ConfigError, match="requires provider_change_runtime"):
        await engine.start()
    assert engine._provider_results == {}


@pytest.mark.asyncio
async def test_cleanup_schedule_runs_bounded_startup_sweep_and_stops_with_engine(
    tmp_path: Path,
) -> None:
    engine = _cleanup_engine(
        tmp_path / "cleanup-runtime.db", provider_change_runtime=_cleanup_runtime()
    )
    await engine.start()
    controller = engine.provider_changes()
    assert controller.cleanup_worker_running
    assert controller.last_cleanup_sweep is not None
    assert controller.last_cleanup_sweep.scanned == 0
    await engine.stop()
    assert engine._provider_changes_controller is None
