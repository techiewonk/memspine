"""Bounded A-MEM evolution (D-42/ADR-015): auto-link a fresh write to its
vector neighbourhood.

Deterministic in v0.1 — no LLM proposes links (that is a later opt-in): a
neighbour qualifies purely on vector similarity above the floor, and the
proposal count is bounded twice (per-write cap AND the node's remaining link
budget). Best-effort by contract: a neighbour that fails a gate is skipped,
never an error — but every proposed link rides the log as a LINK event, so
rebuilds reproduce evolution exactly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from memspine.config.constants import (
    EVOLUTION_LINK_MIN_SIMILARITY,
    EVOLUTION_MAX_LINKS_PER_WRITE,
    LINK_BUDGET,
)
from memspine.core.events import MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.memories.associative.links import link_event, live_links
from memspine.services.graph.base import GraphStore
from memspine.services.vector.base import VectorHit

__all__ = ["propose_links"]

AppendEvent = Callable[[MemoryEvent], Awaitable[None]]


class EvolutionStore(Protocol):
    """The storage slice evolution needs (ports & adapters, D-22)."""

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...


async def propose_links(
    *,
    namespace: str,
    record: MemoryRecord,
    hits: list[VectorHit],
    storage: EvolutionStore,
    graph: GraphStore,
    append_event: AppendEvent,
    min_similarity: float = EVOLUTION_LINK_MIN_SIMILARITY,
    max_links: int = EVOLUTION_MAX_LINKS_PER_WRITE,
    budget: int = LINK_BUDGET,
) -> int:
    """Emit LINK events for qualifying neighbours; returns how many.

    Gates (each skips, never raises): the record itself, an already-linked
    partner, a neighbour outside ``namespace``, a non-ACTIVATED or quarantined
    neighbour (E1: poison never gains graph reach through evolution), a
    neighbour at its own budget, and any hit below the similarity floor.
    """
    src_edges = live_links(await graph.edges_of(record.record_id))
    partners = {edge.dst if edge.src == record.record_id else edge.src for edge in src_edges}
    created = 0
    for hit in sorted(hits, key=lambda h: (-h.score, h.record_id)):
        if created >= max_links or len(src_edges) + created >= budget:
            break
        if hit.score < min_similarity:
            break  # sorted desc: nothing further clears the floor
        if hit.record_id == record.record_id or hit.record_id in partners:
            continue
        neighbour = await storage.get_record(hit.record_id)
        if (
            neighbour is None
            or neighbour.namespace != namespace
            or neighbour.status is not RecordStatus.ACTIVATED
            or neighbour.quarantined
        ):
            continue
        if len(live_links(await graph.edges_of(hit.record_id))) >= budget:
            continue  # the neighbour is full — best-effort, not an error
        await append_event(
            link_event(
                namespace,
                record.record_id,
                hit.record_id,
                "related",
                weight=round(hit.score, 4),
                reason="evolution",
                actor="system",
            )
        )
        partners.add(hit.record_id)
        created += 1
    return created
