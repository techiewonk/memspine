"""Read-only validation evidence for an already running deployment.

This module never starts an Engine, writes messages, changes configuration, or
probes model quality. Infrastructure probes use the Engine's bounded health API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from memspine.config.schema.root import MemspineConfig
from memspine.providers.health import ReadinessReport


class DeploymentInspectionSource(Protocol):
    @property
    def config(self) -> MemspineConfig: ...

    async def readiness(self) -> ReadinessReport: ...

    async def provider_catalog(self, *, probe_health: bool = False) -> list[dict[str, object]]: ...


@dataclass(frozen=True, slots=True)
class DeploymentValidationReport:
    """Allowlisted evidence; secrets, settings and stored content are excluded."""

    environment: str
    production_profile: bool
    infrastructure_ready: bool
    production_validated: bool
    reasons: tuple[str, ...]
    providers: tuple[tuple[str, str, str, str], ...]
    unchecked: tuple[str, ...]


async def inspect_deployment(source: DeploymentInspectionSource) -> DeploymentValidationReport:
    """Inspect live readiness without representing it as full production approval.

    The caller supplies its already started Engine. Readiness checks projection
    checkpoints, but this function does not start
    clients or initiate ingestion, migration, cleanup, or model quality calls.
    """
    config = source.config
    readiness = await source.readiness()
    catalog = await source.provider_catalog()
    production = config.runtime.environment == "production"
    reasons = list(readiness.reasons)
    if not production:
        reasons.append("PRODUCTION_PROFILE_NOT_SELECTED")
    rows = tuple(
        (
            str(row.get("logical_service_id", row.get("name", ""))),
            str(row.get("category", "")),
            str(row.get("name", "")),
            str(row.get("lifecycle_state", "unbuilt")),
        )
        for row in catalog
        if row.get("lifecycle_state") is not None
    )
    return DeploymentValidationReport(
        environment=config.runtime.environment,
        production_profile=production,
        infrastructure_ready=readiness.ready,
        production_validated=False,
        reasons=tuple(reasons),
        providers=rows,
        unchecked=(
            "representative_workload_latency_and_capacity",
            "model_quality_and_budgeted_model_connectivity",
            "infrastructure_failover_and_restore",
            "deployment_access_controls_and_permissions",
        ),
    )


__all__ = ["DeploymentInspectionSource", "DeploymentValidationReport", "inspect_deployment"]
