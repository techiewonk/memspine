"""Durable, explicit planning and execution contracts for provider changes."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from pydantic import Field, TypeAdapter, model_validator
from sqlalchemy import JSON, Column, Integer, String, Table, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from memspine.providers.contracts import Category
from memspine.providers.recovery import (
    CleanupFenceRecoveryReceipt,
    CleanupFenceRecoveryRequest,
    CleanupQuiescenceGuard,
    CleanupQuiescenceProof,
)
from memspine.providers.settings import StrictSettings
from memspine.services.storage.sqlite.schema import metadata as _metadata


class ChangeError(RuntimeError):
    """Base class for controlled-change errors safe to expose to callers."""

    code = "PROVIDER_CHANGE_FAILED"


# Worker ownership handover is a controlled service change rather than a
# constructible provider category, so keep it local to this API's domain.
type ChangeCategory = Category | Literal["worker"]


class UnsupportedChangeStrategy(ChangeError):
    code = "PROVIDER_CHANGE_UNSUPPORTED"


class StaleChangePlan(ChangeError):
    code = "PROVIDER_CHANGE_PLAN_STALE"


class IncompleteChangeSource(ChangeError):
    code = "PROJECTION_REBUILD_SOURCE_INCOMPLETE"


class ActiveChangeExists(ChangeError):
    code = "PROVIDER_CHANGE_ALREADY_ACTIVE"


class ChangeConflict(ChangeError):
    code = "PROVIDER_CHANGE_CONFLICT"


class ChangeStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    ACTIVATING = "activating"
    ACTIVE = "active"
    CLEANUP_PENDING = "cleanup_pending"
    CLEANED = "cleaned"
    FAILED = "failed"


class ChangeModel(StrictSettings):
    """Immutable strict model for persisted provider-change records."""


class ChangeScope(ChangeModel):
    deployment_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    resource_class: Literal[
        "projection", "embedding_space", "model_config", "authoritative_database", "cache", "worker"
    ]
    source_provider: str | None = None
    target_provider: str | None = None
    source_generation: str | None = Field(default=None, max_length=128)
    target_generation: str | None = Field(default=None, max_length=128)
    source_target_fingerprint: str | None = None
    target_fingerprint: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> ChangeScope:
        if self.resource_class == "projection":
            if not self.source_generation or not self.target_generation:
                raise ValueError("projection scope requires source and target generations")
            if self.source_generation == self.target_generation:
                raise ValueError("source and target generations must differ")
        if (
            self.resource_class in {"authoritative_database", "cache", "worker"}
            and not self.target_generation
        ):
            raise ValueError("this resource scope requires a target generation")
        return self


class TargetConfigRef(ChangeModel):
    """Credential-free pointer to validated target config and secret references."""

    provider: str = Field(min_length=1)
    config_id: str = Field(min_length=1)
    semantic_fingerprint: str = Field(min_length=1)
    secret_refs: tuple[str, ...] = ()


class DependencyRevision(ChangeModel):
    service_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)


class SourceCompleteness(ChangeModel):
    """Evidence that the source can be reconstructed through a fixed head."""

    complete: bool
    evidence_id: str | None = None
    source_checkpoint: int | None = Field(default=None, ge=0)
    source_head_seq: int | None = Field(default=None, ge=0)
    missing_ranges: tuple[str, ...] = ()
    snapshot_id: str | None = None

    @model_validator(mode="after")
    def coherent_evidence(self) -> SourceCompleteness:
        if self.complete and (not self.evidence_id or self.missing_ranges):
            raise ValueError(
                "complete source evidence needs an id and cannot contain missing ranges"
            )
        if (
            self.source_checkpoint is not None
            and self.source_head_seq is not None
            and self.source_checkpoint > self.source_head_seq
        ):
            raise ValueError("source checkpoint cannot exceed source head")
        return self


class ResourceEstimate(ChangeModel):
    bytes: int | None = Field(default=None, ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    model_calls: int | None = Field(default=None, ge=0)


class PlanStep(ChangeModel):
    step_id: str = Field(min_length=1)
    phase: Literal[
        "quiesce", "snapshot", "prepare", "migrate", "verify", "activate", "retain", "cleanup"
    ]
    description: str = Field(min_length=1)


class ChangeRequest(ChangeModel):
    category: ChangeCategory
    logical_service_id: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    target_revision: str = Field(min_length=1)
    scope: ChangeScope
    target_config_ref: TargetConfigRef
    requested_strategy: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    dependency_revisions: tuple[DependencyRevision, ...] = ()
    source_completeness: SourceCompleteness


class ChangePlan(ChangeModel):
    plan_id: str = Field(min_length=1)
    plan_version: Literal[1] = 1
    request: ChangeRequest
    source_semantic_fingerprint: str = Field(min_length=1)
    target_semantic_fingerprint: str = Field(min_length=1)
    dependency_revisions: tuple[DependencyRevision, ...]
    steps: tuple[PlanStep, ...]
    required_capabilities: frozenset[str]
    source_completeness: SourceCompleteness
    estimate: ResourceEstimate
    created_at: datetime
    expires_at: datetime
    retention_s: int | None = Field(default=None, ge=0)
    supported: bool
    unsupported_reason: str | None = None

    @model_validator(mode="after")
    def validate_plan(self) -> ChangePlan:
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("plan timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("plan expiry must follow creation")
        if self.supported == bool(self.unsupported_reason):
            raise ValueError(
                "supported plans omit unsupported_reason; unsupported plans require it"
            )
        if self.request.source_completeness != self.source_completeness:
            raise ValueError("plan source evidence must match its request")
        if self.request.target_config_ref.semantic_fingerprint != self.target_semantic_fingerprint:
            raise ValueError("target semantic fingerprint must match its config reference")
        if self.request.scope.resource_class == "projection" and self.retention_s is None:
            raise ValueError("projection plans require a resolved retention period")
        return self


_TRANSITIONS: dict[ChangeStatus, frozenset[ChangeStatus]] = {
    ChangeStatus.RUNNING: frozenset(
        {ChangeStatus.RUNNING, ChangeStatus.VERIFYING, ChangeStatus.FAILED}
    ),
    ChangeStatus.VERIFYING: frozenset(
        {ChangeStatus.VERIFYING, ChangeStatus.VERIFIED, ChangeStatus.FAILED}
    ),
    ChangeStatus.VERIFIED: frozenset({ChangeStatus.ACTIVATING, ChangeStatus.FAILED}),
    ChangeStatus.ACTIVATING: frozenset(
        {ChangeStatus.ACTIVATING, ChangeStatus.ACTIVE, ChangeStatus.FAILED}
    ),
    ChangeStatus.CLEANUP_PENDING: frozenset({ChangeStatus.CLEANUP_PENDING, ChangeStatus.CLEANED}),
}


class OperationCheckpoint(ChangeModel):
    phase: str = "planned"
    source_checkpoint: int | None = Field(default=None, ge=0)
    target_checkpoint: int | None = Field(default=None, ge=0)
    erase_checkpoint: int = Field(default=0, ge=0)
    cursor: str | None = None
    details: Mapping[str, object] = Field(default_factory=dict)


class ChangeResultBase(ChangeModel):
    operation_id: str = Field(min_length=1)
    status: ChangeStatus
    errors: tuple[str, ...] = ()
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def aware_timestamps(self) -> ChangeResultBase:
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("result timestamps must be timezone-aware")
        return self


class ProjectionRebuildResult(ChangeResultBase):
    kind: Literal["projection_rebuild"] = "projection_rebuild"
    deployment_id: str
    source_generation: str
    target_generation: str
    source_checkpoint: int | None = Field(default=None, ge=0)
    target_checkpoint: int | None = Field(default=None, ge=0)
    erase_checkpoint: int = Field(ge=0)
    verification_report_id: str | None = None


class EmbeddingMigrationResult(ChangeResultBase):
    kind: Literal["embedding_migration"] = "embedding_migration"
    old_space_id: str
    new_space_id: str
    query_encoder_revision: str
    document_encoder_revision: str
    target_generation: str
    indexed_record_count: int | None = Field(default=None, ge=0)
    pending_record_count: int | None = Field(default=None, ge=0)
    verification_report_id: str | None = None
    cleanup_report_id: str | None = None


class ModelRevisionResult(ChangeResultBase):
    kind: Literal["model_revision"] = "model_revision"
    logical_role: str
    old_revision: str
    new_revision: str
    producer_revision: str
    prompt_schema_revision: str
    in_flight_policy: Literal["drain", "preserve_revision", "stop"]
    validation_report_id: str | None = None


class DatabaseMigrationResult(ChangeResultBase):
    kind: Literal["database_migration"] = "database_migration"
    source_target_fingerprint: str
    target_fingerprint: str
    source_fence_epoch: int = Field(ge=0)
    source_intent_id: str
    target_receipt_id: str
    schema_revision: str
    copy_checkpoint: int | None = Field(default=None, ge=0)
    verification_report_id: str | None = None
    cutover_ack_state: Literal["pending", "acknowledged", "ambiguous"]


class CacheResetResult(ChangeResultBase):
    kind: Literal["cache_reset"] = "cache_reset"
    old_key_generation: str
    new_key_generation: str
    owned_prefix: str
    ttl_policy_revision: str
    purge_status: Literal["pending", "complete", "incomplete"]
    purge_report_id: str | None = None


class WorkerHandoverResult(ChangeResultBase):
    kind: Literal["worker_handover"] = "worker_handover"
    source_generation: str = Field(min_length=1, max_length=128)
    target_generation: str = Field(min_length=1, max_length=128)
    old_owner_epoch: int = Field(ge=0)
    new_owner_epoch: int = Field(ge=0)
    payload_contract_version: int = Field(ge=1)
    pending_job_count: int | None = Field(default=None, ge=0)
    lease_transfer_status: Literal["pending", "complete", "incomplete"]
    ack_reconciliation_status: Literal["pending", "complete", "incomplete"]
    old_owner_release_report_id: str | None = None


type ChangeResult = Annotated[
    ProjectionRebuildResult
    | EmbeddingMigrationResult
    | ModelRevisionResult
    | DatabaseMigrationResult
    | CacheResetResult
    | WorkerHandoverResult,
    Field(discriminator="kind"),
]
_RESULT_ADAPTER = TypeAdapter(ChangeResult)


class OperationRecord(ChangeModel):
    operation_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    deployment_id: str = Field(min_length=1)
    status: ChangeStatus
    operation_revision: int = Field(ge=1)
    fence_epoch: int = Field(ge=1)
    checkpoint: OperationCheckpoint
    result: ChangeResult | None = None
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime
    activated_at: datetime | None = None


class CleanupSweepReport(ChangeModel):
    """Sanitized bounded result of one automatic projection-cleanup batch."""

    deployment_id: str = Field(min_length=1)
    scanned: int = Field(ge=0)
    eligible: int = Field(ge=0)
    cleaned: int = Field(ge=0)
    deferred: int = Field(ge=0)
    failed: int = Field(ge=0)
    failed_operation_ids: tuple[str, ...] = ()
    next_cursor: str | None = None


class ChangeContext(Protocol):
    """Strategy hooks receive immutable plan plus durable progress callbacks."""

    @property
    def plan(self) -> ChangePlan: ...

    @property
    def operation(self) -> OperationRecord: ...

    async def checkpoint(self, checkpoint: OperationCheckpoint) -> OperationRecord: ...


class ChangeStrategy(Protocol):
    """Category strategy for preparation, verification and scoped retirement."""

    async def execute(self, context: ChangeContext) -> None: ...

    async def verify(self, context: ChangeContext) -> ChangeResult: ...

    async def activate(self, context: ChangeContext, result: ChangeResult) -> None: ...

    async def cleanup(self, context: ChangeContext, result: ChangeResult) -> ChangeResult: ...


class RevisionGuard(Protocol):
    async def current_revision(self, logical_service_id: str) -> str: ...

    async def current_target_fingerprint(self, config_id: str) -> str: ...


class ChangeStore(Protocol):
    async def create_schema(self) -> None: ...

    async def save_plan(self, plan: ChangePlan) -> None: ...

    async def get_plan(self, plan_id: str) -> ChangePlan: ...

    async def claim(self, plan: ChangePlan) -> OperationRecord: ...

    async def resume(self, operation_id: str) -> OperationRecord: ...

    async def claim_cleanup(self, operation_id: str) -> OperationRecord: ...

    async def recover_cleanup_fence(
        self,
        request: CleanupFenceRecoveryRequest,
        proof: CleanupQuiescenceProof,
        *,
        now: datetime,
    ) -> CleanupFenceRecoveryReceipt: ...

    async def get_operation(self, operation_id: str) -> OperationRecord: ...

    async def get_operation_for_plan(self, plan_id: str) -> OperationRecord: ...

    async def list_cleanup_candidates(
        self, deployment_id: str, *, after_operation_id: str | None, limit: int
    ) -> tuple[tuple[OperationRecord, ...], str | None]: ...

    async def update_operation(
        self,
        operation: OperationRecord,
        *,
        status: ChangeStatus | None = None,
        checkpoint: OperationCheckpoint | None = None,
        result: ChangeResult | None = None,
        error_code: str | None = None,
        activated_at: datetime | None = None,
    ) -> OperationRecord: ...

    async def release(self, operation: OperationRecord) -> None: ...


_plans = Table(
    "provider_change_plans",
    _metadata,
    Column("plan_id", String(128), primary_key=True),
    Column("deployment_id", String(256), nullable=False, index=True),
    Column("payload", JSON, nullable=False),
)
_operations = Table(
    "provider_change_operations",
    _metadata,
    Column("operation_id", String(128), primary_key=True),
    Column("plan_id", String(128), nullable=False, unique=True),
    Column("deployment_id", String(256), nullable=False, index=True),
    Column("status", String(32), nullable=False),
    Column("operation_revision", Integer, nullable=False),
    Column("fence_epoch", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)
_fences = Table(
    "provider_change_fences",
    _metadata,
    Column("deployment_id", String(256), primary_key=True),
    Column("epoch", Integer, nullable=False),
    Column("active_operation_id", String(128)),
)
_cleanup_fence_recoveries = Table(
    "provider_cleanup_fence_recoveries",
    _metadata,
    Column("recovery_id", String(128), primary_key=True),
    Column("deployment_id", String(256), nullable=False, index=True),
    Column("operation_id", String(128), nullable=False, index=True),
    Column("proof_id", String(128), nullable=False, unique=True),
    Column("recovered_at", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
)


def _assert_recovery_proof(
    request: CleanupFenceRecoveryRequest,
    proof: CleanupQuiescenceProof,
    now: datetime,
) -> None:
    if now.tzinfo is None:
        raise ValueError("recovery clock must return a timezone-aware timestamp")
    if (
        proof.deployment_id != request.deployment_id
        or proof.operation_id != request.operation_id
        or proof.operation_revision != request.expected_operation_revision
        or proof.fence_epoch != request.expected_fence_epoch
        or proof.evidence_reference != request.evidence_reference
    ):
        raise ChangeConflict("application quiescence proof does not bind the requested state")
    if proof.issued_at > now or proof.expires_at <= now:
        raise StaleChangePlan("application quiescence proof is not currently valid")


class SqlChangeStore:
    """SQLAlchemy Core repository for plans, operations and deployment fences.

    ``create_schema`` is explicit for isolated environments and tests. Production
    installs these tables through its reviewed SQL migration before use.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_schema(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(_metadata.create_all)

    async def save_plan(self, plan: ChangePlan) -> None:
        async with self._engine.begin() as connection:
            exists = await connection.scalar(
                select(_plans.c.plan_id).where(_plans.c.plan_id == plan.plan_id)
            )
            if exists is not None:
                raise ChangeConflict(f"plan {plan.plan_id!r} already exists")
            await connection.execute(
                _plans.insert().values(
                    plan_id=plan.plan_id,
                    deployment_id=plan.request.scope.deployment_id,
                    payload=plan.model_dump(mode="json"),
                )
            )

    async def get_plan(self, plan_id: str) -> ChangePlan:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(_plans.c.payload).where(_plans.c.plan_id == plan_id)
                )
            ).first()
        if row is None:
            raise ChangeConflict(f"unknown change plan {plan_id!r}")
        return ChangePlan.model_validate_json(json.dumps(row.payload))

    async def claim(self, plan: ChangePlan) -> OperationRecord:
        if not plan.supported:
            raise UnsupportedChangeStrategy(
                plan.unsupported_reason or "no strategy supports this category"
            )
        if plan.expires_at <= datetime.now(UTC):
            raise StaleChangePlan("change plan has expired")
        if not plan.source_completeness.complete:
            raise IncompleteChangeSource("source completeness evidence does not permit execution")
        operation_id = str(uuid4())
        now = datetime.now(UTC)
        async with self._engine.begin() as connection:
            existing = await connection.scalar(
                select(_operations.c.operation_id).where(_operations.c.plan_id == plan.plan_id)
            )
            if existing is not None:
                raise ChangeConflict("this plan already has an operation; use resume_change")
            await self._ensure_fence(connection, plan.request.scope.deployment_id)
            result = await connection.execute(
                update(_fences)
                .where(
                    _fences.c.deployment_id == plan.request.scope.deployment_id,
                    _fences.c.active_operation_id.is_(None),
                )
                .values(
                    epoch=_fences.c.epoch + 1,
                    active_operation_id=operation_id,
                )
                .returning(_fences.c.epoch)
            )
            epoch = result.scalar_one_or_none()
            if epoch is None:
                raise ActiveChangeExists("another change holds the deployment fence")
            operation = OperationRecord(
                operation_id=operation_id,
                plan_id=plan.plan_id,
                deployment_id=plan.request.scope.deployment_id,
                status=ChangeStatus.RUNNING,
                operation_revision=1,
                fence_epoch=epoch,
                checkpoint=OperationCheckpoint(),
                created_at=now,
                updated_at=now,
            )
            await connection.execute(
                _operations.insert().values(
                    operation_id=operation.operation_id,
                    plan_id=operation.plan_id,
                    deployment_id=operation.deployment_id,
                    status=operation.status.value,
                    operation_revision=operation.operation_revision,
                    fence_epoch=operation.fence_epoch,
                    payload=operation.model_dump(mode="json"),
                )
            )
        return operation

    async def resume(self, operation_id: str) -> OperationRecord:
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    select(_operations.c.payload).where(_operations.c.operation_id == operation_id)
                )
            ).first()
            if row is None:
                raise ChangeConflict(f"unknown operation {operation_id!r}")
            previous = OperationRecord.model_validate_json(json.dumps(row.payload))
            if previous.status in {ChangeStatus.ACTIVE, ChangeStatus.CLEANED}:
                raise ChangeConflict(f"operation is already {previous.status.value}")
            await self._ensure_fence(connection, previous.deployment_id)
            claimed = await connection.execute(
                update(_fences)
                .where(
                    _fences.c.deployment_id == previous.deployment_id,
                    _fences.c.active_operation_id.is_(None),
                )
                .values(
                    epoch=_fences.c.epoch + 1,
                    active_operation_id=operation_id,
                )
                .returning(_fences.c.epoch)
            )
            epoch = claimed.scalar_one_or_none()
            if epoch is None:
                raise ActiveChangeExists("another change holds the deployment fence")
            resumed = previous.model_copy(
                update={
                    "status": ChangeStatus.RUNNING,
                    "operation_revision": previous.operation_revision + 1,
                    "fence_epoch": epoch,
                    "error_code": None,
                    "updated_at": datetime.now(UTC),
                }
            )
            updated_row = await connection.execute(
                update(_operations)
                .where(
                    _operations.c.operation_id == operation_id,
                    _operations.c.operation_revision == previous.operation_revision,
                )
                .values(
                    status=resumed.status.value,
                    operation_revision=resumed.operation_revision,
                    fence_epoch=resumed.fence_epoch,
                    payload=resumed.model_dump(mode="json"),
                )
            )
            if updated_row.rowcount != 1:
                raise StaleChangePlan("operation revision changed during resume")
        return resumed

    async def claim_cleanup(self, operation_id: str) -> OperationRecord:
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    select(_operations.c.payload).where(_operations.c.operation_id == operation_id)
                )
            ).first()
            if row is None:
                raise ChangeConflict(f"unknown operation {operation_id!r}")
            previous = OperationRecord.model_validate_json(json.dumps(row.payload))
            if previous.status not in {ChangeStatus.ACTIVE, ChangeStatus.CLEANUP_PENDING}:
                raise ChangeConflict(
                    "cleanup claim requires an active or cleanup-pending operation"
                )
            await self._ensure_fence(connection, previous.deployment_id)
            claimed = await connection.execute(
                update(_fences)
                .where(
                    _fences.c.deployment_id == previous.deployment_id,
                    _fences.c.active_operation_id.is_(None),
                )
                .values(epoch=_fences.c.epoch + 1, active_operation_id=operation_id)
                .returning(_fences.c.epoch)
            )
            epoch = claimed.scalar_one_or_none()
            if epoch is None:
                raise ActiveChangeExists("another change holds the deployment fence")
            claimed_operation = previous.model_copy(
                update={
                    "status": ChangeStatus.CLEANUP_PENDING,
                    "operation_revision": previous.operation_revision + 1,
                    "fence_epoch": epoch,
                    "updated_at": datetime.now(UTC),
                }
            )
            updated_row = await connection.execute(
                update(_operations)
                .where(
                    _operations.c.operation_id == operation_id,
                    _operations.c.operation_revision == previous.operation_revision,
                )
                .values(
                    status=claimed_operation.status.value,
                    operation_revision=claimed_operation.operation_revision,
                    fence_epoch=claimed_operation.fence_epoch,
                    payload=claimed_operation.model_dump(mode="json"),
                )
            )
            if updated_row.rowcount != 1:
                raise StaleChangePlan("operation revision changed during cleanup claim")
        return claimed_operation

    async def recover_cleanup_fence(
        self,
        request: CleanupFenceRecoveryRequest,
        proof: CleanupQuiescenceProof,
        *,
        now: datetime,
    ) -> CleanupFenceRecoveryReceipt:
        """CAS-clear one cleanup fence and persist its proof in the same transaction."""
        _assert_recovery_proof(request, proof, now)
        async with self._engine.begin() as connection:
            fence_row = await self._lock_owned_fence(
                connection,
                request.deployment_id,
                operation_id=request.operation_id,
                expected_epoch=request.expected_fence_epoch,
            )
            if fence_row is None:
                raise ChangeConflict("cleanup fence is no longer held by the requested operation")
            operation_row = (
                await connection.execute(
                    select(
                        _operations.c.deployment_id,
                        _operations.c.status,
                        _operations.c.operation_revision,
                        _operations.c.fence_epoch,
                        _operations.c.payload,
                    ).where(_operations.c.operation_id == request.operation_id)
                )
            ).first()
            if operation_row is None:
                raise ChangeConflict("cleanup recovery operation does not exist")
            if operation_row.deployment_id != request.deployment_id:
                raise ChangeConflict("cleanup recovery operation belongs to another deployment")
            operation = OperationRecord.model_validate_json(json.dumps(operation_row.payload))
            if (
                operation_row.status != ChangeStatus.CLEANUP_PENDING.value
                or operation.status is not ChangeStatus.CLEANUP_PENDING
            ):
                raise ChangeConflict("cleanup recovery requires a cleanup-pending operation")
            if (
                operation_row.operation_revision != request.expected_operation_revision
                or operation.operation_revision != request.expected_operation_revision
                or operation_row.fence_epoch != request.expected_fence_epoch
                or operation.fence_epoch != request.expected_fence_epoch
            ):
                raise StaleChangePlan("cleanup recovery state changed; inspect and request fresh proof")
            released_epoch = request.expected_fence_epoch + 1
            recovered_at = now.astimezone(UTC)
            recovered_operation = operation.model_copy(
                update={
                    "operation_revision": operation.operation_revision + 1,
                    "fence_epoch": released_epoch,
                    "updated_at": recovered_at,
                }
            )
            receipt = CleanupFenceRecoveryReceipt(
                deployment_id=request.deployment_id,
                operation_id=request.operation_id,
                operation_revision=recovered_operation.operation_revision,
                previous_fence_epoch=request.expected_fence_epoch,
                released_fence_epoch=released_epoch,
                proof_id=proof.proof_id,
                recovered_at=recovered_at,
            )
            updated = await connection.execute(
                update(_fences)
                .where(
                    _fences.c.deployment_id == request.deployment_id,
                    _fences.c.epoch == request.expected_fence_epoch,
                    _fences.c.active_operation_id == request.operation_id,
                )
                .values(epoch=released_epoch, active_operation_id=None)
            )
            if updated.rowcount != 1:
                raise StaleChangePlan("cleanup fence changed during recovery")
            operation_update = await connection.execute(
                update(_operations)
                .where(
                    _operations.c.operation_id == request.operation_id,
                    _operations.c.deployment_id == request.deployment_id,
                    _operations.c.status == ChangeStatus.CLEANUP_PENDING.value,
                    _operations.c.operation_revision == request.expected_operation_revision,
                    _operations.c.fence_epoch == request.expected_fence_epoch,
                )
                .values(
                    operation_revision=recovered_operation.operation_revision,
                    fence_epoch=recovered_operation.fence_epoch,
                    payload=recovered_operation.model_dump(mode="json"),
                )
            )
            if operation_update.rowcount != 1:
                raise StaleChangePlan("cleanup operation changed during recovery")
            await connection.execute(
                _cleanup_fence_recoveries.insert().values(
                    recovery_id=f"cfr_{uuid4().hex}",
                    deployment_id=request.deployment_id,
                    operation_id=request.operation_id,
                    proof_id=proof.proof_id,
                    recovered_at=recovered_at.isoformat(),
                    payload={
                        "request": request.model_dump(mode="json"),
                        "proof": proof.model_dump(mode="json"),
                        "receipt": receipt.model_dump(mode="json"),
                    },
                )
            )
            return receipt

    async def get_operation(self, operation_id: str) -> OperationRecord:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(_operations.c.payload).where(_operations.c.operation_id == operation_id)
                )
            ).first()
        if row is None:
            raise ChangeConflict(f"unknown operation {operation_id!r}")
        return OperationRecord.model_validate_json(json.dumps(row.payload))

    async def audit_cleanup_fence_recoveries(
        self, deployment_id: str, operation_id: str
    ) -> tuple[CleanupFenceRecoveryReceipt, ...]:
        """Return sanitized recovery receipts for one exact operation scope."""
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(_cleanup_fence_recoveries.c.payload)
                    .where(
                        _cleanup_fence_recoveries.c.deployment_id == deployment_id,
                        _cleanup_fence_recoveries.c.operation_id == operation_id,
                    )
                    .order_by(_cleanup_fence_recoveries.c.recovered_at)
                )
            ).all()
        receipts: list[CleanupFenceRecoveryReceipt] = []
        for row in rows:
            payload = row.payload
            receipts.append(
                CleanupFenceRecoveryReceipt.model_validate_json(json.dumps(payload["receipt"]))
            )
        return tuple(receipts)

    async def get_operation_for_plan(self, plan_id: str) -> OperationRecord:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(_operations.c.payload).where(_operations.c.plan_id == plan_id)
                )
            ).first()
        if row is None:
            raise ChangeConflict(f"plan {plan_id!r} has no operation")
        return OperationRecord.model_validate_json(json.dumps(row.payload))

    async def list_cleanup_candidates(
        self,
        deployment_id: str,
        *,
        after_operation_id: str | None,
        limit: int,
    ) -> tuple[tuple[OperationRecord, ...], str | None]:
        if not deployment_id.strip():
            raise ValueError("cleanup sweep deployment_id must be non-empty")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("cleanup sweep limit must be a positive integer")
        predicates = [
            _operations.c.deployment_id == deployment_id,
            _operations.c.status.in_([ChangeStatus.ACTIVE.value, ChangeStatus.CLEANUP_PENDING.value]),
        ]
        if after_operation_id is not None:
            predicates.append(_operations.c.operation_id > after_operation_id)
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    select(_operations.c.operation_id, _operations.c.payload)
                    .where(*predicates)
                    .order_by(_operations.c.operation_id)
                    .limit(limit)
                )
            ).all()
        operations = tuple(
            OperationRecord.model_validate_json(json.dumps(row.payload)) for row in rows
        )
        next_cursor = rows[-1].operation_id if len(rows) == limit else None
        return operations, next_cursor

    async def update_operation(
        self,
        operation: OperationRecord,
        *,
        status: ChangeStatus | None = None,
        checkpoint: OperationCheckpoint | None = None,
        result: ChangeResult | None = None,
        error_code: str | None = None,
        activated_at: datetime | None = None,
    ) -> OperationRecord:
        updated = operation.model_copy(
            update={
                "status": status or operation.status,
                "operation_revision": operation.operation_revision + 1,
                "checkpoint": checkpoint or operation.checkpoint,
                "result": result if result is not None else operation.result,
                "error_code": error_code,
                "updated_at": datetime.now(UTC),
                "activated_at": activated_at
                if activated_at is not None
                else operation.activated_at,
            }
        )
        if updated.status not in _TRANSITIONS.get(operation.status, frozenset()):
            raise ChangeConflict(
                f"invalid operation transition {operation.status.value} -> {updated.status.value}"
            )
        async with self._engine.begin() as connection:
            fence_row = await self._lock_owned_fence(
                connection,
                operation.deployment_id,
                operation_id=operation.operation_id,
                expected_epoch=operation.fence_epoch,
            )
            if fence_row is None:
                raise StaleChangePlan("operation no longer owns the deployment fence")
            result_db = await connection.execute(
                update(_operations)
                .where(
                    _operations.c.operation_id == operation.operation_id,
                    _operations.c.operation_revision == operation.operation_revision,
                    _operations.c.fence_epoch == operation.fence_epoch,
                )
                .values(
                    status=updated.status.value,
                    operation_revision=updated.operation_revision,
                    payload=updated.model_dump(mode="json"),
                )
            )
            if result_db.rowcount != 1:
                raise ChangeConflict("operation revision changed concurrently")
        return updated

    async def _lock_owned_fence(
        self,
        connection: AsyncConnection,
        deployment_id: str,
        *,
        operation_id: str,
        expected_epoch: int,
    ) -> tuple[int, str | None] | None:
        """Serialize operation writes and recovery on one deployment fence row.

        PostgreSQL uses a row lock. SQLite has no ``FOR UPDATE``; a guarded
        no-op UPDATE obtains its write reservation and validates the same
        owner/epoch before the operation row is read or changed.
        """
        predicate = (
            (_fences.c.deployment_id == deployment_id)
            & (_fences.c.epoch == expected_epoch)
            & (_fences.c.active_operation_id == operation_id)
        )
        if self._engine.dialect.name == "sqlite":
            locked = await connection.execute(
                update(_fences).where(predicate).values(epoch=_fences.c.epoch)
            )
            if locked.rowcount != 1:
                return None
            row = (
                await connection.execute(
                    select(_fences.c.epoch, _fences.c.active_operation_id).where(
                        _fences.c.deployment_id == deployment_id
                    )
                )
            ).first()
            return None if row is None else (int(row.epoch), row.active_operation_id)
        if self._engine.dialect.name == "postgresql":
            row = (
                await connection.execute(
                    select(_fences.c.epoch, _fences.c.active_operation_id)
                    .where(predicate)
                    .with_for_update()
                )
            ).first()
            return None if row is None else (int(row.epoch), row.active_operation_id)
        raise RuntimeError(
            f"durable provider change fences are unsupported for {self._engine.dialect.name}"
        )

    async def release(self, operation: OperationRecord) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                update(_fences)
                .where(
                    _fences.c.deployment_id == operation.deployment_id,
                    _fences.c.active_operation_id == operation.operation_id,
                    _fences.c.epoch == operation.fence_epoch,
                )
                .values(active_operation_id=None)
            )

    async def _ensure_fence(self, connection: AsyncConnection, deployment_id: str) -> None:
        dialect_name = self._engine.dialect.name
        values = {"deployment_id": deployment_id, "epoch": 0, "active_operation_id": None}
        if dialect_name == "sqlite":
            await connection.execute(
                sqlite_insert(_fences)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[_fences.c.deployment_id])
            )
        elif dialect_name == "postgresql":
            await connection.execute(
                pg_insert(_fences)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[_fences.c.deployment_id])
            )
        else:
            raise RuntimeError(f"durable provider change fences are unsupported for {dialect_name}")


