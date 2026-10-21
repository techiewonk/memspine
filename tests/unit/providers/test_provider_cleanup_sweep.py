from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from memspine.providers.changes import (
    ActiveChangeExists,
    ChangeCoordinator,
    ChangeRequest,
    ChangeScope,
    ChangeStatus,
    CleanupSweepReport,
    PlanStep,
    ProjectionRebuildResult,
    ResourceEstimate,
    SourceCompleteness,
    SqlChangeStore,
    TargetConfigRef,
)
from memspine.providers.cleanup_worker import ProjectionCleanupWorker


class _Clock:
    def __init__(self) -> None:
        self.value = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.value


class _RevisionGuard:
    async def current_revision(self, logical_service_id: str) -> str:
        return "source-r1"

    async def current_target_fingerprint(self, config_id: str) -> str:
        return "target-fp"


class _ProjectionStrategy:
    def __init__(self, *, block_cleanup: bool = False, fail_cleanup_once: bool = False) -> None:
        self.cleanup_calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not block_cleanup:
            self.release.set()
        self.fail_cleanup_once = fail_cleanup_once

    async def execute(self, context) -> None:
        return None

    async def verify(self, context) -> ProjectionRebuildResult:
        now = datetime.now(UTC)
        return ProjectionRebuildResult(
            operation_id=context.operation.operation_id,
            status=ChangeStatus.VERIFIED,
            created_at=now,
            updated_at=now,
            deployment_id=context.plan.request.scope.deployment_id,
            source_generation=context.plan.request.scope.source_generation or "",
            target_generation=context.plan.request.scope.target_generation or "",
            source_checkpoint=10,
            target_checkpoint=10,
            erase_checkpoint=4,
            verification_report_id="verified-checkpoint-report",
        )

    async def activate(self, context, result) -> None:
        return None

    async def cleanup(self, context, result) -> ProjectionRebuildResult:
        self.cleanup_calls += 1
        self.entered.set()
        await self.release.wait()
        if self.fail_cleanup_once:
            self.fail_cleanup_once = False
            raise RuntimeError("test cleanup failure; details must not escape sweep report")
        return result


def _request(*, owner_id: str = "deploy-a", service: str = "graph.primary") -> ChangeRequest:
    return ChangeRequest(
        category="graph",
        logical_service_id=service,
        source_revision="source-r1",
        target_revision="target-r2",
        scope=ChangeScope(
            deployment_id="deploy-a",
            owner_id=owner_id,
            resource_class="projection",
            source_provider="old-provider",
            target_provider="new-provider",
            source_generation=f"{service}-old",
            target_generation=f"{service}-new",
            source_target_fingerprint="source-fp",
            target_fingerprint="target-fp",
        ),
        target_config_ref=TargetConfigRef(
            provider="new-provider", config_id="config-r2", semantic_fingerprint="target-fp"
        ),
        requested_strategy="projection_rebuild",
        actor="test-operator",
        source_completeness=SourceCompleteness(complete=True, evidence_id="source-complete"),
    )


async def _setup(strategy: _ProjectionStrategy, clock: _Clock):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlChangeStore(engine)
    await store.create_schema()
    coordinator = ChangeCoordinator(
        store=store,
        revision_guard=_RevisionGuard(),
        strategies={("graph", "projection_rebuild"): strategy},
        plan_ttl_s=3600,
        now=clock,
    )
    return engine, store, coordinator


async def _activate(coordinator: ChangeCoordinator, request: ChangeRequest, *, retention_s: int):
    plan = await coordinator.plan_change(
        request,
        steps=(PlanStep(step_id="rebuild", phase="migrate", description="Rebuild projection"),),
        required_capabilities=frozenset({"checkpoint", "scoped_delete", "erasure"}),
        estimate=ResourceEstimate(),
        retention_s=retention_s,
    )
    operation = await coordinator.execute_change(plan.plan_id)
    return await coordinator.verify_change(operation.operation_id)


