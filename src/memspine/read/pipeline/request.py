"""Read path: request planning and query-contract helpers moved out of the engine (F-10a)."""

from __future__ import annotations

import dataclasses
import hashlib
import time
from collections.abc import (
    Sequence,
)
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from memspine.compose.pack import estimate_tokens
from memspine.config import constants
from memspine.models.encoders.embedding.base import embed_queries
from memspine.models.encoders.query.shape import (
    is_aggregation,
    is_count,
    is_intent_list,
    is_set_question,
    is_set_question_wide,
    rule_read_mode,
)
from memspine.models.providers.llm.structured import structured_call
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.prompts.models import (
    QueryContractOut,
    ReadPlan,
)
from memspine.read.cache import InMemoryBackend, ItemsResult, ReadCache, read_key
from memspine.read.contract import (
    QueryContract,
    build_contract,
    is_ambiguous,
    merge_contract,
    parse_contract,
)
from memspine.read.contract_rules import TypedContract, render_header
from memspine.read.contract_rules import build_contract as build_typed_contract
from memspine.read.pipeline.state import (
    _FORENSICS,
    ReadResult,
)
from memspine.read.router_rules import LayerState, QueryDesc, RoutePlan, ServiceState
from memspine.read.router_rules import route as route_rules
from memspine.read.tiers import ReadConfig as TiersReadConfig
from memspine.read.tiers import Tier, choose_tier, normalise_request
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.public.read import ReadRequest
from memspine.scope import Scope
from memspine.store.events import EventKind
from memspine.store.layer_registry import LayerRegistry, LayerTemplate
from memspine.store.records import (
    MemoryRecord,
)
from memspine.trust.pipeline import consent as trust_consent

_log = get_logger(__name__)


def _list_mode_fires(eng: Any, query: str) -> bool:
    """B1: ``read.list_mode`` is on and the question matches ``read.list_trigger``."""
    read = eng._config().read
    if not read.list_mode:
        return False
    if read.list_trigger == "aggregation":
        return is_aggregation(query) or is_count(query)
    if read.list_trigger == "intent":
        return is_intent_list(query)
    if read.list_trigger == "set_question_wide":
        return is_set_question_wide(query)
    return is_set_question(query)


async def _decided(eng: Any, task: str, question: str, context: str | None, heuristic: str) -> str:
    """The label for ``task``: the decider's when it is configured for the task and sure
    enough, else ``heuristic`` (the existing rule's label, computed by the caller). Never
    raises. Every decision is recorded in the forensics as ``decisions``."""
    if not eng._decider_active(task):
        return heuristic
    read = eng._config().read
    entry: dict[str, Any] = {"task": task, "adapter": read.decider, "heuristic": heuristic}
    label = heuristic
    try:
        decision = await eng._decider_adapter().decide(task, question, context)
        entry.update(decision.as_meta())
        sure = (
            decision.confidence is not None and decision.confidence >= read.decider_min_confidence
        )
        if sure:
            label = decision.label
        entry["used"] = sure
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.decider_failed", task=task, error=str(exc))
        entry.update({"label": None, "confidence": None, "used": False, "error": str(exc)})
    entry["final"] = label
    if (fx := _FORENSICS.get()) is not None:
        fx.setdefault("decisions", []).append(entry)
    return label


async def _list_fires(eng: Any, query: str) -> bool:
    """B1 list-mode trigger; ``task=list_mode`` of the decider replaces the regex."""
    fired = eng._list_mode_fires(query)
    if not eng._config().read.list_mode or not eng._decider_active("list_mode"):
        return fired
    return await eng._decided("list_mode", query, None, "set" if fired else "single") == "set"


