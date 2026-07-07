"""Semantic write pipeline (M13.3): sketch → dedup (M5) → entities → conflict (M4).

Every outcome flows through the write door as events — this store never
touches tables directly:

- duplicate      → MERGE audit event + WRITE of the reinforced kept record,
- same-key facts → CONFLICT audit event + verdict-dependent WRITEs
                   (UPDATE closes the old fact's validity bi-temporally),
- otherwise      → plain WRITE (backfill gets a closed validity interval).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar

from datasketch import MinHashLSH

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.policies.conflict import ConflictPolicy, ConflictVerdict
from memspine.core.policies.dedup import DedupPolicy
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.memories.base import BaseMemory
from memspine.memories.semantic.entities import EntityExtractor
from memspine.observability.logging import EVENT_CONFLICT, EVENT_MERGE, get_logger
from memspine.services.embedding.base import EmbeddingService
from memspine.services.storage.base import StorageService

__all__ = ["SemanticMemory", "SemanticWriteResult"]

_log = get_logger(__name__)

AppendEvent = Callable[[MemoryEvent], Awaitable[None]]


@dataclass(frozen=True)
class SemanticWriteResult:
    record: MemoryRecord  # the surviving record (kept original on merge/reject)
    action: str  # "added" | "merged" | "updated" | "rejected"


def _cosine(u: list[float], v: list[float]) -> float:
    if len(u) != len(v):
        return 0.0
    return sum(a * b for a, b in zip(u, v, strict=True))


class SemanticMemory(BaseMemory):
    name: ClassVar[str] = "semantic"

    def __init__(
        self,
        storage: StorageService,
        embedder: EmbeddingService,
        append_event: AppendEvent,
        conflict: ConflictPolicy,
        dedup: DedupPolicy,
        extractor: EntityExtractor | None = None,
    ) -> None:
        self._storage = storage
        self._embedder = embedder
        self._append_event = append_event
        self._conflict = conflict
        self._dedup = dedup
        self._extractor = extractor
        # Stage-1 LSH index per namespace, built lazily from stored signatures.
        self._indexes: dict[str, MinHashLSH] = {}

    # ── index maintenance ────────────────────────────────────────────────────

    async def _index_for(self, namespace: str) -> MinHashLSH:
        index = self._indexes.get(namespace)
        if index is None:
            index = self._dedup.new_index()
            for record in await self._storage.list_records(namespace, "semantic"):
                if record.status is not RecordStatus.DELETED:
                    self._dedup.index_add(index, record)
            self._indexes[namespace] = index
        return index

    def invalidate_index(self, namespace: str | None = None) -> None:
        """Drop cached LSH state (after rebuild/forget sweeps)."""
        if namespace is None:
            self._indexes.clear()
        else:
            self._indexes.pop(namespace, None)

    # ── the write pipeline ───────────────────────────────────────────────────

    async def write(self, record: MemoryRecord) -> SemanticWriteResult:
        record = self._dedup.annotate(record)  # sketches (D-27) before the door
        index = await self._index_for(record.namespace)

        # M5 stage-1: LSH candidates; stage-2: cosine confirm via embeddings.
        duplicate = await self._confirm_duplicate(index, record)
        if duplicate is not None:
            return await self._merge(duplicate, record)

        # M13.3: entity resolution BEFORE conflict detection. The prompt that
        # produced the keying lands in provenance (D-43/E1 audit trail).
        if self._extractor is not None and record.entity is None:
            facts = await self._extractor.extract(record.content)
            if facts:
                primary = facts[0]
                updates: dict[str, object] = {
                    "entity": primary.entity,
                    "attribute": primary.attribute,
                }
                prompt_version = getattr(self._extractor, "prompt_version", None)
                if prompt_version is not None:
                    updates["source"] = record.source.model_copy(
                        update={"prompt_version": prompt_version}
                    )
                record = record.model_copy(update=updates)

        # M4: conflict ladder over the active fact with the same key.
        if record.entity is not None and record.attribute is not None:
            existing = await self._storage.find_active_fact(
                record.namespace, record.entity, record.attribute
            )
            if existing is not None:
                return await self._resolve_conflict(index, record, existing)

        await self._write_through(index, record)
        return SemanticWriteResult(record=record, action="added")

    # ── stages ───────────────────────────────────────────────────────────────

    async def _confirm_duplicate(
        self, index: MinHashLSH, record: MemoryRecord
    ) -> MemoryRecord | None:
        candidate_ids = self._dedup.candidates(index, record)
        if not candidate_ids:
            return None
        [incoming_vec] = await self._embedder.embed([record.content])
        for candidate_id in candidate_ids:
            candidate = await self._storage.get_record(candidate_id)
            if candidate is None or candidate.status is RecordStatus.DELETED:
                continue
            [candidate_vec] = await self._embedder.embed([candidate.content])
            if _cosine(incoming_vec, candidate_vec) >= self._dedup.cosine_threshold:
                return candidate
        return None

    async def _merge(self, kept: MemoryRecord, incoming: MemoryRecord) -> SemanticWriteResult:
        """Union-preserving merge (M5): reinforce the kept record, never lose
        governance state — consent tags union, PII tier maxes upward."""
        merged = kept.model_copy(
            update={
                "consent_tags": sorted(set(kept.consent_tags) | set(incoming.consent_tags)),
                "pii_tier": max(kept.pii_tier, incoming.pii_tier, key=_pii_rank),
                "scoring": kept.scoring.model_copy(
                    update={
                        "importance": min(1.0, kept.scoring.importance + 0.1),
                        "utility": min(1.0, kept.scoring.utility + 0.05),
                    }
                ),
            }
        )
        await self._append_event(
            MemoryEvent(
                kind=EventKind.MERGE,
                namespace=kept.namespace,
                actor="system",
                payload={
                    "kept_record_id": kept.record_id,
                    "dropped_fingerprint": incoming.content_fingerprint,
                    "reason": "two_stage_dedup",
                },
            )
        )
        await self._write_event(merged)
        _log.info(
            EVENT_MERGE,
            namespace=kept.namespace,
            kept=kept.record_id,
            dropped_fingerprint=incoming.content_fingerprint,
        )
        return SemanticWriteResult(record=merged, action="merged")

    async def _resolve_conflict(
        self, index: MinHashLSH, incoming: MemoryRecord, existing: MemoryRecord
    ) -> SemanticWriteResult:
        verdict = self._conflict.resolve(incoming, existing)
        await self._append_event(
            MemoryEvent(
                kind=EventKind.CONFLICT,
                namespace=incoming.namespace,
                actor="system",
                payload={
                    "verdict": verdict.value,
                    "incoming_record_id": incoming.record_id,
                    "existing_record_id": existing.record_id,
                    "fact_key": [incoming.entity, incoming.attribute],
                },
            )
        )
        _log.info(
            EVENT_CONFLICT,
            namespace=incoming.namespace,
            verdict=verdict.value,
            entity=incoming.entity,
            attribute=incoming.attribute,
        )
        if verdict is ConflictVerdict.NOOP:
            return SemanticWriteResult(record=existing, action="rejected")
        if verdict is ConflictVerdict.UPDATE:
            closed = existing.model_copy(
                update={
                    "valid_to": incoming.valid_from,
                    "superseded_at": incoming.recorded_at,
                    "status": RecordStatus.ARCHIVED,
                    "evolve_to": incoming.record_id,
                }
            )
            await self._write_event(closed)
            await self._write_through(index, incoming)
            return SemanticWriteResult(record=incoming, action="updated")
        # ADD — historical backfill closes its own validity at the current fact.
        backfilled = incoming
        if incoming.valid_from < existing.valid_from:
            backfilled = incoming.model_copy(update={"valid_to": existing.valid_from})
        await self._write_through(index, backfilled)
        return SemanticWriteResult(record=backfilled, action="added")

    # ── event emission ───────────────────────────────────────────────────────

    async def _write_through(self, index: MinHashLSH, record: MemoryRecord) -> None:
        await self._write_event(record)
        self._dedup.index_add(index, record)

    async def _write_event(self, record: MemoryRecord) -> None:
        await self._append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace=record.namespace,
                actor=record.source.role,
                payload={"record": record.model_dump(mode="json")},
            )
        )


_PII_ORDER = {"none": 0, "low": 1, "high": 2, "regulated": 3}


def _pii_rank(tier: object) -> int:
    return _PII_ORDER.get(getattr(tier, "value", str(tier)), 0)