@pytest.mark.asyncio
async def test_sweep_cleans_only_expired_deployment_owned_projection() -> None:
    clock = _Clock()
    strategy = _ProjectionStrategy()
    engine, store, coordinator = await _setup(strategy, clock)
    try:
        due = await _activate(coordinator, _request(service="graph.primary"), retention_s=0)
        not_due = await _activate(coordinator, _request(service="graph.mirror"), retention_s=3600)
        foreign = await _activate(
            coordinator,
            _request(owner_id="another-deployment", service="graph.foreign"),
            retention_s=0,
        )

        report = await coordinator.cleanup_expired_projections("deploy-a", limit=10)

        assert (
            report.scanned,
            report.eligible,
            report.cleaned,
            report.deferred,
            report.failed,
        ) == (
            3,
            1,
            1,
            2,
            0,
        )
        assert strategy.cleanup_calls == 1
        assert (await store.get_operation(due.operation_id)).status is ChangeStatus.CLEANED
        assert (await store.get_operation(not_due.operation_id)).status is ChangeStatus.ACTIVE
        assert (await store.get_operation(foreign.operation_id)).status is ChangeStatus.ACTIVE
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sweep_retries_graceful_cleanup_failure_after_coordinator_restart() -> None:
    clock = _Clock()
    strategy = _ProjectionStrategy(fail_cleanup_once=True)
    engine, store, first = await _setup(strategy, clock)
    try:
        operation = await _activate(first, _request(), retention_s=0)
        failed = await first.cleanup_expired_projections("deploy-a", limit=10)
        assert failed.failed == 1 and failed.cleaned == 0
        persisted = await store.get_operation(operation.operation_id)
        assert persisted.status is ChangeStatus.CLEANUP_PENDING
        assert persisted.error_code == "PROVIDER_CLOSE_FAILED"

        # A backend exception returns through the cleanup code, which persists
        # cleanup_pending and releases the fence; a restarted coordinator retries
        # the same operation ID from that durable state.
        restarted = ChangeCoordinator(
            store=SqlChangeStore(engine),
            revision_guard=_RevisionGuard(),
            strategies={("graph", "projection_rebuild"): strategy},
            plan_ttl_s=3600,
            now=clock,
        )
        retried = await restarted.cleanup_expired_projections("deploy-a", limit=10)
        assert retried.cleaned == 1 and retried.failed == 0
        assert (await store.get_operation(operation.operation_id)).status is ChangeStatus.CLEANED
        assert strategy.cleanup_calls == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sweep_surfaces_abandoned_cleanup_fence_without_stealing_it() -> None:
    clock = _Clock()
    strategy = _ProjectionStrategy()
    engine, store, coordinator = await _setup(strategy, clock)
    try:
        operation = await _activate(coordinator, _request(), retention_s=0)
        claimed = await store.claim_cleanup(operation.operation_id)
        assert claimed.status is ChangeStatus.CLEANUP_PENDING

        # This represents a process dying after the durable claim commits and
        # before it can run strategy.cleanup or release the deployment fence.
        restarted = ChangeCoordinator(
            store=SqlChangeStore(engine),
            revision_guard=_RevisionGuard(),
            strategies={("graph", "projection_rebuild"): strategy},
            plan_ttl_s=3600,
            now=clock,
        )
        report = await restarted.cleanup_expired_projections("deploy-a", limit=10)
        assert report.failed == 1
        assert report.failed_operation_ids == (operation.operation_id,)
        assert strategy.cleanup_calls == 0
        assert (
            await store.get_operation(operation.operation_id)
        ).status is ChangeStatus.CLEANUP_PENDING
        with pytest.raises(ActiveChangeExists):
            await store.claim_cleanup(operation.operation_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_sweeps_are_fenced_and_worker_stop_drains_periodic_tick() -> None:
    clock = _Clock()
    strategy = _ProjectionStrategy(block_cleanup=True)
    engine, _store, coordinator = await _setup(strategy, clock)
    try:
        await _activate(coordinator, _request(), retention_s=0)
        first = asyncio.create_task(coordinator.cleanup_expired_projections("deploy-a", limit=5))
        await asyncio.wait_for(strategy.entered.wait(), timeout=2)
        # A separate coordinator/store instance models another Engine process;
        # both share only the durable SQL fence.
        second_coordinator = ChangeCoordinator(
            store=SqlChangeStore(engine),
            revision_guard=_RevisionGuard(),
            strategies={("graph", "projection_rebuild"): strategy},
            plan_ttl_s=3600,
            now=clock,
        )
        second = await second_coordinator.cleanup_expired_projections("deploy-a", limit=5)
        assert second.failed == 1  # the first process owns the durable deployment fence
        strategy.release.set()
        assert (await first).cleaned == 1

        periodic_started = asyncio.Event()
        periodic_release = asyncio.Event()
        sweep_calls = 0

        async def wait_for_cancel(batch_size: int, cursor: str | None) -> CleanupSweepReport:
            nonlocal sweep_calls
            sweep_calls += 1
            if sweep_calls == 1:
                return CleanupSweepReport(
                    deployment_id="deploy-a",
                    scanned=0,
                    eligible=0,
                    cleaned=0,
                    deferred=0,
                    failed=0,
                )
            periodic_started.set()
            await periodic_release.wait()
            return CleanupSweepReport(
                deployment_id="deploy-a",
                scanned=0,
                eligible=0,
                cleaned=0,
                deferred=0,
                failed=0,
            )

        worker = ProjectionCleanupWorker(interval_s=0.01, batch_size=2, sweep=wait_for_cancel)
        startup = await worker.start()
        assert startup.scanned == 0
        await asyncio.wait_for(periodic_started.wait(), timeout=2)
        stop = asyncio.create_task(worker.stop())
        await asyncio.sleep(0)
        assert not stop.done()
        # stop wakes the interval wait but lets an active backend call finish.
        periodic_release.set()
        await asyncio.wait_for(stop, timeout=2)
        assert worker.last_error is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_startup_cancellation_drains_started_sweep() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def blocked_sweep(batch_size: int, cursor: str | None) -> CleanupSweepReport:
        entered.set()
        await release.wait()
        finished.set()
        return CleanupSweepReport(
            deployment_id="deploy-a",
            scanned=0,
            eligible=0,
            cleaned=0,
            deferred=0,
            failed=0,
        )

    worker = ProjectionCleanupWorker(interval_s=30, batch_size=1, sweep=blocked_sweep)
    startup = asyncio.create_task(worker.start())
    await asyncio.wait_for(entered.wait(), timeout=2)
    startup.cancel()
    await asyncio.sleep(0)
    assert not startup.done()
    assert not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(startup, timeout=2)
    assert finished.is_set()
    await worker.stop()


@pytest.mark.asyncio
async def test_worker_stop_during_startup_drains_before_start_returns() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def blocked_sweep(batch_size: int, cursor: str | None) -> CleanupSweepReport:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return CleanupSweepReport(
            deployment_id="deploy-a",
            scanned=0,
            eligible=0,
            cleaned=0,
            deferred=0,
            failed=0,
        )

    worker = ProjectionCleanupWorker(interval_s=30, batch_size=1, sweep=blocked_sweep)
    startup = asyncio.create_task(worker.start())
    await asyncio.wait_for(entered.wait(), timeout=2)
    stop = asyncio.create_task(worker.stop())
    await asyncio.sleep(0)
    assert not startup.done()
    assert not stop.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(startup, stop), timeout=2)
    await asyncio.sleep(0.02)
    assert calls == 1