async def _list_fires_v2(eng: Any, ns: str, query: str) -> bool:
    """R14-M-1 (``read.list_mode_trigger_v2``): list depth from the question shape and the
    first vector leg's score profile. ``task=list_mode`` of the decider still overrides."""
    from memspine.read.list_trigger import list_signal

    fired = False
    sig = None
    try:
        [query_vector] = await embed_queries(eng._embedder, [query])
        hits = await eng._vector_leg(ns, query_vector, 30)
        sig = list_signal(query, [h.score for h in hits])
        fired = sig.fire
    except Exception as exc:  # an enhancer, never a gate: fall back to the regex
        _log.warning("read.list_trigger_v2_failed", error=str(exc))
        fired = eng._list_mode_fires(query)
    tap.event(
        "read.list_trigger_v2",
        fired=fired,
        score=None if sig is None else round(sig.score, 4),
        regex=eng._list_mode_fires(query),
    )
    if (fx := _FORENSICS.get()) is not None:
        fx["list_trigger_v2"] = {"fired": fired, "score": None if sig is None else sig.score}
    if not eng._decider_active("list_mode"):
        return fired
    return await eng._decided("list_mode", query, None, "set" if fired else "single") == "set"


async def _bridge_fires(
    eng: Any, query: str, read: Any, scored: Sequence[tuple[MemoryRecord, float]]
) -> bool:
    """R2-1b gate; ``task=bridge_hop`` of the decider replaces ``read.bridge_hop_gate``."""
    fired = eng._bridge_gate(query, read)
    if not eng._decider_active("bridge_hop"):
        return fired
    snippets = chr(10).join(f"- {r.content}" for r, _ in scored[:5])
    label = await eng._decided("bridge_hop", query, snippets, "hop" if fired else "no_hop")
    if (fx := _FORENSICS.get()) is not None:
        fx["bridge_gate"] = "decider" if label == "hop" else "skipped"
    return label == "hop"


async def _plan_read_mode(eng: Any, query: str) -> str | None:
    """H24: the decision provider's read mode, or None (rules) on any failure.

    G24: the ``query_shape`` rules go first (counts and sets -> compose, ordering ->
    replay, not gated by confidence); the provider chooses only what they leave open.
    """
    provider = eng._decision_provider()
    if provider is None:
        return None
    ruled = rule_read_mode(query)
    if ruled is not None:
        return ruled
    options = {label: desc for label, (_mode, desc) in eng._READ_OPTIONS.items()}
    try:
        label, confidence = await provider.choose(query, options)
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.planner_failed", error=str(exc))
        return None
    if label not in eng._READ_OPTIONS:
        return None
    gate = eng._config().read.planner_min_confidence
    # A bare label carries no confidence (None): below any positive gate.
    if (confidence is None and gate > 0) or (confidence is not None and confidence < gate):
        # G2b: an unsure choice does not route; keep the default replay read.
        _log.info("read.planner_unsure", label=label, confidence=confidence, gate=gate)
        return "replay"
    return eng._READ_OPTIONS[label][0]


async def _query_contract(eng: Any, query: str) -> QueryContract:
    """A03: the typed query contract of ``query`` under ``read.query_contract``.

    ``heuristic``: the rules (``core/query_contract``). ``llm``: the rules first; ONE
    ``plan@contract`` call only when the rules are unsure of the answer type
    (:func:`is_ambiguous`), and the reply may refine but never blank the contract; any
    failure keeps the rules' contract. Cached per question text (bounded), so the
    reranker hint, the reader header and the forensics share one build and one call."""
    cache: dict[str, QueryContract] = eng.__dict__.setdefault("_contract_cache", {})
    hit = cache.get(query)
    if hit is not None:
        return hit
    contract = build_contract(query)
    if eng._config().read.query_contract == "llm" and is_ambiguous(contract):
        contract = merge_contract(contract, await eng._llm_query_contract(query, contract))
    if len(cache) >= 512:
        cache.clear()
    cache[query] = contract
    return contract


