from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from memspine.providers.changes import (
    CacheResetResult,
    ChangeConflict,
    ChangeCoordinator,
    ChangeRequest,
    ChangeScope,
    ChangeStatus,
    DatabaseMigrationResult,
    DependencyRevision,
    EmbeddingMigrationResult,
    ModelRevisionResult,
    PlanStep,
    ResourceEstimate,
    SourceCompleteness,
    SqlChangeStore,
    TargetConfigRef,
    WorkerHandoverResult,
)
from memspine.providers.service_changes import (
    ChangePreparation,
    ServiceChangeBackend,
    build_service_change_strategies,
)


class Guard:
    async def current_revision(self, logical_service_id: str) -> str:
        return "rev-old"

    async def current_target_fingerprint(self, config_id: str) -> str:
        return "fp-target"


class FakeBackend(ServiceChangeBackend):
    """Protocol test double; it performs no external work."""

    def __init__(
        self,
        *,
        corrupt_result: bool = False,
        wrong_cache_owner: bool = False,
        omit_cleanup_proof: bool = False,
        wrong_worker_generation: bool = False,
    ) -> None:
        self.corrupt_result = corrupt_result
        self.wrong_cache_owner = wrong_cache_owner
        self.omit_cleanup_proof = omit_cleanup_proof
        self.wrong_worker_generation = wrong_worker_generation
        self.execute_count = 0
        self.activated = 0
        self.cleaned = 0

    async def prepare(self, plan, operation):
        scope = plan.request.scope
        return ChangePreparation(
            operation_id=operation.operation_id,
            category=plan.request.category,
            owner_id=scope.owner_id,
            source_fingerprint=scope.source_target_fingerprint or plan.source_semantic_fingerprint,
            target_fingerprint=plan.target_semantic_fingerprint,
            target_generation=scope.target_generation or plan.request.target_revision,
            policy={
                "embedding": "explicit_reembed",
                "llm": "forward_only_revision",
                "rerank": "forward_only_revision",
                "storage": "verified_fenced_migration",
                "cache": "owned_namespace_coldstart",
                "worker": "fenced_worker_handover",
            }[plan.request.category],
        )

    async def execute(self, plan, operation, checkpoint):
        self.execute_count += 1
        if self.corrupt_result and self.execute_count == 1:
            return checkpoint.model_copy(update={"phase": "started"})
        return checkpoint.model_copy(update={"phase": "executed"})

    async def verify(self, plan, operation):
        now = datetime.now(UTC)
        common = {
            "operation_id": operation.operation_id,
            "status": ChangeStatus.VERIFIED,
            "created_at": now,
            "updated_at": now,
        }
        scope = plan.request.scope
        category = plan.request.category
        if category == "embedding":
            return EmbeddingMigrationResult(
                **common,
                old_space_id=scope.source_generation,
                new_space_id=scope.target_generation,
                target_generation=scope.target_generation,
                query_encoder_revision=plan.request.target_revision,
                document_encoder_revision=plan.request.target_revision,
                indexed_record_count=12,
                pending_record_count=0,
                verification_report_id="embedding-report",
            )
        if category in {"llm", "rerank"}:
            return ModelRevisionResult(
                **common,
                logical_role=plan.request.logical_service_id,
                old_revision=plan.request.source_revision,
                new_revision=plan.request.target_revision,
                producer_revision="producer-v2",
                prompt_schema_revision="prompt-schema-v1",
                in_flight_policy="preserve_revision",
                validation_report_id="model-report",
            )
        if category == "storage":
            return DatabaseMigrationResult(
                **common,
                source_target_fingerprint=scope.source_target_fingerprint,
                target_fingerprint=plan.target_semantic_fingerprint,
                source_fence_epoch=4,
                source_intent_id="intent-4",
                target_receipt_id="receipt-4",
                schema_revision="schema-v2",
                copy_checkpoint=12,
                verification_report_id="db-report",
                cutover_ack_state="acknowledged",
            )
        if category == "cache":
            return CacheResetResult(
                **common,
                old_key_generation=scope.source_generation,
                new_key_generation=scope.target_generation,
                owned_prefix="other-owner" if self.wrong_cache_owner else scope.owner_id,
                ttl_policy_revision="ttl-v3",
                purge_status="pending",
            )
        return WorkerHandoverResult(
            **common,
            source_generation=(
                "other-generation" if self.wrong_worker_generation else scope.source_generation
            ),
            target_generation=scope.target_generation,
            old_owner_epoch=7,
            new_owner_epoch=8,
            payload_contract_version=2,
            pending_job_count=0,
            lease_transfer_status="complete",
            ack_reconciliation_status="complete",
        )

    async def activate(self, plan, operation, result):
        self.activated += 1

    async def cleanup(self, plan, operation, result):
        self.cleaned += 1
        if isinstance(result, CacheResetResult):
            return result.model_copy(
                update={
                    "status": ChangeStatus.CLEANED,
                    "purge_status": "complete",
                    "purge_report_id": None if self.omit_cleanup_proof else "cache-purge-report",
                }
            )
        if isinstance(result, EmbeddingMigrationResult):
            return result.model_copy(
                update={
                    "status": ChangeStatus.CLEANED,
                    "cleanup_report_id": None
                    if self.omit_cleanup_proof
                    else "embedding-cleanup-report",
                }
            )
        if isinstance(result, WorkerHandoverResult):
            return result.model_copy(
                update={
                    "status": ChangeStatus.CLEANED,
                    "old_owner_release_report_id": None
                    if self.omit_cleanup_proof
                    else "worker-release-report",
                }
            )
        return result.model_copy(update={"status": ChangeStatus.CLEANED})


