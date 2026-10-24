"""Controlled rebuild and activation of owned projection generations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from memspine.providers.changes import (
    ChangeContext,
    ChangeCoordinator,
    ChangePlan,
    ChangeRequest,
    ChangeResult,
    ChangeScope,
    ChangeStatus,
    OperationRecord,
    PlanStep,
    ProjectionRebuildResult,
    ResourceEstimate,
    SourceCompleteness,
    TargetConfigRef,
)
from memspine.providers.settings import StrictSettings
from memspine.services.storage.provider_tables import (
    _generation_rows,
    _operations,
    _plans,
    _switch_rows,
)
from memspine.services.storage.sqlite.schema import metadata as _switch_metadata


class SwitchManifestStatus(StrEnum):
    PLANNED = "planned"
    BUILDING = "building"
    CATCHING_UP = "catching_up"
    VERIFYING = "verifying"
    ACTIVE = "active"
    RETAINED = "retained"
    CLEANUP_PENDING = "cleanup_pending"
    CLEANED = "cleaned"
    FAILED = "failed"


class GenerationStatus(StrEnum):
    BUILDING = "building"
    ACTIVE = "active"
    RETAINED = "retained"
    CLEANUP_PENDING = "cleanup_pending"
    CLEANED = "cleaned"
    FAILED = "failed"


class SwitchModel(StrictSettings):
    """Strict immutable switch metadata."""


class ProjectionGeneration(SwitchModel):
    deployment_id: str = Field(min_length=1)
    logical_service_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1)
    target_fingerprint: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    status: GenerationStatus
    revision: int = Field(default=1, ge=1)
    checkpoint_seq: int = Field(default=0, ge=0)
    erase_checkpoint_seq: int = Field(default=0, ge=0)
    frozen: bool = False
    updated_at: datetime

    def model_post_init(self, __context: object) -> None:
        super().model_post_init(__context)
        if self.updated_at.tzinfo is None:
            raise ValueError("generation timestamp must be timezone-aware")

    @property
    def physical_generation_id(self) -> str:
        from memspine.providers.placement import physical_generation_id

        return physical_generation_id(self.logical_service_id, self.generation_id)


class ProjectionSwitchManifest(SwitchModel):
    schema_version: Literal[1] = 1
    switch_id: str = Field(min_length=1)
    deployment_id: str = Field(min_length=1)
    source_provider: str = Field(min_length=1)
    target_provider: str = Field(min_length=1)
    source_generation: str = Field(min_length=1, max_length=128)
    target_generation: str = Field(min_length=1, max_length=128)
    source_target_fingerprint: str = Field(min_length=1)
    target_fingerprint: str = Field(min_length=1)
    config_fingerprint: str = Field(min_length=1)
    status: SwitchManifestStatus
    revision: int = Field(ge=1)
    snapshot_checkpoint_seq: int | None = Field(default=None, ge=0)
    target_checkpoint_seq: int = Field(default=0, ge=0)
    cutover_head_seq: int | None = Field(default=None, ge=0)
    erase_checkpoint_seq: int = Field(default=0, ge=0)
    created_at: datetime
    activated_at: datetime | None = None
    expires_at: datetime | None = None
    last_error_code: str | None = None


class SourceSnapshot(SwitchModel):
    snapshot_id: str = Field(min_length=1)
    checkpoint_seq: int = Field(ge=0)
    head_seq: int = Field(ge=0)
    complete: bool
    evidence_id: str = Field(min_length=1)
    missing_ranges: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_complete(self) -> SourceSnapshot:
        if self.complete and self.missing_ranges:
            raise ValueError("complete source snapshot cannot report missing ranges")
        if self.checkpoint_seq > self.head_seq:
            raise ValueError("snapshot checkpoint exceeds source head")
        return self

    def require_complete(self) -> SourceSnapshot:
        if not self.complete or self.missing_ranges:
            raise ValueError("authoritative source snapshot is incomplete")
        return self


class ReplayBatch(SwitchModel):
    source_checkpoint_seq: int = Field(ge=0)
    target_checkpoint_seq: int = Field(ge=0)
    done: bool


class ProjectionVerificationEvidence(SwitchModel):
    report_id: str = Field(min_length=1)
    source_head_seq: int = Field(ge=0)
    target_checkpoint_seq: int = Field(ge=0)
    erase_checkpoint_seq: int = Field(ge=0)
    owner_id: str = Field(min_length=1)
    target_fingerprint: str = Field(min_length=1)
    successful_probes: tuple[str, ...] = Field(min_length=1)
    failed_probes: tuple[str, ...] = ()


class CleanupEvidence(SwitchModel):
    report_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1, max_length=128)
    target_fingerprint: str = Field(min_length=1)
    deleted: bool
    remaining_references: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class ActiveProjectionRoute:
    """A route backed by a completed and persisted projection activation."""

    operation_id: str
    deployment_id: str
    projector_name: str
    category: Literal["graph", "vector", "lexical"]
    logical_service_id: str
    provider: str
    target_fingerprint: str
    generation_id: str
    source_provider: str
    source_target_fingerprint: str
    source_generation_id: str
    target_checkpoint_seq: int

    @property
    def physical_generation_id(self) -> str:
        from memspine.providers.placement import physical_generation_id

        return physical_generation_id(self.logical_service_id, self.generation_id)


def _projector_for_service(category: str, logical_service_id: str) -> str:
    canonical_projectors = {
        ("graph", "graph.primary"): "graph",
        ("vector", "vector"): "vectors",
        ("lexical", "lexical"): "lexical",
    }
    try:
        return canonical_projectors[(category, logical_service_id)]
    except KeyError:
        raise RuntimeError(
            f"projection switch logical service {logical_service_id!r} cannot route "
            f"category {category!r}"
        ) from None


def _detail_int(details: object, key: str) -> int:
    if not isinstance(details, dict):
        raise RuntimeError("persisted switch checkpoint details are invalid")
    value = details.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"persisted switch checkpoint {key!r} is invalid")
    return value


class ProjectionSwitchBackend(Protocol):
    """Adapter hooks required for a safe projection-generation switch."""

    async def stop_admission(self, deployment_id: str, fence_epoch: int) -> None: ...

    async def capture_snapshot(self, deployment_id: str) -> SourceSnapshot: ...

    async def prepare_owned_target(self, generation: ProjectionGeneration) -> None: ...

    async def restore_snapshot(self, snapshot_id: str, generation: ProjectionGeneration) -> int: ...

    async def replay_batch(
        self,
        *,
        deployment_id: str,
        source_generation: str,
        target: ProjectionGeneration,
        after_seq: int,
        through_seq: int,
        max_events: int,
    ) -> ReplayBatch: ...

    async def complete_erasures(
        self,
        *,
        deployment_id: str,
        source_generation: str,
        target: ProjectionGeneration,
    ) -> int: ...

    async def verify(
        self,
        *,
        source: ProjectionGeneration,
        target: ProjectionGeneration,
        source_head_seq: int,
        erase_checkpoint_seq: int,
    ) -> ProjectionVerificationEvidence: ...

    async def activate_target(
        self,
        *,
        deployment_id: str,
        expected_source_revision: str,
        source: ProjectionGeneration,
        target: ProjectionGeneration,
        evidence: ProjectionVerificationEvidence,
    ) -> None: ...

    async def freeze_source(self, generation: ProjectionGeneration) -> None: ...

    async def has_live_references(self, generation: ProjectionGeneration) -> bool: ...

    async def delete_owned_generation(
        self, generation: ProjectionGeneration
    ) -> CleanupEvidence: ...


class ProjectionSwitchStore(Protocol):
    async def create_schema(self) -> None: ...

    async def get_generation(
        self, deployment_id: str, logical_service_id: str, generation_id: str
    ) -> ProjectionGeneration: ...

    async def save_generation(
        self, generation: ProjectionGeneration, *, expected_revision: int | None
    ) -> None: ...

    async def get_switch(self, switch_id: str) -> ProjectionSwitchManifest | None: ...

    async def save_switch(
        self, manifest: ProjectionSwitchManifest, *, expected_revision: int | None
    ) -> None: ...


async def verified_active_projection_routes(
    connection: AsyncConnection,
    deployment_id: str,
) -> Mapping[str, ActiveProjectionRoute]:
    """Resolve current routes only from activated operations with durable verification.

    The operation is the authority for category and logical service identity;
    the switch manifest and active generation must agree with its persisted
    plan/result before a caller may route a built-in provider to that target.
    Cleanup may advance operation and manifest state without changing which
    target generation is active, so those terminal states retain the evidence.
    """
    from memspine.providers.changes import (
        ChangePlan,
        ChangeStatus,
        OperationRecord,
        ProjectionRebuildResult,
    )

    active_evidence_statuses = {
        ChangeStatus.ACTIVE.value,
        ChangeStatus.CLEANUP_PENDING.value,
        ChangeStatus.CLEANED.value,
    }
    operation_rows = (
        await connection.execute(
            select(_operations.c.payload).where(
                _operations.c.deployment_id == deployment_id,
                _operations.c.status.in_(active_evidence_statuses),
            )
        )
    ).all()
    routes: dict[str, ActiveProjectionRoute] = {}
    for (raw_operation,) in operation_rows:
        operation = OperationRecord.model_validate_json(json.dumps(raw_operation))
        if not isinstance(operation.result, ProjectionRebuildResult):
            continue
        if operation.result.status is not ChangeStatus.ACTIVE:
            continue
        if not operation.result.verification_report_id:
            raise RuntimeError("active projection operation lacks verification evidence")
        if operation.result.target_checkpoint is None:
            raise RuntimeError("active projection operation lacks its target checkpoint")
        plan_row = (
            await connection.execute(
                select(_plans.c.payload).where(_plans.c.plan_id == operation.plan_id)
            )
        ).first()
        if plan_row is None:
            raise RuntimeError("active projection operation has no persisted plan")
        plan = ChangePlan.model_validate_json(json.dumps(plan_row.payload))
        request, scope = plan.request, plan.request.scope
        if scope.resource_class != "projection" or request.category not in {
            "graph",
            "vector",
            "lexical",
        }:
            continue
        target_generation = scope.target_generation
        if (
            scope.deployment_id != deployment_id
            or operation.deployment_id != deployment_id
            or operation.result.deployment_id != deployment_id
            or operation.result.target_generation != target_generation
            or operation.result.source_generation != scope.source_generation
        ):
            raise RuntimeError(
                "active projection operation ownership or generation is inconsistent"
            )
        generation_row = (
            await connection.execute(
                select(_generation_rows.c.payload).where(
                    _generation_rows.c.deployment_id == deployment_id,
                    _generation_rows.c.logical_service_id == request.logical_service_id,
                    _generation_rows.c.generation_id == target_generation,
                )
            )
        ).first()
        if generation_row is None:
            raise RuntimeError("active projection operation has no target generation")
        generation = ProjectionGeneration.model_validate_json(json.dumps(generation_row.payload))
        if generation.status is not GenerationStatus.ACTIVE:
            # A later verified operation superseded this historical activation.
            continue
        manifest_row = (
            await connection.execute(
                select(_switch_rows.c.payload).where(
                    _switch_rows.c.switch_id == operation.operation_id,
                    _switch_rows.c.deployment_id == deployment_id,
                )
            )
        ).first()
        if manifest_row is None:
            raise RuntimeError("active projection operation has no switch manifest")
        manifest = ProjectionSwitchManifest.model_validate_json(json.dumps(manifest_row.payload))
        if manifest.status not in {
            SwitchManifestStatus.ACTIVE,
            SwitchManifestStatus.RETAINED,
            SwitchManifestStatus.CLEANUP_PENDING,
            SwitchManifestStatus.CLEANED,
        }:
            raise RuntimeError("active projection operation has no activated switch manifest")
        expected = (
            manifest.target_generation == target_generation
            and manifest.target_provider == scope.target_provider == generation.provider
            and manifest.target_fingerprint
            == scope.target_fingerprint
            == generation.target_fingerprint
            and manifest.deployment_id == deployment_id
            and generation.owner_id == scope.owner_id
            and generation.checkpoint_seq == operation.result.target_checkpoint
            and generation.erase_checkpoint_seq == operation.result.erase_checkpoint
        )
        if not expected:
            raise RuntimeError("active projection switch manifest and generation disagree")
        projector_name = _projector_for_service(request.category, request.logical_service_id)
        route = ActiveProjectionRoute(
            operation_id=operation.operation_id,
            deployment_id=deployment_id,
            projector_name=projector_name,
            category=request.category,
            logical_service_id=request.logical_service_id,
            provider=scope.target_provider or request.target_config_ref.provider,
            target_fingerprint=scope.target_fingerprint or "",
            generation_id=target_generation or "",
            source_provider=scope.source_provider or "",
            source_target_fingerprint=scope.source_target_fingerprint or "",
            source_generation_id=scope.source_generation or "",
            target_checkpoint_seq=operation.result.target_checkpoint,
        )
        previous = routes.get(projector_name)
        if previous is not None and previous.generation_id != route.generation_id:
            raise RuntimeError(
                "multiple active verified projection generations target one projector"
            )
        routes[projector_name] = route
    return routes


class SqlProjectionSwitchStore:
    """Durable generation and switch manifest storage using SQLAlchemy Core."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_schema(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(_switch_metadata.create_all)

    async def get_generation(
        self, deployment_id: str, logical_service_id: str, generation_id: str
    ) -> ProjectionGeneration:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(_generation_rows.c.payload).where(
                        _generation_rows.c.deployment_id == deployment_id,
                        _generation_rows.c.logical_service_id == logical_service_id,
                        _generation_rows.c.generation_id == generation_id,
                    )
                )
            ).first()
        if row is None:
            raise KeyError(
                f"unknown projection generation {deployment_id}/{logical_service_id}/{generation_id}"
            )
        return ProjectionGeneration.model_validate_json(json.dumps(row.payload))

    async def save_generation(
        self, generation: ProjectionGeneration, *, expected_revision: int | None
    ) -> None:
        key = (generation.deployment_id, generation.logical_service_id, generation.generation_id)
        async with self._engine.begin() as connection:
            current = (
                await connection.execute(
                    select(_generation_rows.c.revision).where(
                        _generation_rows.c.deployment_id == key[0],
                        _generation_rows.c.logical_service_id == key[1],
                        _generation_rows.c.generation_id == key[2],
                    )
                )
            ).scalar_one_or_none()
            if current != expected_revision:
                raise RuntimeError("projection generation revision changed concurrently")
            revision = 1 if current is None else current + 1
            value = generation.model_copy(update={"revision": revision})
            payload = value.model_dump(mode="json")
            if current is None:
                await connection.execute(
                    _generation_rows.insert().values(
                        deployment_id=key[0],
                        logical_service_id=key[1],
                        generation_id=key[2],
                        status=value.status.value,
                        revision=revision,
                        payload=payload,
                    )
                )
            else:
                result = await connection.execute(
                    update(_generation_rows)
                    .where(
                        _generation_rows.c.deployment_id == key[0],
                        _generation_rows.c.logical_service_id == key[1],
                        _generation_rows.c.generation_id == key[2],
                        _generation_rows.c.revision == current,
                    )
                    .values(status=value.status.value, revision=revision, payload=payload)
                )
                if result.rowcount != 1:
                    raise RuntimeError("projection generation revision changed concurrently")

    async def get_switch(self, switch_id: str) -> ProjectionSwitchManifest | None:
        async with self._engine.connect() as connection:
            row = (
                await connection.execute(
                    select(_switch_rows.c.payload).where(_switch_rows.c.switch_id == switch_id)
                )
            ).first()
        return (
            ProjectionSwitchManifest.model_validate_json(json.dumps(row.payload)) if row else None
        )

    async def save_switch(
        self, manifest: ProjectionSwitchManifest, *, expected_revision: int | None
    ) -> None:
        async with self._engine.begin() as connection:
            current = await connection.scalar(
                select(_switch_rows.c.revision).where(
                    _switch_rows.c.switch_id == manifest.switch_id
                )
            )
            if current != expected_revision:
                raise RuntimeError("projection switch revision changed concurrently")
            revision = 1 if current is None else current + 1
            value = manifest.model_copy(update={"revision": revision})
            if current is None:
                await connection.execute(
                    _switch_rows.insert().values(
                        switch_id=value.switch_id,
                        deployment_id=value.deployment_id,
                        status=value.status.value,
                        revision=revision,
                        payload=value.model_dump(mode="json"),
                    )
                )
            else:
                result = await connection.execute(
                    update(_switch_rows)
                    .where(
                        _switch_rows.c.switch_id == value.switch_id,
                        _switch_rows.c.revision == current,
                    )
                    .values(
                        status=value.status.value,
                        revision=revision,
                        payload=value.model_dump(mode="json"),
                    )
                )
                if result.rowcount != 1:
                    raise RuntimeError("projection switch revision changed concurrently")


