"""Link budget (M13.6, bounded A-MEM / D-42): per-node association bound.

The budget is enforced at link-*creation* time in the memory layer, never in
the projector — replay must reproduce exactly the links the log records, so a
projector that second-guessed the budget would make rebuilds order-sensitive
(ADR-015). Over-budget links are refused with :class:`ConflictError`; callers
free a slot with :func:`prune_weakest`, which emits a compensating LINK event
with ``weight: 0.0`` (the tombstone the projector upserts inert — the port has
no single-edge delete, and a tombstone replays deterministically).

Provenance edges (``derived_from``) are exempt: they record derivation facts
(consolidation/reflection membership), not associations, and are neither
counted against the budget nor eligible for pruning.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from memspine.config.constants import LINK_BUDGET
from memspine.core.events import EventKind, MemoryEvent
from memspine.exceptions import ConflictError
from memspine.services.graph.base import GraphEdge, GraphStore

__all__ = ["LINK_BUDGET", "assert_within_budget", "link_event", "live_links", "prune_weakest"]

AppendEvent = Callable[[MemoryEvent], Awaitable[None]]

#: Relation types that are provenance, not association — budget-exempt.
_PROVENANCE_RELS = frozenset({"derived_from"})


def live_links(edges: list[GraphEdge]) -> list[GraphEdge]:
    """The edges that count against the budget: positive weight (a ``0.0``
    weight is the prune tombstone) and not provenance."""
    return [edge for edge in edges if edge.weight > 0.0 and edge.rel_type not in _PROVENANCE_RELS]


def link_event(
    namespace: str,
    src: str,
    dst: str,
    rel: str,
    weight: float,
    reason: str,
    actor: str = "user",
) -> MemoryEvent:
    """The canonical LINK event shape (ADR-015). ``rel``/``reason`` are short
    slugs stored as edge properties — they never enter a context window, so
    the firewall's content gate does not apply to them (documented, ADR-015)."""
    return MemoryEvent(
        kind=EventKind.LINK,
        namespace=namespace,
        actor=actor,
        payload={"src": src, "dst": dst, "rel": rel, "weight": weight, "reason": reason},
    )


async def assert_within_budget(graph: GraphStore, node_id: str, budget: int = LINK_BUDGET) -> None:
    """Refuse (loudly) a link that would push ``node_id`` past its budget."""
    count = len(live_links(await graph.edges_of(node_id)))
    if count >= budget:
        raise ConflictError(
            f"record {node_id} already carries {count} associative links "
            f"(budget {budget}, M13.6) — prune_weakest() frees a slot"
        )


async def prune_weakest(
    graph: GraphStore,
    append_event: AppendEvent,
    namespace: str,
    node_id: str,
) -> GraphEdge | None:
    """Retire the node's weakest live link via a ``weight: 0.0`` LINK event.

    Deterministic: ties break on ``(weight, src, dst, rel_type)`` so the same
    log always prunes the same edge on replay. Returns the pruned edge, or
    ``None`` when the node has no live links.
    """
    live = live_links(await graph.edges_of(node_id))
    if not live:
        return None
    weakest = min(live, key=lambda edge: (edge.weight, edge.src, edge.dst, edge.rel_type))
    await append_event(
        link_event(
            namespace,
            weakest.src,
            weakest.dst,
            weakest.rel_type,
            weight=0.0,
            reason="budget_prune",
            actor="system",
        )
    )
    return weakest
