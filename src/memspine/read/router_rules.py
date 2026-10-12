"""LIB-23: rule router over the layer registry (R-04, R-05, layer policy).

Pure and unwired: wiring into the read path is R3-03. No I/O, no model call, no engine imports.

``route(query_desc, layers, tier)`` maps a described query and the state of the namespace's layers
to a :class:`RoutePlan`: the legs to run (each with a family label for fusion and a fetch budget),
the legs left out with an explicit reason, whether to rerank, and whether the deep tier may spend
a sufficiency check and a query rewrite. Every decision appends to ``plan.trace``, so a route is
never silent (R-04: a misroute is silent, so every route logs its reason).

Decided rules this module encodes:

* Vector, lexical and temporal are the core legs and always requested (R-05). Temporal runs with a
  full budget when the query has a time scope and a small one otherwise.
* Entity, graph, episode and profile legs run only when a rule switches them on and their layer
  exists and is populated (layer policy). The graph leg needs L3 and a graph store; the episode
  leg needs L2; the profile leg needs L5.
* A leg runs only when its layer is enabled **and** populated, and its service is installed.
  Otherwise it is listed in ``skipped`` with a reason code.
* Shapes drive legs: temporal/as-of -> temporal window plus the matching time view; join ->
  entity + graph; list/count -> wider pools and an exhaustive-listing request; generic -> no
  personal profile leg.
* Rerank on every read when a reranker is installed (Q5/Q6). A missing reranker is recorded.
* A sufficiency check and rewrite are allowed in the deep tier only, and only with a worker model.

Determinism: the plan is a pure function of the arguments; legs come out in a fixed order.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_TIERS",
    "LEG_ORDER",
    "Family",
    "Layer",
    "LayerState",
    "PlannedLeg",
    "QueryDesc",
    "RerankPlan",
    "RewritePlan",
    "RoutePlan",
    "RouterConfig",
    "RuleFired",
    "ServiceState",
    "Shape",
    "SkippedLeg",
    "Tier",
    "TierSpec",
    "TimeView",
    "route",
]

Layer = Literal["L0", "L1", "L2", "L3", "L4", "L5"]
Family = Literal["dense", "lexical", "structured", "derived"]
Tier = Literal["fast", "standard", "deep"]
Shape = Literal["lookup", "temporal", "as_of", "join", "list", "count", "generic"]
TimeView = Literal["current", "history", "as_of"]
SkipReason = Literal[
    "layer_disabled", "layer_empty", "service_missing", "not_routed", "generic_query"
]

LEG_ORDER: tuple[str, ...] = (
    "vector",
    "lexical",
    "temporal",
    "entity",
    "graph",
    "episode",
    "profile",
)

_FAMILY: dict[str, Family] = {
    "vector": "dense",
    "lexical": "lexical",
    "temporal": "structured",
    "entity": "structured",
    "graph": "derived",
    "episode": "derived",
    "profile": "derived",
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- inputs ----------------------------------------------------------------------


class QueryDesc(_Frozen):
    """What the router needs to know about the question (a projection of the query contract)."""

    shape: Shape = "lookup"
    has_time_scope: bool = False  # the question carries a time phrase or window
    time_view: TimeView = "current"  # requested view; the as_of shape forces ``as_of``
    named_subjects: tuple[str, ...] = ()
    many_cardinality: bool = False  # the answer is a set (list mode)
    personal: bool = False  # the question is about the asking user
    standing_answer: bool = False  # a stored standing answer / profile entry may settle it
    episodic_cue: bool = False  # the question points at a session or episode
    language: str | None = None  # BCP-47 tag for the lexical tokeniser; None = namespace default


class ServiceState(_Frozen):
    embedder: bool = True
    lexical_index: bool = True
    reranker: bool = True
    graph_store: bool = False
    worker_model: bool = False


class LayerState(_Frozen):
    """Layer registry snapshot for the scope: which layers are on, and how full each is.

    ``enabled`` comes from the template. L0 is always enabled, whatever the caller passes.
    ``counts`` is the number of records per layer for the scope; a missing layer counts as 0.
    """

    enabled: frozenset[Layer] = frozenset({"L0"})
    counts: Mapping[Layer, int] = Field(default_factory=dict)
    services: ServiceState = Field(default_factory=ServiceState)

    def is_enabled(self, layer: Layer) -> bool:
        return layer == "L0" or layer in self.enabled

    def count(self, layer: Layer) -> int:
        return max(0, int(self.counts.get(layer, 0)))

    def is_populated(self, layer: Layer) -> bool:
        return self.is_enabled(layer) and self.count(layer) > 0


class TierSpec(_Frozen):
    fetch_k: int
    pool_size: int
    timeout_ms: int


DEFAULT_TIERS: dict[Tier, TierSpec] = {
    "fast": TierSpec(fetch_k=32, pool_size=32, timeout_ms=800),
    "standard": TierSpec(fetch_k=48, pool_size=48, timeout_ms=1500),
    "deep": TierSpec(fetch_k=64, pool_size=64, timeout_ms=3000),
}


class RouterConfig(_Frozen):
    tiers: Mapping[Tier, TierSpec] = Field(default_factory=lambda: dict(DEFAULT_TIERS))
    wide_factor: int = 2  # list/count fetch multiplier
    pool_cap: int = 64  # R-Q4: at most 64 into the reranker
    wide_fetch_cap: int = 128
    secondary_factor: float = 0.5  # entity/graph/episode/profile share of the base fetch_k
    idle_temporal_factor: float = 0.25  # temporal leg share when the query has no time scope
    max_rewrites: int = 3


# --- outputs ---------------------------------------------------------------------


class PlannedLeg(_Frozen):
    name: str
    family: Family
    fetch_k: int
    timeout_ms: int
    reason: str
    language: str | None = None  # lexical leg only


class SkippedLeg(_Frozen):
    name: str
    reason: SkipReason
    detail: str


class RerankPlan(_Frozen):
    enabled: bool
    top_n: int
    reason: str


class RewritePlan(_Frozen):
    sufficiency_check_allowed: bool
    rewrite_allowed: bool
    max_rewrites: int
    reason: str


class RuleFired(_Frozen):
    rule: str
    detail: str


class RoutePlan(_Frozen):
    tier: Tier
    legs: tuple[PlannedLeg, ...]
    skipped: tuple[SkippedLeg, ...]
    pool_size: int
    rerank: RerankPlan
    rewrite: RewritePlan
    time_view: TimeView
    list_mode: bool
    exhaustive: bool  # request exhaustive listing / counting from the downstream stages
    missing_services: tuple[str, ...]
    trace: tuple[RuleFired, ...]

    def leg_names(self) -> tuple[str, ...]:
        return tuple(leg.name for leg in self.legs)

    def families(self) -> dict[str, Family]:
        """Leg name -> family label, ready for the fusion step."""
        return {leg.name: leg.family for leg in self.legs}

    def skipped_reason(self, name: str) -> SkipReason | None:
        for s in self.skipped:
            if s.name == name:
                return s.reason
        return None


# --- router ----------------------------------------------------------------------


def route(
    query_desc: QueryDesc,
    layers: LayerState,
    tier: Tier = "standard",
    config: RouterConfig | None = None,
) -> RoutePlan:
    cfg = config or RouterConfig()
    spec = cfg.tiers[tier]
    q = query_desc
    svc = layers.services
    trace: list[RuleFired] = []

    def fire(rule: str, detail: str) -> None:
        trace.append(RuleFired(rule=rule, detail=detail))

    fire("tier", f"{tier}: fetch_k={spec.fetch_k}, pool={spec.pool_size}")

    # --- shape rules: which optional legs are requested, and the budget scale ------
    wide = q.shape in ("list", "count") or q.many_cardinality
    exhaustive = q.shape in ("list", "count")
    if wide:
        fire("shape.list", f"shape={q.shape} many={q.many_cardinality}: wider pools, list mode")
    if exhaustive:
        fire("shape.exhaustive", "list/count: request exhaustive listing")

    time_view: TimeView = q.time_view
    if q.shape == "as_of":
        time_view = "as_of"
        fire("shape.as_of", "as-of question: temporal leg and as_of time view")
    elif q.shape == "temporal" or q.has_time_scope:
        fire("shape.temporal", f"time scope: temporal leg on the window, time view {time_view}")

    time_scoped = q.shape in ("temporal", "as_of") or q.has_time_scope
    want: dict[str, str] = {
        "vector": "core leg (R-05)",
        "lexical": "core leg (R-05)",
        "temporal": "core leg (R-05)",
    }
    if q.named_subjects or q.shape == "join":
        want["entity"] = (
            "join: entity leg" if q.shape == "join" else f"named subjects: {len(q.named_subjects)}"
        )
        fire("shape.entity", want["entity"])
    if q.shape == "join":
        want["graph"] = "join: one-hop graph expansion"
        fire("shape.join", "join: entity + graph (graph needs L3)")
    if q.shape in ("temporal", "as_of", "list", "count") or q.episodic_cue:
        want["episode"] = "episode cue or cross-session shape"
        fire("shape.episode", want["episode"])
    if q.shape == "generic":
        fire("shape.generic", "generic question: no personal profile leg")
    elif q.personal or q.standing_answer:
        want["profile"] = "personal question or standing answer"
        fire("shape.profile", want["profile"])

    # --- budgets -------------------------------------------------------------------
    base_k = spec.fetch_k
    if wide:
        base_k = min(base_k * cfg.wide_factor, cfg.wide_fetch_cap)
    sec_k = max(1, int(base_k * cfg.secondary_factor))
    idle_k = max(1, int(base_k * cfg.idle_temporal_factor))

    def budget(name: str) -> int:
        if name in ("vector", "lexical"):
            return base_k
        if name == "temporal":
            return base_k if time_scoped else idle_k
        return sec_k

    # --- availability: layer policy and services -----------------------------------
    legs: list[PlannedLeg] = []
    skipped: list[SkippedLeg] = []
    missing: list[str] = []

    def note_missing(service: str) -> None:
        if service not in missing:
            missing.append(service)

    def skip(name: str, reason: SkipReason, detail: str) -> None:
        skipped.append(SkippedLeg(name=name, reason=reason, detail=detail))
        fire("skip." + name, f"{reason}: {detail}")

    def layers_ok(name: str, need: tuple[Layer, ...]) -> bool:
        """True when at least one of ``need`` is enabled and populated; else record the skip."""
        if any(layers.is_populated(layer) for layer in need):
            return True
        enabled = [layer for layer in need if layers.is_enabled(layer)]
        if not enabled:
            skip(name, "layer_disabled", "/".join(need) + " disabled")
        else:
            skip(name, "layer_empty", "/".join(enabled) + " has no records in scope")
        return False

    for name in LEG_ORDER:
        if name not in want:
            if name == "profile" and q.shape == "generic":
                skip(name, "generic_query", "generic question needs no personal profile")
            else:
                skipped.append(
                    SkippedLeg(name=name, reason="not_routed", detail="no rule asked for it")
                )
            continue
        if name in ("vector", "lexical", "temporal", "entity"):
            ok = layers_ok(name, ("L0", "L1"))  # raw text or its facts carry these legs
        elif name == "graph":
            ok = layers_ok(name, ("L3",))
        elif name == "episode":
            ok = layers_ok(name, ("L2",))
        else:
            ok = layers_ok(name, ("L5",))
        if not ok:
            continue
        if name == "vector" and not svc.embedder:
            note_missing("embedder")
            skip(name, "service_missing", "no embedder installed")
            continue
        if name == "lexical" and not svc.lexical_index:
            note_missing("lexical_index")
            skip(name, "service_missing", "no lexical index installed")
            continue
        if name == "graph" and not svc.graph_store:
            note_missing("graph_store")
            skip(name, "service_missing", "no graph store installed")
            continue
        legs.append(
            PlannedLeg(
                name=name,
                family=_FAMILY[name],
                fetch_k=budget(name),
                timeout_ms=spec.timeout_ms,
                reason=want[name],
                language=q.language if name == "lexical" else None,
            )
        )
        fire("leg." + name, f"fetch_k={budget(name)} family={_FAMILY[name]}")

    if q.language is not None and any(leg.name == "lexical" for leg in legs):
        fire("lexical.language", f"language-aware tokeniser: {q.language}")
    if not any(leg.name in ("vector", "lexical") for leg in legs):
        fire("no_core_leg", "neither vector nor lexical can run")

    # --- pool and rerank -----------------------------------------------------------
    pool_size = min(spec.pool_size, cfg.pool_cap)
    if wide:
        pool_size = min(pool_size * cfg.wide_factor, cfg.pool_cap)
        fire("pool.wide", f"list/count: pool widened to {pool_size} (cap {cfg.pool_cap})")
    if svc.reranker:
        rerank = RerankPlan(enabled=True, top_n=pool_size, reason="reranker installed (Q5/Q6)")
        fire("rerank", f"rerank top {pool_size}")
    else:
        note_missing("reranker")
        rerank = RerankPlan(enabled=False, top_n=0, reason="no reranker installed")
        fire("rerank.missing", "no reranker installed: fused order is final")

    # --- deep-tier sufficiency check and rewrite -----------------------------------
    if tier != "deep":
        rewrite = RewritePlan(
            sufficiency_check_allowed=False,
            rewrite_allowed=False,
            max_rewrites=0,
            reason=f"{tier} tier: zero-call router, no rewrites",
        )
        fire("rewrite.off", rewrite.reason)
    elif not svc.worker_model:
        note_missing("worker_model")
        rewrite = RewritePlan(
            sufficiency_check_allowed=False,
            rewrite_allowed=False,
            max_rewrites=0,
            reason="deep tier but no worker model",
        )
        fire("rewrite.no_worker", rewrite.reason)
    else:
        rewrite = RewritePlan(
            sufficiency_check_allowed=True,
            rewrite_allowed=True,
            max_rewrites=cfg.max_rewrites,
            reason="deep tier with worker model",
        )
        fire("rewrite.deep", f"sufficiency check and up to {cfg.max_rewrites} rewrites allowed")

    return RoutePlan(
        tier=tier,
        legs=tuple(legs),
        skipped=tuple(skipped),
        pool_size=pool_size,
        rerank=rerank,
        rewrite=rewrite,
        time_view=time_view,
        list_mode=wide,
        exhaustive=exhaustive,
        missing_services=tuple(missing),
        trace=tuple(trace),
    )
