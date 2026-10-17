"""Explicit backend-driven change strategies for non-projection services.

This module supplies orchestration and evidence checks only. It does not create
clients, call paid model APIs, copy authoritative data, or delete resources on
its own. A category is executable only when its caller injects a backend that
owns those operations and can return verifiable evidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol

from pydantic import Field

from memspine.providers.changes import (
    CacheResetResult,
    ChangeCategory,
    ChangeConflict,
    ChangeContext,
    ChangePlan,
    ChangeResult,
    ChangeStatus,
    DatabaseMigrationResult,
    EmbeddingMigrationResult,
    ModelRevisionResult,
    OperationCheckpoint,
    OperationRecord,
    WorkerHandoverResult,
)
from memspine.providers.settings import StrictSettings


class ChangePreparation(StrictSettings):
    """Non-secret evidence that a backend prepared the planned target."""

    operation_id: str = Field(min_length=1)
    category: ChangeCategory
    owner_id: str = Field(min_length=1)
    source_fingerprint: str = Field(min_length=1)
    target_fingerprint: str = Field(min_length=1)
    target_generation: str = Field(min_length=1)
    policy: Literal[
        "explicit_reembed",
        "forward_only_revision",
        "verified_fenced_migration",
        "owned_namespace_coldstart",
        "fenced_worker_handover",
    ]


class ServiceChangeBackend(Protocol):
    """Injected execution surface; implementation owns idempotency and I/O."""

    async def prepare(
        self, plan: ChangePlan, operation: OperationRecord
    ) -> ChangePreparation: ...

    async def execute(
        self,
        plan: ChangePlan,
        operation: OperationRecord,
        checkpoint: OperationCheckpoint,
    ) -> OperationCheckpoint: ...

    async def verify(self, plan: ChangePlan, operation: OperationRecord) -> ChangeResult: ...

    async def activate(
        self, plan: ChangePlan, operation: OperationRecord, result: ChangeResult
    ) -> None: ...

    async def cleanup(
        self, plan: ChangePlan, operation: OperationRecord, result: ChangeResult
    ) -> ChangeResult: ...


_CATEGORY_CONFIG: dict[tuple[ChangeCategory, str], tuple[str, type[ChangeResult]]] = {
    ("embedding", "reembed"): ("embedding_space", EmbeddingMigrationResult),
    ("llm", "forward_revision"): ("model_config", ModelRevisionResult),
    ("reranker", "forward_revision"): ("model_config", ModelRevisionResult),
    ("storage", "verified_migration"): ("authoritative_database", DatabaseMigrationResult),
    ("cache", "coldstart_reset"): ("cache", CacheResetResult),
    ("worker", "handover"): ("worker", WorkerHandoverResult),
}
_CATEGORY_POLICY: dict[tuple[ChangeCategory, str], str] = {
    ("embedding", "reembed"): "explicit_reembed",
    ("llm", "forward_revision"): "forward_only_revision",
    ("reranker", "forward_revision"): "forward_only_revision",
    ("storage", "verified_migration"): "verified_fenced_migration",
    ("cache", "coldstart_reset"): "owned_namespace_coldstart",
    ("worker", "handover"): "fenced_worker_handover",
}


def _nonempty(value: str | None, field: str) -> None:
    if not value or not value.strip():
        raise ChangeConflict(f"backend evidence is missing {field}")


class ServiceChangeStrategy:
    """Validate ownership/fingerprints around an explicitly injected backend."""

    def __init__(
        self,
        category: ChangeCategory,
        strategy_name: str,
        backend: ServiceChangeBackend,
    ) -> None:
        key = (category, strategy_name)
        config = _CATEGORY_CONFIG.get(key)
        if config is None:
            raise ValueError("unsupported service change category/strategy pair")
        self.category = category
        self.strategy_name = strategy_name
        self.resource_class, self.result_type = config
        self.backend = backend

    def _validate_plan(self, plan: ChangePlan) -> None:
        request = plan.request
        scope = request.scope
        if request.category != self.category or request.requested_strategy != self.strategy_name:
            raise ChangeConflict("change plan does not match this strategy")
        if scope.resource_class != self.resource_class:
            raise ChangeConflict("change plan has the wrong resource class")
        if not request.source_completeness.complete:
            raise ChangeConflict("change source is not complete")
        if scope.owner_id == "" or scope.target_fingerprint != plan.target_semantic_fingerprint:
            raise ChangeConflict("change plan ownership or target fingerprint is inconsistent")
        if self.category in {"embedding", "cache", "worker", "storage"}:
            if not scope.source_generation or not scope.target_generation:
                raise ChangeConflict("change requires explicit source and target generations")
            if scope.source_generation == scope.target_generation:
                raise ChangeConflict("source and target generations must differ")
        if self.category == "storage":
            _nonempty(scope.source_target_fingerprint, "source target fingerprint")
        if self.category in {"cache", "worker", "embedding"}:
            _nonempty(scope.owner_id, "resource owner")

    async def execute(self, context: ChangeContext) -> None:
        plan, operation = context.plan, context.operation
        self._validate_plan(plan)
        prepared = await self.backend.prepare(plan, operation)
        scope = plan.request.scope
        if (
            prepared.operation_id != operation.operation_id
            or prepared.category != self.category
            or prepared.owner_id != scope.owner_id
            or prepared.target_fingerprint != plan.target_semantic_fingerprint
            or prepared.target_generation
            != (scope.target_generation or plan.request.target_revision)
        ):
            raise ChangeConflict("backend preparation evidence does not match the plan")
        expected_source = scope.source_target_fingerprint or plan.source_semantic_fingerprint
        if prepared.source_fingerprint != expected_source:
            raise ChangeConflict("backend source fingerprint does not match the plan")
        expected_policy = _CATEGORY_POLICY[(self.category, self.strategy_name)]
        if prepared.policy != expected_policy:
            raise ChangeConflict("backend did not attest the required bounded execution policy")
        await context.checkpoint(
            context.operation.checkpoint.model_copy(update={"phase": "prepared"})
        )
        checkpoint = await self.backend.execute(plan, context.operation, context.operation.checkpoint)
        if checkpoint.phase not in {"executed", "verified"}:
            raise ChangeConflict("backend did not provide a completed execution checkpoint")
        await context.checkpoint(checkpoint)

    async def verify(self, context: ChangeContext) -> ChangeResult:
        plan = context.plan
        self._validate_plan(plan)
        result = await self.backend.verify(plan, context.operation)
        if not isinstance(result, self.result_type):
            raise ChangeConflict("backend returned evidence for another change category")
        if result.operation_id != context.operation.operation_id:
            raise ChangeConflict("backend result belongs to another operation")
        if result.status != ChangeStatus.VERIFIED:
            raise ChangeConflict("backend result is not verified")
        self._validate_result(plan, result)
        return result

    async def activate(self, context: ChangeContext, result: ChangeResult) -> None:
        if not isinstance(result, self.result_type):
            raise ChangeConflict("activation result does not match this strategy")
        self._validate_result(context.plan, result)
        await self.backend.activate(context.plan, context.operation, result)

    async def cleanup(self, context: ChangeContext, result: ChangeResult) -> ChangeResult:
        if self.category not in {"embedding", "cache", "worker"}:
            raise ChangeConflict("cleanup is not supported for this service change")
        if not isinstance(result, self.result_type):
            raise ChangeConflict("cleanup result does not match this strategy")
        self._validate_result(context.plan, result)
        cleaned = await self.backend.cleanup(context.plan, context.operation, result)
        if not isinstance(cleaned, self.result_type):
            raise ChangeConflict("backend cleanup returned another result category")
        if cleaned.operation_id != result.operation_id or cleaned.status != ChangeStatus.CLEANED:
            raise ChangeConflict("backend cleanup did not confirm this operation as cleaned")
        self._validate_result(context.plan, cleaned, cleanup=True)
        return cleaned

    def _validate_result(
        self, plan: ChangePlan, result: ChangeResult, *, cleanup: bool = False
    ) -> None:
        scope = plan.request.scope
        if isinstance(result, EmbeddingMigrationResult):
            if (
                result.old_space_id != scope.source_generation
                or result.new_space_id != scope.target_generation
                or result.target_generation != scope.target_generation
                or result.query_encoder_revision != plan.request.target_revision
                or result.document_encoder_revision != plan.request.target_revision
                or result.pending_record_count != 0
            ):
                raise ChangeConflict("embedding migration evidence does not match the target space")
            if not cleanup and not result.verification_report_id:
                raise ChangeConflict("embedding migration requires a verification report")
            if cleanup:
                _nonempty(result.cleanup_report_id, "owned old-space cleanup report")
        elif isinstance(result, ModelRevisionResult):
            if (
                result.logical_role != plan.request.logical_service_id
                or result.old_revision != plan.request.source_revision
                or result.new_revision != plan.request.target_revision
            ):
                raise ChangeConflict("model revision evidence does not match the forward plan")
            _nonempty(result.producer_revision, "producer revision")
            _nonempty(result.prompt_schema_revision, "prompt schema revision")
            _nonempty(result.validation_report_id, "model revision validation report")
            if cleanup:
                raise ChangeConflict("model artifacts cannot be automatically cleaned up")
        elif isinstance(result, DatabaseMigrationResult):
            if (
                result.source_target_fingerprint != scope.source_target_fingerprint
                or result.target_fingerprint != plan.target_semantic_fingerprint
                or result.cutover_ack_state != "acknowledged"
                or result.source_fence_epoch < 1
            ):
                raise ChangeConflict("database cutover lacks authoritative fence/receipt evidence")
            _nonempty(result.source_intent_id, "source intent")
            _nonempty(result.target_receipt_id, "target receipt")
            _nonempty(result.schema_revision, "schema revision")
            _nonempty(result.verification_report_id, "verification report")
            if cleanup:
                raise ChangeConflict("authoritative databases cannot be automatically cleaned up")
        elif isinstance(result, CacheResetResult):
            if (
                result.old_key_generation != scope.source_generation
                or result.new_key_generation != scope.target_generation
                or result.owned_prefix != scope.owner_id
            ):
                raise ChangeConflict("cache result exceeds its owned namespace")
            _nonempty(result.ttl_policy_revision, "TTL policy revision")
            if cleanup and result.purge_status != "complete":
                raise ChangeConflict("cache cleanup did not confirm a complete scoped purge")
            if cleanup:
                _nonempty(result.purge_report_id, "owned cache purge report")
        elif isinstance(result, WorkerHandoverResult):
            if (
                result.source_generation != scope.source_generation
                or result.target_generation != scope.target_generation
                or result.new_owner_epoch <= result.old_owner_epoch
                or result.pending_job_count != 0
                or result.lease_transfer_status != "complete"
                or result.ack_reconciliation_status != "complete"
            ):
                raise ChangeConflict(
                    "worker handover evidence does not match the planned generations or is not fenced and fully reconciled"
                )
            if result.pending_job_count != 0:
                raise ChangeConflict("worker cleanup requires proof that no jobs remain pending")
            if cleanup:
                _nonempty(result.old_owner_release_report_id, "old worker owner release report")


def build_service_change_strategies(
    backends: Mapping[tuple[ChangeCategory, str], ServiceChangeBackend],
) -> dict[tuple[ChangeCategory, str], ServiceChangeStrategy]:
    """Build only the strategies for which an implementation was injected."""
    return {key: ServiceChangeStrategy(*key, backend) for key, backend in backends.items()}


__all__ = [
    "ChangePreparation",
    "ServiceChangeBackend",
    "ServiceChangeStrategy",
    "build_service_change_strategies",
]