def make_request(category: str, strategy: str, resource: str) -> ChangeRequest:
    uses_generations = category in {"embedding", "storage", "cache", "worker"}
    scope = ChangeScope(
        deployment_id="deployment-a",
        owner_id="namespace-a",
        resource_class=resource,
        source_generation="generation-old" if uses_generations else None,
        target_generation="generation-new" if uses_generations else None,
        source_target_fingerprint="fp-source" if category == "storage" else None,
        target_fingerprint="fp-target",
    )
    return ChangeRequest(
        category=category,
        logical_service_id="service-a",
        source_revision="rev-old",
        target_revision="rev-new",
        scope=scope,
        target_config_ref=TargetConfigRef(
            provider="target", config_id="config-a", semantic_fingerprint="fp-target"
        ),
        requested_strategy=strategy,
        actor="operator",
        dependency_revisions=(DependencyRevision(service_id="storage", revision="rev-old"),),
        source_completeness=SourceCompleteness(complete=True, evidence_id="source-complete"),
    )


CASES = (
    ("embedding", "reembed", "embedding_space", True),
    ("llm", "forward_revision", "model_config", False),
    ("rerank", "forward_revision", "model_config", False),
    ("storage", "verified_migration", "authoritative_database", False),
    ("cache", "coldstart_reset", "cache", True),
    ("worker", "handover", "worker", True),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("category", "strategy_name", "resource", "cleanup"), CASES)