async def _llm_query_contract(eng: Any, query: str, rules: QueryContract) -> QueryContract | None:
    """One ``plan`` role call (``plan@contract``), or None (the rules' contract stands)."""
    if eng._llm is None or eng._prompts is None or "plan" not in eng._llm.roles:
        _log.warning("read.query_contract_unbound", role="plan")
        return None
    try:
        out = await structured_call(
            eng._llm.for_role("plan"),
            eng._prompts.select("plan", condition="contract"),
            {"query": query},
            QueryContractOut,
        )
        return parse_contract(out.model_dump(), rules)
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.query_contract_failed", error=str(exc))
        return None


async def _llm_read_plan(eng: Any, query: str) -> ReadPlan | None:
    """G2a: one ``plan`` role call (counted by the router), or None (rules).

    None when the role is not bound, the call fails, or the reply is not a
    valid :class:`ReadPlan`; each case logs a warning (an enhancer, never a gate).
    """
    if eng._llm is None or eng._prompts is None or "plan" not in eng._llm.roles:
        _log.warning("read.planner_unbound", planner="llm", role="plan")
        return None
    try:
        version = eng._config().read.planner_version
        return await structured_call(
            eng._llm.for_role("plan"),
            # #35: plan@v2 also writes evidence-seeking subqueries for lookups;
            # #36: plan@v3 also names the persons and the time expression.
            eng._prompts.select("plan", condition=None if version == "v1" else version),
            {"query": query},
            ReadPlan,
        )
    except Exception as exc:
        _log.warning("read.planner_failed", planner="llm", error=str(exc))
        return None


async def _query_rewrite_probes(eng: Any, query: str) -> list[str]:
    """P4 (JustMem COMPOSE): up to two answer-free rewrites from the ``query_rewrite``
    role (``@compose`` variant), when ``read.compose_rewrites`` is on and the role is
    bound. Any failure yields no extra probes (an enhancer, never a gate)."""
    if (
        not eng._config().read.compose_rewrites
        or eng._llm is None
        or eng._prompts is None
        or "query_rewrite" not in eng._llm.roles
    ):
        return []
    prompt = eng._prompts.select("query_rewrite", condition="compose")
    try:
        text = await eng._llm.for_role("query_rewrite").chat(prompt.render({"query": query}))
    except Exception as exc:
        _log.warning("read.query_rewrite_failed", error=str(exc))
        return []
    lines = [line.strip(" -*0123456789.\t") for line in text.splitlines()]
    return [line for line in lines if line and line.lower() != query.lower()][:2]


# --- R3-15 / LIB-19: read-result cache (ER-31 B: off until enabled; POL-1: revalidate every hit)

_NON_MUTATING = frozenset({EventKind.RETRIEVE, EventKind.EXPOSE})


def _bump_generations(eng: Any, event: Any) -> None:
    """Count state-changing events: the store generation moves on every write-door event except
    reads and audit exposures; the erasure generation on every FORGET. Cheap, always on."""
    if event.kind in _NON_MUTATING:
        return
    d = eng.__dict__
    d["_store_generation"] = d.get("_store_generation", 0) + 1
    if event.kind == EventKind.FORGET:
        d["_erasure_generation"] = d.get("_erasure_generation", 0) + 1


def _read_cache(eng: Any) -> ReadCache[ReadResult] | None:
    cfg = eng._config().read.cache
    if not cfg.enabled:
        return None
    cache = eng.__dict__.get("_read_result_cache")
    if cache is None:
        cache = ReadCache(
            "read",
            enabled=True,
            backend=InMemoryBackend(cfg.max_entries),
            ttl_seconds=cfg.ttl_seconds,
            clock=time.monotonic if cfg.ttl_seconds else None,
        )
        eng.__dict__["_read_result_cache"] = cache
    return cache


def _cache_scope(eng: Any, namespace: str) -> OwnerScope:
    """The scope in the read-cache key. F-16c ``isolation.scope_keys_v2``: the typed tenant + owner
    of the namespace (the session is never in a key); off: the namespace as both, as before."""
    if eng._config().isolation.scope_keys_v2:
        return Scope.from_namespace(namespace).to_owner_scope()
    return OwnerScope(tenant=namespace, owner=namespace)


