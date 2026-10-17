from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from memspine.providers.changes import (
    ActiveChangeExists,
    ChangeConflict,
    ChangeCoordinator,
    ChangeRequest,
    ChangeScope,
    ChangeStatus,
    DependencyRevision,
    IncompleteChangeSource,
    OperationCheckpoint,
    PlanStep,
    ProjectionRebuildResult,
    ResourceEstimate,
    SourceCompleteness,
    SqlChangeStore,
    StaleChangePlan,
    TargetConfigRef,
    UnsupportedChangeStrategy,
)


class RevisionGuard:
    source = "graph-r1"
    target = "fp-target"
    revisions: dict[str, str] = {}

    async def current_revision(self, logical_service_id: str) -> str:
        return self.revisions.get(logical_service_id, self.source)

    async def current_target_fingerprint(self, config_id: str) -> str:
        return self.target


class Strategy:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.activation_count = 0
        self.cleanup_count = 0

    async def execute(self, context) -> None:
        self.calls += 1
        if context.operation.checkpoint.phase == "planned":
            await context.checkpoint(
                OperationCheckpoint(phase="copying", source_checkpoint=2, target_checkpoint=2)
            )
        if self.fail_first and self.calls == 1:
            raise OSError("simulated interruption")

    async def verify(self, context):
        now = datetime.now(UTC)
        return ProjectionRebuildResult(
            operation_id=context.operation.operation_id,
            status=ChangeStatus.VERIFIED,
            created_at=now,
            updated_at=now,
            deployment_id=context.plan.request.scope.deployment_id,
            source_generation=context.plan.request.scope.source_generation or "",
            target_generation=context.plan.request.scope.target_generation or "",
            source_checkpoint=8,
            target_checkpoint=8,
            erase_checkpoint=2,
            verification_report_id="verify-report-1",
        )

    async def activate(self, context, result) -> None:
        self.activation_count += 1

    async def cleanup(self, context, result):
        self.cleanup_count += 1
        return result


def make_request(*, complete: bool = True, deployment_id: str = "deploy-a") -> ChangeRequest:
    evidence = SourceCompleteness(
        complete=complete,
        evidence_id="source-proof" if complete else None,
        source_checkpoint=4,
        source_head_seq=8,
        missing_ranges=() if complete else ("5..8",),
    )
    return ChangeRequest(
        category="graph",
        logical_service_id="graph.primary",
        source_revision="graph-r1",
        target_revision="neo4j-r2",
        scope=ChangeScope(
            deployment_id=deployment_id,
            owner_id=f"{deployment_id}:graph",
            resource_class="projection",
            source_provider="ladybug",
            target_provider="neo4j",
            source_generation="gen-old",
            target_generation="gen-new",
            source_target_fingerprint="fp-old",
            target_fingerprint="fp-target",
        ),
        target_config_ref=TargetConfigRef(
            provider="neo4j", config_id="graph-config-v2", semantic_fingerprint="fp-target"
        ),
        requested_strategy="projection_rebuild",
        actor="operator",
        dependency_revisions=(DependencyRevision(service_id="storage", revision="sql-v1"),),
        source_completeness=evidence,
    )


async def setup_coordinator(*, strategy: Strategy, now=None):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlChangeStore(engine)
    await store.create_schema()
    guard = RevisionGuard()
    guard.revisions = {"storage": "sql-v1"}
    coordinator = ChangeCoordinator(
        store=store,
        revision_guard=guard,
        strategies={("graph", "projection_rebuild"): strategy},
        plan_ttl_s=3600,
        now=now or (lambda: datetime.now(UTC)),
    )
    return engine, store, guard, coordinator


async def make_plan(coordinator, request=None, *, retention_s: int = 0):
    return await coordinator.plan_change(
        request or make_request(),
        steps=(PlanStep(step_id="copy", phase="migrate", description="Copy source state"),),
        required_capabilities=frozenset({"checkpoint", "scoped_delete"}),
        estimate=ResourceEstimate(bytes=100),
        retention_s=retention_s,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["status", "errors"])
