from __future__ import annotations

import asyncio

import pytest

from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.health import ProbeStatus, ProviderStatusService


@pytest.mark.asyncio
async def test_required_graph_health_and_projection_catchup_gate_readiness() -> None:
    status = ProviderStatusService(health_timeout_s=0.1)
    status.register(
        category="graph",
        provider="neo4j",
        required=True,
        check=lambda: _constant(False),
    )
    status.register(
        category="llm",
        provider="cloud",
        required=False,
        check=None,
    )

    report = await status.readiness(projector_catchup={"graph": False, "vectors": True})
    assert not report.ready
    assert report.reasons == (
        "required_provider_unhealthy:graph:neo4j:graph:neo4j",
        "projector_not_caught_up:graph",
    )
    assert [item.status for item in report.health.providers] == [
        ProbeStatus.UNHEALTHY,
        ProbeStatus.NOT_CHECKED,
    ]


@pytest.mark.asyncio
async def test_probe_timeout_and_exception_are_bounded_and_sanitized() -> None:
    status = ProviderStatusService(health_timeout_s=0.01)
    status.register(
        category="storage",
        provider="sqlite",
        required=True,
        check=_slow,
    )
    status.register(
        category="graph",
        provider="neo4j",
        required=True,
        check=_raises_secret_error,
    )

    report = await status.health()
    assert [item.status for item in report.providers] == [
        ProbeStatus.TIMED_OUT,
        ProbeStatus.UNHEALTHY,
    ]
    assert [item.diagnostic_code for item in report.providers] == [
        "health_probe_timeout",
        "health_probe_failed",
    ]
    assert "do-not-leak" not in repr(report)


@pytest.mark.asyncio
async def test_build_result_probe_is_optional_and_does_not_probe_implicitly() -> None:
    result = BuildResult(
        service=object(),
        provider=ProviderRef("llm", "remote"),
        resources=(),
        effective_capabilities=frozenset({"generate"}),
    )
    status = ProviderStatusService(health_timeout_s=0.1)
    status.register_build_result(result, required=False)
    report = await status.health()
    assert report.providers[0].status is ProbeStatus.NOT_CHECKED


@pytest.mark.asyncio
async def test_primary_and_mirror_results_keep_separate_logical_health_ids() -> None:
    async def healthy() -> bool:
        return True

    provider = ProviderRef("graph", "neo4j")
    status = ProviderStatusService(health_timeout_s=0.1)
    for logical_id in ("graph.primary", "graph.mirror"):
        status.register_build_result(
            BuildResult(object(), provider, (), frozenset({"graph"}), health_probe=healthy),
            required=logical_id.endswith("primary"),
            logical_id=logical_id,
        )

    report = await status.health()
    assert [item.logical_id for item in report.providers] == [
        "graph.primary",
        "graph.mirror",
    ]
    assert [item.required for item in report.providers] == [True, False]


async def _constant(value: bool) -> bool:
    return value


async def _slow() -> bool:
    await asyncio.sleep(0.1)
    return True


async def _raises_secret_error() -> bool:
    raise RuntimeError("provider failed with token do-not-leak")
