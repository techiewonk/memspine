# ruff: noqa: E501
"""Edge compiler: assertion envelopes -> graph delta (LIB-35; M-16, M-17, ER-20, S5-3).

The envelope is truth; the graph is a projection compiled from it by code, never by the model. For every
assertion this module emits:

* one **assertion node** (id stable under assertion key + occurrence + scope), so an n-ary fact is one node
  rather than several unrelated binary edges;
* **role edges** from that node to entity / value / time nodes (``subject``, ``object``, ``value``,
  ``time`` and any further role carried in the code-compiled ``envelope.graph["nary"]["roles"]``, for
  example ``location``);
* one **binary projection edge** ``subject -> object`` labelled by the relation, for plain traversal.

Rules encoded:

* S5-3: asserted-or-negated and bound content gives normal edges; hypothetical, planned, reported,
  question, partly bound or unbound content gives edges tagged ``tentative``, which default graph reads
  exclude (the ``include_tentative`` flag of :func:`visible_edges` shows them on request). A mention with
  no resolved entity gets no edge at all (an unresolved binding never becomes a definitive entity edge);
  it is listed in ``GraphDelta.unbound`` instead.
* Status: a superseded assertion, or one whose event interval has ended, is *closed* (``valid_to`` set),
  never deleted. A contested assertion keeps its edges, flagged ``contested``. A hidden (merged duplicate)
  assertion contributes nothing.
* A-B-A: edge keys include the occurrence id, so a value that returns is a new edge with its own key, never
  a reopen of the closed one. Re-compiling the same envelope gives the same keys (idempotent replay).
* Lineage: every node and edge carries ``assertion_id`` as its source, so erasing the assertion removes
  exactly its nodes and edges (:func:`retract`).
* Old-key aliases (M-17): ``aliases`` on the assertion node and on role edges are the ids the same object
  would have had under each ``envelope.key_aliases`` entry.

Pure and deterministic: no I/O, no clock, no model calls. Output ordering is sorted and independent of the
input order. ``envelope.extras`` is never read (model-authored, not used by graph logic).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from memspine.derive.integrate import record_id
from memspine.schemas.durable.assertion import AssertionEnvelope
from memspine.schemas.durable.assessment import is_tentative
from memspine.store import keys

__all__ = [
    "GraphDelta",
    "GraphEdge",
    "GraphNode",
    "GraphSnapshot",
    "apply_delta",
    "assertion_node_id",
    "compile_edges",
    "retract",
    "visible_edges",
]

NodeKind = Literal["assertion", "entity", "value", "time"]
EdgeKind = Literal["role", "projection"]

#: Roles with a fixed meaning in the envelope; further roles come from ``graph["nary"]["roles"]``.
_FIXED_ROLES = ("subject", "object", "value", "time")


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: NodeKind
    label: str | None = None
    #: The assertion that produced this node (lineage). ``None`` for entity nodes (owned by resolution).
    assertion_id: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphEdge:
    key: str
    kind: EdgeKind
    label: str  # role name for role edges, relation for the projection edge
    src: str
    dst: str
    assertion_id: str  # source assertion node id (lineage: erasing it removes this edge)
    record_id: str
    occurrence_id: str
    tenant: str
    owner: str
    tentative: bool = False
    negated: bool = False
    contested: bool = False
    closed: bool = False
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    status: str = "active"  # the assertion status this edge was compiled from
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphDelta:
    """Upserts plus removals. Closed edges are upserts (with ``valid_to``), not removals."""

    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    #: Explicit removals (from :func:`retract` when envelopes are supplied).
    remove_nodes: tuple[str, ...] = ()
    remove_edges: tuple[str, ...] = ()
    #: Lineage removal: drop every node and edge whose ``assertion_id`` is in this set.
    retracted_assertions: tuple[str, ...] = ()
    #: ``(assertion_id, role, mention_or_ref)`` of roles left out because no entity was resolved.
    unbound: tuple[tuple[str, str, str], ...] = ()
    #: Record ids of hidden assertions that produced nothing.
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphSnapshot:
    nodes: Mapping[str, GraphNode] = field(default_factory=dict)
    edges: Mapping[str, GraphEdge] = field(default_factory=dict)


# --------------------------------------------------------------------------- identities


def _digest(domain: str, parts: list[Any]) -> str:
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return f"{domain}_{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:32]}"


def _assertion_id(env: AssertionEnvelope, key_digest: str) -> str:
    return _digest("an", [env.scope.tenant, env.scope.owner, key_digest, env.occurrence_id])


def assertion_node_id(env: AssertionEnvelope) -> str:
    """The assertion node id: stable under scope + assertion key + occurrence (replay-safe)."""
    return _assertion_id(env, env.assertion_key.digest())


def _edge_key(kind: str, label: str, src: str, dst: str, env: AssertionEnvelope) -> str:
    """Kind + endpoints + occurrence (A-B-A: a returning value has a new occurrence, hence a new key)."""
    return keys.edge_key(
        kind, label, src, dst, env.occurrence_id, env.scope.tenant, env.scope.owner
    )


def _value_node_id(env: AssertionEnvelope) -> str:
    v = env.value
    return _digest("vl", [env.scope.tenant, env.scope.owner, type(v).__name__, str(v)])


def _time_node_id(env: AssertionEnvelope, anchor: datetime) -> str:
    # Keyed on the interval start (else end) and precision only: closing an interval later must not
    # change the edge key. The end lives on the edge as ``valid_to``.
    return _digest(
        "tm", [env.scope.tenant, env.scope.owner, anchor.isoformat(), env.time.event_precision]
    )


# --------------------------------------------------------------------------- helpers


def _ref_at(ref: Any) -> datetime | None:
    return getattr(ref, "at", None) if ref is not None else None


def _event_bounds(env: AssertionEnvelope) -> tuple[datetime | None, datetime | None]:
    ev = env.time.event
    if ev is None:
        return None, None
    return _ref_at(ev.start), _ref_at(ev.end)


def _resolver(
    entity_ids: Mapping[str, str] | Collection[str] | None,
) -> tuple[Mapping[str, str], frozenset[str] | None]:
    if entity_ids is None:
        return {}, None
    if isinstance(entity_ids, Mapping):
        return entity_ids, None
    return {}, frozenset(entity_ids)


def _mention_entity(
    mention_id: str,
    own: str | None,
    mapping: Mapping[str, str],
    allowed: frozenset[str] | None,
) -> str | None:
    ent = mapping.get(mention_id) or own
    if ent is None:
        return None
    if allowed is not None and ent not in allowed:
        return None
    return ent


def _extra_roles(
    env: AssertionEnvelope, allowed: frozenset[str] | None
) -> list[tuple[str, str, bool]]:
    """``(role, entity_id, resolved)`` from the code-compiled ``graph['nary']['roles']`` (sorted)."""
    g = env.graph
    if not isinstance(g, dict):
        return []
    roles = (g.get("nary") or {}).get("roles") if isinstance(g.get("nary"), dict) else None
    if not isinstance(roles, dict):
        return []
    out: list[tuple[str, str, bool]] = []
    for role in sorted(roles):
        if role in _FIXED_ROLES:
            continue
        raw = roles[role]
        targets = raw if isinstance(raw, list | tuple) else [raw]
        for t in sorted({str(x) for x in targets if x not in (None, "")}):
            out.append((str(role), t, allowed is None or t in allowed))
    return out


def _is_tentative(env: AssertionEnvelope) -> bool:
    return env.status == "tentative" or is_tentative(env.assessment)


def _fallback_time(env: AssertionEnvelope) -> datetime | None:
    return env.time.source_time or env.time.recorded_at


# --------------------------------------------------------------------------- compile


def compile_edges(
    envelopes: Iterable[AssertionEnvelope],
    entity_ids: Mapping[str, str] | Collection[str] | None = None,
) -> GraphDelta:
    """Compile assertions into nodes and edges.

    ``entity_ids`` is either a mapping ``mention_id -> entity_id`` (resolver output, wins over
    ``Mention.entity_id``) or a collection of known entity ids (a role pointing outside it is treated as
    unbound). ``None`` uses ``Mention.entity_id`` as is. The result does not depend on input order.
    """
    mapping, allowed = _resolver(entity_ids)
    nodes: dict[str, GraphNode] = {}
    edges: dict[str, GraphEdge] = {}
    unbound: set[tuple[str, str, str]] = set()
    skipped: set[str] = set()

    for env in sorted(envelopes, key=lambda e: (record_id(e), e.occurrence_id)):
        rid = record_id(env)
        if env.status == "hidden":
            skipped.add(rid)
            continue
        aid = assertion_node_id(env)
        alias_keys = sorted(set(env.key_aliases) - {env.assertion_key.digest()})
        alias_ids = tuple(_assertion_id(env, k) for k in alias_keys)
        start, end = _event_bounds(env)
        closed = env.status == "superseded" or end is not None
        valid_from = start or _fallback_time(env)
        valid_to: datetime | None = None
        if closed:
            valid_to = end or _fallback_time(env)
        tentative = _is_tentative(env)
        negated = env.assessment.modality == "negated" or env.assessment.negated
        flags: dict[str, Any] = {
            "assertion_id": aid,
            "record_id": rid,
            "occurrence_id": env.occurrence_id,
            "tenant": env.scope.tenant,
            "owner": env.scope.owner,
            "tentative": tentative,
            "negated": negated,
            "contested": env.status == "contested",
            "closed": closed,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "status": env.status,
        }

        nodes.setdefault(
            aid,
            GraphNode(
                id=aid,
                kind="assertion",
                label=env.assertion_key.rel,
                assertion_id=aid,
                aliases=alias_ids,
            ),
        )

        def add_role(
            role: str,
            target: str,
            kind: NodeKind,
            label: str | None,
            *,
            env: AssertionEnvelope = env,
            aid: str = aid,
            alias_ids: tuple[str, ...] = alias_ids,
            flags: dict[str, Any] = flags,
        ) -> None:
            nodes.setdefault(
                target,
                GraphNode(
                    id=target,
                    kind=kind,
                    label=label,
                    assertion_id=None if kind == "entity" else aid,
                ),
            )
            _put(
                edges,
                GraphEdge(
                    key=_edge_key("role", role, aid, target, env),
                    kind="role",
                    label=role,
                    src=aid,
                    dst=target,
                    aliases=tuple(_edge_key("role", role, a, target, env) for a in alias_ids),
                    **flags,
                ),
            )

        resolved: dict[str, str] = {}
        for role, m in (("subject", env.subject), ("object", env.object)):
            if m is None:
                continue
            ent = _mention_entity(m.mention_id, m.entity_id, mapping, allowed)
            if ent is None:
                unbound.add((aid, role, m.mention_id))
                continue
            resolved[role] = ent
            add_role(role, ent, "entity", m.surface)

        if env.value is not None:
            add_role("value", _value_node_id(env), "value", str(env.value))
        anchor = start or end
        if anchor is not None:
            add_role("time", _time_node_id(env, anchor), "time", None)
        for role, ent, ok in _extra_roles(env, allowed):
            if ok:
                add_role(role, ent, "entity", None)
            else:
                unbound.add((aid, role, ent))

        if "subject" in resolved and "object" in resolved:
            s, o = resolved["subject"], resolved["object"]
            rel = env.assertion_key.rel
            _put(
                edges,
                GraphEdge(
                    key=_edge_key("projection", rel, s, o, env),
                    kind="projection",
                    label=rel,
                    src=s,
                    dst=o,
                    **flags,
                ),
            )

    return GraphDelta(
        nodes=tuple(sorted(nodes.values(), key=lambda n: n.id)),
        edges=tuple(sorted(edges.values(), key=lambda e: e.key)),
        unbound=tuple(sorted(unbound)),
        skipped=tuple(sorted(skipped)),
    )


def _put(edges: dict[str, GraphEdge], edge: GraphEdge) -> None:
    old = edges.get(edge.key)
    if old is None or repr(asdict(edge)) > repr(asdict(old)):
        edges[edge.key] = edge


# --------------------------------------------------------------------------- erasure


def retract(
    assertion_ids: Iterable[str],
    envelopes: Iterable[AssertionEnvelope] | None = None,
    entity_ids: Mapping[str, str] | Collection[str] | None = None,
) -> GraphDelta:
    """Delta that removes everything sourced from the given assertions (erasure, LIB-09 compatible).

    ``assertion_ids`` are assertion node ids (:func:`assertion_node_id`); record and occurrence ids of the
    supplied ``envelopes`` are accepted too. The delta always carries ``retracted_assertions`` (remove all
    nodes and edges with that source). When ``envelopes`` are given it also lists the explicit node and
    edge keys (including old-key aliases) so a store without lineage indexes can delete them directly.
    Entity nodes are never removed here: they belong to entity resolution.
    """
    wanted = {str(a) for a in assertion_ids}
    envs = list(envelopes or [])
    matched: list[AssertionEnvelope] = []
    ids: set[str] = set(wanted)
    for env in envs:
        aid = assertion_node_id(env)
        if wanted & {aid, record_id(env), env.occurrence_id}:
            matched.append(env)
            ids.discard(record_id(env))
            ids.discard(env.occurrence_id)
            ids.add(aid)
    rm_nodes: set[str] = set()
    rm_edges: set[str] = set()
    if matched:
        delta = compile_edges([_unhide(e) for e in matched], entity_ids)
        for n in delta.nodes:
            if n.assertion_id is not None:
                rm_nodes.add(n.id)
                rm_nodes.update(n.aliases)
        for e in delta.edges:
            rm_edges.add(e.key)
            rm_edges.update(e.aliases)
    return GraphDelta(
        remove_nodes=tuple(sorted(rm_nodes)),
        remove_edges=tuple(sorted(rm_edges)),
        retracted_assertions=tuple(sorted(ids)),
    )


def _unhide(env: AssertionEnvelope) -> AssertionEnvelope:
    """Un-hide an envelope so retraction enumerates its keys even if it was merged away."""
    return env.model_copy(update={"status": "active"}) if env.status == "hidden" else env


# --------------------------------------------------------------------------- applying and reading


def apply_delta(snapshot: GraphSnapshot, delta: GraphDelta) -> GraphSnapshot:
    """Reference reducer (pure): upserts, explicit removals, then lineage removal."""
    nodes = dict(snapshot.nodes)
    edges = dict(snapshot.edges)
    for n in delta.nodes:
        nodes[n.id] = n
    for e in delta.edges:
        edges[e.key] = e
    for nid in delta.remove_nodes:
        nodes.pop(nid, None)
    for key in delta.remove_edges:
        edges.pop(key, None)
    if delta.retracted_assertions:
        gone = set(delta.retracted_assertions)
        nodes = {k: v for k, v in nodes.items() if v.assertion_id not in gone}
        edges = {k: v for k, v in edges.items() if v.assertion_id not in gone}
    return GraphSnapshot(nodes, edges)


def visible_edges(
    edges: Iterable[GraphEdge], *, include_tentative: bool = False, include_closed: bool = True
) -> list[GraphEdge]:
    """Default graph read: tentative edges are excluded unless asked for (S5-3)."""
    return [
        e
        for e in edges
        if (include_tentative or not e.tentative) and (include_closed or not e.closed)
    ]
