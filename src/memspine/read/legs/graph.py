"""R3-05b (R-09): the graph one-hop leg, ``OneHopConvergent``, over L3.

Seeds are entity nodes: the entities the query names, and the entities of the best base hits. The
leg looks at every edge the edge compiler projected (``graph.edge_compiler_v2``, F-02c), drops the
*tentative* ones (S5-3: hypothetical, planned, reported, question, partly bound), and walks exactly
one hop from each seed. A node reached from at least ``min_seeds`` distinct seeds is *convergent*:
it bridges the seeds (two people who know the same third person, an assertion that names two of
the seeds). The leg returns the records on the edges joining a seed to a convergent node, scored
by how many of the seeds converge (``k / n_seeds``, in ``(0, 1]``). The read pipeline then lifts
those fact records to their evidence turns through lineage (``read.pipeline.lift``).

The leg is empty with ``no_records:L3`` when the namespace holds no L3 record, and with a reason
(``no_seeds``, ``no_edges``, ``no_convergence``) otherwise; it never raises for that. One
``read.graph_hop`` wiretap event reports seeds, reached nodes, convergent nodes and the tentative
edges dropped (ids and counts only).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from memspine.observability import tap
from memspine.read.legs.core import Hit, LegContext, LegOutput, LegRegistry

__all__ = ["GRAPH_LAYERS", "HopEdge", "HopResult", "OneHopConvergent", "one_hop", "with_graph_leg"]

GRAPH_LAYERS: tuple[str, ...] = ("L3",)


@dataclass(frozen=True)
class HopEdge:
    """One compiled edge, reduced to what the walk needs (ids and flags, never text)."""

    src: str
    dst: str
    record_id: str
    tentative: bool = False
    closed: bool = False


@dataclass
class HopResult:
    seeds: list[str]
    #: node id -> the seeds that reach it in one hop (a seed is never counted as reaching itself)
    reached: dict[str, set[str]] = field(default_factory=dict)
    convergent: list[str] = field(default_factory=list)
    dropped_tentative: int = 0
    #: record id -> best convergence fraction
    scores: dict[str, float] = field(default_factory=dict)


def one_hop(
    seeds: Sequence[str],
    edges: Iterable[HopEdge],
    *,
    min_seeds: int = 2,
    include_closed: bool = False,
    admit_record: set[str] | None = None,
) -> HopResult:
    """Pure: one hop from ``seeds`` over the non-tentative ``edges``; convergent = >= ``min_seeds``.

    ``admit_record`` (when given) keeps only edges whose record is in the set (the live records)."""
    uniq = list(dict.fromkeys(seeds))
    seed_set = set(uniq)
    out = HopResult(seeds=uniq)
    kept: list[HopEdge] = []
    for e in edges:
        if e.tentative:
            out.dropped_tentative += 1
        elif (e.closed and not include_closed) or (
            admit_record is not None and e.record_id not in admit_record
        ):
            continue
        else:
            kept.append(e)
    touching: list[tuple[str, HopEdge]] = []  # (the other end, the edge)
    for e in kept:
        for here, there in ((e.src, e.dst), (e.dst, e.src)):
            if here in seed_set and there != here:
                touching.append((there, e))
                out.reached.setdefault(there, set()).add(here)
    need = max(1, min_seeds)
    convergent = {n for n, by in out.reached.items() if len(by) >= need}
    out.convergent = sorted(convergent)
    n_seeds = max(len(uniq), 1)
    for there, e in touching:
        if there in convergent:
            frac = len(out.reached[there]) / n_seeds
            if frac > out.scores.get(e.record_id, 0.0):
                out.scores[e.record_id] = frac
    return out


class OneHopConvergent:
    """Fact records on the edges that join query seeds to a node several seeds reach."""

    name = "graph"
    family = "structured"
    layers = GRAPH_LAYERS

    async def run(self, ctx: LegContext) -> LegOutput:
        reg = ctx.layer_registry
        if reg is not None:
            if not reg.is_populated("L3"):
                return LegOutput([], "no_records:L3")
        elif not ctx.records_for(self.layers):
            return LegOutput([], "no_records:L3")
        seeds = list(dict.fromkeys(ctx.graph_seeds))
        if not seeds:
            return LegOutput([], "no_seeds")
        if not ctx.graph_edges:
            return LegOutput([], "no_edges")
        live = {r.record_id for r in ctx.records}
        res = one_hop(seeds, ctx.graph_edges, min_seeds=ctx.graph_min_seeds, admit_record=live)
        if tap.enabled():
            tap.event(
                "read.graph_hop",
                seeds=list(res.seeds),
                reached={n: sorted(by) for n, by in sorted(res.reached.items())},
                convergent=list(res.convergent),
                dropped_tentative=res.dropped_tentative,
                records=len(res.scores),
            )
        if not res.scores:
            return LegOutput([], "no_convergence")
        ordered = sorted(res.scores.items(), key=lambda kv: (-kv[1], kv[0]))[: ctx.fetch_k]
        return LegOutput([Hit(rid, s) for rid, s in ordered])


_EXTENDED: dict[int, LegRegistry] = {}


def with_graph_leg(base: LegRegistry) -> LegRegistry:
    """``base`` plus the graph one-hop leg, as a new registry (the shared one is never changed)."""
    cached = _EXTENDED.get(id(base))
    if cached is not None and "graph" in cached.names():
        return cached
    out = LegRegistry()
    for name in base.names():
        out.register(base.get(name), required=base.is_required(name))
    out.register(OneHopConvergent())  # type: ignore[arg-type]  # CI-GREEN: pre-existing typing debt
    _EXTENDED[id(base)] = out
    return out
