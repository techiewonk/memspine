from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from memspine.providers.changes import (
    ChangeConflict,
    ChangeCoordinator,
    ChangeError,
    ChangeRequest,
    ChangeScope,
    ChangeStatus,
    PlanStep,
    ProjectionRebuildResult,
    ResourceEstimate,
    SourceCompleteness,
    SqlChangeStore,
    StaleChangePlan,
    TargetConfigRef,
    _fences,
)
from memspine.providers.control import EngineProviderChanges
from memspine.providers.recovery import (
    CleanupFenceRecoveryRequest,
    CleanupQuiescenceProof,
)


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
    def __init__(self) -> None:
        self.cleanup_calls = 0

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
        return result


class _QuiescenceGuard:
    def __init__(self, clock: _Clock, *, expired: bool = False) -> None:
        self._clock = clock
        self._expired = expired
        self.calls = 0
        self.proof_id = "proof-cleanup-1"

    async def prove_quiescence(
        self, request: CleanupFenceRecoveryRequest
    ) -> CleanupQuiescenceProof:
        self.calls += 1
        now = self._clock()
        return CleanupQuiescenceProof(
            deployment_id=request.deployment_id,
            operation_id=request.operation_id,
            operation_revision=request.expected_operation_revision,
            fence_epoch=request.expected_fence_epoch,
            evidence_reference=request.evidence_reference,
            proof_id=self.proof_id,
            issued_at=now - (timedelta(minutes=2) if self._expired else timedelta(seconds=1)),
            expires_at=now - timedelta(minutes=1) if self._expired else now + timedelta(minutes=5),
        )


