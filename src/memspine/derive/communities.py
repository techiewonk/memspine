# ruff: noqa: E501
"""Entity communities (LIB-36): deterministic clustering over the assertion graph, plus summary job plans.

Pure and deterministic: no I/O, no clock reads, no model calls. The summary text itself is a model call made
elsewhere (O-16 / O-17); this module only decides *which* communities exist, *which* changed since the last
run, and *what each summary job may read and cost*. Wiring into the sleep tier is O4-06; nothing here is
imported by the engine.

Algorithm (one choice, documented)
----------------------------------
Weighted **label propagation**, asynchronous, in a **fixed order**: nodes are visited in sorted-id order, every
node starts with its own id as label, and each visit moves a node to the label with the largest summed edge
weight among its neighbours. Ties keep the node's current label when it is among the tied labels, otherwise take
the smallest label string. Because the order, the initial labels and the tie-break depend only on node ids and
weights (never on input order), the result is identical under any permutation of the input edges. Parallel
edges between a pair are summed; self-loops are ignored. Disconnected parts never merge. The iteration cap
(``max_iterations``) bounds the work; hitting it is reported on the result, not hidden. Label propagation can
over-merge a very dense graph (the "collapse" failure of v1); ``max_community_size`` is the guard: a larger
group is returned as-is but flagged ``oversized`` so the caller can refuse to summarise it or re-run with a
stricter edge threshold. This is not Leiden: it is the dependency-free option that keeps this module pure.

Edge hygiene
------------
Tentative edges (S5-3) and closed edges (``valid_to`` set) are ignored unless asked for. Hub entities (the owner
self-entity) are removed from the graph, or down-weighted with ``hub_weight`` in (0, 1], so the owner does not
glue every cluster into one. Assertions on hub edges still count as members' evidence when the other endpoint is
in the community.

Identity and lineage
--------------------
A community id is ``com_`` + the first 16 hex of SHA-256 over the sorted member ids, so it is stable across runs.
:func:`update` keeps the previous id when the new group overlaps a previous community with Jaccard at or above a
threshold, so a small membership change does not orphan a summary. A summary is a derived record: its lineage is
the community's member assertion ids, its trust is at most the minimum parent trust (:class:`SummaryJob`), and
when a member assertion is erased the community lands in ``changed`` so the summary is re-run (``rerun_summary``).
"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Community",
    "CommunityChanges",
    "CommunityConfig",
    "CommunityEdge",
    "CommunityResult",
    "GraphDelta",
    "SummaryJob",
    "SummaryPlan",
    "community_id",
    "detect_communities",
    "plan_summary_jobs",
    "summary_jobs",
    "update",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CommunityEdge(_Frozen):
    """Minimal undirected input edge. A graph-delta edge (LIB-35) maps onto this field by field."""

    src: str
    dst: str
    weight: float = 1.0
    kind: str = ""
    tentative: bool = False
    valid_to: datetime | str | None = None
    #: Assertions that support this edge (lineage of any summary that covers it).
    assertion_ids: tuple[str, ...] = ()


class CommunityConfig(_Frozen):
    include_tentative: bool = False
    include_closed: bool = False
    #: Entity ids excluded from clustering (the owner self-entity).
    hubs: frozenset[str] = frozenset()
    #: Multiplier on edges touching a hub: 0 drops them (exclusion), (0, 1] keeps them down-weighted.
    hub_weight: float = Field(default=0.0, ge=0.0, le=1.0)
    min_weight: float = Field(default=0.0, ge=0.0)
    #: Communities smaller than this are not returned (no summary for a pair is the default floor of 2).
    min_size: int = Field(default=2, ge=1)
    max_community_size: int = Field(default=200, ge=2)
    max_iterations: int = Field(default=50, ge=1)
    top_entities: int = Field(default=5, ge=1)
    #: A new group inherits a previous id when Jaccard is at or above this.
    id_keep_jaccard: float = Field(default=0.6, gt=0.0, le=1.0)


class Community(_Frozen):
    id: str
    #: Sorted entity ids.
    members: tuple[str, ...]
    #: Highest weighted-degree members inside the community (ties by id).
    top_entities: tuple[str, ...]
    #: Member assertion ids (summary lineage), strongest edges first, then id.
    assertion_ids: tuple[str, ...]
    internal_weight: float = 0.0
    oversized: bool = False


class CommunityResult(_Frozen):
    communities: tuple[Community, ...]
    converged: bool = True
    iterations: int = 0


class GraphDelta(_Frozen):
    """What changed in the graph since the previous run. Only used to decide what needs a re-run."""

    touched_entities: frozenset[str] = frozenset()
    #: Assertions erased or superseded since the previous run (``rerun_summary`` trigger).
    removed_assertion_ids: frozenset[str] = frozenset()


class CommunityChanges(_Frozen):
    communities: tuple[Community, ...]
    #: No previous counterpart: a summary must be created.
    created: tuple[str, ...] = ()
    #: Kept id but members or evidence changed: the summary must be re-run.
    changed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    #: Previous ids with no successor: their summaries are retired.
    removed: tuple[str, ...] = ()
    #: new id (as detected from members) -> kept previous id, only where they differ.
    id_map: dict[str, str] = Field(default_factory=dict)

    @property
    def needs_summary(self) -> tuple[str, ...]:
        return tuple(sorted({*self.created, *self.changed}))


class SummaryJob(_Frozen):
    community_id: str
    members: tuple[str, ...]
    #: Full lineage: every member assertion id (the summary record's parents).
    lineage: tuple[str, ...]
    #: The subset the model call may read within the per-job input budget.
    input_assertion_ids: tuple[str, ...]
    estimated_tokens: int
    #: Trust ceiling for the summary: min over parents' trust, or ``None`` when no trust map was given.
    trust_cap: float | None = None
    reason: str = "created"


class SummaryPlan(_Frozen):
    jobs: tuple[SummaryJob, ...]
    #: Jobs that did not fit the budget; they stay dirty and are counted, never dropped (O-05).
    deferred: tuple[str, ...] = ()
    spent_tokens: int = 0


# --------------------------------------------------------------------------------------------------------------


def community_id(members: Iterable[str]) -> str:
    digest = hashlib.sha256("\x1f".join(sorted(set(members))).encode("utf-8")).hexdigest()
    return f"com_{digest[:16]}"


def _usable(edge: CommunityEdge, cfg: CommunityConfig) -> bool:
    if edge.src == edge.dst or edge.weight <= 0 or not math.isfinite(edge.weight):
        return False
    if edge.tentative and not cfg.include_tentative:
        return False
    if edge.valid_to is not None and not cfg.include_closed:
        return False
    return edge.weight >= cfg.min_weight


def _build_adjacency(
    edges: Iterable[CommunityEdge], cfg: CommunityConfig
) -> tuple[dict[str, dict[str, float]], list[CommunityEdge]]:
    adj: dict[str, dict[str, float]] = defaultdict(dict)
    kept: list[CommunityEdge] = []
    for e in edges:
        if not _usable(e, cfg):
            continue
        kept.append(e)
        w = e.weight
        touches_hub = e.src in cfg.hubs or e.dst in cfg.hubs
        if touches_hub:
            w *= cfg.hub_weight
            if w <= 0:
                continue
        a, b = (e.src, e.dst) if e.src < e.dst else (e.dst, e.src)
        adj[a][b] = adj[a].get(b, 0.0) + w
        adj[b][a] = adj[b].get(a, 0.0) + w
    return adj, kept


def _propagate(
    adj: dict[str, dict[str, float]], cfg: CommunityConfig
) -> tuple[dict[str, str], bool, int]:
    nodes = sorted(adj)
    label = {n: n for n in nodes}
    for it in range(1, cfg.max_iterations + 1):
        moved = False
        for n in nodes:
            score: dict[str, float] = defaultdict(float)
            for m in sorted(adj[n]):
                score[label[m]] += adj[n][m]
            if not score:
                continue
            best = max(score.values())
            tied = sorted(
                lab
                for lab, s in score.items()
                if math.isclose(s, best, rel_tol=1e-9, abs_tol=1e-12)
            )
            new = label[n] if label[n] in tied else tied[0]
            if new != label[n]:
                label[n] = new
                moved = True
        if not moved:
            return label, True, it
    return label, False, cfg.max_iterations


def detect_communities(
    edges: Iterable[CommunityEdge], config: CommunityConfig | None = None
) -> CommunityResult:
    """Cluster the entity graph. Same edges in any order give the same result."""

    cfg = config or CommunityConfig()
    edge_list = list(edges)
    adj, kept = _build_adjacency(edge_list, cfg)
    adj = {n: r for n, r in adj.items() if r}
    label, converged, iters = _propagate(adj, cfg)

    groups: dict[str, list[str]] = defaultdict(list)
    for n, lab in label.items():
        groups[lab].append(n)

    out: list[Community] = []
    for members in groups.values():
        if len(members) < cfg.min_size:
            continue
        out.append(_make_community(sorted(members), adj, kept, cfg))
    out.sort(key=lambda c: c.id)
    return CommunityResult(communities=tuple(out), converged=converged, iterations=iters)


def _make_community(
    members: list[str],
    adj: dict[str, dict[str, float]],
    kept: list[CommunityEdge],
    cfg: CommunityConfig,
) -> Community:
    mset = set(members)
    degree = {n: sum(w for m, w in adj[n].items() if m in mset) for n in members}
    top = sorted(members, key=lambda n: (-degree[n], n))[: cfg.top_entities]
    best_w: dict[str, float] = {}
    internal = 0.0
    for e in kept:
        a_in, b_in = e.src in mset, e.dst in mset
        a_hub, b_hub = e.src in cfg.hubs, e.dst in cfg.hubs
        inside = (a_in and b_in) or (a_in and b_hub) or (b_in and a_hub)
        if not inside:
            continue
        if a_in and b_in:
            internal += e.weight
        for aid in e.assertion_ids:
            best_w[aid] = max(best_w.get(aid, 0.0), e.weight)
    aids = tuple(sorted(best_w, key=lambda a: (-best_w[a], a)))
    return Community(
        id=community_id(members),
        members=tuple(members),
        top_entities=tuple(top),
        assertion_ids=aids,
        internal_weight=round(internal, 9),
        oversized=len(members) > cfg.max_community_size,
    )


# --------------------------------------------------------------------------------------------------------------


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def update(
    prev_communities: Iterable[Community],
    graph_delta: GraphDelta,
    *,
    edges: Iterable[CommunityEdge],
    config: CommunityConfig | None = None,
) -> CommunityChanges:
    """Re-cluster over the current ``edges`` and report which summaries need a re-run.

    ``graph_delta`` scopes the judgement of "changed": a community whose id was kept needs a re-run if its
    member set differs, if it holds a touched entity, or if one of its parents was removed.
    """

    cfg = config or CommunityConfig()
    prev = sorted(prev_communities, key=lambda c: c.id)
    fresh = list(detect_communities(edges, cfg).communities)

    # Greedy one-to-one id carry-over: best Jaccard first, ties by (prev id, new id).
    pairs: list[tuple[float, str, str]] = []
    for n in fresh:
        for p in prev:
            j = _jaccard(n.members, p.members)
            if j >= cfg.id_keep_jaccard:
                pairs.append((j, p.id, n.id))
    pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
    used_prev: set[str] = set()
    used_new: set[str] = set()
    assigned: dict[str, str] = {}
    for _j, pid, nid in pairs:
        if pid in used_prev or nid in used_new:
            continue
        used_prev.add(pid)
        used_new.add(nid)
        assigned[nid] = pid

    prev_by_id = {p.id: p for p in prev}
    final: list[Community] = []
    created: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    id_map: dict[str, str] = {}
    for n in fresh:
        kept_id = assigned.get(n.id, n.id)
        if kept_id != n.id:
            id_map[n.id] = kept_id
        c = n.model_copy(update={"id": kept_id})
        final.append(c)
        old = prev_by_id.get(kept_id)
        if old is None:
            created.append(kept_id)
        elif (
            old.members != c.members
            or set(c.members) & graph_delta.touched_entities
            or set(old.assertion_ids) & graph_delta.removed_assertion_ids
            or set(old.assertion_ids) != set(c.assertion_ids)
        ):
            changed.append(kept_id)
        else:
            unchanged.append(kept_id)
    final.sort(key=lambda c: c.id)
    removed = sorted(p.id for p in prev if p.id not in used_prev)
    return CommunityChanges(
        communities=tuple(final),
        created=tuple(sorted(created)),
        changed=tuple(sorted(changed)),
        unchanged=tuple(sorted(unchanged)),
        removed=tuple(removed),
        id_map=dict(sorted(id_map.items())),
    )


# --------------------------------------------------------------------------------------------------------------


def plan_summary_jobs(
    changes: CommunityChanges,
    budget: int,
    *,
    trust_of: Mapping[str, float] | None = None,
    base_tokens: int = 200,
    tokens_per_assertion: int = 40,
    max_input_tokens: int = 4000,
    estimate: Callable[[Community, int], int] | None = None,
) -> SummaryPlan:
    """Plan summary jobs for the communities that need one, within a token ``budget``.

    Priority: created before changed, then larger communities, then id. A job that does not fit is deferred
    (it stays dirty), but later smaller jobs may still fit. Oversized communities are never planned. Each job's
    input is truncated to ``max_input_tokens`` (strongest assertions first); its lineage stays complete.
    """

    created = set(changes.created)
    by_id = {c.id: c for c in changes.communities}
    todo = [by_id[i] for i in changes.needs_summary if i in by_id and not by_id[i].oversized]
    todo.sort(key=lambda c: (c.id not in created, -len(c.members), c.id))

    jobs: list[SummaryJob] = []
    deferred: list[str] = []
    spent = 0
    max_assertions = max(0, (max_input_tokens - base_tokens) // max(1, tokens_per_assertion))
    for c in todo:
        inputs = c.assertion_ids[:max_assertions]
        cost = (
            estimate(c, len(inputs))
            if estimate
            else base_tokens + tokens_per_assertion * len(inputs)
        )
        if spent + cost > budget:
            deferred.append(c.id)
            continue
        spent += cost
        cap: float | None = None
        if trust_of is not None:
            vals = [trust_of[a] for a in c.assertion_ids if a in trust_of]
            cap = min(vals) if vals else None
        jobs.append(
            SummaryJob(
                community_id=c.id,
                members=c.members,
                lineage=c.assertion_ids,
                input_assertion_ids=inputs,
                estimated_tokens=cost,
                trust_cap=cap,
                reason="created" if c.id in created else "changed",
            )
        )
    return SummaryPlan(jobs=tuple(jobs), deferred=tuple(deferred), spent_tokens=spent)


def summary_jobs(changed: CommunityChanges, budget: int, **kwargs: object) -> list[SummaryJob]:
    """Convenience form of :func:`plan_summary_jobs` returning only the jobs."""

    return list(plan_summary_jobs(changed, budget, **kwargs).jobs)  # type: ignore[arg-type]