def _read_cache_key(eng: Any, query: str, namespace: str, viewer: str | None, params: Any) -> Any:
    """The ER-31 key of one read; ``params`` is every argument that shapes the result."""
    config = eng._config()
    model_ids: list[str] = []
    for attr in ("_embedder", "_reranker"):
        obj = getattr(eng, attr, None)
        ident = getattr(obj, "embedder_id", None) or getattr(obj, "reranker_id", None)
        if ident is None and obj is not None:
            ident = type(obj).__name__
        if ident:
            model_ids.append(f"{attr.strip('_')}:{ident}")
    blob = config.read.model_dump_json(exclude={"cache"}) + config.consent.model_dump_json()
    config_hash = "sha256:" + hashlib.sha256((blob + repr(params)).encode()).hexdigest()
    try:
        return read_key(
            scope=_cache_scope(eng, namespace),
            viewer=viewer,
            query=query,
            config_hash=config_hash,
            model_ids=model_ids,
            store_generation=eng.__dict__.get("_store_generation", 0),
            erasure_generation=eng.__dict__.get("_erasure_generation", 0),
            consent_generation=trust_consent.generation(eng),  # T5-03: None unless consent.v2
        )
    except ValueError:  # e.g. an empty query: not cacheable
        return None


def _copy_result(result: ReadResult) -> ReadResult:
    """A shallow copy: callers may mutate the context list without touching the cached entry."""
    return ReadResult(
        result.mode, dataclasses.replace(result.context, records=list(result.context.records))
    )


async def _revalidate_result(eng: Any, result: ReadResult) -> ItemsResult[Any]:
    """POL-1: keep only the records still eligible now (live, admitted, consent-ok)."""
    from memspine.config import constants

    items = []
    for rec in result.context.records:
        if constants.LEAD_TAG in rec.tags or rec.source.channel in ("lead", "persona"):
            items.append(rec)  # synthetic block: never stored, rebuilt per read
            continue
        live = await eng._storage.get_record(rec.record_id)
        if live is not None and eng._context_eligible(live) and eng._consent_ok(live):
            items.append(rec)
    total = len(result.context.records)
    return ItemsResult(
        tuple(items), revalidated_partial=len(items) < total, dropped=total - len(items)
    )


async def _cache_get(
    eng: Any, query: str, namespace: str, viewer: str | None, session_id: str | None, params: Any
) -> tuple[ReadResult | None, Any]:
    """Start of ``read``: ``(hit, key)``. ``hit`` is None on miss/stale/partial; ``key`` is None if
    caching does not apply (flag off, stateful session read, uncacheable query)."""
    cache = _read_cache(eng)
    if cache is None or session_id is not None:
        return None, None
    key = _read_cache_key(eng, query, namespace, viewer, params)
    if key is None:
        return None, None
    before = len(cache)
    value = cache.get(key)
    if value is None:
        tap.event("read.cache", outcome="stale" if len(cache) < before else "miss")
        return None, key
    check = await _revalidate_result(eng, value)
    if check.revalidated_partial:
        cache.get(key, revalidate=lambda _v: None)  # evict: a dropped record must not come back
        tap.event("read.cache", outcome="revalidated-partial", dropped=check.dropped)
        return None, key
    tap.event("read.cache", outcome="hit", records=len(check.items))
    return _copy_result(value), key


def _cache_put(eng: Any, key: Any, result: ReadResult) -> None:
    """End of ``read``: store under the key computed at the START (so a write during the read makes
    the entry stale on its first lookup). No-op when ``key`` is None."""
    cache = _read_cache(eng)
    if cache is not None and key is not None:
        cache.put(key, _copy_result(result))

