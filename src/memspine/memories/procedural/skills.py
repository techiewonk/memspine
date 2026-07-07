"""Procedural memory store (M13.4): skills and plans as staged, versioned records.

A procedural record's identity is its ``entity`` (skill/task name) and its
``attribute`` names the subtype: ``"skill"`` (hand-authored how-to), ``"plan"``
(E6: a validated multi-step plan captured on task success), or ``"prompt"``
(a prompt-as-memory version, see ``prompt_registry``).

The stage ladder (``lifecycle``) maps onto the record lifecycle so existing
gates need no new machinery: draft/staged/verified ride ``RESOLVING`` (held
out of retrieval exactly like E1 quarantine holds a suspect write), ``active``
is ``ACTIVATED``, ``deprecated`` is ``ARCHIVED``. Promotions are *delta*
lifecycle events through the write door — never full snapshots (P3.1 rule).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import ClassVar, Protocol

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus, SourceInfo
from memspine.exceptions import ConflictError
from memspine.memories.base import BaseMemory
from memspine.memories.procedural.lifecycle import SkillStage, is_usable, next_stage
from memspine.observability.logging import get_logger

__all__ = ["SKILL_KINDS", "ProceduralMemory", "make_skill_record", "stage_status"]

_log = get_logger(__name__)

AppendEvent = Callable[[MemoryEvent], Awaitable[None]]

#: Procedural subtypes carried in ``attribute`` (M13.4 + E6).
SKILL_KINDS: tuple[str, ...] = ("skill", "plan", "prompt")


class ProceduralStore(Protocol):
    """The storage slice procedural needs (ports & adapters, D-22)."""

    async def list_records(
        self, namespace: str, memory_type: str | None = None
    ) -> list[MemoryRecord]: ...

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...


def stage_status(stage: SkillStage) -> RecordStatus:
    """Where a stage sits in the record lifecycle: only ACTIVE skills surface
    as current truth; everything pre-active is held like quarantine (E1)."""
    if stage is SkillStage.ACTIVE:
        return RecordStatus.ACTIVATED
    if stage is SkillStage.DEPRECATED:
        return RecordStatus.ARCHIVED
    return RecordStatus.RESOLVING


def make_skill_record(
    namespace: str,
    name: str,
    content: str,
    *,
    kind: str = "skill",
    source: SourceInfo | None = None,
) -> MemoryRecord:
    """A new procedural record. Skills enter at ``draft``; plans enter at
    ``staged`` (E6 — a plan is only captured after a task *succeeded*, which
    is itself the first validation step)."""
    if kind not in SKILL_KINDS:
        raise ConflictError(f"unknown procedural kind {kind!r} (valid: {', '.join(SKILL_KINDS)})")
    stage = SkillStage.STAGED if kind == "plan" else SkillStage.DRAFT
    return MemoryRecord(
        namespace=namespace,
        memory_type="procedural",
        content=content,
        entity=name,
        attribute=kind,
        skill_stage=stage.value,
        status=stage_status(stage),
        source=source or SourceInfo(role="user", channel="internal"),
    )


class ProceduralMemory(BaseMemory):
    name: ClassVar[str] = "procedural"

    def __init__(self, storage: ProceduralStore, append_event: AppendEvent) -> None:
        self._storage = storage
        self._append_event = append_event

    async def list(
        self,
        namespace: str,
        *,
        kind: str | None = None,
        usable_only: bool = False,
    ) -> list[MemoryRecord]:
        """Procedural records; ``usable_only`` applies the M13.4 execution gate
        (ACTIVE stage, ACTIVATED status, never quarantined)."""
        records = await self._storage.list_records(namespace, "procedural")
        if kind is not None:
            records = [record for record in records if record.attribute == kind]
        if usable_only:
            records = [record for record in records if self.usable(record)]
        return records

    @staticmethod
    def usable(record: MemoryRecord) -> bool:
        return (
            is_usable(record.skill_stage)
            and record.status is RecordStatus.ACTIVATED
            and not record.quarantined
        )

    async def promote(self, record_id: str, *, dry_run_passed: bool = False) -> MemoryRecord:
        """One legal step up the ladder, as a delta event through the door.

        A quarantined record can never be promoted here: corroboration (E1)
        is the only exit from quarantine — otherwise a poisoned skill could be
        walked into ``active`` by the same channel that wrote it.
        """
        record = await self._require_procedural(record_id)
        if record.quarantined:
            raise ConflictError(
                f"record {record_id} is quarantined (E1) — corroboration, not promotion, "
                "is the only path out"
            )
        current = self._stage_of(record)
        target = next_stage(current, dry_run_passed=dry_run_passed)
        return await self._transition(record, current, target, reason="skill_promotion")

    async def deprecate(self, record_id: str) -> MemoryRecord:
        """Retire a skill from any live stage; terminal (M13.4)."""
        record = await self._require_procedural(record_id)
        current = self._stage_of(record)
        if current is SkillStage.DEPRECATED:
            return record  # idempotent: already terminal
        return await self._transition(
            record, current, SkillStage.DEPRECATED, reason="skill_deprecated"
        )

    async def _transition(
        self,
        record: MemoryRecord,
        current: SkillStage,
        target: SkillStage,
        *,
        reason: str,
    ) -> MemoryRecord:
        change: dict[str, object] = {
            "skill_stage": target.value,
            "status": stage_status(target).value,
        }
        if target is SkillStage.DEPRECATED:
            change["valid_to"] = datetime.now(UTC).isoformat()
        await self._append_event(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=record.namespace,
                actor="system",
                payload={
                    "record_id": record.record_id,
                    "set": change,
                    "transition": f"skill:{current.value}->{target.value}",
                    "reason": reason,
                },
            )
        )
        _log.info(
            "memory.skill_transition",
            namespace=record.namespace,
            record_id=record.record_id,
            transition=f"{current.value}->{target.value}",
        )
        updated = record.model_copy(
            update={
                "skill_stage": target.value,
                "status": stage_status(target),
                **(
                    {"valid_to": datetime.fromisoformat(str(change["valid_to"]))}
                    if "valid_to" in change
                    else {}
                ),
            }
        )
        return updated

    async def _require_procedural(self, record_id: str) -> MemoryRecord:
        record = await self._storage.get_record(record_id)
        if record is None:
            raise ConflictError(f"no such record {record_id!r}")
        if record.memory_type != "procedural":
            raise ConflictError(
                f"record {record_id} is {record.memory_type!r}, not procedural — "
                "only skills/plans ride the stage ladder (M13.4)"
            )
        return record

    @staticmethod
    def _stage_of(record: MemoryRecord) -> SkillStage:
        if record.skill_stage is None:
            raise ConflictError(
                f"record {record.record_id} has no skill_stage — it never entered "
                "the ladder (write it via add_skill/record_plan)"
            )
        return SkillStage(record.skill_stage)
