"""Neo4j graph projection through the outbox (F-16e, M-04 / M-18, owner Q4).

Flag ``graph.neo4j_outbox``.
Two halves, joined only by the durable outbox (stage :data:`GRAPH_STAGE`):

``Neo4jOutboxProjector``  a normal event-log ``Projector``. For every graph-relevant event (WRITE
                          nodes, LINK edges and tombstones, FORGET) it appends one outbox item
                          holding the event, and returns. The outbox row is the truth, so a crash
                          loses nothing; the projector's high-water mark moves once the row exists.
``Neo4jGraphHandler``     the registered stage handler. It applies an item to a Neo4j ``GraphStore``
                          by running the SAME ``GraphProjector`` the embedded graph uses, so
                          the two graphs are built by one set of rules, and every call is an
                          idempotent MERGE or delete. The runner's watermark and holes (F-04) give
                          resume-after-crash and per-item retry; re-applying an item is harmless.

Retire / erasure. A FORGET removes the record node, its edges and any entity node left with no
live mention (``GraphProjector``). A compiled-edge tombstone (LINK weight 0, reason
``<compiled>:<why>``) also prunes the compiled assertion / value / time nodes no live edge touches
any more, which is what ``LinkProjection.retract_records`` does on the embedded graph.

Rebuild. ``reset()`` enqueues a ``clear`` and the engine's normal replay re-feeds every event;
:func:`rebuild_from_log` does the same straight into a store without the queue.

The embedded graph store is untouched and stays the read path; Neo4j is a server-profile mirror.
No credentials are handled here; the executor is built by the caller.
"""

from __future__ import annotations

from typing import Any, Protocol

from memspine.memories.associative.entities import EntityPolicy
from memspine.memories.associative.projector import GraphProjector
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.schemas.durable.common import OwnerScope
from memspine.services.graph.base import GraphStore
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.projections.projector import Projector

__all__ = [
    "GRAPH_SCOPE",
    "GRAPH_STAGE",
    "Neo4jGraphHandler",
    "Neo4jOutboxProjector",
    "rebuild_from_log",
]

_log = get_logger(__name__)

#: The outbox stage (and read-barrier capability) of the graph projection.
GRAPH_STAGE = "graph_projection"
#: One ordered stream for the whole graph: a node delete names no namespace, so ordering across
#: namespaces is kept by using a single (scope, stage) offset sequence and one watermark.
GRAPH_SCOPE = OwnerScope(tenant="default", owner="graph-neo4j")

_KINDS = frozenset({EventKind.WRITE, EventKind.LINK, EventKind.FORGET})


class _Outbox(Protocol):
    def register(self, stage: str, *, prepare: Any, commit: Any) -> None: ...

    async def enqueue(self, scope: OwnerScope, stage: str, payload: dict[str, Any]) -> Any: ...

    async def drain(self, scope: OwnerScope | None = None) -> Any: ...


class Neo4jOutboxProjector(Projector):
    """Event-log projector that queues graph work for Neo4j instead of doing it."""

    name = "graph_neo4j"

    def __init__(self, outbox: _Outbox, *, drain: bool = True) -> None:
        self._outbox = outbox
        self._drain = drain
        self._batch = False
        self._dirty = False

    async def apply(self, event: MemoryEvent) -> None:
        if event.kind not in _KINDS:
            return
        payload = {"op": "event", "event": event.model_dump(mode="json")}
        handle = await self._outbox.enqueue(GRAPH_SCOPE, GRAPH_STAGE, payload)
        tap.event(
            "graph.neo4j.enqueue",
            kind=event.kind.value,
            seq=event.seq,
            namespace=event.namespace,
            job_id=getattr(handle, "job_id", None),
        )
        self._dirty = True
        if not self._batch:
            await self.flush()

    async def reset(self) -> None:
        await self._outbox.enqueue(GRAPH_SCOPE, GRAPH_STAGE, {"op": "clear"})
        tap.event("graph.neo4j.enqueue", kind="clear")
        self._dirty = True

    def begin_batch(self) -> None:
        self._batch = True

    async def flush(self) -> None:
        self._batch = False
        if self._drain and self._dirty:
            self._dirty = False
            await self._outbox.drain(GRAPH_SCOPE)


class Neo4jGraphHandler:
    """Applies outbox items to a Neo4j ``GraphStore``. Idempotent, safe to re-apply."""

    def __init__(
        self,
        store: GraphStore,
        entities: EntityPolicy | None = None,
        *,
        compiled: str = "",
    ) -> None:
        self._store = store
        self._projector = GraphProjector(store, entities)
        #: marker the edge compiler stamps on its nodes / edges (``edge_compiler_v2``)
        self._compiled = compiled

    def register(self, outbox: _Outbox) -> None:
        outbox.register(GRAPH_STAGE, prepare=self.prepare, commit=self.commit)

    async def prepare(self, scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        return payload  # nothing slow: the item already carries the event

    async def commit(self, scope: OwnerScope, offset: int, prepared: Any) -> None:
        await self.apply(prepared)

    async def apply(self, payload: dict[str, Any]) -> None:
        op = payload.get("op")
        if op == "clear":
            await self._store.clear()
            tap.event("graph.neo4j.apply", op="clear")
            return
        if op != "event":
            raise ValueError(f"unknown graph_projection op {op!r}")
        event = MemoryEvent.model_validate(payload["event"])
        await self._projector.apply(event)
        pruned = await self._prune(event)
        tap.event(
            "graph.neo4j.apply",
            op="event",
            kind=event.kind.value,
            seq=event.seq,
            namespace=event.namespace,
            pruned=pruned,
        )

    async def _prune(self, event: MemoryEvent) -> bool:
        """A compiled-edge tombstone takes the compiled nodes nobody else uses with it."""
        if event.kind is not EventKind.LINK or not self._compiled:
            return False
        p = event.payload
        reason = str(p.get("reason", ""))
        if float(p.get("weight", 1.0)) > 0 or not reason.startswith(f"{self._compiled}:"):
            return False
        prune = getattr(self._store, "prune_dead", None)
        if prune is None:
            return False
        await prune([str(p["src"]), str(p["dst"])], self._compiled)
        return True


async def rebuild_from_log(storage: Any, handler: Neo4jGraphHandler, *, batch: int = 1000) -> int:
    """Replay the whole event log into the handler's store (clear first), without the queue.

    The same code path as a queued item, so the result equals the incrementally fed graph."""
    await handler.apply({"op": "clear"})
    after, count = 0, 0
    while True:
        events = await storage.read_events(after_seq=after, limit=batch)
        if not events:
            break
        for event in events:
            if event.kind in _KINDS:
                await handler.apply({"op": "event", "event": event.model_dump(mode="json")})
                count += 1
            assert event.seq is not None
            after = event.seq
    tap.event("graph.neo4j.rebuild", events=count)
    return count
