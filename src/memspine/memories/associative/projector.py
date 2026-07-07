"""Graph projector (D0.1/ADR-015): the association graph as a projection.

Projected kinds:

- ``WRITE`` — one node per record (labels: ``[memory_type]``, properties kept
  minimal: ``namespace``). WRITE payloads carrying derivation provenance
  (``consolidation``/``reflection`` member ids, P3.1/M13.7) also project
  ``derived_from`` edges from the derived record to each member,
- ``LINK`` — one edge per event (``rel``/``weight``/``reason`` ride as edge
  properties). A ``weight: 0.0`` LINK is the budget-prune tombstone (ADR-015):
  the edge is upserted inert, and every reader treats weight ``<= 0`` as gone,
- ``FORGET`` — ``delete_node`` cascades every touching edge (M7).

Idempotency: every operation is an upsert or an idempotent delete, so catch-up
re-delivery and full rebuilds are safe. Known limit (documented, ADR-015): a
DECAY_TRANSITION that changes ``memory_type`` (working→episodic page-out) does
not relabel the node — labels are advisory, never a retrieval gate.
"""

from __future__ import annotations

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.projector import Projector
from memspine.core.records import MemoryRecord
from memspine.observability.logging import get_logger
from memspine.services.graph.base import GraphStore

__all__ = ["GraphProjector"]

_log = get_logger(__name__)

#: WRITE-payload keys that carry derivation provenance (audit-traced too).
_DERIVATION_KEYS = ("consolidation", "reflection")


class GraphProjector(Projector):
    name = "graph"

    def __init__(self, store: GraphStore) -> None:
        self._store = store

    async def apply(self, event: MemoryEvent) -> None:
        if event.kind is EventKind.WRITE:
            record = MemoryRecord.model_validate(event.payload["record"])
            await self._store.upsert_node(
                record.record_id,
                labels=[record.memory_type],
                properties={"namespace": record.namespace},
            )
            for key in _DERIVATION_KEYS:
                derivation = event.payload.get(key)
                if derivation is None:
                    continue
                if not isinstance(derivation, dict):
                    # Malformed provenance must be loud (D-18): a silent skip
                    # here drops derived_from edges from the projection forever.
                    _log.warning(
                        "graph_projector.malformed_derivation",
                        seq=event.seq,
                        key=key,
                        got=type(derivation).__name__,
                    )
                    continue
                members = derivation.get("member_record_ids")
                if not isinstance(members, list):
                    _log.warning(
                        "graph_projector.malformed_member_record_ids",
                        seq=event.seq,
                        key=key,
                        got=type(members).__name__,
                    )
                    continue
                for member in members:
                    await self._store.upsert_edge(
                        record.record_id,
                        str(member),
                        "derived_from",
                        {"weight": 1.0, "reason": key},
                    )
        elif event.kind is EventKind.LINK:
            payload = event.payload
            await self._store.upsert_edge(
                str(payload["src"]),
                str(payload["dst"]),
                str(payload.get("rel", "related")),
                {
                    "weight": float(payload.get("weight", 1.0)),
                    "reason": str(payload.get("reason", "")),
                },
            )
        elif event.kind is EventKind.FORGET:
            # A forgotten memory must stop being reachable (M7): the node and
            # every touching edge go, both soft and hard forget.
            await self._store.delete_node(str(event.payload["record_id"]))

    async def reset(self) -> None:
        await self._store.clear()
