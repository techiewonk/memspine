"""Bounded provider health probes and Engine readiness assessments.

Provider lifecycle, provider health and projection catch-up are independent
signals. Health probes run only when a builder explicitly supplies a probe;
model providers therefore receive no implicit or paid quality request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from sqlalchemy import text

from memspine.providers.contracts import BuildResult, Category, SqlAccess

HealthProbe = Callable[[], Awaitable[bool]]


class ProbeStatus(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    TIMED_OUT = "timed_out"
    NOT_CHECKED = "not_checked"


@dataclass(frozen=True, slots=True)
class ProviderProbeResult:
    logical_id: str
    category: Category
    provider: str
    required: bool
    status: ProbeStatus
    diagnostic_code: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderHealthReport:
    providers: tuple[ProviderProbeResult, ...]


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    ready: bool
    reasons: tuple[str, ...]
    health: ProviderHealthReport
    projector_catchup: Mapping[str, bool]


@dataclass(frozen=True, slots=True)
class _Probe:
    logical_id: str
    category: Category
    provider: str
    required: bool
    check: HealthProbe | None


class ProviderStatusService:
    """Run explicitly registered health probes under one finite deadline."""

    def __init__(self, *, health_timeout_s: float) -> None:
        if (
            isinstance(health_timeout_s, bool)
            or health_timeout_s <= 0
            or not float(health_timeout_s) < float("inf")
        ):
            raise ValueError("health_timeout_s must be finite and positive")
        self._timeout_s = health_timeout_s
        self._probes: dict[tuple[Category, str], _Probe] = {}

    def register(
        self,
        *,
        category: Category,
        provider: str,
        required: bool,
        check: HealthProbe | None,
        logical_id: str | None = None,
    ) -> None:
        if not provider or provider.strip() != provider:
            raise ValueError("provider identifier must be non-empty and trimmed")
        resolved_id = logical_id or f"{category}:{provider}"
        if not resolved_id or resolved_id.strip() != resolved_id:
            raise ValueError("logical provider id must be non-empty and trimmed")
        key = (category, resolved_id)
        if key in self._probes:
            raise ValueError(f"health probe already registered for {category}/{resolved_id}")
        self._probes[key] = _Probe(resolved_id, category, provider, required, check)

    def register_build_result(
        self, result: BuildResult[object], *, required: bool, logical_id: str | None = None
    ) -> None:
        """Attach the optional builder health probe to the sanitized provider identity."""
        self.register(
            category=result.provider.category,
            provider=result.provider.name,
            required=required,
            check=result.health_probe,
            logical_id=logical_id,
        )

    async def health(self) -> ProviderHealthReport:
        results = await asyncio.gather(*(self._probe(item) for item in self._probes.values()))
        return ProviderHealthReport(tuple(results))

    async def readiness(self, *, projector_catchup: Mapping[str, bool]) -> ReadinessReport:
        report = await self.health()
        reasons = [
            f"required_provider_unhealthy:{item.logical_id}:{item.category}:{item.provider}"
            for item in report.providers
            if item.required and item.status is not ProbeStatus.HEALTHY
        ]
        reasons.extend(
            f"projector_not_caught_up:{name}"
            for name, caught_up in sorted(projector_catchup.items())
            if not caught_up
        )
        return ReadinessReport(
            ready=not reasons,
            reasons=tuple(reasons),
            health=report,
            projector_catchup=dict(projector_catchup),
        )

    async def _probe(self, item: _Probe) -> ProviderProbeResult:
        if item.check is None:
            return ProviderProbeResult(
                item.logical_id,
                item.category,
                item.provider,
                item.required,
                ProbeStatus.NOT_CHECKED,
            )
        try:
            async with asyncio.timeout(self._timeout_s):
                healthy = await item.check()
        except TimeoutError:
            return ProviderProbeResult(
                item.logical_id,
                item.category,
                item.provider,
                item.required,
                ProbeStatus.TIMED_OUT,
                "health_probe_timeout",
            )
        except Exception:
            # Exception text may contain credentials, request data or vendor
            # diagnostics. Keep the public report bounded and non-sensitive.
            return ProviderProbeResult(
                item.logical_id,
                item.category,
                item.provider,
                item.required,
                ProbeStatus.UNHEALTHY,
                "health_probe_failed",
            )
        return ProviderProbeResult(
            item.logical_id,
            item.category,
            item.provider,
            item.required,
            ProbeStatus.HEALTHY if healthy else ProbeStatus.UNHEALTHY,
            None if healthy else "health_probe_failed",
        )


async def sql_health_probe(sql: SqlAccess) -> bool:
    """Check SQL responsiveness with a non-mutating, non-provider query."""
    async with sql.engine.connect() as connection:
        result = cast(int | None, await connection.scalar(text("SELECT 1")))
        return result == 1


__all__ = [
    "HealthProbe",
    "ProbeStatus",
    "ProviderHealthReport",
    "ProviderProbeResult",
    "ProviderStatusService",
    "ReadinessReport",
    "sql_health_probe",
]
