"""Reflective memory store (M13.7): insights derived from episodic experience.

A reflection is an ordinary record (``memory_type="reflective"``) whose WRITE
event names its source records (``reflection.member_record_ids``) — the same
derivation-provenance pattern consolidation uses (P3.1), so ``audit taint``
(E1) propagates through reflections with no special casing. Depth rides
``reflection_depth`` and is capped by ``guards`` (0 raw → 1 reflection →
2 meta, terminal).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import ClassVar, Protocol

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.exceptions import ConflictError
from memspine.memories.base import BaseMemory
from memspine.memories.reflective.guards import reflection_depth_for
from memspine.observability.logging import get_logger

__all__ = ["ReflectiveMemory"]

_log = get_logger(__name__)

AppendEvent = Callable[[MemoryEvent], Awaitable[None]]
#: Firewall gate the engine injects (E1) — same seam as resource ingest;
#: async so the full anomaly context (vector neighbours) participates.
AssessRecord = Callable[[MemoryRecord], Awaitable[MemoryRecord]]
#: MTI-D cap the engine injects (R2-5): parent view trusts -> deposited trust.
CapTrust = Callable[[float, Sequence[float]], float]


class ReflectiveStore(Protocol):
    """The storage slice reflective needs (ports & adapters, D-22)."""

    async def list_records(
        self, namespace: str, memory_type: str | None = None
    ) -> list[MemoryRecord]: ...

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...


class ReflectiveMemory(BaseMemory):
    name: ClassVar[str] = "reflective"
    needs: ClassVar[tuple[str, ...]] = ("episodic",)

    def __init__(
        self,
        storage: ReflectiveStore,
        append_event: AppendEvent,
        assess: AssessRecord | None = None,
    ) -> None:
        self._storage = storage
        self._append_event = append_event
        self._assess = assess

    async def reflect(
        self,
        namespace: str,
        content: str,
        source_record_ids: list[str],
        source: SourceInfo | None = None,
        parent_views: Sequence[float] | None = None,
        cap_trust: CapTrust | None = None,
    ) -> MemoryRecord:
        """Write one reflection derived from ``source_record_ids``.

        The guards run against the *fetched* parents (never caller-supplied
        depths), so the cap and the quarantine-laundering refusal cannot be
        bypassed by lying about a parent.

        R2-5: the evidence ids are also the record's ``source.parents``, so the
        integrity cap, ``effective_trust`` (live re-evaluation) and the
        cross-namespace taint walk all see the lineage. With integrity on the
        engine passes ``parent_views`` and ``cap_trust`` (MTI-D).
        """
        parents: list[MemoryRecord] = []
        for record_id in source_record_ids:
            parent = await self._storage.get_record(record_id)
            # Namespace isolation: parents must live in the target namespace —
            # a reflection must never span tenants (taint would cross the
            # boundary too). One error message for missing AND foreign so this
            # is not a cross-namespace existence oracle.
            if parent is None or parent.namespace != namespace:
                raise ConflictError(
                    f"reflection source {record_id!r} does not exist in {namespace!r}"
                )
            parents.append(parent)
        depth = reflection_depth_for(parents)
        # role="assistant", never "system": reflection content is
        # LLM/caller-authored free text — the privileged-role firewall
        # exemptions must not apply to it (E1).
        base = source or SourceInfo(role="assistant", channel="reflection")
        lineage = list(dict.fromkeys([*base.parents, *source_record_ids]))
        # R2-11: an insight drawn only from assistant claims stays one.
        claim = all("assistant_claim" in parent.tags for parent in parents)
        record = MemoryRecord(
            namespace=namespace,
            memory_type="reflective",
            content=content,
            reflection_depth=depth,
            source=base.model_copy(update={"parents": lineage}),
            tags=["assistant_claim"] if claim else [],
        )
        if self._assess is not None:
            record = await self._assess(record)  # E1: reflections pass the gate too
        # Derived content is never more trusted than its least-trusted parent —
        # same rule as consolidation summaries (D-47 §5): no trust laundering
        # through a low-trust (but unquarantined) source.
        floor = min(parent.trust for parent in parents)
        if record.trust > floor:
            record = record.model_copy(update={"trust": floor})
        if parent_views and cap_trust is not None:
            record = record.model_copy(update={"trust": cap_trust(record.trust, parent_views)})
        await self._append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace=namespace,
                actor=record.source.role,
                payload={
                    "record": record.model_dump(mode="json"),
                    # Derivation provenance (E1/P3.1): taint flows through here.
                    "reflection": {"member_record_ids": list(source_record_ids)},
                },
            )
        )
        log = _log.warning if record.quarantined else _log.info
        log(
            "memory.quarantined" if record.quarantined else "memory.write",
            namespace=namespace,
            record_id=record.record_id,
            reflection_depth=depth,
            sources=len(parents),
        )
        return record

    async def reflections(self, namespace: str, *, depth: int | None = None) -> list[MemoryRecord]:
        """Stored reflections, optionally filtered to one depth level."""
        records = await self._storage.list_records(namespace, "reflective")
        if depth is not None:
            records = [record for record in records if record.reflection_depth == depth]
        return records
