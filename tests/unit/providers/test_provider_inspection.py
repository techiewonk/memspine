from __future__ import annotations

import pytest

from memspine import Engine
from memspine.providers.contracts import BuildResult, ProviderRef, ResourceRef
from memspine.providers.health import (
    ProbeStatus,
    ProviderHealthReport,
    ProviderProbeResult,
)
from memspine.providers.inspection import inspect_providers
from memspine.providers.lifecycle import LifecycleState
from memspine.store.events import EventKind, MemoryEvent


def test_inspection_preserves_logical_bindings_and_does_not_probe() -> None:
    async def forbidden_probe() -> bool:
        pytest.fail("inspection must not invoke probes")

    result = BuildResult(
        object(),
        ProviderRef("llm", "litellm"),
        (),
        frozenset({"llm"}),
        health_probe=forbidden_probe,
    )
    rows = inspect_providers(
        results={"llm.extract": result, "llm.chat": result},
        resource_states={},
    )
    active = [row for row in rows if row["logical_service_id"] is not None]
    assert {row["logical_service_id"] for row in active} == {"llm.extract", "llm.chat"}
    assert all(row["lifecycle_state"] == "started" for row in active)
    assert all(row["health_state"] is None for row in active)
    assert all("settings" not in row and "service" not in row for row in rows)


def test_inspection_reports_cleanup_failure_and_health_separately() -> None:
    rows = inspect_providers(
        results={
            "embedding": BuildResult(
                object(),
                ProviderRef("embedding", "hash"),
                (ResourceRef("encoder", "owned"),),
                frozenset({"embedding"}),
            )
        },
        resource_states={"encoder": LifecycleState.FAILED},
        health=ProviderHealthReport(
            (
                ProviderProbeResult(
                    "embedding",
                    "embedding",
                    "hash",
                    False,
                    ProbeStatus.NOT_CHECKED,
                ),
            )
        ),
    )
    active = next(row for row in rows if row["logical_service_id"] == "embedding")
    assert active["lifecycle_state"] == "failed"
    assert active["health_state"] == "not_checked"
    unbuilt = next(row for row in rows if row["name"] == "onnx")
    assert unbuilt["lifecycle_state"] is None


def test_healthy_projection_is_pending_until_replay_catches_up() -> None:
    result = BuildResult(object(), ProviderRef("lexical", "tantivy"), (), frozenset({"lexical"}))
    rows = inspect_providers(
        results={"lexical": result},
        resource_states={},
        health=ProviderHealthReport(
            (
                ProviderProbeResult(
                    "lexical",
                    "lexical",
                    "tantivy",
                    True,
                    ProbeStatus.HEALTHY,
                ),
            )
        ),
        projector_catchup={"lexical:english": False},
    )
    row = next(row for row in rows if row["logical_service_id"] == "lexical")
    assert row["health_state"] == "healthy"
    assert row["readiness_state"] == "pending"


@pytest.mark.asyncio
async def test_engine_catalog_uses_actual_selected_services_without_settings() -> None:
    async with Engine(
        template="core",
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
    ) as engine:
        await engine.write(
            "A private diagnostic payload that must not enter the catalog", namespace="inspection"
        )
        rows = await engine.provider_catalog()
        active = [row for row in rows if row["logical_service_id"] is not None]
        assert {row["category"] for row in active} >= {"storage", "vector", "embedding", "cache"}
        assert all(row["resolved_provider"] == row["name"] for row in active)
        assert all(row["health_state"] is None for row in active)
        vector = next(row for row in active if row["category"] == "vector")
        assert vector["projection_generation"] is not None
        assert vector["checkpoint_seq"] > 0
        assert vector["source_head_seq"] >= vector["checkpoint_seq"]
        assert vector["lag_events"] == 0
        assert vector["sanitized_error_code"] is None
        assert "private diagnostic payload" not in repr(rows)
        assert engine._storage is not None
        await engine._storage.append_event(
            MemoryEvent(kind=EventKind.RETRIEVE, namespace="inspection", actor="test", payload={})
        )
        lagged = next(
            row for row in await engine.provider_catalog() if row["logical_service_id"] == "vector"
        )
        assert lagged["checkpoint_seq"] == vector["checkpoint_seq"] and lagged["lag_events"] == 1
        probed = await engine.provider_catalog(probe_health=True)
        sql = next(
            row for row in probed if row["category"] == "storage" and row["logical_service_id"]
        )
        assert sql["health_state"] == "healthy"
        assert "busy_timeout_s" not in repr(rows)