async def test_verification_cannot_activate_failed_or_error_result(invalid: str) -> None:
    class InvalidStrategy(Strategy):
        async def verify(self, context):
            result = await super().verify(context)
            update = (
                {"status": ChangeStatus.FAILED}
                if invalid == "status"
                else {"errors": ("mandatory probe failed",)}
            )
            return result.model_copy(update=update)

    strategy = InvalidStrategy()
    engine, store, _, coordinator = await setup_coordinator(strategy=strategy)
    try:
        plan = await make_plan(coordinator)
        operation = await coordinator.execute_change(plan.plan_id)
        with pytest.raises(ChangeConflict, match="verified result without errors"):
            await coordinator.verify_change(operation.operation_id)
        assert strategy.activation_count == 0
        assert (await store.get_operation(operation.operation_id)).status is ChangeStatus.FAILED
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_plan_is_persisted_then_sql_fence_blocks_second_operation() -> None:
    strategy = Strategy()
    engine, store, _, coordinator = await setup_coordinator(strategy=strategy)
    try:
        first = await make_plan(coordinator)
        second = await make_plan(coordinator)
        assert (await store.get_plan(first.plan_id)) == first
        operation = await coordinator.execute_change(first.plan_id)
        assert operation.status == ChangeStatus.RUNNING
        with pytest.raises(ActiveChangeExists):
            await coordinator.execute_change(second.plan_id)
        assert strategy.calls == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_interrupted_operation_resumes_same_id_and_monotonic_checkpoint() -> None:
    strategy = Strategy(fail_first=True)
    engine, store, _, coordinator = await setup_coordinator(strategy=strategy)
    try:
        plan = await make_plan(coordinator)
        with pytest.raises(OSError, match="interruption"):
            await coordinator.execute_change(plan.plan_id)
        first_record = await store.get_operation_for_plan(plan.plan_id)
        assert first_record.status == ChangeStatus.FAILED
        assert first_record.checkpoint.target_checkpoint == 2
        resumed = await coordinator.resume_change(first_record.operation_id)
        assert resumed.operation_id == first_record.operation_id
        assert resumed.fence_epoch > first_record.fence_epoch
        assert resumed.checkpoint.target_checkpoint == 2
        assert strategy.calls == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_source_or_dependency_revision_rejects_plan_before_fence() -> None:
    engine, store, guard, coordinator = await setup_coordinator(strategy=Strategy())
    try:
        plan = await make_plan(coordinator)
        guard.revisions["storage"] = "sql-v2"
        with pytest.raises(StaleChangePlan, match="dependency"):
            await coordinator.execute_change(plan.plan_id)
        guard.revisions["storage"] = "sql-v1"
        guard.source = "graph-r2"
        with pytest.raises(StaleChangePlan, match="source revision"):
            await coordinator.execute_change(plan.plan_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_incomplete_source_and_unsupported_category_never_start() -> None:
    strategy = Strategy()
    engine, _, _, coordinator = await setup_coordinator(strategy=strategy)
    try:
        plan = await make_plan(coordinator, make_request(complete=False))
        with pytest.raises(IncompleteChangeSource):
            await coordinator.execute_change(plan.plan_id)
        assert strategy.calls == 0
        unsupported = await make_plan(
            coordinator,
            make_request().model_copy(
                update={"category": "embedding", "requested_strategy": "reembed"}
            ),
        )
        assert not unsupported.supported
        with pytest.raises(UnsupportedChangeStrategy):
            await coordinator.execute_change(unsupported.plan_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_verify_activates_then_production_cleanup_waits_for_retention() -> None:
    now = datetime.now(UTC)
    strategy = Strategy()
    engine, store, _, coordinator = await setup_coordinator(strategy=strategy, now=lambda: now)
    try:
        plan = await make_plan(coordinator, retention_s=604800)
        operation = await coordinator.execute_change(plan.plan_id)
        result = await coordinator.verify_change(operation.operation_id)
        active = await store.get_operation(operation.operation_id)
        assert result.status == ChangeStatus.ACTIVE
        assert active.status == ChangeStatus.ACTIVE
        assert active.activated_at == now
        assert strategy.activation_count == 1
        with pytest.raises(ChangeConflict, match="retention"):
            await coordinator.cleanup_change(operation.operation_id)
        coordinator._now = lambda: now + timedelta(days=7)  # type: ignore[method-assign]
        cleaned = await coordinator.cleanup_change(operation.operation_id)
        assert cleaned.status == ChangeStatus.CLEANED
        assert strategy.cleanup_count == 1
    finally:
        await engine.dispose()
