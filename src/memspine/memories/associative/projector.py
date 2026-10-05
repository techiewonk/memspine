"""Graph projector (D0.1/ADR-015): the association graph as a projection.

Projected kinds:

- ``WRITE`` — one node per record (labels: ``[memory_type]``, properties kept
  minimal: ``namespace``; also the node's ``namespace`` column, KB-1). WRITE
  payloads carrying derivation provenance (``consolidation``/``reflection``
  member ids, P3.1/M13.7) also project ``derived_from`` edges from the derived
  record to each member,
- ``LINK`` — one edge per event (``rel``/``weight``/``reason`` ride as edge
  properties; ``kind`` too when the payload carries one), stored under the
  event's namespace so PPR/BFS/Leiden stay inside one tenant (KB-1). A
  ``weight: 0.0`` LINK is the budget-prune tombstone (ADR-015): the edge is
  upserted inert, and every reader treats weight ``<= 0`` as gone,
- ``FORGET`` — ``delete_node`` cascades every touching edge (M7).

GP-2 (``memories.associative.policies.entity_nodes``, off by default): a WRITE
also projects one ``ent:<namespace>:<canonical>`` node per entity the record
names and a ``mentions`` edge record -> entity weighted by the record's trust
(:mod:`memspine.memories.associative.entities`). A re-projected record whose
names changed tombstones its stale mentions; a FORGET removes the record's
mentions with its node, and an entity left with no live mention is removed.
Everything is derived from the event alone, so rebuild == incremental.

Idempotency: every operation is an upsert or an idempotent delete, so catch-up
re-delivery and full rebuilds are safe. Known limit (documented, ADR-015): a
DECAY_TRANSITION that changes ``memory_type`` (working→episodic page-out) does
not relabel the node — labels are advisory, never a retrieval gate.
"""

from __future__ import annotations

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.projector import Projector
from memspine.core.records import MemoryRecord
from memspine.memories.associative.entities import (
    ENTITY_LABEL,
    MENTIONS_REL,
    EntityPolicy,
    entity_node_id,
    is_entity_node,
    record_entity_names,
)
from memspine.observability.logging import get_logger
from memspine.services.graph.base import GraphStore

__all__ = ["GraphProjector"]

_log = get_logger(__name__)

#: WRITE-payload keys that carry derivation provenance (audit-traced too).
_DERIVATION_KEYS = ("consolidation", "reflection")


class GraphProjector(Projector):
    name = "graph"

    def __init__(self, store: GraphStore, entities: EntityPolicy | None = None) -> None:
        self._store = store
        self._entities = entities

    async def apply(self, event: MemoryEvent) -> None:
        if event.kind is EventKind.WRITE:
            record = MemoryRecord.model_validate(event.payload["record"])
            await self._store.upsert_node(
                record.record_id,
                labels=[record.memory_type],
                properties={"namespace": record.namespace},
                namespace=record.namespace,
            )
            if self._entities is not None:
                await self._project_mentions(record, self._entities)
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
                        namespace=record.namespace,
                    )
        elif event.kind is EventKind.LINK:
            payload = event.payload
            properties: dict[str, object] = {
                "weight": float(payload.get("weight", 1.0)),
                "reason": str(payload.get("reason", "")),
            }
            kind = payload.get("kind")
            if isinstance(kind, str) and kind:
                properties["kind"] = kind
            await self._store.upsert_edge(
                str(payload["src"]),
                str(payload["dst"]),
                str(payload.get("rel", "related")),
                properties,
                namespace=event.namespace,
            )
        elif event.kind is EventKind.FORGET:
            # A forgotten memory must stop being reachable (M7): the node and
            # every touching edge go, both soft and hard forget.
            record_id = str(event.payload["record_id"])
            mentioned = await self._mentioned(record_id) if self._entities is not None else []
            await self._store.delete_node(record_id)
            await self._drop_orphans(mentioned)

    async def _mentioned(self, record_id: str, live_only: bool = False) -> list[str]:
        """The entity nodes ``record_id``'s ``mentions`` edges point at."""
        return [
            edge.dst
            for edge in await self._store.edges_of(record_id)
            if edge.rel_type == MENTIONS_REL
            and edge.src == record_id
            and is_entity_node(edge.dst)
            and (edge.weight > 0 or not live_only)
        ]

    async def _project_mentions(self, record: MemoryRecord, policy: EntityPolicy) -> None:
        """GP-2: entity nodes + ``mentions`` edges for one WRITE (idempotent)."""
        weight = max(0.0, min(1.0, record.trust))
        wanted = (
            [entity_node_id(record.namespace, name) for name in record_entity_names(record, policy)]
            if weight > 0
            else []
        )
        stale = [
            node
            for node in await self._mentioned(record.record_id, live_only=True)
            if node not in wanted
        ]
        for node_id in wanted:
            await self._store.upsert_node(
                node_id,
                labels=[ENTITY_LABEL],
                properties={"namespace": record.namespace},
                namespace=record.namespace,
            )
            await self._store.upsert_edge(
                record.record_id,
                node_id,
                MENTIONS_REL,
                {"weight": weight, "reason": "entity"},
                namespace=record.namespace,
            )
        for node_id in stale:
            # The port has no single-edge delete: a weight-0 tombstone (ADR-015).
            await self._store.upsert_edge(
                record.record_id,
                node_id,
                MENTIONS_REL,
                {"weight": 0.0, "reason": "entity"},
                namespace=record.namespace,
            )
        await self._drop_orphans(stale)

    async def _drop_orphans(self, entity_ids: list[str]) -> None:
        """Remove each entity node no live ``mentions`` edge points at any more."""
        for node_id in dict.fromkeys(entity_ids):
            edges = await self._store.edges_of(node_id)
            if not any(e.rel_type == MENTIONS_REL and e.weight > 0 for e in edges):
                await self._store.delete_node(node_id)

    async def reset(self) -> None:
        await self._store.clear()
