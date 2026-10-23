"""Allowlisted runtime provider inspection, without native client introspection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from sqlalchemy import func, select

from memspine.providers.catalog import catalog
from memspine.providers.contracts import BuildResult, SqlAccess
from memspine.providers.health import ProbeStatus, ProviderHealthReport
from memspine.providers.lifecycle import LifecycleState
from memspine.services.storage.sqlite.schema import (
    memory_events,
    projector_offsets,
    provider_projector_bindings,
)


@dataclass(frozen=True, slots=True)
class ProjectionDiagnostic:
    generation_id: str
    checkpoint_seq: int
    source_head_seq: int | None
    lag_events: int


async def projection_diagnostics(
    sql: SqlAccess, deployment_id: str
) -> dict[str, ProjectionDiagnostic]:
    """Read only generation/checkpoint counters; never load event payloads."""
    offset = func.coalesce(projector_offsets.c.last_seq, 0)
    head = select(func.max(memory_events.c.seq)).scalar_subquery()
    lag = (
        select(func.count())
        .select_from(memory_events)
        .where(memory_events.c.seq > offset)
        .correlate(projector_offsets)
        .scalar_subquery()
    )
    statement = (
        select(
            provider_projector_bindings.c.projector_name,
            provider_projector_bindings.c.generation_id,
            offset.label("checkpoint_seq"),
            head.label("head_seq"),
            lag.label("lag_events"),
        )
        .select_from(
            provider_projector_bindings.outerjoin(
                projector_offsets,
                provider_projector_bindings.c.projector_name == projector_offsets.c.projector_name,
            )
        )
        .where(provider_projector_bindings.c.deployment_id == deployment_id)
    )
    async with sql.engine.connect() as connection:
        rows = (await connection.execute(statement)).all()
    diagnostics: dict[str, ProjectionDiagnostic] = {}
    for row in rows:
        logical_id = {
            "vectors": "vector",
            "graph": "graph.primary",
        }.get(row.projector_name)
        if logical_id is None and row.projector_name.startswith("lexical"):
            logical_id = "lexical"
        if logical_id is not None:
            diagnostics[logical_id] = ProjectionDiagnostic(
                row.generation_id, row.checkpoint_seq, row.head_seq, row.lag_events
            )
    return diagnostics


def inspect_providers(
    *,
    results: Mapping[str, BuildResult[object]],
    resource_states: Mapping[str, LifecycleState],
    health: ProviderHealthReport | None = None,
    configured: Mapping[str, str] | None = None,
    projector_catchup: Mapping[str, bool] | None = None,
    projections: Mapping[str, ProjectionDiagnostic] | None = None,
) -> list[dict[str, object]]:
    """Combine descriptor inventory with one row per built logical service.

    Repeated LLM roles remain distinct. Unbuilt
    inventory entries have no runtime state. Probe execution belongs to the
    caller; this function never invokes a model, connects, or serializes settings.
    """
    inventory = {(entry.category, entry.name): entry for entry in catalog()}
    probes = {} if health is None else {item.logical_id: item for item in health.providers}
    configured = configured or {}
    rows: list[dict[str, object]] = []
    built: set[tuple[str, str]] = set()
    for logical_id, result in sorted(results.items()):
        key = (result.provider.category, result.provider.name)
        entry = inventory[key]
        built.add(key)
        states = [resource_states.get(ref.resource_id) for ref in result.resources]
        # A successful resource-free build is started too (e.g. hash embedding).
        lifecycle = LifecycleState.STARTED
        for candidate in (
            LifecycleState.FAILED,
            LifecycleState.STOPPING,
            LifecycleState.STOPPED,
            LifecycleState.BUILDING,
        ):
            if candidate in states:
                lifecycle = candidate
                break
        probe = probes.get(logical_id)
        readiness = None
        if probe is not None and probe.status is not ProbeStatus.NOT_CHECKED:
            readiness = "ready" if probe.status is ProbeStatus.HEALTHY else "failed"
            projector_name = {
                "vector": "vectors",
                "graph.primary": "graph",
            }.get(logical_id)
            catchup = projector_catchup or {}
            pending = (
                projector_name in catchup and not catchup[projector_name]
                if projector_name is not None
                else logical_id == "lexical"
                and any(
                    not caught_up
                    for name, caught_up in catchup.items()
                    if name.startswith("lexical")
                )
            )
            if pending and readiness == "ready":
                readiness = "pending"
        selected = configured.get(logical_id, result.provider.name)
        row = replace(
            entry,
            availability="available",
            configured_provider=selected,
            resolved_provider=result.provider.name,
            selection_reason=(
                "automatic selection" if selected == "auto" else "explicit selection"
            ),
            lifecycle_state=lifecycle.value,
            health_state=None if probe is None else probe.status.value,
            readiness_state=readiness,
            sanitized_error_code=None if probe is None else probe.diagnostic_code,
        ).to_dict()
        row["logical_service_id"] = logical_id
        row["effective_capabilities"] = sorted(result.effective_capabilities)
        projection = (projections or {}).get(logical_id)
        if projection is not None:
            row.update(
                {
                    "projection_generation": projection.generation_id,
                    "checkpoint_seq": projection.checkpoint_seq,
                    "source_head_seq": projection.source_head_seq,
                    "lag_events": projection.lag_events,
                }
            )
        rows.append(row)
    for inventory_key, entry in sorted(inventory.items()):
        if inventory_key not in built:
            row = entry.to_dict()
            row["logical_service_id"] = None
            row["effective_capabilities"] = None
            rows.append(row)
    return rows


__all__ = ["ProjectionDiagnostic", "inspect_providers", "projection_diagnostics"]