class ProjectionSwitchStrategy:
    """Rebuild a fresh owned generation, verify it, then activate and retain source."""

    def __init__(
        self,
        *,
        backend: ProjectionSwitchBackend,
        store: ProjectionSwitchStore,
        owner_id: str,
        target_provider: str,
        target_fingerprint: str,
        batch_size: int,
    ) -> None:
        if not owner_id or not target_provider or not target_fingerprint:
            raise ValueError("projection switch ownership and target identity are required")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._backend = backend
        self._store = store
        self._owner_id = owner_id
        self._target_provider = target_provider
        self._target_fingerprint = target_fingerprint
        self._batch_size = batch_size

    async def execute(self, context: ChangeContext) -> None:
        plan = context.plan
        request = plan.request
        scope = request.scope
        if scope.owner_id != self._owner_id or scope.target_fingerprint != self._target_fingerprint:
            raise ValueError("plan ownership and target fingerprint must match the switch strategy")
        if scope.resource_class != "projection" or request.category not in {
            "graph",
            "vector",
            "lexical",
        }:
            raise ValueError("projection rebuild strategy requires graph, vector or lexical scope")
        if not plan.source_completeness.complete:
            raise ValueError("projection source completeness is not proven")
        await self._source_generation(context)
        target = self._target_generation(context, GenerationStatus.BUILDING)
        manifest = await self._manifest(context, SwitchManifestStatus.PLANNED)
        checkpoint = context.operation.checkpoint

        if checkpoint.phase == "planned":
            await self._backend.stop_admission(scope.deployment_id, context.operation.fence_epoch)
            snapshot = (
                await self._backend.capture_snapshot(scope.deployment_id)
            ).require_complete()
            checkpoint = checkpoint.model_copy(
                update={
                    "phase": "snapshot",
                    "source_checkpoint": snapshot.checkpoint_seq,
                    "target_checkpoint": 0,
                    "details": {
                        "snapshot_id": snapshot.snapshot_id,
                        "source_head_seq": snapshot.head_seq,
                    },
                }
            )
            await self._save_manifest(
                context,
                manifest.model_copy(
                    update={
                        "status": SwitchManifestStatus.BUILDING,
                        "snapshot_checkpoint_seq": snapshot.checkpoint_seq,
                        "cutover_head_seq": snapshot.head_seq,
                    }
                ),
            )
            await context.checkpoint(checkpoint)

        checkpoint = context.operation.checkpoint
        if checkpoint.phase == "snapshot":
            await self._backend.prepare_owned_target(target)
            snapshot_id = str(checkpoint.details["snapshot_id"])
            target_seq = await self._backend.restore_snapshot(snapshot_id, target)
            checkpoint = checkpoint.model_copy(
                update={"phase": "replaying", "target_checkpoint": target_seq}
            )
            await self._save_generation(target.model_copy(update={"checkpoint_seq": target_seq}))
            await self._save_manifest(
                context,
                (await self._manifest(context, SwitchManifestStatus.BUILDING)).model_copy(
                    update={"target_checkpoint_seq": target_seq}
                ),
            )
            await context.checkpoint(checkpoint)

        checkpoint = context.operation.checkpoint
        if checkpoint.phase == "replaying":
            head = _detail_int(checkpoint.details, "source_head_seq")
            if (checkpoint.source_checkpoint or 0) >= head:
                checkpoint = checkpoint.model_copy(update={"phase": "catching_up"})
                await context.checkpoint(checkpoint)
            while (checkpoint.source_checkpoint or 0) < head:
                batch = await self._backend.replay_batch(
                    deployment_id=scope.deployment_id,
                    source_generation=scope.source_generation or "",
                    target=target.model_copy(
                        update={"checkpoint_seq": checkpoint.target_checkpoint or 0}
                    ),
                    after_seq=checkpoint.source_checkpoint or 0,
                    through_seq=head,
                    max_events=self._batch_size,
                )
                if batch.source_checkpoint_seq > head:
                    raise RuntimeError("source replay advanced beyond the fenced head")
                if batch.source_checkpoint_seq < (checkpoint.source_checkpoint or 0):
                    raise RuntimeError("source replay checkpoint moved backwards")
                if batch.target_checkpoint_seq < (checkpoint.target_checkpoint or 0):
                    raise RuntimeError("target replay checkpoint moved backwards")
                if not batch.done and batch.source_checkpoint_seq == checkpoint.source_checkpoint:
                    raise RuntimeError("replay batch made no progress")
                if batch.done and batch.source_checkpoint_seq != head:
                    raise RuntimeError("replay completed before reaching the fenced source head")
                checkpoint = checkpoint.model_copy(
                    update={
                        "source_checkpoint": batch.source_checkpoint_seq,
                        "target_checkpoint": batch.target_checkpoint_seq,
                        "phase": "replaying" if not batch.done else "catching_up",
                    }
                )
                target = target.model_copy(update={"checkpoint_seq": batch.target_checkpoint_seq})
                await self._save_generation(target)
                await self._save_manifest(
                    context,
                    (await self._manifest(context, SwitchManifestStatus.CATCHING_UP)).model_copy(
                        update={"target_checkpoint_seq": batch.target_checkpoint_seq}
                    ),
                )
                await context.checkpoint(checkpoint)
                if batch.done:
                    break

        checkpoint = context.operation.checkpoint
        if checkpoint.phase == "catching_up":
            head = _detail_int(checkpoint.details, "source_head_seq")
            erase_seq = await self._backend.complete_erasures(
                deployment_id=scope.deployment_id,
                source_generation=scope.source_generation or "",
                target=target.model_copy(
                    update={"checkpoint_seq": checkpoint.target_checkpoint or 0}
                ),
            )
            checkpoint = checkpoint.model_copy(
                update={"phase": "ready_to_verify", "erase_checkpoint": erase_seq}
            )
            await self._save_generation(
                target.model_copy(
                    update={
                        "checkpoint_seq": checkpoint.target_checkpoint or 0,
                        "erase_checkpoint_seq": erase_seq,
                    }
                )
            )
            await self._save_manifest(
                context,
                (await self._manifest(context, SwitchManifestStatus.VERIFYING)).model_copy(
                    update={
                        "cutover_head_seq": head,
                        "target_checkpoint_seq": checkpoint.target_checkpoint or 0,
                        "erase_checkpoint_seq": erase_seq,
                    }
                ),
            )
            await context.checkpoint(checkpoint)

    async def verify(self, context: ChangeContext) -> ChangeResult:
        checkpoint = context.operation.checkpoint
        if checkpoint.phase != "ready_to_verify":
            raise RuntimeError("projection target has not completed replay and erasure catch-up")
        source = await self._source_generation(context)
        target = self._target_generation(context, GenerationStatus.BUILDING).model_copy(
            update={"checkpoint_seq": checkpoint.target_checkpoint or 0}
        )
        head = _detail_int(checkpoint.details, "source_head_seq")
        evidence = await self._backend.verify(
            source=source,
            target=target,
            source_head_seq=head,
            erase_checkpoint_seq=checkpoint.erase_checkpoint,
        )
        if evidence.failed_probes:
            raise RuntimeError("projection verification reported failed mandatory probes")
        if (
            evidence.source_head_seq != head
            or evidence.target_checkpoint_seq != checkpoint.target_checkpoint
        ):
            raise RuntimeError("projection verification checkpoint does not match the switch plan")
        if evidence.erase_checkpoint_seq < checkpoint.erase_checkpoint:
            raise RuntimeError("projection erasure verification is behind the required checkpoint")
        if (
            evidence.owner_id != target.owner_id
            or evidence.target_fingerprint != target.target_fingerprint
        ):
            raise RuntimeError("projection verification ownership does not match the target")
        await context.checkpoint(
            checkpoint.model_copy(
                update={
                    "phase": "ready_to_activate",
                    "details": {
                        **dict(checkpoint.details),
                        "verification_evidence": evidence.model_dump(mode="json"),
                    },
                }
            )
        )
        created = datetime.now(UTC)
        return ProjectionRebuildResult(
            operation_id=context.operation.operation_id,
            status=ChangeStatus.VERIFIED,
            created_at=created,
            updated_at=created,
            deployment_id=context.plan.request.scope.deployment_id,
            source_generation=context.plan.request.scope.source_generation or "",
            target_generation=context.plan.request.scope.target_generation or "",
            source_checkpoint=head,
            target_checkpoint=checkpoint.target_checkpoint,
            erase_checkpoint=evidence.erase_checkpoint_seq,
            verification_report_id=evidence.report_id,
        )

    async def activate(self, context: ChangeContext, result: ChangeResult) -> None:
        if not isinstance(result, ProjectionRebuildResult) or not result.verification_report_id:
            raise RuntimeError("projection activation requires typed verification evidence")
        source = await self._source_generation(context)
        target = self._target_generation(context, GenerationStatus.BUILDING).model_copy(
            update={
                "status": GenerationStatus.ACTIVE,
                "checkpoint_seq": result.target_checkpoint or 0,
                "erase_checkpoint_seq": result.erase_checkpoint,
            }
        )
        if context.operation.checkpoint.phase != "ready_to_activate":
            raise RuntimeError("projection activation requires a persisted verification checkpoint")
        evidence = ProjectionVerificationEvidence.model_validate_json(
            json.dumps(context.operation.checkpoint.details["verification_evidence"])
        )
        if evidence.report_id != result.verification_report_id:
            raise RuntimeError("persisted verification evidence does not match the result")
        await self._backend.activate_target(
            deployment_id=target.deployment_id,
            expected_source_revision=context.plan.request.source_revision,
            source=source,
            target=target,
            evidence=evidence,
        )
        await self._backend.freeze_source(source)
        retained = source.model_copy(update={"status": GenerationStatus.RETAINED, "frozen": True})
        await self._save_generation(target)
        await self._save_generation(retained)
        manifest = await self._manifest(context, SwitchManifestStatus.ACTIVE)
        await self._save_manifest(
            context,
            manifest.model_copy(
                update={
                    "status": SwitchManifestStatus.RETAINED,
                    "activated_at": datetime.now(UTC),
                    "expires_at": datetime.now(UTC)
                    + timedelta(seconds=context.plan.retention_s or 0),
                }
            ),
        )

    async def cleanup(self, context: ChangeContext, result: ChangeResult) -> ChangeResult:
        if not isinstance(result, ProjectionRebuildResult):
            raise RuntimeError("projection cleanup requires a projection rebuild result")
        source = await self._source_generation(context)
        if not source.frozen or source.status != GenerationStatus.RETAINED:
            raise RuntimeError("source generation is not frozen and retained")
        if await self._backend.has_live_references(source):
            raise RuntimeError("source generation still has active request or rollback references")
        await self._save_generation(
            source.model_copy(update={"status": GenerationStatus.CLEANUP_PENDING})
        )
        manifest = await self._manifest(context, SwitchManifestStatus.CLEANUP_PENDING)
        if manifest.status not in {
            SwitchManifestStatus.CLEANUP_PENDING,
            SwitchManifestStatus.CLEANED,
        }:
            await self._save_manifest(
                context,
                manifest.model_copy(update={"status": SwitchManifestStatus.CLEANUP_PENDING}),
            )
        evidence = await self._backend.delete_owned_generation(source)
        if (
            not evidence.deleted
            or evidence.remaining_references
            or evidence.owner_id != source.owner_id
            or evidence.generation_id != source.generation_id
            or evidence.target_fingerprint != source.target_fingerprint
        ):
            raise RuntimeError("owned generation cleanup is incomplete or has mismatched ownership")
        await self._save_generation(source.model_copy(update={"status": GenerationStatus.CLEANED}))
        manifest = await self._manifest(context, SwitchManifestStatus.CLEANUP_PENDING)
        await self._save_manifest(
            context, manifest.model_copy(update={"status": SwitchManifestStatus.CLEANED})
        )
        return result

    async def _source_generation(self, context: ChangeContext) -> ProjectionGeneration:
        scope = context.plan.request.scope
        try:
            return await self._store.get_generation(
                scope.deployment_id,
                context.plan.request.logical_service_id,
                scope.source_generation or "",
            )
        except KeyError:
            if not scope.source_target_fingerprint:
                raise RuntimeError("source generation ownership fingerprint is required") from None
            source = ProjectionGeneration(
                deployment_id=scope.deployment_id,
                logical_service_id=context.plan.request.logical_service_id,
                generation_id=scope.source_generation or "",
                provider=scope.source_provider or context.plan.request.source_revision,
                target_fingerprint=scope.source_target_fingerprint,
                owner_id=scope.owner_id,
                status=GenerationStatus.ACTIVE,
                updated_at=datetime.now(UTC),
            )
            await self._store.save_generation(source, expected_revision=None)
            return source

    def _target_generation(
        self, context: ChangeContext, status: GenerationStatus
    ) -> ProjectionGeneration:
        scope = context.plan.request.scope
        return ProjectionGeneration(
            deployment_id=scope.deployment_id,
            logical_service_id=context.plan.request.logical_service_id,
            generation_id=scope.target_generation or "",
            provider=scope.target_provider or self._target_provider,
            target_fingerprint=self._target_fingerprint,
            owner_id=self._owner_id,
            status=status,
            updated_at=datetime.now(UTC),
        )

    async def _manifest(
        self, context: ChangeContext, fallback_status: SwitchManifestStatus
    ) -> ProjectionSwitchManifest:
        found = await self._store.get_switch(context.operation.operation_id)
        if found is not None:
            return found
        plan = context.plan
        scope = plan.request.scope
        return ProjectionSwitchManifest(
            switch_id=context.operation.operation_id,
            deployment_id=scope.deployment_id,
            source_provider=scope.source_provider or plan.request.source_revision,
            target_provider=scope.target_provider or self._target_provider,
            source_generation=scope.source_generation or "",
            target_generation=scope.target_generation or "",
            source_target_fingerprint=scope.source_target_fingerprint or "",
            target_fingerprint=self._target_fingerprint,
            config_fingerprint=plan.target_semantic_fingerprint,
            status=fallback_status,
            revision=1,
            created_at=context.operation.created_at,
        )

    async def _save_manifest(
        self, context: ChangeContext, manifest: ProjectionSwitchManifest
    ) -> None:
        current = await self._store.get_switch(manifest.switch_id)
        await self._store.save_switch(
            manifest,
            expected_revision=current.revision if current is not None else None,
        )

    async def _save_generation(self, generation: ProjectionGeneration) -> None:
        try:
            current = await self._store.get_generation(
                generation.deployment_id,
                generation.logical_service_id,
                generation.generation_id,
            )
        except KeyError:
            expected_revision = None
        else:
            expected_revision = current.revision
        await self._store.save_generation(generation, expected_revision=expected_revision)