def _request(deployment_id: str = "deploy-a") -> ChangeRequest:
    service = "graph.primary"
    return ChangeRequest(
        category="graph",
        logical_service_id=service,
        source_revision="source-r1",
        target_revision="target-r2",
        scope=ChangeScope(
            deployment_id=deployment_id,
            owner_id=deployment_id,
            resource_class="projection",
            source_provider="old-provider",
            target_provider="new-provider",
            source_generation="graph-old",
            target_generation="graph-new",
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


async def _setup(database_url: str = "sqlite+aiosqlite:///:memory:"):
    clock = _Clock()
    engine = create_async_engine(database_url)
    store = SqlChangeStore(engine)
    await store.create_schema()
    strategy = _ProjectionStrategy()
    coordinator = ChangeCoordinator(
        store=store,
        revision_guard=_RevisionGuard(),
        strategies={("graph", "projection_rebuild"): strategy},
        plan_ttl_s=3600,
        now=clock,
    )
    plan = await coordinator.plan_change(
        _request(),
        steps=(PlanStep(step_id="rebuild", phase="migrate", description="Rebuild projection"),),
        required_capabilities=frozenset({"checkpoint", "scoped_delete", "erasure"}),
        estimate=ResourceEstimate(),
        retention_s=0,
    )
    operation = await coordinator.execute_change(plan.plan_id)
    operation = await coordinator.verify_change(operation.operation_id)
    pending = await store.claim_cleanup(operation.operation_id)
    return engine, store, coordinator, clock, strategy, pending


def _recovery_request(operation, **updates) -> CleanupFenceRecoveryRequest:
    values = {
        "deployment_id": "deploy-a",
        "operation_id": operation.operation_id,
        "expected_operation_revision": operation.operation_revision,
        "expected_fence_epoch": operation.fence_epoch,
        "evidence_reference": "incident-2026-10-17-ops-42",
        "actor": "operator-42",
    }
    values.update(updates)
    return CleanupFenceRecoveryRequest(**values)


@pytest.mark.asyncio
async def test_proven_recovery_advances_fence_and_resumes_same_cleanup() -> None:
    engine, store, coordinator, clock, strategy, pending = await _setup()
    try:
        guard = _QuiescenceGuard(clock)
        request = _recovery_request(pending)
        receipt = await coordinator.recover_abandoned_cleanup_fence(request, quiescence_guard=guard)
        assert receipt.previous_fence_epoch == pending.fence_epoch
        assert receipt.released_fence_epoch == pending.fence_epoch + 1
        assert receipt.operation_revision == pending.operation_revision + 1
        recovered_operation = await store.get_operation(pending.operation_id)
        assert recovered_operation.operation_revision == receipt.operation_revision
        assert recovered_operation.fence_epoch == receipt.released_fence_epoch
        assert recovered_operation.status is ChangeStatus.CLEANUP_PENDING

        async with engine.connect() as connection:
            fence = (
                await connection.execute(
                    select(_fences.c.epoch, _fences.c.active_operation_id).where(
                        _fences.c.deployment_id == "deploy-a"
                    )
                )
            ).one()
        assert fence.epoch == receipt.released_fence_epoch
        assert fence.active_operation_id is None
        with pytest.raises(StaleChangePlan, match="no longer owns"):
            await store.update_operation(pending, status=ChangeStatus.CLEANUP_PENDING)
        assert await store.audit_cleanup_fence_recoveries("deploy-a", pending.operation_id) == (
            receipt,
        )

        retried = await coordinator.cleanup_expired_projections("deploy-a", limit=5)
        assert retried.cleaned == 1 and retried.failed == 0
        assert (await store.get_operation(pending.operation_id)).status is ChangeStatus.CLEANED
        assert strategy.cleanup_calls == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_operation_update_and_recovery_serialize_on_fence_and_revision(tmp_path) -> None:
    engine, store, coordinator, clock, _strategy, pending = await _setup(
        f"sqlite+aiosqlite:///{tmp_path / 'operation-recovery-race.db'}"
    )
    try:
        guard = _QuiescenceGuard(clock)
        request = _recovery_request(pending)
        results = await asyncio.gather(
            store.update_operation(pending, status=ChangeStatus.CLEANUP_PENDING),
            coordinator.recover_abandoned_cleanup_fence(request, quiescence_guard=guard),
            return_exceptions=True,
        )
        succeeded = [result for result in results if not isinstance(result, BaseException)]
        failed = [result for result in results if isinstance(result, BaseException)]
        assert len(succeeded) == 1, repr(results)
        assert len(failed) == 1 and isinstance(failed[0], ChangeError), repr(results)

        operation = await store.get_operation(pending.operation_id)
        assert operation.operation_revision == pending.operation_revision + 1
        async with engine.connect() as connection:
            fence = (
                await connection.execute(
                    select(_fences.c.epoch, _fences.c.active_operation_id).where(
                        _fences.c.deployment_id == "deploy-a"
                    )
                )
            ).one()
        receipts = await store.audit_cleanup_fence_recoveries("deploy-a", pending.operation_id)
        if receipts:
            assert succeeded[0].operation_revision == operation.operation_revision
            assert fence.epoch == receipts[0].released_fence_epoch
            assert fence.active_operation_id is None
        else:
            assert fence.epoch == pending.fence_epoch
            assert fence.active_operation_id == pending.operation_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_competing_recoveries_use_exact_revision_epoch_and_one_time_proof(
    tmp_path,
) -> None:
    engine, store, coordinator, clock, _strategy, pending = await _setup(
        f"sqlite+aiosqlite:///{tmp_path / 'recovery-race.db'}"
    )
    try:
        guard = _QuiescenceGuard(clock)
        request = _recovery_request(pending)
        results = await asyncio.gather(
            coordinator.recover_abandoned_cleanup_fence(request, quiescence_guard=guard),
            coordinator.recover_abandoned_cleanup_fence(request, quiescence_guard=guard),
            return_exceptions=True,
        )
        assert sum(not isinstance(result, BaseException) for result in results) == 1
        assert sum(isinstance(result, ChangeError) for result in results) == 1, repr(results)
        assert (
            len(await store.audit_cleanup_fence_recoveries("deploy-a", pending.operation_id)) == 1
        )
        assert guard.calls == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_rejects_wrong_deployment_before_application_guard() -> None:
    engine, _store, coordinator, clock, _strategy, pending = await _setup()
    try:
        guard = _QuiescenceGuard(clock)
        request = _recovery_request(pending, deployment_id="deploy-b")
        with pytest.raises(ChangeConflict, match="another deployment"):
            await coordinator.recover_abandoned_cleanup_fence(request, quiescence_guard=guard)
        assert guard.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_rejects_expired_quiescence_proof() -> None:
    engine, store, coordinator, clock, _strategy, pending = await _setup()
    try:
        guard = _QuiescenceGuard(clock, expired=True)
        with pytest.raises(StaleChangePlan, match="not currently valid"):
            await coordinator.recover_abandoned_cleanup_fence(
                _recovery_request(pending), quiescence_guard=guard
            )
        assert (
            await store.get_operation(pending.operation_id)
        ).status is ChangeStatus.CLEANUP_PENDING
        assert await store.audit_cleanup_fence_recoveries("deploy-a", pending.operation_id) == ()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_rejects_stale_operation_revision_without_calling_guard() -> None:
    engine, store, coordinator, clock, _strategy, pending = await _setup()
    try:
        advanced = await store.update_operation(
            pending, status=ChangeStatus.CLEANUP_PENDING, error_code="RETRYABLE"
        )
        guard = _QuiescenceGuard(clock)
        with pytest.raises(StaleChangePlan, match="state changed"):
            await coordinator.recover_abandoned_cleanup_fence(
                _recovery_request(pending), quiescence_guard=guard
            )
        assert advanced.operation_revision == pending.operation_revision + 1
        assert guard.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_engine_controller_requires_injected_guard_and_limits_scope() -> None:
    engine, store, coordinator, clock, _strategy, pending = await _setup()
    try:
        no_guard = EngineProviderChanges(
            coordinator=coordinator,
            store=store,
            deployment_id="deploy-a",
            assert_active=lambda: None,
        )
        with pytest.raises(ChangeConflict, match="requires an application quiescence guard"):
            await no_guard.recover_abandoned_cleanup_fence(_recovery_request(pending))

        guard = _QuiescenceGuard(clock)
        controller = EngineProviderChanges(
            coordinator=coordinator,
            store=store,
            deployment_id="deploy-a",
            assert_active=lambda: None,
            cleanup_quiescence_guard=guard,
        )
        with pytest.raises(ChangeConflict, match="does not belong to this Engine"):
            await controller.recover_abandoned_cleanup_fence(
                _recovery_request(pending, deployment_id="deploy-b")
            )
        assert guard.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_engine_provider_changes_uses_injected_recovery_guard(tmp_path) -> None:
    from memspine import Engine
    from memspine.providers.control import ProviderChangeRuntime

    clock = _Clock()
    strategy = _ProjectionStrategy()
    guard = _QuiescenceGuard(clock)
    engine = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(tmp_path / "engine-recovery.db"), "busy_timeout_s": 5.0},
            },
            "cache": {"provider": "memory", "settings": {}},
        },
        embedding={"provider": "hash", "settings": {}},
        memories={"semantic": {"enabled": True}},
        provider_change_runtime=ProviderChangeRuntime(
            revision_guard=_RevisionGuard(),
            strategies={("graph", "projection_rebuild"): strategy},
            plan_ttl_s=3600,
            cleanup_quiescence_guard=guard,
        ),
    )
    await engine.start()
    try:
        changes = engine.provider_changes()
        deployment_id = engine._provider_owner_id
        assert deployment_id is not None
        request = _request(deployment_id)
        plan = await changes.plan_change(
            request,
            steps=(PlanStep(step_id="rebuild", phase="migrate", description="Rebuild projection"),),
            required_capabilities=frozenset({"checkpoint", "scoped_delete", "erasure"}),
            estimate=ResourceEstimate(),
            retention_s=0,
        )
        operation = await changes.execute_change(plan.plan_id)
        operation = await changes.verify_change(operation.operation_id)
        pending = await changes._store.claim_cleanup(operation.operation_id)
        receipt = await changes.recover_abandoned_cleanup_fence(
            _recovery_request(pending, deployment_id=deployment_id)
        )
        assert receipt.proof_id == guard.proof_id
        assert guard.calls == 1
        assert await changes.cleanup_fence_recoveries(pending.operation_id) == (receipt,)
    finally:
        await engine.stop()
