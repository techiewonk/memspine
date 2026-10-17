"""Engine-bound administrative provider changes with durable scope checks."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from memspine.providers.changes import (
    ChangeCategory,
    ChangeConflict,
    ChangeCoordinator,
    ChangePlan,
    ChangeRequest,
    ChangeResult,
    ChangeStrategy,
    CleanupSweepReport,
    OperationRecord,
    PlanStep,
    ResourceEstimate,
    RevisionGuard,
    SqlChangeStore,
)
from memspine.providers.cleanup_worker import ProjectionCleanupWorker
from memspine.providers.recovery import (
    CleanupFenceRecoveryReceipt,
    CleanupFenceRecoveryRequest,
    CleanupQuiescenceGuard,
)


@dataclass(frozen=True, slots=True)
class ProviderChangeRuntime:
    """Programmatic, non-serializable dependencies for change control.

    Callbacks and strategies are supplied by the embedding application, never
    loaded from provider settings or a persisted config document.
    """

    revision_guard: RevisionGuard
    strategies: Mapping[tuple[ChangeCategory, str], ChangeStrategy]
    plan_ttl_s: float
    cleanup_quiescence_guard: CleanupQuiescenceGuard | None = None

    def __post_init__(self) -> None:
        if isinstance(self.plan_ttl_s, bool) or not math.isfinite(self.plan_ttl_s) or self.plan_ttl_s <= 0:
            raise ValueError("plan_ttl_s must be finite and positive")
        object.__setattr__(self, "strategies", MappingProxyType(dict(self.strategies)))


class EngineProviderChanges:
    """Explicit plan/execute/resume/verify/cleanup over an Engine's own SQL.

    Strategy registration and revision/config sources are explicit dependencies.
    Merely constructing this service does not migrate, connect or probe models.
    """

    def __init__(
        self,
        *,
        coordinator: ChangeCoordinator,
        store: SqlChangeStore,
        deployment_id: str,
        assert_active: Callable[[], None],
        cleanup_interval_s: float | None = None,
        cleanup_batch_size: int = 50,
        cleanup_quiescence_guard: CleanupQuiescenceGuard | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._store = store
        self._deployment_id = deployment_id
        self._assert_active = assert_active
        self._cleanup_interval_s = cleanup_interval_s
        self._cleanup_batch_size = cleanup_batch_size
        self._cleanup_quiescence_guard = cleanup_quiescence_guard
        self._cleanup_worker: ProjectionCleanupWorker | None = None

    def _check_request(self, request: ChangeRequest) -> None:
        self._assert_active()
        if (
            request.scope.deployment_id != self._deployment_id
            or request.scope.owner_id != self._deployment_id
        ):
            raise ChangeConflict("provider change scope does not belong to this Engine deployment")

    async def _check_plan(self, plan_id: str) -> None:
        self._assert_active()
        self._check_request((await self._store.get_plan(plan_id)).request)

    async def _check_operation(self, operation_id: str) -> None:
        self._assert_active()
        operation = await self._store.get_operation(operation_id)
        await self._check_plan(operation.plan_id)

    async def plan_change(
        self,
        request: ChangeRequest,
        *,
        steps: Sequence[PlanStep],
        required_capabilities: frozenset[str],
        estimate: ResourceEstimate | None = None,
        retention_s: int | None = None,
    ) -> ChangePlan:
        self._check_request(request)
        return await self._coordinator.plan_change(
            request,
            steps=steps,
            required_capabilities=required_capabilities,
            estimate=estimate,
            retention_s=retention_s,
        )

    async def execute_change(self, plan_id: str) -> OperationRecord:
        await self._check_plan(plan_id)
        return await self._coordinator.execute_change(plan_id)

    async def resume_change(self, operation_id: str) -> OperationRecord:
        await self._check_operation(operation_id)
        return await self._coordinator.resume_change(operation_id)

    async def get_operation(self, operation_id: str) -> OperationRecord:
        """Read an operation's current revision and fence epoch for explicit recovery."""
        await self._check_operation(operation_id)
        return await self._store.get_operation(operation_id)

    async def verify_change(self, operation_id: str) -> ChangeResult:
        await self._check_operation(operation_id)
        return await self._coordinator.verify_change(operation_id)

    async def cleanup_change(self, operation_id: str) -> OperationRecord:
        await self._check_operation(operation_id)
        return await self._coordinator.cleanup_change(operation_id)

    async def recover_abandoned_cleanup_fence(
        self, request: CleanupFenceRecoveryRequest
    ) -> CleanupFenceRecoveryReceipt:
        """Recover a cleanup claim only through the injected application proof guard."""
        self._assert_active()
        if request.deployment_id != self._deployment_id:
            raise ChangeConflict("cleanup recovery scope does not belong to this Engine deployment")
        if self._cleanup_quiescence_guard is None:
            raise ChangeConflict("cleanup fence recovery requires an application quiescence guard")
        return await self._coordinator.recover_abandoned_cleanup_fence(
            request, quiescence_guard=self._cleanup_quiescence_guard
        )

    async def cleanup_fence_recoveries(
        self, operation_id: str
    ) -> tuple[CleanupFenceRecoveryReceipt, ...]:
        await self._check_operation(operation_id)
        return await self._store.audit_cleanup_fence_recoveries(
            self._deployment_id, operation_id
        )

    async def sweep_expired_projection_cleanup(
        self, batch_size: int, after_operation_id: str | None = None
    ) -> CleanupSweepReport:
        self._assert_active()
        return await self._coordinator.cleanup_expired_projections(
            self._deployment_id,
            limit=batch_size,
            after_operation_id=after_operation_id,
        )

    async def start_cleanup_worker(self) -> CleanupSweepReport:
        """Start configured immediate/periodic cleanup after Engine startup."""
        self._assert_active()
        if self._cleanup_interval_s is None:
            raise ChangeConflict("automatic projection cleanup is not configured")
        if self._cleanup_worker is not None:
            raise ChangeConflict("automatic projection cleanup is already running")
        worker = ProjectionCleanupWorker(
            interval_s=self._cleanup_interval_s,
            batch_size=self._cleanup_batch_size,
            sweep=self.sweep_expired_projection_cleanup,
        )
        self._cleanup_worker = worker
        try:
            return await worker.start()
        except BaseException:
            # Keep the worker discoverable during startup so Engine teardown
            # can drain any shielded SQL/backend cleanup before closing clients.
            await worker.stop()
            if self._cleanup_worker is worker:
                self._cleanup_worker = None
            raise

    async def stop_cleanup_worker(self) -> None:
        worker, self._cleanup_worker = self._cleanup_worker, None
        if worker is not None:
            await worker.stop()

    @property
    def cleanup_worker_running(self) -> bool:
        return self._cleanup_worker is not None

    @property
    def last_cleanup_sweep(self) -> CleanupSweepReport | None:
        return self._cleanup_worker.last_report if self._cleanup_worker is not None else None

    @property
    def last_cleanup_sweep_error(self) -> str | None:
        return self._cleanup_worker.last_error if self._cleanup_worker is not None else None


__all__ = ["EngineProviderChanges", "ProviderChangeRuntime"]