class ProjectionSwitcher:
    """Small facade for planned projection changes; no config-triggered work."""

    def __init__(self, coordinator: ChangeCoordinator) -> None:
        self._coordinator = coordinator

    async def plan_switch(
        self,
        *,
        category: Literal["graph", "vector", "lexical"],
        logical_service_id: str,
        source_revision: str,
        target_revision: str,
        deployment_id: str,
        owner_id: str,
        source_generation: str,
        target_generation: str,
        source_target_fingerprint: str,
        target_fingerprint: str,
        source_provider: str,
        target_provider: str,
        config_id: str,
        source_completeness: SourceCompleteness,
        retention_s: int,
        actor: str,
    ) -> ChangePlan:
        request = ChangeRequest(
            category=category,
            logical_service_id=logical_service_id,
            source_revision=source_revision,
            target_revision=target_revision,
            scope=ChangeScope(
                deployment_id=deployment_id,
                owner_id=owner_id,
                resource_class="projection",
                source_provider=source_provider,
                target_provider=target_provider,
                source_generation=source_generation,
                target_generation=target_generation,
                source_target_fingerprint=source_target_fingerprint,
                target_fingerprint=target_fingerprint,
            ),
            target_config_ref=TargetConfigRef(
                provider=target_provider,
                config_id=config_id,
                semantic_fingerprint=target_fingerprint,
            ),
            requested_strategy="projection_rebuild",
            actor=actor,
            source_completeness=source_completeness,
        )
        return await self._coordinator.plan_change(
            request,
            steps=(
                PlanStep(
                    step_id="rebuild",
                    phase="migrate",
                    description="Rebuild an owned projection generation",
                ),
                PlanStep(
                    step_id="verify",
                    phase="verify",
                    description="Verify projection parity, ownership and erasures",
                ),
                PlanStep(
                    step_id="activate",
                    phase="activate",
                    description="Activate the verified target generation",
                ),
                PlanStep(
                    step_id="cleanup",
                    phase="cleanup",
                    description="Retire the old owned generation after retention",
                ),
            ),
            required_capabilities=frozenset({"checkpoint", "scoped_delete", "erasure"}),
            estimate=ResourceEstimate(),
            retention_s=retention_s,
        )

    async def execute_switch(self, plan_id: str) -> OperationRecord:
        return await self._coordinator.execute_change(plan_id)

    async def resume_switch(self, operation_id: str) -> OperationRecord:
        return await self._coordinator.resume_change(operation_id)

    async def verify_switch(self, operation_id: str) -> ChangeResult:
        return await self._coordinator.verify_change(operation_id)

    async def cleanup_switch(self, operation_id: str) -> OperationRecord:
        return await self._coordinator.cleanup_change(operation_id)