async def test_injected_category_backend_runs_durable_plan_to_typed_result(
    category: str, strategy_name: str, resource: str, cleanup: bool
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        store = SqlChangeStore(engine)
        await store.create_schema()
        backend = FakeBackend()
        strategies = build_service_change_strategies({((category, strategy_name)): backend})
        coordinator = ChangeCoordinator(
            store=store,
            revision_guard=Guard(),
            strategies=strategies,
            plan_ttl_s=3600,
        )
        plan = await coordinator.plan_change(
            make_request(category, strategy_name, resource),
            steps=(PlanStep(step_id="prepare", phase="prepare", description="Prepare target"),),
            required_capabilities=frozenset({"explicit_backend", "verification"}),
            estimate=ResourceEstimate(),
            retention_s=0 if cleanup else None,
        )
        operation = await coordinator.execute_change(plan.plan_id)
        assert operation.checkpoint.phase == "executed"
        result = await coordinator.verify_change(operation.operation_id)
        assert result.status == ChangeStatus.ACTIVE
        assert backend.activated == 1
        if cleanup:
            cleaned = await coordinator.cleanup_change(operation.operation_id)
            assert cleaned.status == ChangeStatus.CLEANED
            assert backend.cleaned == 1
        else:
            with pytest.raises(ChangeConflict, match="unavailable"):
                await coordinator.cleanup_change(operation.operation_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_missing_backend_persists_unsupported_plan_without_execution() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        store = SqlChangeStore(engine)
        await store.create_schema()
        coordinator = ChangeCoordinator(
            store=store,
            revision_guard=Guard(),
            strategies=build_service_change_strategies({}),
            plan_ttl_s=3600,
        )
        request = make_request("storage", "verified_migration", "authoritative_database")
        plan = await coordinator.plan_change(
            request,
            steps=(),
            required_capabilities=frozenset(),
        )
        assert not plan.supported
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_backend_must_report_completed_checkpoint_before_verification() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        store = SqlChangeStore(engine)
        await store.create_schema()
        backend = FakeBackend(corrupt_result=True)
        strategies = build_service_change_strategies({("cache", "coldstart_reset"): backend})
        coordinator = ChangeCoordinator(
            store=store,
            revision_guard=Guard(),
            strategies=strategies,
            plan_ttl_s=3600,
        )
        plan = await coordinator.plan_change(
            make_request("cache", "coldstart_reset", "cache"),
            steps=(),
            required_capabilities=frozenset(),
        )
        with pytest.raises(ChangeConflict, match="completed execution checkpoint"):
            await coordinator.execute_change(plan.plan_id)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_interrupted_backend_resumes_same_durable_operation() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        store = SqlChangeStore(engine)
        await store.create_schema()
        backend = FakeBackend(corrupt_result=True)
        coordinator = ChangeCoordinator(
            store=store,
            revision_guard=Guard(),
            strategies=build_service_change_strategies({("cache", "coldstart_reset"): backend}),
            plan_ttl_s=3600,
        )
        plan = await coordinator.plan_change(
            make_request("cache", "coldstart_reset", "cache"),
            steps=(),
            required_capabilities=frozenset(),
        )
        with pytest.raises(ChangeConflict):
            await coordinator.execute_change(plan.plan_id)
        failed = await store.get_operation_for_plan(plan.plan_id)
        resumed = await coordinator.resume_change(failed.operation_id)
        assert resumed.operation_id == failed.operation_id
        assert resumed.fence_epoch > failed.fence_epoch
        assert resumed.checkpoint.phase == "executed"
        assert backend.execute_count == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cache_result_cannot_cross_namespace_owner_boundary() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        store = SqlChangeStore(engine)
        await store.create_schema()
        backend = FakeBackend(wrong_cache_owner=True)
        coordinator = ChangeCoordinator(
            store=store,
            revision_guard=Guard(),
            strategies=build_service_change_strategies({("cache", "coldstart_reset"): backend}),
            plan_ttl_s=3600,
        )
        plan = await coordinator.plan_change(
            make_request("cache", "coldstart_reset", "cache"),
            steps=(),
            required_capabilities=frozenset(),
        )
        operation = await coordinator.execute_change(plan.plan_id)
        with pytest.raises(ChangeConflict, match="owned namespace"):
            await coordinator.verify_change(operation.operation_id)
        assert backend.activated == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_handover_result_must_match_planned_generations() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        store = SqlChangeStore(engine)
        await store.create_schema()
        backend = FakeBackend(wrong_worker_generation=True)
        coordinator = ChangeCoordinator(
            store=store,
            revision_guard=Guard(),
            strategies=build_service_change_strategies({("worker", "handover"): backend}),
            plan_ttl_s=3600,
        )
        plan = await coordinator.plan_change(
            make_request("worker", "handover", "worker"),
            steps=(),
            required_capabilities=frozenset(),
        )
        operation = await coordinator.execute_change(plan.plan_id)
        with pytest.raises(ChangeConflict, match="planned generations"):
            await coordinator.verify_change(operation.operation_id)
        assert backend.activated == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_owned_cache_cleanup_requires_purge_receipt() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        store = SqlChangeStore(engine)
        await store.create_schema()
        backend = FakeBackend(omit_cleanup_proof=True)
        coordinator = ChangeCoordinator(
            store=store,
            revision_guard=Guard(),
            strategies=build_service_change_strategies({("cache", "coldstart_reset"): backend}),
            plan_ttl_s=3600,
        )
        plan = await coordinator.plan_change(
            make_request("cache", "coldstart_reset", "cache"),
            steps=(),
            required_capabilities=frozenset(),
            retention_s=0,
        )
        operation = await coordinator.execute_change(plan.plan_id)
        active = await coordinator.verify_change(operation.operation_id)
        assert active.status == ChangeStatus.ACTIVE
        with pytest.raises(ChangeConflict, match="purge report"):
            await coordinator.cleanup_change(operation.operation_id)
        record = await store.get_operation(operation.operation_id)
        assert record.status == ChangeStatus.CLEANUP_PENDING
    finally:
        await engine.dispose()


def test_factory_rejects_unknown_category_strategy_pair() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        build_service_change_strategies({("storage", "silent_success"): FakeBackend()})


@pytest.mark.parametrize("ttl", [True, 0, -1, float("inf"), float("nan")])
def test_coordinator_rejects_non_positive_or_non_finite_plan_ttl(ttl: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        ChangeCoordinator(
            store=None,  # type: ignore[arg-type]
            revision_guard=None,  # type: ignore[arg-type]
            strategies={},
            plan_ttl_s=ttl,
        )
