"""Event-log projection switching adapters for graph, vector and lexical stores.

This module connects the durable switch state machine to Memspine's existing
category projectors. Provider builders supply a generation-scoped projector;
the adapter owns the common event-log replay mechanics while the generation
handle owns vendor-specific isolation, parity probes, routing and deletion.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal, Protocol
from uuid import uuid4

from memspine.memories.associative.entities import EntityPolicy
from memspine.memories.associative.projector import GraphProjector
from memspine.models.encoders.embedding.base import EmbeddingService
from memspine.providers.switching import (
    CleanupEvidence,
    GenerationStatus,
    ProjectionGeneration,
    ProjectionSwitchStore,
    ProjectionVerificationEvidence,
    ReplayBatch,
    SourceSnapshot,
)
from memspine.services.graph.base import GraphStore
from memspine.services.lexical.base import LexicalStore
from memspine.services.lexical.projector import LexicalProjector
from memspine.services.vector.base import VectorStore
from memspine.services.vector.projector import VectorProjector
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.projections.projector import Projector

ProjectionCategory = Literal["graph", "vector", "lexical"]


class ReplayEventLog(Protocol):
    """The durable source-log surface needed for a complete generation build."""

    @property
    def can_rebuild(self) -> bool: ...

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]: ...

    async def get_offset(self, projector_name: str) -> int: ...


class ProjectionGenerationHandle(Protocol):
    """One isolated provider generation and its category projector."""

    @property
    def projector(self) -> Projector: ...

    async def verify(
        self,
        *,
        source: ProjectionGeneration,
        target: ProjectionGeneration,
        source_head_seq: int,
        erase_checkpoint_seq: int,
    ) -> ProjectionVerificationEvidence: ...

    async def activate(
        self,
        *,
        source: ProjectionGeneration,
        target: ProjectionGeneration,
        expected_source_revision: str,
        evidence: ProjectionVerificationEvidence,
    ) -> None: ...

    async def freeze(self, generation: ProjectionGeneration) -> None: ...

    async def has_live_references(self, generation: ProjectionGeneration) -> bool: ...

    async def delete_owned(self, generation: ProjectionGeneration) -> CleanupEvidence: ...

    async def close(self) -> None: ...


GenerationOpener = Callable[[ProjectionGeneration], Awaitable[ProjectionGenerationHandle]]
AdmissionStopper = Callable[[str, int], Awaitable[None]]


class EventLogProjectionBackend:
    """Replay the authoritative event log through an owned category projector.

    ``stop_admission`` must fence event-producing writes for the deployment
    until ``activate_target`` atomically routes new writes to the target. The
    target opener must return a generation-isolated projector whose reset,
    verification and deletion operations cannot affect a sibling generation.
    """

    def __init__(
        self,
        *,
        category: ProjectionCategory,
        event_log: ReplayEventLog,
        switch_store: ProjectionSwitchStore,
        open_generation: GenerationOpener,
        stop_admission: AdmissionStopper,
        read_batch_size: int,
    ) -> None:
        if read_batch_size < 1:
            raise ValueError("read_batch_size must be positive")
        self._category = category
        self._event_log = event_log
        self._switch_store = switch_store
        self._open_generation = open_generation
        self._stop_admission = stop_admission
        self._read_batch_size = read_batch_size

    async def stop_admission(self, deployment_id: str, fence_epoch: int) -> None:
        await self._stop_admission(deployment_id, fence_epoch)

    async def capture_snapshot(self, deployment_id: str) -> SourceSnapshot:
        del deployment_id  # the injected event-log instance is already deployment scoped
        if not self._event_log.can_rebuild:
            return SourceSnapshot(
                snapshot_id=f"event-log:{uuid4()}",
                checkpoint_seq=0,
                head_seq=0,
                complete=False,
                evidence_id="event-log-not-rebuildable",
                missing_ranges=("event-log history is not retained",),
            )

        first = await self._event_log.read_events(after_seq=0, limit=1)
        if first and first[0].seq != 1:
            return SourceSnapshot(
                snapshot_id=f"event-log:{uuid4()}",
                checkpoint_seq=0,
                head_seq=first[0].seq or 0,
                complete=False,
                evidence_id="event-log-history-pruned",
                missing_ranges=(f"1..{(first[0].seq or 1) - 1}",),
            )
        if not first:
            get_offset = getattr(self._event_log, "get_offset", None)
            if get_offset is not None:
                projector_name = {"graph": "graph", "vector": "vectors", "lexical": "lexical"}[
                    self._category
                ]
                if await get_offset(projector_name) > 0:
                    return SourceSnapshot(
                        snapshot_id=f"event-log:{uuid4()}",
                        checkpoint_seq=0,
                        head_seq=0,
                        complete=False,
                        evidence_id="event-log-empty-after-projection-advanced",
                        missing_ranges=("previously projected events are no longer retained",),
                    )

        head = 0
        cursor = 0
        while True:
            events = await self._event_log.read_events(
                after_seq=cursor, limit=self._read_batch_size
            )
            if not events:
                break
            for event in events:
                if event.seq is None or event.seq != cursor + 1:
                    return SourceSnapshot(
                        snapshot_id=f"event-log:{uuid4()}",
                        checkpoint_seq=0,
                        head_seq=head,
                        complete=False,
                        evidence_id="event-log-sequence-gap",
                        missing_ranges=(f"{cursor + 1}..{event.seq or 'unknown'}",),
                    )
                cursor = event.seq
                head = event.seq
        return SourceSnapshot(
            snapshot_id=f"event-log:{uuid4()}",
            checkpoint_seq=0,
            head_seq=head,
            complete=True,
            evidence_id=f"event-log-complete:{head}",
        )

    async def prepare_owned_target(self, generation: ProjectionGeneration) -> None:
        self._validate_generation(generation)
        handle = await self._open_generation(generation)
        try:
            await handle.projector.reset()
        finally:
            await handle.close()

    async def restore_snapshot(self, snapshot_id: str, generation: ProjectionGeneration) -> int:
        if not snapshot_id.startswith("event-log:"):
            raise ValueError("snapshot does not belong to the event-log replay adapter")
        self._validate_generation(generation)
        # The snapshot is the empty projection at sequence zero. The complete
        # source log is replayed in batches by replay_batch.
        return 0

    async def replay_batch(
        self,
        *,
        deployment_id: str,
        source_generation: str,
        target: ProjectionGeneration,
        after_seq: int,
        through_seq: int,
        max_events: int,
    ) -> ReplayBatch:
        del deployment_id, source_generation
        self._validate_generation(target)
        if max_events < 1 or after_seq < 0 or through_seq < after_seq:
            raise ValueError("replay range and batch size are invalid")
        handle = await self._open_generation(target)
        projector = handle.projector
        cursor = after_seq
        try:
            events = await self._event_log.read_events(
                after_seq=after_seq,
                limit=min(max_events, self._read_batch_size),
            )
            events = [
                event for event in events if event.seq is not None and event.seq <= through_seq
            ]
            if not events and after_seq < through_seq:
                raise RuntimeError("event log ended before the captured source head")
            if events:
                projector.begin_batch()
                for event in events:
                    if event.seq != cursor + 1:
                        raise RuntimeError("event log has a gap during projection replay")
                    await projector.apply(event)
                    cursor = event.seq
                await projector.flush()
        finally:
            await handle.close()
        return ReplayBatch(
            source_checkpoint_seq=cursor,
            target_checkpoint_seq=cursor,
            done=cursor >= through_seq,
        )

    async def complete_erasures(
        self,
        *,
        deployment_id: str,
        source_generation: str,
        target: ProjectionGeneration,
    ) -> int:
        del deployment_id, source_generation
        self._validate_generation(target)
        # FORGET events are handled by the standard graph/vector/lexical
        # projectors in ordered replay. The durable target checkpoint is the
        # erasure checkpoint; there is no separate untracked erasure channel.
        return target.checkpoint_seq

    async def apply_retained_erasure(
        self, generation: ProjectionGeneration, event: MemoryEvent
    ) -> ProjectionGeneration:
        """Apply and durably checkpoint one erasure on a frozen retained source.

        Re-delivery after a crash is safe: the projector's FORGET operations
        are idempotent, and the separate erasure cursor advances only after the
        projector call succeeds.
        """
        self._validate_generation(generation)
        if event.kind is not EventKind.FORGET or event.seq is None:
            raise ValueError("retained-generation erasure requires a sequenced FORGET event")
        logged = await self._event_log.read_events(after_seq=event.seq - 1, limit=1)
        if not logged or logged[0].seq != event.seq or logged[0].event_id != event.event_id:
            raise ValueError(
                "retained-generation erasure is not present in the authoritative event log"
            )
        current = await self._switch_store.get_generation(
            generation.deployment_id,
            generation.logical_service_id,
            generation.generation_id,
        )
        if (
            current.owner_id != generation.owner_id
            or current.target_fingerprint != generation.target_fingerprint
        ):
            raise ValueError(
                "retained-generation erasure ownership does not match persisted metadata"
            )
        if current.status is not GenerationStatus.RETAINED or not current.frozen:
            raise ValueError("erasures may be applied only to a frozen retained generation")
        if event.seq < current.erase_checkpoint_seq:
            raise ValueError("retained-generation erasure arrived behind its durable checkpoint")
        if event.seq == current.erase_checkpoint_seq:
            return current

        # ``erase_checkpoint_seq`` is a high-water mark. Advancing it over an
        # earlier FORGET would make that erasure impossible to replay later, so
        # prove this is the next FORGET in the authoritative ordered log before
        # touching the retained projection.
        cursor = current.erase_checkpoint_seq
        next_forget: MemoryEvent | None = None
        while cursor < event.seq:
            batch = await self._event_log.read_events(
                after_seq=cursor,
                limit=self._read_batch_size,
            )
            if not batch:
                raise ValueError("authoritative event log ended before retained erasure")
            for logged_event in batch:
                if logged_event.seq is None or logged_event.seq != cursor + 1:
                    raise ValueError("authoritative event log has a gap before retained erasure")
                cursor = logged_event.seq
                if logged_event.kind is EventKind.FORGET and next_forget is None:
                    next_forget = logged_event
                if cursor >= event.seq:
                    break
        if next_forget is None or next_forget.event_id != event.event_id:
            raise ValueError("retained-generation erasure is not the next unapplied FORGET event")

        handle = await self._open_generation(current)
        try:
            await handle.projector.apply(event)
        finally:
            await handle.close()
        updated = current.model_copy(
            update={
                "erase_checkpoint_seq": event.seq,
                "updated_at": datetime.now(UTC),
            }
        )
        await self._switch_store.save_generation(updated, expected_revision=current.revision)
        return await self._switch_store.get_generation(
            generation.deployment_id,
            generation.logical_service_id,
            generation.generation_id,
        )

    async def verify(
        self,
        *,
        source: ProjectionGeneration,
        target: ProjectionGeneration,
        source_head_seq: int,
        erase_checkpoint_seq: int,
    ) -> ProjectionVerificationEvidence:
        self._validate_generation(target)
        if source.owner_id != target.owner_id:
            raise ValueError("source and target projection ownership differs")
        handle = await self._open_generation(target)
        try:
            return await handle.verify(
                source=source,
                target=target,
                source_head_seq=source_head_seq,
                erase_checkpoint_seq=erase_checkpoint_seq,
            )
        finally:
            await handle.close()

    async def activate_target(
        self,
        *,
        deployment_id: str,
        expected_source_revision: str,
        source: ProjectionGeneration,
        target: ProjectionGeneration,
        evidence: ProjectionVerificationEvidence,
    ) -> None:
        del deployment_id
        self._validate_generation(target)
        handle = await self._open_generation(target)
        try:
            await handle.activate(
                source=source,
                target=target,
                expected_source_revision=expected_source_revision,
                evidence=evidence,
            )
        finally:
            await handle.close()

    async def freeze_source(self, generation: ProjectionGeneration) -> None:
        self._validate_generation(generation)
        handle = await self._open_generation(generation)
        try:
            await handle.freeze(generation)
        finally:
            await handle.close()

    async def has_live_references(self, generation: ProjectionGeneration) -> bool:
        self._validate_generation(generation)
        handle = await self._open_generation(generation)
        try:
            return await handle.has_live_references(generation)
        finally:
            await handle.close()

    async def delete_owned_generation(self, generation: ProjectionGeneration) -> CleanupEvidence:
        self._validate_generation(generation)
        handle = await self._open_generation(generation)
        try:
            return await handle.delete_owned(generation)
        finally:
            await handle.close()

    def _validate_generation(self, generation: ProjectionGeneration) -> None:
        if not generation.logical_service_id:
            raise ValueError("projection generation logical service identity is required")
        if generation.owner_id == "":
            raise ValueError("projection generation owner is required")
        if generation.generation_id == "":
            raise ValueError("projection generation identity is required")


def graph_projector(
    store: GraphStore,
    *,
    entities: EntityPolicy | None = None,
    embed: Callable[[list[str]], Awaitable[list[list[float]]]] | None = None,
) -> Projector:
    """Construct the canonical graph projector for an isolated generation."""
    return GraphProjector(store, entities=entities, embed=embed)


def vector_projector(store: VectorStore, embedder: EmbeddingService) -> Projector:
    """Construct the canonical vector projector for an isolated generation."""
    return VectorProjector(store, embedder)


def lexical_projector(store: LexicalStore, *, name: str = "lexical") -> Projector:
    """Construct the canonical lexical projector for an isolated generation."""
    return LexicalProjector(store, name=name)


__all__ = [
    "EventLogProjectionBackend",
    "ProjectionCategory",
    "ProjectionGenerationHandle",
    "ReplayEventLog",
    "graph_projector",
    "lexical_projector",
    "vector_projector",
]