# ---------------------------------------------------------------------------------------------
# R3-01 / R3-02 / R3-03 wiring (flag-off by default): request normalisation and tier choice
# (LIB-47), the typed contract and its header (LIB-16), the rule router (LIB-23).
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReadPlanState:
    """What the v2 request stage decided for one read."""

    request: ReadRequest | None = None
    tier: Tier | None = None
    contract: TypedContract | None = None
    route: RoutePlan | None = None
    #: optional legs the router left out: ``_search`` does not run them (``read.router_rules``).
    legs_off: frozenset[str] = frozenset()


#: The wiring state of the read in progress (None: no v2 flag is on, or outside ``Engine.read``).
_READ_PLAN: ContextVar[ReadPlanState | None] = ContextVar("memspine_read_plan", default=None)


def current_plan() -> ReadPlanState | None:
    """The v2 plan of the read running in this task, for later stages (tier, route, contract)."""
    return _READ_PLAN.get()


def v2_flags_on(read: Any) -> bool:
    return bool(read.tiers_v2 or read.contract_rules or read.router_rules)


def typed_contract(eng: Any, query: str) -> TypedContract:
    """LIB-16 rules contract of ``query``, cached per question text (bounded)."""
    cache: dict[str, TypedContract] = eng.__dict__.setdefault("_typed_contract_cache", {})
    hit = cache.get(query)
    if hit is None:
        hit = build_typed_contract(query)
        if len(cache) >= 512:
            cache.clear()
        cache[query] = hit
    return hit


_SHAPE_MAP = {"single": "lookup", "join": "join", "as_of": "as_of", "history": "temporal"}


def query_desc_of(contract: TypedContract) -> QueryDesc:
    """Project the typed contract onto the router's question description."""
    view = contract.time_view
    if contract.shape == "list_count":
        shape = "count" if contract.answer_type == "count" else "list"
    elif contract.shape in _SHAPE_MAP:
        shape = _SHAPE_MAP[contract.shape]
    elif contract.personal is False and contract.shape in ("unknown", "latest"):
        shape = "generic"
    else:
        shape = "lookup"
    time_view = view.kind if view.kind in ("history", "as_of") else "current"
    return QueryDesc(
        shape=shape,  # type: ignore[arg-type]
        has_time_scope=view.kind in ("range", "history", "as_of"),
        time_view=time_view,  # type: ignore[arg-type]
        named_subjects=tuple(contract.targets),
        many_cardinality=contract.cardinality == "many",
        personal=bool(contract.personal),
        language=contract.language if contract.language != "unknown" else None,
    )


def _layer_of(record: MemoryRecord) -> str | None:
    """Best-effort layer of a stored record (the store has no layer field yet)."""
    if constants.LEAD_TAG in record.tags or record.source.channel in ("lead", "persona"):
        return None
    mtype = record.memory_type
    if mtype == "episodic":
        return "L0"
    if mtype in ("profile", "preference") or constants.PROFILE_TAG in record.tags:
        return "L5"
    return "L1"


def _services(eng: Any, read: Any) -> ServiceState:
    return ServiceState(
        embedder=eng._embedder is not None,
        lexical_index=bool(eng._lexical is not None and read.hybrid),
        reranker=read.rerank != "off" and not getattr(eng, "_rerank_unavailable", False),
        graph_store=eng._graph is not None,
        worker_model=bool(eng._llm is not None and "plan" in getattr(eng._llm, "roles", ())),
    )