@pytest.mark.asyncio
async def test_worker_stop_preserves_cancellation_after_draining_periodic_sweep() -> None:
    periodic_entered = asyncio.Event()
    periodic_release = asyncio.Event()
    calls = 0

    async def blocked_periodic(batch_size: int, cursor: str | None) -> CleanupSweepReport:
        nonlocal calls
        calls += 1
        if calls == 1:
            return CleanupSweepReport(
                deployment_id="deploy-a",
                scanned=0,
                eligible=0,
                cleaned=0,
                deferred=0,
                failed=0,
            )
        periodic_entered.set()
        await periodic_release.wait()
        return CleanupSweepReport(
            deployment_id="deploy-a",
            scanned=0,
            eligible=0,
            cleaned=0,
            deferred=0,
            failed=0,
        )

    worker = ProjectionCleanupWorker(interval_s=0.01, batch_size=1, sweep=blocked_periodic)
    await worker.start()
    await asyncio.wait_for(periodic_entered.wait(), timeout=2)
    stopping = asyncio.create_task(worker.stop())
    await asyncio.sleep(0)
    stopping.cancel()
    await asyncio.sleep(0)
    assert not stopping.done()
    periodic_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stopping, timeout=2)
    assert calls == 2


@pytest.mark.asyncio
async def test_sweep_cursor_advances_in_bounded_batches() -> None:
    clock = _Clock()
    strategy = _ProjectionStrategy()
    engine, _store, coordinator = await _setup(strategy, clock)
    try:
        for service in ("graph.one", "graph.two", "graph.three"):
            await _activate(coordinator, _request(service=service), retention_s=3600)
        first = await coordinator.cleanup_expired_projections("deploy-a", limit=2)
        assert first.scanned == 2
        assert first.next_cursor is not None
        second = await coordinator.cleanup_expired_projections(
            "deploy-a", limit=2, after_operation_id=first.next_cursor
        )
        assert second.scanned == 1
        assert second.next_cursor is None
    finally:
        await engine.dispose()
