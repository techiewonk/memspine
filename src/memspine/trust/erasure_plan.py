"""Erasure planner (G-13, M-25): hide then re-derive over the lineage DAG. Pure and unwired.

Binding decisions: schema session 4 (S4-5: each derived record loses the erased parent; no live
support parent left means hidden at once and queued for re-derivation from the remaining sources;
losing only a binding parent means unbound and hidden; summaries re-run without the erased input)
and sessions 10 and 11 (logical invisibility is immediate; the manifest carries ids and the erasure
generation, never content; a tracked purge removes projections, vectors, graph and caches; a backup
restore re-applies the manifest; rolling retention keeps lineage and tombstones and advertises a
limited rebuild; the erasure generation is part of the cache key; results are revalidated, POL-1).

Nothing here touches a store. The caller applies the plan, wires the purge steps to a job runner and
persists the manifest (T5-04, F-16a). ``trust/erasure.py`` is the older payload scrubber and is not
used here.

Edge roles and what losing a parent of that role does to the child:

* ``support``: the child stays valid while another live support parent remains (``keep``),
  otherwise it is hidden and re-derived (``hide_and_rederive``).
* ``binding``: the child is unbound and hidden (``unbind_and_hide``).
* ``member`` / ``merge_member``: aggregate membership; the child is re-run without the erased input
  (``rerun_summary``), or re-derived if no member and no support parent is left.
* ``context``, ``cites``, ``version_of``, ``supersedes``: the child never depended on the parent for
  its content; the edge is dropped (``keep``).

A parent counts as live only if it is not erased, not hidden by the plan, and not a descendant of
child (a record cannot be kept alive by its own output, so cycles are treated conservatively).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.erasure import (
    CONTENT_KEYS,
    ErasedIds,
    ErasureManifest,
    RetentionBoundary,
)
from memspine.schemas.durable.lineage import LineageEdge
from memspine.schemas.public.plugins import CacheKey

__all__ = [
    "ErasureAction",
    "ErasurePlan",
    "ErasureState",
    "InMemoryLineageGraph",
    "LineageGraph",
    "PlannedAction",
    "PurgeProgress",
    "PurgeStep",
    "RetentionReport",
    "apply_plan",
    "assert_manifest_has_no_content",
    "cache_key_current",
    "manifest",
    "plan_erasure",
    "purge_steps",
    "reapply_on_restore",
    "retention_report",
    "visible",
]

ErasureAction = Literal["hide_and_rederive", "unbind_and_hide", "rerun_summary", "keep"]

_STRENGTH: dict[str, int] = {
    "keep": 0,
    "unbind_and_hide": 1,
    "rerun_summary": 2,
    "hide_and_rederive": 3,
}
_DEPENDENCY_ROLES = frozenset({"support", "binding", "member", "merge_member"})
_MEMBER_ROLES = frozenset({"member", "merge_member"})


class LineageGraph(Protocol):
    """Read access to the lineage DAG. ``parents(x)`` are the edges whose child is ``x``;
    ``children(x)`` the edges whose parent is ``x``."""

    def parents(self, record_id: str) -> Sequence[LineageEdge]: ...

    def children(self, record_id: str) -> Sequence[LineageEdge]: ...


class InMemoryLineageGraph:
    """A dict-backed LineageGraph. Edges are unique per (child, parent, role)."""

    def __init__(self, edges: Iterable[LineageEdge] = ()) -> None:
        self._edges: dict[tuple[str, str, str], LineageEdge] = {}
        self._by_child: dict[str, dict[tuple[str, str, str], LineageEdge]] = {}
        self._by_parent: dict[str, dict[tuple[str, str, str], LineageEdge]] = {}
        for e in edges:
            self.add_edge(e)

    @staticmethod
    def _key(e: LineageEdge) -> tuple[str, str, str]:
        return (e.child, e.parent, e.role)

    def add_edge(self, edge: LineageEdge) -> None:
        k = self._key(edge)
        self._edges[k] = edge
        self._by_child.setdefault(edge.child, {})[k] = edge
        self._by_parent.setdefault(edge.parent, {})[k] = edge

    def remove_edges(self, edges: Iterable[LineageEdge]) -> None:
        for e in edges:
            k = self._key(e)
            self._edges.pop(k, None)
            self._by_child.get(e.child, {}).pop(k, None)
            self._by_parent.get(e.parent, {}).pop(k, None)

    def edges(self) -> list[LineageEdge]:
        return [self._edges[k] for k in sorted(self._edges)]

    def parents(self, record_id: str) -> list[LineageEdge]:
        d = self._by_child.get(record_id, {})
        return [d[k] for k in sorted(d)]

    def children(self, record_id: str) -> list[LineageEdge]:
        d = self._by_parent.get(record_id, {})
        return [d[k] for k in sorted(d)]


@dataclass(frozen=True)
class PlannedAction:
    record_id: str
    action: ErasureAction
    lost_parents: tuple[str, ...]  # parents that were erased or hidden by this plan
    remaining_sources: tuple[str, ...]  # live support/member parents to re-derive from

    @property
    def hidden(self) -> bool:
        return self.action != "keep"

    @property
    def needs_rederivation(self) -> bool:
        return self.action in ("hide_and_rederive", "rerun_summary")


@dataclass(frozen=True)
class ErasurePlan:
    erased: frozenset[str]
    generation: int
    actions: tuple[PlannedAction, ...]  # one per affected dependant, sorted by id
    edges_to_remove: tuple[LineageEdge, ...]  # every edge touching an erased record

    def action_for(self, record_id: str) -> ErasureAction | None:
        for a in self.actions:
            if a.record_id == record_id:
                return a.action
        return None

    @property
    def hidden(self) -> frozenset[str]:
        """Erased records plus every dependant whose action is not ``keep``."""
        return self.erased | {a.record_id for a in self.actions if a.hidden}

    @property
    def rederive_queue(self) -> tuple[str, ...]:
        return tuple(a.record_id for a in self.actions if a.needs_rederivation)


def _descendants(
    graph: LineageGraph, record_id: str, cache: dict[str, frozenset[str]]
) -> frozenset[str]:
    got = cache.get(record_id)
    if got is not None:
        return got
    seen: set[str] = set()
    queue = deque([record_id])
    while queue:
        cur = queue.popleft()
        for e in graph.children(cur):
            if e.role in _DEPENDENCY_ROLES and e.child not in seen:
                seen.add(e.child)
                queue.append(e.child)
    out = frozenset(seen)
    cache[record_id] = out
    return out


def _classify(
    graph: LineageGraph, child: str, lost: Collection[str], cache: dict[str, frozenset[str]]
) -> PlannedAction | None:
    edges = graph.parents(child)
    lost_edges = [e for e in edges if e.parent in lost]
    if not lost_edges:
        return None
    desc = _descendants(graph, child, cache)

    def live(e: LineageEdge) -> bool:
        return e.parent not in lost and e.parent not in desc

    live_support = [e for e in edges if e.role == "support" and live(e)]
    live_members = [e for e in edges if e.role in _MEMBER_ROLES and live(e)]
    lost_support = any(e.role == "support" for e in lost_edges)
    lost_members = any(e.role in _MEMBER_ROLES for e in lost_edges)
    lost_binding = any(e.role == "binding" for e in lost_edges)

    found: list[ErasureAction] = ["keep"]
    if lost_support and not live_support:
        found.append("rerun_summary" if live_members else "hide_and_rederive")
    if lost_members:
        found.append(
            "hide_and_rederive" if not live_members and not live_support else "rerun_summary"
        )
    if lost_binding:
        found.append("unbind_and_hide")
    action = max(found, key=lambda a: _STRENGTH[a])
    return PlannedAction(
        record_id=child,
        action=action,
        lost_parents=tuple(sorted({e.parent for e in lost_edges})),
        remaining_sources=tuple(sorted({e.parent for e in (*live_support, *live_members)})),
    )


def plan_erasure(
    erased_ids: Iterable[str],
    graph: LineageGraph,
    *,
    generation: int,
    already_lost: Collection[str] = (),
) -> ErasurePlan:
    """The transitive plan for erasing ``erased_ids``.

    ``already_lost`` are ids already erased or hidden by earlier plans; they count as lost parents
    but are not reported again, which makes repeated erasure idempotent. A dependant reached by
    several paths gets one action, the strongest. Pure: the graph is not modified.
    """
    if generation < 1:
        raise ValueError("generation must be >= 1")
    erased = frozenset(erased_ids)
    prior = frozenset(already_lost) - erased
    lost: set[str] = set(erased) | set(prior)
    cache: dict[str, frozenset[str]] = {}

    frontier = deque(sorted(lost))
    while frontier:
        cur = frontier.popleft()
        for e in graph.children(cur):
            c = e.child
            if c in lost:
                continue
            pa = _classify(graph, c, lost, cache)
            if pa is not None and pa.hidden:
                lost.add(c)
                frontier.append(c)
    # A kept record only turns hidden by gaining a lost parent, which re-queues it above, so
    # ``lost`` is a fixpoint. Final actions are recomputed against it.
    affected: set[str] = set()
    for src in lost:
        affected.update(e.child for e in graph.children(src))
    actions: list[PlannedAction] = []
    for c in sorted(affected - erased - prior):
        pa = _classify(graph, c, lost, cache)
        if pa is not None:
            actions.append(pa)
    removed: dict[tuple[str, str, str], LineageEdge] = {}
    for r in erased:
        for e in (*graph.parents(r), *graph.children(r)):
            removed[(e.child, e.parent, e.role)] = e
    return ErasurePlan(
        erased=erased,
        generation=generation,
        actions=tuple(actions),
        edges_to_remove=tuple(removed[k] for k in sorted(removed)),
    )


# ---------------------------------------------------------------- visibility state


@dataclass(frozen=True)
class ErasureState:
    """What is invisible now, and the current erasure generation (part of every cache key)."""

    erased: frozenset[str] = frozenset()
    hidden: frozenset[str] = frozenset()  # dependants hidden pending re-derivation or unbinding
    generation: int = 0

    @property
    def lost(self) -> frozenset[str]:
        return self.erased | self.hidden

    def release(self, record_ids: Iterable[str]) -> ErasureState:
        """Re-derived records become visible again (erased ids never do)."""
        return ErasureState(self.erased, self.hidden - frozenset(record_ids), self.generation)


def apply_plan(state: ErasureState, plan: ErasurePlan) -> ErasureState:
    """Fold a plan into the state. Idempotent and monotone; the generation never goes back."""
    erased = state.erased | plan.erased
    hidden = (state.hidden | {a.record_id for a in plan.actions if a.hidden}) - erased
    return ErasureState(
        erased=erased, hidden=hidden, generation=max(state.generation, plan.generation)
    )


def visible(record_id: str, state: ErasureState) -> bool:
    """POL-1 revalidation: erased or hidden ids are never visible, even if a cache returns them."""
    return record_id not in state.erased and record_id not in state.hidden


def cache_key_current(key: CacheKey, state: ErasureState) -> bool:
    """A cache entry keyed under an older erasure generation must not be served."""
    return key.erasure_generation >= state.generation


# ---------------------------------------------------------------- manifest


def assert_manifest_has_no_content(m: ErasureManifest) -> None:
    """Raise if any key in the dumped manifest is a content key (beyond the schema check)."""

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and k.lower() in CONTENT_KEYS:
                    raise AssertionError(f"content key in erasure manifest: {path}.{k}")
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(m.model_dump(), "manifest")


def manifest(
    plan: ErasurePlan,
    *,
    manifest_id: str,
    scope: OwnerScope,
    erased_at: datetime,
    requested_by: str | None = None,
    reason_code: Literal["user_request", "retention", "admin", "legal"] = "user_request",
    id_kinds: Mapping[str, Literal["record", "event", "source", "entity"]] | None = None,
) -> ErasureManifest:
    """The manifest for a plan: erased ids and the generation only. Hidden dependants are not
    listed; a restore recomputes them from the lineage (``reapply_on_restore`` with a graph)."""
    kinds = id_kinds or {}
    buckets: dict[str, list[str]] = {"record": [], "event": [], "source": [], "entity": []}
    for i in sorted(plan.erased):
        buckets[kinds.get(i, "record")].append(i)
    m = ErasureManifest(
        manifest_id=manifest_id,
        scope=scope,
        erasure_generation=plan.generation,
        erased_at=erased_at,
        requested_by=requested_by,
        reason_code=reason_code,
        ids=ErasedIds(
            record_ids=buckets["record"],
            event_ids=buckets["event"],
            source_ids=buckets["source"],
            entity_ids=buckets["entity"],
        ),
    )
    assert_manifest_has_no_content(m)
    return m


# ---------------------------------------------------------------- tracked physical purge


@dataclass(frozen=True)
class PurgeStep:
    step_id: str
    store: str
    kind: Literal["delete_ids", "bump_generation"]
    ids: tuple[str, ...] = ()
    generation: int = 0  # the erasure generation (caches: entries below it are dropped)


@dataclass(frozen=True)
class PurgeProgress:
    """Pure progress bookkeeping for a purge job."""

    steps: tuple[PurgeStep, ...]
    done: frozenset[str] = field(default_factory=frozenset)  # step_ids

    def mark_done(self, step_id: str) -> PurgeProgress:
        if step_id not in {s.step_id for s in self.steps}:
            raise KeyError(step_id)
        return PurgeProgress(self.steps, self.done | {step_id})

    @property
    def pending(self) -> tuple[PurgeStep, ...]:
        return tuple(s for s in self.steps if s.step_id not in self.done)

    @property
    def done_count(self) -> int:
        return len(self.done)

    @property
    def pending_count(self) -> int:
        return len(self.steps) - len(self.done)

    @property
    def complete(self) -> bool:
        return self.pending_count == 0

    @property
    def fraction(self) -> float:
        return 1.0 if not self.steps else len(self.done) / len(self.steps)


_CACHE_STORES = frozenset({"cache", "caches"})


def purge_steps(
    plan: ErasurePlan, stores: Sequence[str], *, batch_size: int | None = None
) -> PurgeProgress:
    """One purge task per store (or per batch of ids): erased and hidden ids are removed from
    projections, vectors and graph; a cache store gets a generation bump instead of id deletes."""
    if batch_size is not None and batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    ids = tuple(sorted(plan.hidden))
    steps: list[PurgeStep] = []
    for store in dict.fromkeys(stores):
        if store in _CACHE_STORES:
            steps.append(
                PurgeStep(
                    f"g{plan.generation}:{store}:0", store, "bump_generation", (), plan.generation
                )
            )
            continue
        size = batch_size or max(len(ids), 1)
        for n, start in enumerate(range(0, len(ids), size)):
            steps.append(
                PurgeStep(
                    f"g{plan.generation}:{store}:{n}",
                    store,
                    "delete_ids",
                    ids[start : start + size],
                    plan.generation,
                )
            )
    return PurgeProgress(tuple(steps))


# ---------------------------------------------------------------- backup restore


def reapply_on_restore(
    manifests: Iterable[ErasureManifest],
    restored_ids: Collection[str],
    graph: LineageGraph | None = None,
) -> set[str]:
    """Ids in a restored backup that must be hidden again. Every id named by any manifest is hidden
    if present; with the restored lineage ``graph`` the dependants are re-planned too."""
    named: set[str] = set()
    generation = 1
    for m in manifests:
        named.update(m.ids.record_ids, m.ids.event_ids, m.ids.source_ids, m.ids.entity_ids)
        generation = max(generation, m.erasure_generation)
    restored = set(restored_ids)
    hide = named & restored
    if graph is not None and named:
        plan = plan_erasure(named, graph, generation=generation)
        hide |= set(plan.hidden) & restored
    return hide


# ---------------------------------------------------------------- rolling retention


@dataclass(frozen=True)
class RetentionReport:
    rebuild: Literal["full", "limited", "unavailable"]
    rebuildable: frozenset[str]  # every root source is inside retained history
    limited: frozenset[str]  # lineage and tombstone kept, but a root source was pruned or unknown
    unavailable: frozenset[str]
    tombstones_kept: bool
    lineage_kept: bool

    @property
    def advertises_limited_rebuild(self) -> bool:
        return self.rebuild == "limited"


def _roots(graph: LineageGraph, record_id: str) -> set[str]:
    roots: set[str] = set()
    seen = {record_id}
    queue = deque([record_id])
    while queue:
        cur = queue.popleft()
        ps = [e.parent for e in graph.parents(cur) if e.role in _DEPENDENCY_ROLES]
        if not ps:
            roots.add(cur)
        for p in ps:
            if p not in seen:
                seen.add(p)
                queue.append(p)
    return roots


def retention_report(
    boundary: RetentionBoundary,
    graph: LineageGraph,
    record_ids: Iterable[str],
    *,
    origin_seq: Mapping[str, int] | None = None,
    origin_ts: Mapping[str, datetime] | None = None,
) -> RetentionReport:
    """Which records can still be rebuilt under the retention boundary. A root source whose origin
    is at or before the boundary (or unknown) was pruned: its dependants stay listed via lineage
    and tombstones, but only a limited rebuild is possible."""
    ids = frozenset(record_ids)
    if boundary.mode == "full":
        return RetentionReport("full", ids, frozenset(), frozenset(), True, True)
    if boundary.mode == "ephemeral":
        return RetentionReport(
            "unavailable",
            frozenset(),
            frozenset(),
            ids,
            boundary.tombstones_kept,
            boundary.lineage_kept,
        )
    seqs = origin_seq or {}
    tss = origin_ts or {}

    def retained(root: str) -> bool:
        if boundary.boundary_seq is not None:
            s = seqs.get(root)
            if s is None or s <= boundary.boundary_seq:
                return False
        if boundary.boundary_ts is not None:
            t = tss.get(root)
            if t is None or t <= boundary.boundary_ts:
                return False
        return True

    ok: set[str] = set()
    part: set[str] = set()
    for r in sorted(ids):
        (ok if all(retained(x) for x in _roots(graph, r)) else part).add(r)
    return RetentionReport(
        "limited",
        frozenset(ok),
        frozenset(part),
        frozenset(),
        boundary.tombstones_kept,
        boundary.lineage_kept,
    )