async def layer_state(eng: Any, ns: str, read: Any) -> LayerState:
    """Adapter: the :class:`LayerRegistry` of the namespace as a :class:`LayerState` (F-02b).

    Enabled = what the template switches on plus every populated layer; counts = live records per
    ``layer`` column. Two router-specific supplements keep the earlier behaviour where the column
    cannot say it: L2 also counts the distinct sessions of the raw turns (the episode leg reads
    sessions, not summaries) and L3 is on when a graph store is. If the registry cannot be built
    (no storage, an old store) the inferred adapter :func:`_inferred_layer_state` answers."""
    try:
        storage = eng._require_started()
        template = LayerTemplate.from_config(
            eng._config(),
            worker_model=_services(eng, read).worker_model,
            graph_store=eng._graph is not None,
        )
        registry = await LayerRegistry.load(storage, ns, template)
    except Exception:  # fall back, never fail a read on bookkeeping
        return await _inferred_layer_state(eng, ns, read)
    counts: dict[str, int] = dict(registry.counts)
    enabled: set[str] = set(registry.enabled) | {"L0", "L1"}
    listed = await eng._records(ns)
    sessions = {r.group_id for r in listed if r.memory_type == "episodic" and r.group_id}
    profile = sum(1 for r in listed if _layer_of(r) == "L5")  # profile / preference entries
    if profile and not counts.get("L5"):
        counts["L5"] = profile
        enabled.add("L5")
    if sessions and counts.get("L2", 0) < len(sessions):
        counts["L2"] = len(sessions)
        enabled.add("L2")
    if eng._graph is not None:
        counts["L3"] = max(counts.get("L3", 0), counts.get("L0", 0) + counts.get("L1", 0))
        enabled.add("L3")
    return LayerState(
        enabled=frozenset(enabled),  # type: ignore[arg-type]
        counts=counts,  # type: ignore[arg-type]
        services=_services(eng, read),
    )


async def _inferred_layer_state(eng: Any, ns: str, read: Any) -> LayerState:
    """The pre-registry adapter (RW-A): layers inferred from memory type, tags and the graph store.

    L0 = episodic raw turns, L1 = other stored records (facts), L2 = distinct sessions
    (``group_id``) of the raw turns, L3 = graph store (count = records, an upper bound),
    L5 = profile / preference records. Services come from the live engine."""
    counts: dict[str, int] = {}
    sessions: set[str] = set()
    for rec in await eng._records(ns):
        layer = _layer_of(rec)
        if layer is None:
            continue
        counts[layer] = counts.get(layer, 0) + 1
        if layer == "L0" and rec.group_id:
            sessions.add(rec.group_id)
    enabled: set[str] = {"L0", "L1"}
    if sessions:
        counts["L2"] = len(sessions)
        enabled.add("L2")
    if eng._graph is not None:
        counts["L3"] = counts.get("L0", 0) + counts.get("L1", 0)
        enabled.add("L3")
    if counts.get("L5"):
        enabled.add("L5")
    return LayerState(
        enabled=frozenset(enabled),  # type: ignore[arg-type]
        counts=counts,  # type: ignore[arg-type]
        services=_services(eng, read),
    )