class ChangeContextImpl:
    """Per-execution context that persists each strategy checkpoint."""

    def __init__(self, plan: ChangePlan, operation: OperationRecord, store: ChangeStore) -> None:
        self._plan = plan
        self._operation = operation
        self._store = store

    @property
    def plan(self) -> ChangePlan:
        return self._plan

    @property
    def operation(self) -> OperationRecord:
        return self._operation

    async def checkpoint(self, checkpoint: OperationCheckpoint) -> OperationRecord:
        old = self._operation.checkpoint
        if (
            old.source_checkpoint is not None
            and checkpoint.source_checkpoint is not None
            and checkpoint.source_checkpoint < old.source_checkpoint
        ):
            raise ChangeConflict("source checkpoint cannot move backwards")
        if (
            old.target_checkpoint is not None
            and checkpoint.target_checkpoint is not None
            and checkpoint.target_checkpoint < old.target_checkpoint
        ):
            raise ChangeConflict("target checkpoint cannot move backwards")
        if checkpoint.erase_checkpoint < old.erase_checkpoint:
            raise ChangeConflict("erase checkpoint cannot move backwards")
        self._operation = await self._store.update_operation(self._operation, checkpoint=checkpoint)
        return self._operation


class ChangeCoordinator:
    """Plans and runs registered strategies under durable revision/fence guards."""

    def __init__(
        self,
        *,
        store: ChangeStore,
        revision_guard: RevisionGuard,
        strategies: Mapping[tuple[ChangeCategory, str], ChangeStrategy],
        plan_ttl_s: float,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if isinstance(plan_ttl_s, bool) or not math.isfinite(plan_ttl_s) or plan_ttl_s <= 0:
            raise ValueError("plan_ttl_s must be finite and positive")
        self._store = store
        self._revision_guard = revision_guard
        self._strategies = dict(strategies)
        self._plan_ttl_s = plan_ttl_s
        self._now = now

    async def plan_change(
        self,
        request: ChangeRequest,
        *,
        steps: Sequence[PlanStep],
        required_capabilities: frozenset[str],
        estimate: ResourceEstimate | None = None,
        retention_s: int | None = None,
    ) -> ChangePlan:
        """Persist an immutable plan without acquiring a fence or doing migration work."""
        now = self._now()
        supported = (request.category, request.requested_strategy) in self._strategies
        plan = ChangePlan(
            plan_id=str(uuid4()),
            request=request,
            source_semantic_fingerprint=request.source_revision,
            target_semantic_fingerprint=request.target_config_ref.semantic_fingerprint,
            dependency_revisions=request.dependency_revisions,
            steps=tuple(steps),
            required_capabilities=required_capabilities,
            source_completeness=request.source_completeness,
            estimate=estimate or ResourceEstimate(),
            created_at=now,
            expires_at=now + timedelta(seconds=self._plan_ttl_s),
            retention_s=retention_s,
            supported=supported,
            unsupported_reason=None
            if supported
            else "no registered strategy supports this category and strategy",
        )
        await self._store.save_plan(plan)
        return plan

    async def execute_change(self, plan_id: str) -> OperationRecord:
        plan = await self._store.get_plan(plan_id)
        self._assert_supported(plan)
        await self._assert_fresh(plan)
        operation = await self._store.claim(plan)
        return await self._run(plan, operation)

    async def resume_change(self, operation_id: str) -> OperationRecord:
        before = await self._store.get_operation(operation_id)
        plan = await self._store.get_plan(before.plan_id)
        self._assert_supported(plan)
        await self._assert_fresh(plan, check_expiry=False)
        operation = await self._store.resume(operation_id)
        return await self._run(plan, operation)

    async def verify_change(self, operation_id: str) -> ChangeResult:
        operation = await self._store.get_operation(operation_id)
        plan = await self._store.get_plan(operation.plan_id)
        self._assert_supported(plan)
        if operation.status not in {ChangeStatus.RUNNING, ChangeStatus.VERIFYING}:
            raise ChangeConflict(f"operation in {operation.status.value} cannot be verified")
        strategy = self._strategy(plan)
        context = ChangeContextImpl(plan, operation, self._store)
        verifying = await self._store.update_operation(operation, status=ChangeStatus.VERIFYING)
        context._operation = verifying
        try:
            result = _RESULT_ADAPTER.validate_python(await strategy.verify(context))
            if result.operation_id != operation_id:
                raise ChangeConflict("verification result belongs to another operation")
            if result.status is not ChangeStatus.VERIFIED or result.errors:
                raise ChangeConflict("activation requires a verified result without errors")
            context._operation = await self._store.update_operation(
                context.operation,
                status=ChangeStatus.VERIFIED,
                result=result,
                checkpoint=context.operation.checkpoint,
            )
            context._operation = await self._store.update_operation(
                context.operation, status=ChangeStatus.ACTIVATING
            )
            await strategy.activate(context, result)
            result = _RESULT_ADAPTER.validate_python(
                result.model_copy(update={"status": ChangeStatus.ACTIVE})
            )
            context._operation = await self._store.update_operation(
                context.operation,
                status=ChangeStatus.ACTIVE,
                result=result,
                activated_at=self._now(),
            )
            await self._store.release(context.operation)
            return result
        except BaseException as exc:
            failed = await self._store.update_operation(
                context.operation,
                status=ChangeStatus.FAILED,
                error_code=getattr(exc, "code", "PROVIDER_CHANGE_FAILED"),
            )
            await self._store.release(failed)
            raise

    async def cleanup_change(self, operation_id: str) -> OperationRecord:
        operation = await self._store.get_operation(operation_id)
        plan = await self._store.get_plan(operation.plan_id)
        self._assert_supported(plan)
        self._assert_cleanup_scope(plan, operation)
        if operation.status not in {ChangeStatus.ACTIVE, ChangeStatus.CLEANUP_PENDING}:
            raise ChangeConflict("cleanup requires a verified active change")
        strategy = self._strategy(plan)
        if operation.result is None:
            raise ChangeConflict("cleanup requires persisted typed verification evidence")
        pending = await self._store.claim_cleanup(operation_id)
        context = ChangeContextImpl(plan, pending, self._store)
        try:
            result = _RESULT_ADAPTER.validate_python(
                await strategy.cleanup(context, operation.result)
            )
            cleaned = await self._store.update_operation(
                context.operation, status=ChangeStatus.CLEANED, result=result
            )
        except BaseException as exc:
            pending = await self._store.update_operation(
                context.operation,
                status=ChangeStatus.CLEANUP_PENDING,
                error_code=getattr(exc, "code", "PROVIDER_CLOSE_FAILED"),
            )
            await self._store.release(pending)
            raise
        await self._store.release(cleaned)
        return cleaned

    async def recover_abandoned_cleanup_fence(
        self,
        request: CleanupFenceRecoveryRequest,
        *,
        quiescence_guard: CleanupQuiescenceGuard,
    ) -> CleanupFenceRecoveryReceipt:
        """Recover only a proven-quiescent, exact cleanup-pending deployment fence."""
        operation = await self._store.get_operation(request.operation_id)
        if operation.deployment_id != request.deployment_id:
            raise ChangeConflict("cleanup recovery operation belongs to another deployment")
        if (
            operation.operation_revision != request.expected_operation_revision
            or operation.fence_epoch != request.expected_fence_epoch
            or operation.status is not ChangeStatus.CLEANUP_PENDING
        ):
            raise StaleChangePlan("cleanup recovery operation state changed")
        plan = await self._store.get_plan(operation.plan_id)
        scope = plan.request.scope
        self._assert_supported(plan)
        self._strategy(plan)
        self._assert_cleanup_scope(plan, operation)
        if (
            scope.deployment_id != request.deployment_id
            or scope.owner_id != request.deployment_id
            or scope.resource_class != "projection"
            or plan.request.category not in {"graph", "vector", "lexical"}
            or not isinstance(operation.result, ProjectionRebuildResult)
            or operation.result.status is not ChangeStatus.ACTIVE
            or operation.result.deployment_id != request.deployment_id
            or operation.result.source_generation != scope.source_generation
            or operation.result.target_generation != scope.target_generation
        ):
            raise ChangeConflict("cleanup recovery is limited to an owned projection operation")
        try:
            proof = CleanupQuiescenceProof.model_validate(
                await quiescence_guard.prove_quiescence(request)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ChangeConflict("application quiescence proof could not be validated") from None
        return await self._store.recover_cleanup_fence(request, proof, now=self._now())

    async def cleanup_expired_projections(
        self,
        deployment_id: str,
        *,
        limit: int,
        after_operation_id: str | None = None,
    ) -> CleanupSweepReport:
        """Clean one bounded, keyset-paginated batch of expired owned projections.

        Only supported graph/vector/lexical projection plans owned by the given
        deployment are considered. The durable cleanup claim still supplies the
        cross-process fence; this scan never deletes resources itself.
        """
        if not deployment_id.strip():
            raise ValueError("cleanup sweep deployment_id must be non-empty")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("cleanup sweep limit must be a positive integer")
        candidates, next_cursor = await self._store.list_cleanup_candidates(
            deployment_id, after_operation_id=after_operation_id, limit=limit
        )
        now = self._now()
        eligible = cleaned = deferred = 0
        failed_ids: list[str] = []
        for operation in candidates:
            plan = await self._store.get_plan(operation.plan_id)
            scope = plan.request.scope
            if (
                operation.deployment_id != deployment_id
                or scope.deployment_id != deployment_id
                or scope.owner_id != deployment_id
                or scope.resource_class != "projection"
                or plan.request.category not in {"graph", "vector", "lexical"}
                or not plan.supported
                or (plan.request.category, plan.request.requested_strategy) not in self._strategies
                or operation.result is None
                or not isinstance(operation.result, ProjectionRebuildResult)
                or operation.result.status is not ChangeStatus.ACTIVE
                or operation.result.deployment_id != deployment_id
                or operation.result.source_generation != scope.source_generation
                or operation.result.target_generation != scope.target_generation
                or operation.activated_at is None
                or plan.retention_s is None
                or now < operation.activated_at + timedelta(seconds=plan.retention_s)
            ):
                deferred += 1
                continue
            eligible += 1
            try:
                await self.cleanup_change(operation.operation_id)
                cleaned += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                # Keep durable status/error_code as the actionable detail. Never
                # expose exception text, which can contain connection settings.
                failed_ids.append(operation.operation_id)
        return CleanupSweepReport(
            deployment_id=deployment_id,
            scanned=len(candidates),
            eligible=eligible,
            cleaned=cleaned,
            deferred=deferred,
            failed=len(failed_ids),
            failed_operation_ids=tuple(failed_ids),
            next_cursor=next_cursor,
        )

    async def _run(self, plan: ChangePlan, operation: OperationRecord) -> OperationRecord:
        strategy = self._strategy(plan)
        context = ChangeContextImpl(plan, operation, self._store)
        try:
            await strategy.execute(context)
        except BaseException as exc:
            failed = await self._store.update_operation(
                context.operation,
                status=ChangeStatus.FAILED,
                error_code=getattr(exc, "code", "PROVIDER_CHANGE_FAILED"),
            )
            await self._store.release(failed)
            raise
        return context.operation

    async def _assert_fresh(self, plan: ChangePlan, *, check_expiry: bool = True) -> None:
        current = await self._revision_guard.current_revision(plan.request.logical_service_id)
        if current != plan.request.source_revision:
            raise StaleChangePlan("source revision changed after planning; create a new plan")
        target = await self._revision_guard.current_target_fingerprint(
            plan.request.target_config_ref.config_id
        )
        if target != plan.target_semantic_fingerprint:
            raise StaleChangePlan("target config changed after planning; create a new plan")
        for dependency in plan.dependency_revisions:
            current_dependency = await self._revision_guard.current_revision(dependency.service_id)
            if current_dependency != dependency.revision:
                raise StaleChangePlan(
                    "a dependency revision changed after planning; create a new plan"
                )
        if check_expiry and plan.expires_at <= self._now():
            raise StaleChangePlan("change plan has expired")
        if not plan.source_completeness.complete:
            raise IncompleteChangeSource("source completeness evidence does not permit execution")

    def _assert_supported(self, plan: ChangePlan) -> None:
        if not plan.supported:
            raise UnsupportedChangeStrategy(
                plan.unsupported_reason or "unsupported change strategy"
            )

    def _strategy(self, plan: ChangePlan) -> ChangeStrategy:
        strategy = self._strategies.get((plan.request.category, plan.request.requested_strategy))
        if strategy is None:
            raise UnsupportedChangeStrategy(
                "no registered strategy supports this category and strategy"
            )
        return strategy

    def _assert_cleanup_scope(self, plan: ChangePlan, operation: OperationRecord) -> None:
        scope = plan.request.scope
        allowed = {
            "projection": {"graph", "vector", "lexical"},
            "embedding_space": {"embedding"},
            "cache": {"cache"},
            "worker": {"worker"},
        }
        if scope.resource_class not in allowed:
            raise ChangeConflict("automatic cleanup is unavailable for this resource class")
        if plan.request.category not in allowed[scope.resource_class]:
            raise ChangeConflict("cleanup is unavailable for this provider category")
        if (
            not scope.owner_id
            or not scope.target_generation
            or not scope.target_fingerprint
            or not scope.source_generation
            or scope.target_generation == scope.source_generation
        ):
            raise ChangeConflict("cleanup requires distinct explicit generations and ownership")
        if plan.retention_s is None:
            raise ChangeConflict("cleanup requires explicit resolved retention")
        if operation.activated_at is None:
            raise ChangeConflict("cleanup requires a persisted activation timestamp")
        if self._now() < operation.activated_at + timedelta(seconds=plan.retention_s):
            raise ChangeConflict("projection retention period has not expired")
