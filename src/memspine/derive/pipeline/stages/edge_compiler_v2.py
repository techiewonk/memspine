"""Derive stage: compile_edges (F-02c, ``graph.edge_compiler_v2``): assertions -> typed graph edges.

The wiring of the edge compiler (LIB-35, ``memspine.derive.edges``) and the link projection. Off by
default; the stage then reports ``skipped`` and the sleep cycle does not schedule it.

* **EdgeCompiler** (pure): a live or superseded semantic fact becomes an assertion envelope (F-02a
  ``store.envelope.to_envelope``, which already carries the F-02b tri-temporal fields: ``source_at``,
  ``event_precision``, ``event_basis``), enriched in code from the record tags an ``extract_graph`` edge
  fact carries (``rel:``, ``dst:``, ``kind:``): subject and object mentions are resolved to the existing
  entity-node ids (``ent:<namespace>:<canonical>``, the entity policy applied), and the scope is the typed
  tenant + owner when ``isolation.scope_keys_v2`` is on (M-05/M-16: entity scope, the session is never part
  of a key). The library then emits an assertion node, role edges and one binary projection edge per
  fact (n-ary roles, A-B-A occurrence keys). Tentative rule (S5-3): hypothetical / planned / reported /
  question content, assistant-proposed content that no one confirmed, a partly bound edge (the object
  was rejected as an entity) and an unbound edge are tagged ``tentative``; default reads exclude them.
* **LinkProjection**: writes the delta as LINK events (replayable, ``payload["props"]`` carries the
  typed edge properties, ``src_node`` / ``dst_node`` the labels of the nodes this layer owns), only for
  edges whose stored form differs (idempotent re-projection appends nothing). A record that is gone,
  hidden, quarantined or archived without a successor loses its edges (tombstone LINK events, then the
  assertion node and the value / time nodes nobody else uses are deleted); a superseded record keeps
  closed edges (``valid_to``). :meth:`LinkProjection.retract_records` is the erasure hook (lineage: edges
  carry the source ``record_id``).

Edge properties hold ids, times, flags and trust only; never record text.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from memspine.derive.edges import GraphDelta, GraphEdge, GraphNode, compile_edges
from memspine.derive.fact_policy import FactPurpose
from memspine.derive.pipeline.stages.context import PipelineContext, live_records, sweep_namespaces
from memspine.derive.pipeline.stages.envelope_adapter import is_fact
from memspine.memories.associative.entities import (
    EntityPolicy,
    canonical_entity,
    entity_node_id,
    is_entity_node,
)
from memspine.memories.associative.links import RESERVED_RELS
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.identity import AssertionKey, occurrence_id
from memspine.scope import Scope
from memspine.services.graph.base import GraphEdge as StoredEdge
from memspine.services.graph.base import GraphStore
from memspine.store.envelope import to_envelope
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord

_log = get_logger(__name__)

__all__ = [
    "COMPILED",
    "CompiledEdges",
    "EdgeCompiler",
    "LinkProjection",
    "ProjectionStats",
    "compile_graph_edges",
    "retract_after_forget",
]

#: The marker every projected edge and node carries.
COMPILED = "edge_compiler_v2"
#: Role-edge relation prefix in the graph store (the projection edge keeps the relation label).
ROLE_PREFIX = "role:"
_TENTATIVE_MODALITIES = ("hypothetical", "planned", "reported", "question")
_TENTATIVE_TAGS = frozenset({"tentative", *_TENTATIVE_MODALITIES})

AppendEvent = Callable[[MemoryEvent], Awaitable[None]]


def _tag(record: MemoryRecord, prefix: str) -> str | None:
    return next((t[len(prefix) :] for t in record.tags if t.startswith(prefix)), None)


# --------------------------------------------------------------------------- compiler


@dataclass(frozen=True)
class CompiledEdges:
    """The library delta plus the per-record facts the library does not carry."""

    namespace: str
    delta: GraphDelta
    #: record id -> (trust, sorted evidence ids, evidence role counts, event precision, source_at iso)
    meta: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Records that were not compiled, with the reason.
    skipped: Mapping[str, str] = field(default_factory=dict)


class EdgeCompiler:
    """Pure: records -> envelopes -> :class:`CompiledEdges`. No I/O, no clock, no model."""

    def __init__(self, *, scoped: bool = False, policy: EntityPolicy | None = None) -> None:
        self.scoped = scoped
        self.policy = policy or EntityPolicy.from_policy(True)
        assert self.policy is not None

    def eligible(self, record: MemoryRecord) -> bool:
        """A semantic fact with a subject entity, live or superseded with a successor."""
        return is_fact(record, include_history=True, purpose=FactPurpose.EDGES)

    def envelope(self, record: MemoryRecord) -> AssertionEnvelope:
        """The enriched assertion envelope of ``record`` (the F-02a envelope plus code-resolved roles)."""
        env = to_envelope(record)
        ns = record.namespace
        scope = env.scope
        if self.scoped:  # M-05 / M-16: tenant + owner, never the session
            scope = Scope.from_namespace(ns).to_owner_scope()
        assert isinstance(scope, OwnerScope)
        rel = _tag(record, "rel:") or env.assertion_key.rel
        kind = _tag(record, "kind:") or env.assertion_key.kind
        dst_raw = _tag(record, "dst:")
        subj = canonical_entity(record.entity or "")
        subj_ok = bool(subj) and self.policy.accepts(subj)  # type: ignore[union-attr]
        subj_node = entity_node_id(ns, subj) if subj_ok else None
        obj_node: str | None = None
        obj_rejected = False
        if dst_raw:
            dst = canonical_entity(dst_raw)
            if dst and dst != subj:
                if self.policy.accepts(dst):  # type: ignore[union-attr]
                    obj_node = entity_node_id(ns, dst)
                else:
                    obj_rejected = True
        key = AssertionKey(
            src=env.assertion_key.src,
            rel=rel,
            dst=canonical_entity(dst_raw) if dst_raw else "",
            kind=kind,
        )
        cited = list(record.source.parents) or [record.record_id]
        conversation = record.source.message_id or record.record_id
        modality = env.assessment.modality
        tag_set = set(record.tags)
        if "retract" in tag_set:
            modality = "negated"
        elif tag_set & _TENTATIVE_TAGS:
            hit = next((m for m in _TENTATIVE_MODALITIES if m in tag_set), "hypothetical")
            modality = hit  # type: ignore[assignment]
        elif record.provenance_class == "assistant_proposed":
            modality = "reported"  # proposed by the assistant, never confirmed by the user
        binding = "bound"
        if subj_node is None or obj_rejected:
            binding = "partly" if subj_node is not None or obj_node is not None else "unbound"
        assessment = env.assessment.model_copy(update={"modality": modality, "binding": binding})
        subject = (
            Mention(
                surface=record.entity or subj,
                mention_id=f"m_{record.record_id}",
                entity_id=subj_node,
            )
            if record.entity
            else None
        )
        obj = (
            Mention(surface=dst_raw, mention_id=f"o_{record.record_id}", entity_id=obj_node)
            if dst_raw and dst_raw.strip() and canonical_entity(dst_raw) != subj
            else None
        )
        return env.model_copy(
            update={
                "scope": scope,
                "assertion_key": key,
                "occurrence_id": occurrence_id(conversation, cited, key),
                "subject": subject,
                "object": obj,
                "assessment": assessment,
                "kind": kind,
            }
        )

    def compile(self, records: Iterable[MemoryRecord], namespace: str) -> CompiledEdges:
        envs: list[AssertionEnvelope] = []
        meta: dict[str, dict[str, Any]] = {}
        skipped: dict[str, str] = {}
        for record in sorted(records, key=lambda r: r.record_id):
            if not self.eligible(record):
                skipped[record.record_id] = "not_an_assertion"
                continue
            env = self.envelope(record)
            envs.append(env)
            roles = Counter(e.role for e in env.evidence)
            ids = {e.turn.source_id for e in env.evidence if e.turn is not None}
            ids.update(record.source.parents)
            meta[record.record_id] = {
                "trust": env.assessment.source_trust,
                "evidence_ids": sorted(ids),
                "evidence_roles": dict(sorted(roles.items())),
                "event_precision": env.time.event_precision,
                "event_basis": (
                    env.time.event.start.basis
                    if env.time.event is not None and env.time.event.start is not None
                    else None
                ),
                "source_at": env.time.source_time.isoformat() if env.time.source_time else None,
            }
        delta = compile_edges(envs)
        return CompiledEdges(namespace, delta, meta, skipped)


# --------------------------------------------------------------------------- projection


@dataclass
class ProjectionStats:
    nodes: int = 0
    written: int = 0
    unchanged: int = 0
    tentative: int = 0
    closed: int = 0
    retracted_edges: int = 0
    retracted_nodes: int = 0
    unbound: int = 0

    def as_dict(self) -> dict[str, int]:
        return {k: v for k, v in self.__dict__.items() if v}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _rel_type(edge: GraphEdge) -> str:
    if edge.kind == "role":
        return f"{ROLE_PREFIX}{edge.label}"
    return f"fact:{edge.label}" if edge.label in RESERVED_RELS else edge.label


def _edge_props(edge: GraphEdge, meta: Mapping[str, Any]) -> dict[str, object]:
    props: dict[str, object] = {
        "compiled": COMPILED,
        "edge_key": edge.key,
        "edge_kind": edge.kind,
        "assertion_id": edge.assertion_id,
        "record_id": edge.record_id,
        "occurrence_id": edge.occurrence_id,
        "tenant": edge.tenant,
        "owner": edge.owner,
        "tentative": edge.tentative,
        "negated": edge.negated,
        "contested": edge.contested,
        "closed": edge.closed,
        "valid_from": _iso(edge.valid_from),
        "valid_to": _iso(edge.valid_to),
        "status": edge.status,
        "trust": meta.get("trust"),
        "evidence_ids": list(meta.get("evidence_ids", ())),
        "source_at": meta.get("source_at"),
        "event_precision": meta.get("event_precision"),
        "event_basis": meta.get("event_basis"),
    }
    return props


def _weight(meta: Mapping[str, Any]) -> float:
    trust = meta.get("trust")
    return max(0.0, min(1.0, float(trust))) if isinstance(trust, int | float) else 1.0


def _node_payload(node: GraphNode, namespace: str, record_id: str | None) -> dict[str, object]:
    return {
        "labels": [node.kind],
        "properties": {
            "namespace": namespace,
            "compiled": COMPILED,
            "assertion_id": node.assertion_id,
            "record_id": record_id,
            "label": node.label if node.kind == "assertion" else None,
        },
    }


class LinkProjection:
    """Projects a compiled delta onto the graph as LINK events, and takes it back out."""

    def __init__(self, graph: GraphStore, append: AppendEvent) -> None:
        self._graph = graph
        self._append = append

    async def _current(self, namespace: str) -> list[StoredEdge]:
        return [
            e
            for e in await self._graph.edge_list(namespace)
            if e.properties.get("compiled") == COMPILED
        ]

    async def projected_record_ids(self, namespace: str) -> set[str]:
        """Records whose compiled edges are live in the graph (the lineage index)."""
        return {
            str(e.properties["record_id"])
            for e in await self._current(namespace)
            if e.weight > 0 and e.properties.get("record_id")
        }

    async def project(self, compiled: CompiledEdges) -> ProjectionStats:
        """Append a LINK event for every edge whose stored form differs; idempotent."""
        ns = compiled.namespace
        stats = ProjectionStats(unbound=len(compiled.delta.unbound))
        stored = {(e.src, e.dst, e.rel_type): e.properties for e in await self._current(ns)}
        nodes = {n.id: n for n in compiled.delta.nodes}
        for edge in compiled.delta.edges:
            meta = compiled.meta.get(edge.record_id, {})
            props = _edge_props(edge, meta)
            weight = _weight(meta)
            rel = _rel_type(edge)
            expected = {**props, "weight": weight}
            have = stored.get((edge.src, edge.dst, rel))
            if have is not None and all(have.get(k) == v for k, v in expected.items()):
                stats.unchanged += 1
                continue
            payload: dict[str, object] = {
                "src": edge.src,
                "dst": edge.dst,
                "rel": rel,
                "weight": weight,
                "reason": COMPILED,
                "props": props,
            }
            for side, nid in (("src_node", edge.src), ("dst_node", edge.dst)):
                node = nodes.get(nid)
                if node is not None and node.kind != "entity":
                    payload[side] = _node_payload(node, ns, edge.record_id)
            await self._append(
                MemoryEvent(kind=EventKind.LINK, namespace=ns, actor="system", payload=payload)
            )
            stats.written += 1
            stats.tentative += int(edge.tentative)
            stats.closed += int(edge.closed)
        stats.nodes = len(nodes)
        return stats

    async def retract_records(
        self, namespace: str, record_ids: Iterable[str], *, reason: str = "retract"
    ) -> ProjectionStats:
        """Remove every compiled edge sourced from ``record_ids`` (erasure / retire, by lineage).

        Edges are tombstoned with a LINK event (weight 0, properties replaced, so the log replays to the
        same graph) and the assertion node plus any value / time node nobody else uses is deleted."""
        wanted = {str(r) for r in record_ids}
        stats = ProjectionStats()
        if not wanted:
            return stats
        hit = [
            e
            for e in await self._current(namespace)
            if e.weight > 0 and str(e.properties.get("record_id")) in wanted
        ]
        assertions: set[str] = set()
        others: set[str] = set()
        for e in hit:
            if e.properties.get("edge_kind") == "role":
                assertions.add(e.src)
                if not is_entity_node(e.dst):
                    others.add(e.dst)
            await self._append(
                MemoryEvent(
                    kind=EventKind.LINK,
                    namespace=namespace,
                    actor="system",
                    payload={
                        "src": e.src,
                        "dst": e.dst,
                        "rel": e.rel_type,
                        "weight": 0.0,
                        "reason": f"{COMPILED}:{reason}",
                    },
                )
            )
            stats.retracted_edges += 1
        for node_id in sorted(assertions):
            await self._graph.delete_node(node_id)
            stats.retracted_nodes += 1
        for node_id in sorted(others):  # value / time nodes are shared: only drop the unused ones
            if not [x for x in await self._graph.edges_of(node_id) if x.weight > 0]:
                await self._graph.delete_node(node_id)
                stats.retracted_nodes += 1
        return stats

    async def visible_edges(
        self, namespace: str, *, include_tentative: bool = False, include_closed: bool = True
    ) -> list[StoredEdge]:
        """Default graph read of the compiled layer: tentative edges are excluded unless asked (S5-3)."""
        return [
            e
            for e in await self._current(namespace)
            if e.weight > 0
            and (include_tentative or not e.properties.get("tentative"))
            and (include_closed or not e.properties.get("closed"))
        ]


# --------------------------------------------------------------------------- stage


async def _compile_one(
    ctx: PipelineContext, namespace: str, compiler: EdgeCompiler
) -> dict[str, int]:
    assert ctx.graph is not None and ctx.append_event is not None
    projection = LinkProjection(ctx.graph, ctx.append_event)
    records = await live_records(ctx, namespace, "semantic")
    tap.event("graph.compile.start", namespace=namespace, records=len(records))
    compiled = compiler.compile(records, namespace)
    delta = compiled.delta
    if tap.enabled():
        tap.event(
            "graph.compile.delta",
            namespace=namespace,
            assertions=sum(1 for n in delta.nodes if n.kind == "assertion"),
            edges=len(delta.edges),
            tentative=sum(1 for e in delta.edges if e.tentative),
            closed=sum(1 for e in delta.edges if e.closed),
            unbound=len(delta.unbound),
            skipped=len(delta.skipped),
            scoped=compiler.scoped,
        )
    stats = await projection.project(compiled)
    tap.event(
        "graph.compile.project",
        namespace=namespace,
        written=stats.written,
        unchanged=stats.unchanged,
        tentative=stats.tentative,
    )
    # Retire: projected records that are no longer assertions (erased, hidden, archived with no successor).
    live = {r.record_id for r in records if compiler.eligible(r)}
    gone = sorted(await projection.projected_record_ids(namespace) - live)
    if gone:
        gone_stats = await projection.retract_records(namespace, gone, reason="retire")
        stats.retracted_edges += gone_stats.retracted_edges
        stats.retracted_nodes += gone_stats.retracted_nodes
        tap.event(
            "graph.compile.retract",
            namespace=namespace,
            records=len(gone),
            edges=gone_stats.retracted_edges,
            nodes=gone_stats.retracted_nodes,
            reason="retire",
        )
    return stats.as_dict()


async def compile_graph_edges(ctx: PipelineContext) -> dict[str, object]:
    """F-02c: compile the namespace's assertions into graph edges (``graph.edge_compiler_v2``)."""
    if not ctx.config.graph.edge_compiler_v2:
        return {"status": "skipped", "reason": "graph.edge_compiler_v2 off"}
    if ctx.graph is None:
        return {"status": "skipped", "reason": "no graph store"}
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    compiler = EdgeCompiler(scoped=bool(ctx.config.isolation.scope_keys_v2))
    totals: Counter[str] = Counter()
    errors: list[str] = []
    for namespace in await sweep_namespaces(ctx):
        try:
            async with ctx.lock(namespace):
                totals.update(await _compile_one(ctx, namespace, compiler))
        except Exception as exc:  # one bad namespace must not kill the sleep cycle
            errors.append(f"{namespace}: {exc}")
            _log.warning("compile_edges.namespace_failed", namespace=namespace, error=str(exc))
    stats: dict[str, object] = {"status": "ok", **dict(totals)}
    if errors:
        stats.update(status="partial", errors=errors)
    return stats


async def retract_after_forget(eng: Any, namespace: str, record_ids: Sequence[str]) -> None:
    """Erasure hook (flag on): the forgotten records' compiled edges go with them (by lineage)."""
    graph = getattr(eng, "_graph", None)
    if graph is None or not eng._config().graph.edge_compiler_v2:
        return
    stats = await LinkProjection(graph, eng._append_and_project).retract_records(
        namespace, record_ids, reason="erasure"
    )
    tap.event(
        "graph.compile.retract",
        namespace=namespace,
        records=len(record_ids),
        edges=stats.retracted_edges,
        nodes=stats.retracted_nodes,
        reason="erasure",
    )