async def plan_read(
    eng: Any,
    query: str,
    ns: str,
    *,
    viewer: str | None,
    budget_tokens: int,
    as_of: Any = None,
) -> tuple[str, ReadPlanState]:
    """Entry of a read under any of ``read.tiers_v2`` / ``contract_rules`` / ``router_rules``.

    Returns the (possibly normalised) query and the plan, which :func:`current_plan` serves
    to the later stages. The caller does not call this with all three flags off."""
    read = eng._config().read
    request: ReadRequest | None = None
    tier: Tier | None = None
    cfg = TiersReadConfig(default_depth=read.tiers_v2_depth)
    if read.tiers_v2:
        fields: dict[str, Any] = {
            "query": query,
            "scope": OwnerScope(tenant=ns, owner=viewer or ns),
            "viewer": viewer,
            "budget_tokens": budget_tokens if budget_tokens > 0 else None,
        }
        if isinstance(as_of, datetime) and as_of.tzinfo is not None:
            fields["as_of_valid"] = as_of
        request = normalise_request(fields, viewer, cfg)
        tap.event(
            "read.request.normalised",
            namespace=ns,
            viewer=viewer,
            depth=request.depth,
            budget_tokens=request.budget_tokens,
            query_changed=request.query != query,
        )
        query = request.query
    contract = typed_contract(eng, query)
    tap.event(
        "read.contract.built",
        shape=contract.shape,
        answer_type=contract.answer_type,
        time_view=contract.time_view.kind,
        personal=contract.personal,
        cardinality=contract.cardinality,
        n_targets=len(contract.targets),
        language=contract.language,
        unknown=list(contract.unknown),
    )
    layers: LayerState | None = None
    if read.router_rules:
        layers = await layer_state(eng, ns, read)
    if request is not None:
        chosen = choose_tier(
            request, contract, layers or LayerState(services=_services(eng, read)), cfg
        )
        tier = chosen
        tap.event(
            "read.tier.chosen",
            tier=chosen.name,
            degraded_from=chosen.degraded_from,
            max_generative_calls=chosen.max_generative_calls,
            max_rerank_pool=chosen.max_rerank_pool,
            slo_p95_ms=chosen.slo_p95_ms,
        )
    route: RoutePlan | None = None
    legs_off: frozenset[str] = frozenset()
    if layers is not None:
        route = route_rules(
            query_desc_of(contract), layers, tier.name if tier is not None else "standard"
        )
        planned = set(route.leg_names())
        legs_off = frozenset(n for n in ("entity", "graph", "episode") if n not in planned)
        tap.event(
            "read.router.plan",
            tier=route.tier,
            legs_run=list(route.leg_names()),
            legs_skipped=[{"leg": s.name, "reason": s.reason} for s in route.skipped],
            legs_suppressed=sorted(legs_off),
            rerank=route.rerank.enabled,
            list_mode=route.list_mode,
            missing_services=list(route.missing_services),
        )
    state = ReadPlanState(request, tier, contract, route, legs_off)
    if (fx := _FORENSICS.get()) is not None:
        fx["read_plan"] = {
            "tier": tier.name if tier else None,
            "legs": list(route.leg_names()) if route else None,
            "legs_off": sorted(legs_off),
        }
    return query, state


def tier_pool() -> int:
    """FX-READPATH (R-13): the rerank pool of the tier chosen for this read, else 0.

    Only a read under ``read.tiers_v2`` has a tier; the assembly search fetches at least this
    many candidates before ``rerank_v2`` (see ``windows._assemble_core``)."""
    plan = _READ_PLAN.get()
    return plan.tier.max_rerank_pool if plan is not None and plan.tier is not None else 0


def tier_allows_generative() -> bool:
    """FX-READPATH (R-01): False when the tier chosen for this read allows zero generative
    calls (fast, standard); True for deep and for any read without a tier."""
    plan = _READ_PLAN.get()
    return plan is None or plan.tier is None or plan.tier.max_generative_calls > 0


def legs_off() -> frozenset[str]:
    """Optional legs (``entity``, ``graph``, ``episode``) the router suppressed for this read."""
    plan = _READ_PLAN.get()
    return plan.legs_off if plan is not None else frozenset()


def add_typed_header(eng: Any, query: str, ns: str, context: Any) -> Any:
    """``read.contract_rules``: one ``[contract] ...`` lead line from the LIB-16 contract,
    inserted before the evidence like the A03 header (which stays available on its own flag).
    A failure keeps the context as it was."""
    try:
        plan = _READ_PLAN.get()
        contract = plan.contract if plan and plan.contract else typed_contract(eng, query)
        line = render_header(contract)
        if not line:
            return context
        records = list(context.records)
        evid = [
            r
            for r in records
            if constants.LEAD_TAG not in r.tags and r.source.channel not in ("lead", "persona")
        ]
        if not evid:
            return context
        records.insert(0, eng._lead_record(ns, line, evid))
        context.records = records
        context.boundary_index += 1
        context.tokens_used += estimate_tokens(line)
        tap.event("read.contract.header", chars=len(line), header=tap.content(line))
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.contract_rules_header_failed", error=str(exc))
    return context
