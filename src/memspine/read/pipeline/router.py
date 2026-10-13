"""Read path: retrieve, search, read and the routed read, moved out of the engine (F-10a)."""

from __future__ import annotations

from collections.abc import (
    Callable,
    Sequence,
)
from datetime import timedelta
from typing import Any

from memspine.compose.pack import (
    AssembledContext,
    estimate_tokens,
)
from memspine.compose.pack import ranked as rank_pairs
from memspine.config import constants
from memspine.core import trace_sink
from memspine.core.language import language_scope
from memspine.core.read_filters import (
    DateBound,
    DateFilter,
    RecordScope,
    active_date_filter,
    active_record_scope,
    as_of_scope,
    date_filter_scope,
    record_scope,
    to_utc,
)
from memspine.core.rule_edges import is_why_question
from memspine.exceptions import (
    MemspineError,
)
from memspine.memories.episodic.sessions import topic_segments
from memspine.models.encoders.embedding.base import embed_queries
from memspine.models.encoders.query.perspective import (
    QuestionPerspective,
)
from memspine.models.encoders.query.shape import (
    is_aggregation,
    is_count,
    is_ordering,
)
from memspine.models.encoders.query.temporal_query import (
    LegHit,
    NamedLeg,
)
from memspine.models.providers.rerank.base import concat_background, gated_rerank
from memspine.observability import tap
from memspine.observability.logging import (
    EVENT_RETRIEVE,
    get_logger,
)
from memspine.prompts.models import (
    ReadPlan,
)
from memspine.read.contract import (
    rerank_hint,
)
from memspine.read.pipeline import freshness as read_freshness
from memspine.read.pipeline import request as read_request
from memspine.read.pipeline.fusion import (
    _balanced_pool,
    _cut_missing,
    _family_slots,
    _ids,
    _leg_rank_map,
    _log_leg_floor,
    _minmax_normalize,
    _minmax_quiet,
    _protected_ids,
    fuse_families,
)
from memspine.read.pipeline.legs import registry_base_legs
from memspine.read.pipeline.lift import lift_stage
from memspine.read.pipeline.rerank import (
    _static_prefilter,
    rerank_v2_stage,
)
from memspine.read.pipeline.state import (
    _FOCAL_ENTITY,
    _FORENSICS,
    _LIST_MODE,
    _RAW_SCORES,
    _RERANK_SUPPORT,
    _RERANKED,
    _SEARCH_MAX_WIDEN,
    ReadResult,
    _passive_hidden,
)
from memspine.services.lexical.base import minmax_fuse, rrf_fuse
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import (
    MemoryRecord,
    chrono_key,
)
from memspine.store.scope import validate_namespace
from memspine.trust.privacy import (
    read_scope,
)

_log = get_logger(__name__)


def _reply_targets(record: MemoryRecord) -> list[str]:
    """G27: the record ids ``record``'s ``reply_to:<id>`` tags name.

    A copy of ``ingest.write._reply_targets`` (``read`` must not import ``ingest``).
    """
    prefix = constants.REPLY_TO_PREFIX
    return [tag[len(prefix) :] for tag in record.tags if tag.startswith(prefix) and tag != prefix]


def _ledger_key(ns: str, session_id: str | None) -> tuple[str, str]:
    """B0 ledger key. Reads without a session id share ONE anonymous ledger
    per namespace (``""``): the engine cannot tell their callers apart.

    R4-1 choice: that ledger is fail-closed. It is never reset by a read or
    consumed by a write (``turn`` acts like ``session`` for it), and is only
    cleared by ``end_session(namespace)``. Resetting it would let caller Y's
    read or write drop caller X's reads before X writes (laundering);
    keeping it can only over-attribute parents, which lowers trust, never
    raises it. Callers that want per-turn precision pass a ``session_id``.
    """
    return (ns, session_id or "")


def _remember_last_read(
    eng: Any, ns: str, session_id: str | None, results: Sequence[tuple[MemoryRecord, float]]
) -> None:
    from memspine.ingest import role_wiring  # lazy: the echo guard lives on the write side

    role_wiring.remember_last_read(eng, ns, session_id, results)


def _record_reads(
    eng: Any, ns: str, session_id: str | None, results: Sequence[tuple[MemoryRecord, float]]
) -> None:
    """B0: remember what this session was shown, with the view trust it saw."""
    if eng._config().write.role_policy_v2 and results:  # W2-03: the echo guard's last read
        _remember_last_read(eng, ns, session_id, results)
    policy = eng._integrity()
    if not policy.enabled or policy.implicit_parents == "off" or not results:
        return
    key = eng._ledger_key(ns, session_id)
    if policy.implicit_parents == "turn" and session_id and key in eng._ledger_written:
        # The first read after a write opens a new turn: the old turn's
        # parents stop applying (they applied to every write of that turn).
        eng._read_ledger.pop(key, None)
        eng._ledger_written.discard(key)
    ledger = eng._read_ledger.setdefault(key, {})
    for record, _ in results:
        if record.memory_type == "shared":
            continue
        seen = ledger.get(record.record_id)
        ledger[record.record_id] = record.trust if seen is None else min(seen, record.trust)


def _consume_reads(eng: Any, ns: str, session_id: str | None) -> dict[str, float]:
    """B0: the reads that become implicit parents of this write.

    ``turn`` (R4-1): the parents apply to EVERY write until the session's
    next read, not just the first one; a read-once, write-twice agent cannot
    launder through its second write. ``session``: until ``end_session``.
    """
    policy = eng._integrity()
    if not policy.enabled or policy.implicit_parents == "off":
        return {}
    key = eng._ledger_key(ns, session_id)
    if policy.implicit_parents == "turn" and session_id and key in eng._read_ledger:
        eng._ledger_written.add(key)
    return dict(eng._read_ledger.get(key, {}))


async def retrieve(
    eng: Any,
    namespace: str = "default",
    memory_type: str | None = None,
    group_id: str | None = None,
    tags: list[str] | None = None,
    include_held: bool = False,
    purpose: str | None = None,
    include_passive: bool = False,
) -> list[MemoryRecord]:
    """P0 read path: relational listing. ``group_id``/``tags`` (D2) narrow to
    a sub-scope within the namespace; tags match records carrying ALL of the
    given tags.

    #11: records the firewall holds (quarantined) are left out unless
    ``include_held=True``, the operator's audit view; :meth:`list_quarantined`
    is the review queue.

    ``purpose`` (#50): the read's purpose, checked under ``consent.enforce``.
    #53: records of PASSIVE sessions are left out unless ``include_passive=True``
    or ``group_id`` names their group."""
    storage = eng._require_started()
    ns = validate_namespace(namespace)
    with read_scope(purpose) as outer:
        listed = await storage.list_records(ns, memory_type, group_id)
        if not include_held:
            listed = [r for r in listed if not r.quarantined]
        listed = [r for r in listed if not _passive_hidden(r, group_id)]
        records = [r for r in eng._inflate_all(listed, ns) if eng._consent_ok(r)]
        if tags:
            wanted = set(tags)
            records = [r for r in records if wanted.issubset(r.tags)]
        _log.info(EVENT_RETRIEVE, namespace=ns, memory_type=memory_type, count=len(records))
        if outer:
            await eng._audit_read("retrieve", ns, [r.record_id for r in records])
    return records


def _coverage_reorder(
    scored: list[tuple[MemoryRecord, float]], penalty: float
) -> list[tuple[MemoryRecord, float]]:
    """R14-M-1 (``read.coverage_select``): session-diverse order of the ranked pool; same
    members, same scores, same length (``read/list_trigger.py:coverage_order``)."""
    from memspine.read.list_trigger import coverage_order

    order = coverage_order(
        scored,
        [score for _, score in scored],
        [record.group_id for record, _ in scored],
        penalty=penalty,
        keys=[" ".join(record.content.lower().split()) for record, _ in scored],
    )
    out = [scored[i] for i in order]
    if tap.enabled():
        moved = sum(1 for new, old in enumerate(order) if new != old)
        tap.event("read.coverage_select", pool=len(scored), moved=moved)
    return out


async def _lexical_guarded(eng: Any, ns: str, query: str, fetch_k: int) -> Any:
    """The BM25 leg; a broken lexical leg degrades to vector-only, never takes down the search."""
    assert eng._lexical is not None  # narrowed by use_hybrid
    try:
        lexical_query = (
            await eng._strip_speaker_names(ns, query)
            if eng._config().read.lexical_strip_names
            else query
        )
        return await eng._lexical_leg(ns, lexical_query, fetch_k)
    except Exception as exc:
        # Defense in depth: fusing an empty leg preserves the vector ordering.
        _log.warning("lexical.search_failed", namespace=ns, error=str(exc))
        return []


async def _gather_base_legs(
    eng: Any,
    ns: str,
    query: str,
    query_vector: Any,
    fetch_k: int,
    use_hybrid: bool,
    probe_texts: Sequence[str],
    probe_vectors: Sequence[Any],
) -> tuple[Any, Any, Any]:
    """R3-15 ``read.parallel_legs``: the legs that depend only on the query (vector, lexical,
    metadata, encoder, probes) run together. Results keep the sequential order, so fusion is
    unchanged. Timings go to the wiretap as ``read.legs.parallel``."""
    import asyncio
    import time

    timings: dict[str, float] = {}

    async def timed(name: str, coro: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return await coro
        finally:
            timings[name] = round((time.perf_counter() - t0) * 1000, 2)

    names = ["vector"]
    coros = [timed("vector", eng._vector_leg(ns, query_vector, fetch_k))]
    if use_hybrid:
        names.append("lexical")
        coros.append(timed("lexical", _lexical_guarded(eng, ns, query, fetch_k)))
    names.append("metadata")
    coros.append(timed("metadata", eng._metadata_legs(ns, query, fetch_k)))
    if eng._config().read.query_encoder != "none":
        names.append("encoder")
        coros.append(timed("encoder", eng._encoder_legs(ns, query)))
    for i, (text, vector) in enumerate(zip(probe_texts, probe_vectors, strict=True)):
        names.append(f"probe{i}")
        coros.append(timed(f"probe{i}", eng._probe_legs(ns, text, vector, fetch_k, use_hybrid)))
    t0 = time.perf_counter()
    out = await asyncio.gather(*coros)  # results come back in argument order
    by_name = dict(zip(names, out, strict=True))
    extra_legs = list(by_name["metadata"])
    if "encoder" in by_name:
        extra_legs += by_name["encoder"]
    for name in names:
        if name.startswith("probe"):
            extra_legs += by_name[name]
    tap.event(
        "read.legs.parallel",
        legs=names,
        leg_ms=timings,
        wall_ms=round((time.perf_counter() - t0) * 1000, 2),
    )
    return by_name["vector"], by_name.get("lexical", []), extra_legs


async def search(
    eng: Any,
    query: str,
    namespace: str = "default",
    top_k: int = constants.SEARCH_TOP_K,
    group_id: str | None = None,
    tags: list[str] | None = None,
    session_id: str | None = None,
    ledger_id: str | None = None,
    sessions: Sequence[str] | None = None,
    roles: Sequence[str] | None = None,
    memory_types: Sequence[str] | None = None,
    tags_any: Sequence[str] | None = None,
    focal_entity: str | None = None,
    purpose: str | None = None,
    include_passive: bool = False,
    *,
    valid_from_after: DateBound | None = None,
    valid_from_before: DateBound | None = None,
    valid_to_after: DateBound | None = None,
    valid_to_before: DateBound | None = None,
    recorded_after: DateBound | None = None,
    recorded_before: DateBound | None = None,
    date_filter_mode: str = "and",
    viewer: str | None = None,
) -> list[tuple[MemoryRecord, float]]:
    """Semantic retrieval (P1 + E8 opt-in stages, D-51):
    ``[static_prefilter?] → vector/hybrid → [rerank?] → score`` (MMR and
    assembly follow in :meth:`assemble`).

    The retrieval leg is **vector-only by default**; with ``read.hybrid: true``
    (D-25) a lexical BM25 leg (``services/lexical``) is fused into the
    candidate ranking via reciprocal-rank fusion (``rrf_fuse``), so a record
    that only lexical search would surface can enter the results. Hybrid is
    opt-in for backward-compat (default-on is the intended v0.2 flip); off,
    results are bit-identical to the vector-only pipeline. The E1
    status/quarantine gate below runs on the FUSED candidates, so held
    content never surfaces through the lexical leg either.
    ``static_prefilter`` is a cheap lexical-overlap gate applied POST-fusion,
    not a true pre-vector prefilter.

    The gates run before the cut to ``top_k``: when archived, quarantined or
    filtered-out records fill the first window, the legs are queried again
    with a wider window (up to 64x) until ``top_k`` live candidates are found
    or the index is exhausted.

    Returns ``(record, score)`` pairs sorted by the M1 composite score
    (recency/relevance/importance + utility), not raw cosine — a stale
    near-duplicate loses to a fresher, proven-useful memory. Each search
    appends one RETRIEVE event so access stats (reinforcement, M1) update
    through the write door like every other mutation. Both E8 stages are
    off by default: results are bit-identical to the plain pipeline.

    ``purpose`` (#50): the read's purpose, checked under ``consent.enforce``.
    #53: records of PASSIVE sessions (session lifecycle) are left out unless
    ``include_passive=True``, ``session_id`` names their session, or ``group_id``
    names their group. With the lifecycle off no record is passive.

    Date filters (#37, :class:`~memspine.core.read_filters.DateFilter`): bounds on
    ``valid_from`` / ``valid_to`` / ``recorded_at`` (``*_after`` inclusive,
    ``*_before`` exclusive; datetimes, dates or ISO text; an open ``valid_to`` is
    later than any date), combined by ``date_filter_mode`` (``and``: every bound,
    ``or``: any). They restrict every leg to the matching records BEFORE fusion and
    the ``top_k`` cut (each leg looks over the whole namespace), so a filter never
    costs recall. No bound: unchanged.
    """
    session_id = ledger_id if ledger_id is not None else session_id  # I9
    if focal_entity:  # G-11: an extra graph seed for this read
        _FOCAL_ENTITY.set(focal_entity)
    date_filter = DateFilter.build(
        valid_from_after=valid_from_after,
        valid_from_before=valid_from_before,
        valid_to_after=valid_to_after,
        valid_to_before=valid_to_before,
        recorded_after=recorded_after,
        recorded_before=recorded_before,
        mode=date_filter_mode,
    )
    with (
        read_scope(purpose) as outer,
        date_filter_scope(date_filter),
        record_scope(  # I7/I8/G-12, I53 viewer, I48/I52 bars
            RecordScope.build(
                sessions,
                roles,
                memory_types,
                tags_any,
                viewer=viewer,
                check=await eng._scope_check(query),
            )
        ),
    ):
        scored = await eng._search(
            query,
            namespace,
            top_k,
            group_id=group_id,
            tags=tags,
            session_id=session_id,
            keep_k=top_k,
            search_pool=eng._config().read.rerank_v2_search_pool,
        )
        if outer:
            await eng._audit_read("search", namespace, [r.record_id for r, _ in scored])
    return scored


async def _search(
    eng: Any,
    query: str,
    namespace: str,
    top_k: int,
    *,
    group_id: str | None = None,
    tags: list[str] | None = None,
    session_id: str | None = None,
    keep_k: int,
    memory_type: str | None = None,
    hide: Callable[[MemoryRecord], bool] | None = None,
    probes: Sequence[str] = (),
    fused_legs: Sequence[list[LegHit]] = (),
    search_pool: int = 0,
) -> list[tuple[MemoryRecord, float]]:
    """:meth:`search` with ``keep_k``: how many results the caller finally keeps
    (assembly fetches ``candidate_pool x top_k``); the H18 rerank gate uses it.

    ``probes`` (#35, the LLM planner v2's lookup subqueries): each adds a vector
    leg (and a lexical leg under hybrid) to the RRF fusion. Empty: unchanged.

    ``hide`` (G1b/G3b): records a read header already shows leave with the gates,
    before the cut, the rerank, the ``rerank_keep`` cut, the RETRIEVE event and the
    B0 ledger. The legs widen until ``top_k`` visible candidates survive, so a
    header that hides most of the best hits never leaves the read short.

    ``fused_legs`` (#36): precomputed ranked legs (the persons / time leg) fused by RRF
    like the C3' legs. Empty: unchanged.

    ``search_pool`` (FU-RRPOOL, ``read.rerank_v2_search_pool``, passed by :func:`search` only):
    with ``read.rerank_v2`` on, fetch and rerank ``max(top_k, search_pool)`` candidates, then
    cut to ``top_k``. 0 = off. :func:`assemble` already widens through ``candidate_pool`` and
    does not pass it.

    The active date filter (#37, :func:`active_date_filter`) keeps only matching
    records in every leg, before fusion; each leg then looks over the whole
    namespace so no matching record is cut by a leg window."""
    if eng._embedder is None or eng._vector is None or eng._scoring is None:
        raise MemspineError("retrieval services not constructed — engine not started?")
    if top_k < 1:
        # SQLite LIMIT treats -1 as unbounded while Python's ``[:top_k]`` slice
        # would diverge; reject rather than let the two layers silently disagree
        # (REST already guards ``ge=1``).
        raise ValueError(f"top_k must be >= 1, got {top_k}")
    ns = validate_namespace(namespace)
    final_k = top_k
    if search_pool > top_k and (eng._config().read.rerank_v2 or eng._config().read.tiers_v2):
        # FU-RRPOOL: the reranker sees a wider pool; the cut back to ``final_k`` follows it.
        top_k = search_pool
    if eng._config().read.freshness_v2:  # O4-01b (O-06): staleness metadata, optional raw fallback
        stale_leg = await read_freshness.apply(eng, ns)
        if stale_leg:
            fused_legs = [*fused_legs, stale_leg]
    [query_vector] = await embed_queries(eng._embedder, [query])
    if eng._firewall.signals.query_anomaly:
        eng._query_history.observe(ns, query_vector)  # N22
    use_hybrid = eng._config().read.hybrid and eng._lexical is not None
    probe_texts = list(
        dict.fromkeys(p.strip() for p in probes if p.strip() and p.strip().lower() != query.lower())
    )
    probe_vectors = await embed_queries(eng._embedder, probe_texts) if probe_texts else []
    # Hybrid recall (E8/D-25): fetch a wider candidate window per leg so a
    # record ranked just outside a single leg's top_k, but strong when the two
    # legs combine, can still enter the fused top_k.
    # The session leg is a second fused leg like BM25, so it widens the window the same way.
    wide = use_hybrid or eng._config().read.session_leg
    base_fetch = top_k * constants.LEXICAL_FETCH_MULTIPLIER if wide else top_k
    allowed = await eng._date_allowed(ns)
    if allowed is not None:
        if not allowed[0]:
            _log.info(EVENT_RETRIEVE, namespace=ns, query=True, count=0)
            return []
        base_fetch = max(base_fetch, allowed[1])
    widen = 1
    # A header can hide most of the best hits (mined facts outrank raw turns), so
    # a hiding search may look further down the legs than the gates alone would.
    max_widen = _SEARCH_MAX_WIDEN * (constants.HEADER_HIDE_OVERFETCH if hide else 1)
    # GP-3 (read.graph_leg): computed once, from the first widen's legs.
    read_persp = eng._config().read
    persp_qp: QuestionPerspective | None = None
    if read_persp.perspective_mode != "off" or read_persp.speaker_vote_mode == "perspective":
        persp_qp = await eng._question_perspective(ns, query)
    off_legs = read_request.legs_off()  # R3-03 (read.router_rules): empty when the flag is off
    graph_leg: list[LegHit] | None = (
        None if eng._config().read.graph_leg and "graph" not in off_legs else []
    )
    # GP-9 (read.graph_communities): community summaries this query may read.
    community_gate: list[str] | None = None
    protected: dict[str, str] = {}  # I75a: record id -> leg whose top-N it belongs to
    family_slots: dict[str, list[str]] = {}  # R02: family -> protected ids (source_family)
    while True:
        protected = {}
        fetch_k = base_fetch * widen
        if eng._config().read.legs_v2:
            # R3-04 (read.legs_v2): vector + language-aware BM25 + temporal through the registry
            vector_hits, lexical_hits, extra_legs = await registry_base_legs(
                eng, ns, query, query_vector, fetch_k, use_hybrid, probe_texts, probe_vectors
            )
        elif eng._config().read.parallel_legs:
            vector_hits, lexical_hits, extra_legs = await _gather_base_legs(
                eng, ns, query, query_vector, fetch_k, use_hybrid, probe_texts, probe_vectors
            )
        else:
            vector_hits = await eng._vector_leg(ns, query_vector, fetch_k)
            # Hybrid (D-25): fuse the lexical BM25 leg via RRF. Off (default),
            # ``ranked`` is exactly the vector hits in cosine order — bit-identical.
            if use_hybrid:
                lexical_hits = await _lexical_guarded(eng, ns, query, fetch_k)
            else:
                lexical_hits = []
            extra_legs = await eng._metadata_legs(ns, query, fetch_k)
            if eng._config().read.query_encoder != "none":
                extra_legs += await eng._encoder_legs(ns, query)
            for text, vector in zip(probe_texts, probe_vectors, strict=True):
                extra_legs += await eng._probe_legs(ns, text, vector, fetch_k, use_hybrid)
        if community_gate is None and eng._config().read.graph_communities:
            community_gate = await eng._community_gate(
                ns, query, vector_hits, lexical_hits, extra_legs, use_hybrid
            )
        if graph_leg is None:
            graph_leg = await eng._graph_leg(
                ns, query, vector_hits, lexical_hits, extra_legs, use_hybrid
            )
            if community_gate:
                in_leg = {hit.record_id for hit in graph_leg}
                graph_leg += [LegHit(pid, 1.0) for pid in community_gate if pid not in in_leg]
        if graph_leg:
            extra_legs = [*extra_legs, graph_leg]
        read_now = eng._config().read
        if (read_now.cohesion_leg or read_now.entity_expand_leg) and "entity" not in off_legs:
            extra_legs += await eng._anchor_legs(ns, vector_hits, lexical_hits, fetch_k)
        if read_now.graph_node_search and "graph" not in off_legs:
            extra_legs += await eng._graph_node_legs(ns, query, query_vector, fetch_k)
        if read_now.maxsim_leg:
            extra_legs += await eng._maxsim_leg(ns, query_vector, vector_hits, lexical_hits)
        if read_now.session_leg and "episode" not in off_legs:
            extra_legs += await eng._session_leg(ns, query_vector, fetch_k)
        if (
            persp_qp is not None
            and read_now.perspective_mode != "off"
            and "subject" in read_now.perspective_axes
            and read_now.perspective_leg  # I75b: False = multiplier only, no RRF leg
        ):
            extra_legs += await eng._perspective_leg(ns, persp_qp, vector_hits)
        if read_now.list_mode and _LIST_MODE.get():
            extra_legs += await eng._speaker_vote_leg(ns, query, query_vector, vector_hits, fetch_k)
        extra_legs += [leg if isinstance(leg, NamedLeg) else list(leg) for leg in fused_legs if leg]
        floors = eng._config().read.leg_min_scores
        if floors and not (read_now.family_fusion and (use_hybrid or extra_legs)):
            # R3-06: under family_fusion the floors go into the fusion (a protected
            # hit is never floored)
            # C6 (per-leg score floors): weak hits never enter the fusion.
            logging_floors = _FORENSICS.get() is not None  # V02: zero cost when off
            if "vector" in floors:
                if logging_floors:
                    _log_leg_floor("vector", vector_hits, floors["vector"])
                vector_hits = [h for h in vector_hits if h.score >= floors["vector"]]
            if "lexical" in floors:
                if logging_floors:
                    _log_leg_floor("lexical", lexical_hits, floors["lexical"])
                lexical_hits = [h for h in lexical_hits if h.score >= floors["lexical"]]
        if allowed is not None:
            vector_hits = [h for h in vector_hits if h.record_id in allowed[0]]
            lexical_hits = [h for h in lexical_hits if h.record_id in allowed[0]]
            ids = allowed[0]
            extra_legs = [
                kept for leg in extra_legs if (kept := [h for h in leg if h.record_id in ids])
            ]
        if (use_hybrid or extra_legs) and read_now.family_fusion:
            # R3-06 (LIB-10): family fusion + protected pool + floors, instead of per-leg RRF.
            _over = 3 if read_now.rerank_balanced and read_now.rerank != "off" else 1
            ranked, protected, family_slots = fuse_families(
                vector_hits,
                lexical_hits,
                use_hybrid,
                extra_legs,
                pool_size=top_k * widen * _over,
                protect_n=read_now.pool_protect_per_leg or 3,
                rrf_k=read_now.rrf_k or constants.RRF_K,
                floors=dict(floors) if floors else None,
            )
        elif use_hybrid or extra_legs:
            rrf_k = eng._config().read.rrf_k or constants.RRF_K
            # N16 (plan v3.2): per-leg weights (vector, lexical, extra legs).
            lw = eng._leg_weights_for(query)
            weights = (
                [lw.get("vector", 1.0), lw.get("lexical", 1.0)]
                + [
                    lw.get(getattr(leg, "name", "extra"), lw.get("extra", 1.0))
                    for leg in extra_legs
                ]
                if lw
                else None
            )
            minmax = eng._config().read.fusion == "minmax"
            if minmax:
                # N52: min-max-normalised scores summed per leg, in place of RRF.
                fused = minmax_fuse(vector_hits, lexical_hits, extra_legs, weights)
            else:
                fused = rrf_fuse(
                    vector_hits, lexical_hits, k=rrf_k, extra=extra_legs, weights=weights
                )
            read_cut = eng._config().read
            # GR-15 fix (2026-10-10): with ``rerank_balanced`` the fused list must keep more
            # than the pool size, or ``_balanced_pool`` below has nothing to choose from and
            # is a no-op (measured: identical answers on 233 dev questions).
            over = 3 if read_cut.rerank_balanced and read_cut.rerank != "off" else 1
            fused_full = fused
            fused = fused[: top_k * widen * over]
            protect_n = read_cut.pool_protect_per_leg
            if protect_n > 0:
                # I75a: keep each leg's top-N alive through the fused-list cut (bounded:
                # at most legs * N extra entries, in fused order).
                named = [
                    ("vector", vector_hits),
                    *([("lexical", lexical_hits)] if use_hybrid else []),
                    *[(getattr(leg, "name", "extra"), leg) for leg in extra_legs],
                ]
                if read_cut.pool_protect_mode == "source_family":
                    family_slots = _family_slots(named, protect_n)
                    protected = {rid: fam for fam, ids in family_slots.items() for rid in ids}
                else:
                    protected = _protected_ids(named, protect_n)
                kept_ids = {rid for rid, _ in fused}
                fused = fused + [
                    (rid, sc) for rid, sc in fused_full if rid in protected and rid not in kept_ids
                ]
            if _FORENSICS.get() is not None and len(fused) < len(fused_full):
                # I78: the fused-list cut, before any gate: where each dropped hit stood.
                survived = {rid for rid, _ in fused}
                leg_rank = _leg_rank_map(
                    [
                        ("vector", vector_hits),
                        *([("lexical", lexical_hits)] if use_hybrid else []),
                        *[(getattr(leg, "name", "extra"), leg) for leg in extra_legs],
                    ]
                )
                for pos, (rid, _) in enumerate(fused_full, start=1):
                    if rid not in survived:
                        leg_name, leg_pos = leg_rank.get(rid, (None, None))
                        trace_sink.cut(
                            "pool_cut",
                            rid,
                            pool=top_k,
                            fused_rank=pos,
                            best_leg=leg_name,
                            best_leg_rank=leg_pos,
                        )
            # F1: raw RRF scores are ~1/(k+1) (≈0.016), but the M1 composite
            # expects relevance in [0, 1]. Normalize by the theoretical max (a
            # record ranked #1 in EVERY non-empty leg) so the fused relevance
            # composes with recency/importance exactly like a cosine similarity
            # would — otherwise relevance collapses and recency/importance
            # dominate under hybrid. Without C3' legs this is 2/(k+1).
            legs = 2 + len(extra_legs) if use_hybrid else 1 + len(extra_legs)
            if weights is not None:  # N16: the max is the weighted leg sum
                leg_ws = weights if use_hybrid else [weights[0], *weights[2:]]
                rrf_max = sum(leg_ws) / (rrf_k + 1) or 1.0
            else:
                rrf_max = legs / (rrf_k + 1)
            if minmax:  # N52: the max is the (weighted) leg count, without 1/(k+1)
                rrf_max *= rrf_k + 1
            ranked: list[tuple[str, float]] = [(rid, score / rrf_max) for rid, score in fused]
        else:
            if read_now.family_fusion and floors and "vector" in floors:
                vector_hits = [h for h in vector_hits if h.score >= floors["vector"]]
            ranked = [(hit.record_id, hit.score) for hit in vector_hits]
        if (fx := _FORENSICS.get()) is not None:
            fx["vector"] = [(h.record_id, h.score) for h in vector_hits]
            fx["lexical"] = [(h.record_id, h.score) for h in lexical_hits]
            fx["extra_legs"] = [
                (getattr(leg, "name", "extra"), [(h.record_id, h.score) for h in leg])
                for leg in extra_legs
            ]
            fx["fused"] = list(ranked)
        candidates = await eng._gate_hits(ns, ranked, group_id, tags, memory_type)
        if community_gate is not None:
            # GP-9: a community summary no seed entity belongs to is never read.
            admitted = set(community_gate)
            _pre = _ids(candidates)
            candidates = [
                pair
                for pair in candidates
                if pair[0].source.channel != "reorganize" or pair[0].record_id in admitted
            ]
            _cut_missing(_pre, candidates, "community_gate")
        if allowed is not None:
            # A cue passed the leg filter as a key; its resolved target is judged here.
            date_filter = active_date_filter()
            scope = active_record_scope()  # I7 / I8
            _pre = _ids(candidates)
            candidates = [
                pair
                for pair in candidates
                if (date_filter is None or date_filter.matches(pair[0]))
                and (scope is None or scope.matches(pair[0]))
            ]
            _cut_missing(_pre, candidates, "date_or_session_scope")
        if hide is not None:
            _pre = _ids(candidates)
            candidates = [pair for pair in candidates if not hide(pair[0])]
            _cut_missing(_pre, candidates, "header_hide")
        if persp_qp is not None and read_persp.perspective_mode != "off":
            _pre = _ids(candidates)
            candidates = eng._apply_perspective(candidates, persp_qp)
            _cut_missing(_pre, candidates, "perspective_filter")
        if read_persp.reinjection_penalty > 0.0 and session_id:  # I64
            candidates = eng._penalise_reinjected(candidates, ns, session_id)
        # Exhaustion is judged on the legs (before any gate or cut).
        exhausted = len(vector_hits) < fetch_k and len(lexical_hits) < fetch_k
        if len(candidates) >= top_k or exhausted or widen >= max_widen:
            break
        widen *= 4  # the gates removed too many: look further down the legs
    if eng._config().read.anticipatory_cues and candidates:
        # C8': a target reached both directly and via a cue keeps its best score.
        best: dict[str, tuple[MemoryRecord, float]] = {}
        for rec, rel in candidates:
            if rec.record_id not in best or rel > best[rec.record_id][1]:
                best[rec.record_id] = (rec, rel)
        candidates = sorted(best.values(), key=lambda pair: pair[1], reverse=True)
    if candidates and eng._config().read.graph_rerank != "off":
        # #22: graph-proximity and episode-mentions boosts, before the cut.
        candidates = await eng._graph_rerank(ns, query, candidates)
    walk = eng._config().read.causal_walk
    if (
        candidates
        and walk != "off"
        and eng._associative is not None
        and (walk == "always" or is_why_question(query))
    ):
        # W12/G27: the causes and answered messages of the best candidates.
        candidates = await eng._causal_walk(ns, candidates, group_id, tags, memory_type)
        if hide is not None:
            candidates = [pair for pair in candidates if not hide(pair[0])]
        date_filter = active_date_filter()
        if date_filter is not None:
            candidates = [pair for pair in candidates if date_filter.matches(pair[0])]
    read_pre = eng._config().read
    if read_pre.rerank_balanced and read_pre.rerank != "off" and len(candidates) > top_k:
        # GR-15 (Graphiti d40da88): the pool the reranker sees takes the legs'
        # best hits in turn, so one leg cannot fill it before the cut.
        candidates = _balanced_pool(candidates, [vector_hits, lexical_hits, *extra_legs], top_k)
    _pre = _ids(candidates)
    if protected and (
        eng._config().read.pool_protect_mode == "source_family" or read_pre.family_fusion
    ):
        # R02: the pool stays ``top_k``: family-protected hits first, then RRF order; the
        # lowest-ranked unprotected candidates are evicted, never a protected one.
        held = [pair for pair in candidates if pair[0].record_id in protected][:top_k]
        held_ids = {pair[0].record_id for pair in held}
        room = max(top_k - len(held), 0)
        rest = [pair for pair in candidates if pair[0].record_id not in held_ids]
        keep_ids = held_ids | {pair[0].record_id for pair in rest[:room]}
        evicted = [pair[0].record_id for pair in rest[room:]]
        candidates = [pair for pair in candidates if pair[0].record_id in keep_ids]
        if (fx := _FORENSICS.get()) is not None:
            fx["protected"] = {rid: protected[rid] for rid in held_ids}
            fx["pool_family"] = {
                "slots": {
                    fam: [rid for rid in ids if rid in held_ids]
                    for fam, ids in family_slots.items()
                },
                "evicted": evicted,
                "pool_size": len(candidates),
            }
    elif protected:
        # I75a: the fused pool plus each leg's protected top-N (at most legs * N more).
        candidates = candidates[:top_k] + [
            pair for pair in candidates[top_k:] if pair[0].record_id in protected
        ]
        if (fx := _FORENSICS.get()) is not None:
            fx["protected"] = dict(protected)
    else:
        candidates = candidates[:top_k]
    if _pre is not None:
        # I78: a pool cut says where the dropped hit stood (fused rank, best leg and rank).
        kept_pool = {pair[0].record_id for pair in candidates}
        fused_rank = {rid: i for i, (rid, _) in enumerate(ranked, start=1)}
        leg_rank = _leg_rank_map(
            [
                ("vector", vector_hits),
                ("lexical", lexical_hits),
                *[(getattr(leg, "name", "extra"), leg) for leg in extra_legs],
            ]
        )
        for rid in _pre:
            if rid not in kept_pool:
                leg_name, leg_pos = leg_rank.get(rid, (None, None))
                trace_sink.cut(
                    "pool_cut",
                    rid,
                    pool=top_k,
                    fused_rank=fused_rank.get(rid),
                    best_leg=leg_name,
                    best_leg_rank=leg_pos,
                )
    # E8 stage: static prefilter (opt-in, default off).
    if candidates and eng._config().read.static_prefilter:
        _pre = _ids(candidates)
        candidates = _static_prefilter(query, candidates)
        _cut_missing(_pre, candidates, "static_prefilter")
    # E4 stage: model2vec static-embedding prefilter (opt-in, default off).
    # A cheap static-cosine gate that narrows the candidate set before the
    # expensive rerank/score; a missing [static] extra self-disables it.
    if candidates and eng._static_prefilter_on and not eng._static_unavailable:
        candidates = await eng._static_embedding_prefilter(query, candidates, top_k)
    # E8 stage: cross-encoder rerank (opt-in, default off). The reranked
    # relevance replaces the vector similarity; the E1 gates above already
    # ran, so a reranker can only reorder live content, never resurface
    # held content. Failures degrade loudly to the vector ordering.
    if candidates and eng._config().read.relevance_filter:
        _pre = _ids(candidates)
        candidates = await eng._relevance_filter(query, candidates)
        _cut_missing(_pre, candidates, "relevance_filter")
    if candidates and read_pre.lift_before_rerank:
        # R3-08 (LIB-11): derived hits become their source turns before the rerank scores the pool.
        candidates = await lift_stage(
            eng,
            ns,
            candidates,
            protected,
            group_id=group_id,
            tags=tags,
            memory_type=memory_type,
            hide=hide,
            max_turns=read_pre.lift_max_turns,
        )
    reranker = eng._rerank_provider()
    gate = eng._config().read.rerank_max_top_k
    if gate is not None and keep_k > gate and not read_pre.rerank_v2:
        # H18: reranking pays when few of many candidates are kept. The gate
        # judges what the caller keeps, not the (candidate_pool-wide) fetch.
        reranker = None
    read_cfg = eng._config().read
    if read_cfg.skip_rerank_for_ordering and is_ordering(query):
        reranker = None  # Agent Zero: a relevance reranker scrambles temporal order
    # R3-07: rerank_v2 reranks every read (chain in ``rerank_v2_chain``); ordering questions
    # still skip.
    v2_on = read_cfg.rerank_v2 and not (read_cfg.skip_rerank_for_ordering and is_ordering(query))
    reranked = False
    _RERANK_SUPPORT.set(None)
    if (fx := _FORENSICS.get()) is not None:
        fx["pool"] = [(record.record_id, score) for record, score in candidates]
        fx["reranker"] = getattr(reranker, "reranker_id", None)
    rerank_query = query
    if read_cfg.query_contract != "off":  # A03: the contract is logged; the hint is opt-in
        contract = await eng._query_contract(query)
        hint = rerank_hint(contract) if "rerank" in read_cfg.query_contract_use else None
        if hint and (reranker is not None or v2_on):
            rerank_query = query + hint
        if (fx := _FORENSICS.get()) is not None:
            fx["query_contract"] = {
                **contract.as_meta(),
                "rerank_hint": hint,
                "mode": read_cfg.query_contract,
                "use": list(read_cfg.query_contract_use),
                "status": "observed",
            }
    elif (fx := _FORENSICS.get()) is not None:
        # V02: say that the contract was not computed rather than leave the field out
        fx["query_contract"] = {"mode": "off", "status": "absent"}
    if v2_on and candidates:
        candidates, reranked = await rerank_v2_stage(eng, ns, rerank_query, candidates, read_cfg)
    elif reranker is not None and candidates:
        documents = [concat_background(record) for record, _ in candidates]
        if read_cfg.rerank_date_prefix:
            # Hindsight: the cross-encoder sees when each candidate happened.
            documents = [
                doc
                if constants.UNDATED_TAG in record.tags
                else f"[Date: {record.valid_from:%Y-%m-%d}] {doc}"
                for (record, _), doc in zip(candidates, documents, strict=True)
            ]
        if read_cfg.rerank_context:
            documents = await eng._with_session_neighbours(
                ns, candidates, documents, read_cfg.rerank_context
            )
        try:
            eng._rerank_calls += 1
            raw_scores = await gated_rerank(reranker, rerank_query, documents)
            if len(raw_scores) > 1:
                _RERANK_SUPPORT.set(sorted(raw_scores, reverse=True)[1])
            if (raw_sink := _RAW_SCORES.get()) is not None:
                for (record, _), raw in zip(candidates, raw_scores, strict=True):
                    raw_sink[record.record_id] = max(
                        float(raw), raw_sink.get(record.record_id, float("-inf"))
                    )
            if (fx := _FORENSICS.get()) is not None:
                fx["rerank_scores"] = [
                    (record.record_id, float(raw))
                    for (record, _), raw in zip(candidates, raw_scores, strict=True)
                ]
            if read_cfg.rerank_gate is not None and max(raw_scores) < read_cfg.rerank_gate:
                # N41: the reranker is not confident: keep the retrieval order.
                eng._rerank_gated += 1
            else:
                relevances = _minmax_normalize(raw_scores)
                if read_cfg.rerank_blend is not None:
                    w = read_cfg.rerank_blend
                    prior = _minmax_quiet([score for _, score in candidates])
                    relevances = [
                        w * r + (1.0 - w) * p for r, p in zip(relevances, prior, strict=True)
                    ]
                candidates = [
                    (record, relevance)
                    for (record, _), relevance in zip(candidates, relevances, strict=True)
                ]
                reranked = True
        except Exception as exc:
            eng._rerank_failures += 1
            _log.warning(
                "rerank.failed", namespace=ns, reranker=reranker.reranker_id, error=str(exc)
            )
    scored = [
        (record, eng._scoring.composite_score(record, relevance=relevance))
        for record, relevance in candidates
    ]
    integrity = eng._integrity()
    if integrity.enabled and integrity.live_reevaluation:
        live = []
        for record, score in scored:
            effective = await eng.effective_trust(record.record_id)
            live.append((record.model_copy(update={"trust": effective}), score))
        scored = live
    if integrity.enabled:
        # MTI read door, own namespace: view trust is the record's trust.
        _pre = _ids(scored)
        scored = [
            (record, integrity.ranked(score, record.trust))
            for record, score in scored
            if integrity.admits(record.trust)
        ]
        _cut_missing(_pre, scored, "integrity_admission_theta")
    scored = rank_pairs(scored)
    mmr_lambda = eng._config().read.mmr_lambda
    if mmr_lambda is not None and len(scored) > 2:
        # G-10 (Graphiti MMR): diversity among near-duplicate hits, on embeddings.
        scored = await eng._mmr_order(query, scored, mmr_lambda)
    if read_cfg.coverage_select and _LIST_MODE.get() and len(scored) > 2:
        scored = _coverage_reorder(scored, read_cfg.coverage_session_penalty)  # R14-M-1
    _RERANKED.set(reranked)
    if (
        reranked
        and read_cfg.rerank_keep is not None
        and read_cfg.candidate_pool > 1
        and not _LIST_MODE.get()  # B1: list mode keeps the whole reranked pool
    ):
        # G5b: the wider pool fed the reranker; only its best few go on.
        _pre = _ids(scored)
        scored = scored[: read_cfg.rerank_keep]
        _cut_missing(_pre, scored, "rerank_keep", keep=read_cfg.rerank_keep)
    if len(scored) > final_k:
        # FU-RRPOOL: the wide pool was reranked; only the caller's top_k leave.
        _pre = _ids(scored)
        scored = scored[:final_k]
        _cut_missing(_pre, scored, "rerank_v2_search_pool", keep=final_k)
        tap.event("read.rerank_v2_search_pool", pool=top_k, kept=final_k)
    if scored and eng._config().read.record_access:
        # Reinforcement stats via the log (M1): last_accessed_at + access_count.
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.RETRIEVE,
                namespace=ns,
                actor="system",
                payload={"record_ids": [record.record_id for record, _ in scored]},
            )
        )
    if (fx := _FORENSICS.get()) is not None:
        fx["final"] = [(record.record_id, score) for record, score in scored]
    _log.info(EVENT_RETRIEVE, namespace=ns, query=True, count=len(scored))
    eng._record_reads(ns, session_id, scored)
    return scored


async def read(
    eng: Any,
    query: str,
    namespace: str = "default",
    mode: str | None = None,
    budget_tokens: int = constants.ASSEMBLE_BUDGET_TOKENS,
    top_k: int = constants.ASSEMBLE_TOP_K,
    replay_window: int = 2,
    compose_pool: int = 3,
    session_id: str | None = None,
    ledger_id: str | None = None,
    sessions: Sequence[str] | None = None,
    roles: Sequence[str] | None = None,
    memory_types: Sequence[str] | None = None,
    tags_any: Sequence[str] | None = None,
    focal_entity: str | None = None,
    purpose: str | None = None,
    include_passive: bool = False,
    *,
    valid_from_after: DateBound | None = None,
    valid_from_before: DateBound | None = None,
    valid_to_after: DateBound | None = None,
    valid_to_before: DateBound | None = None,
    recorded_after: DateBound | None = None,
    recorded_before: DateBound | None = None,
    date_filter_mode: str = "and",
    as_of: DateBound | None = None,
    viewer: str | None = None,
) -> ReadResult:
    """C7': mode-routed read. Rules decide; no model on the read path.

    ``mode=None`` uses ``read.default_mode`` (``auto`` unless a template pins one).

    - ``full``: every live, admitted record in the namespace, chronological,
      when it fits ``budget_tokens`` (else falls back to ``retrieve``);
    - ``replay``: :meth:`assemble`, then each retrieved episodic turn is
      expanded to ``replay_window`` neighbouring raw turns of its session
      (the hit first, then neighbours nearest first, skipping any that do
      not fit);
    - ``retrieve``: exactly :meth:`assemble`;
    - ``compose`` (H3): for aggregation questions. Pools ``compose_pool x top_k``
      candidates from the query and its core-terms probe (rank-fused with
      ``read.rrf_k``), then picks them session-diversely (the best hit of each
      session in turn) up to ``2 x top_k`` records and the budget,
      chronological. It abstains, applies the relative floor and drops near
      duplicates like assembly;
    - ``auto``: ``full`` if it fits; else ``compose`` for aggregation questions;
      else ``replay`` when episodic memory is on and a hit is episodic; else
      ``retrieve``.

    Every mode keeps ``read.reply_reserve_tokens`` free and gets the same
    presentation: relative-date annotation, the current-state view, the
    wrappers, dated render and gap markers.

    Full and replay honour the same gates as search: erased (DELETED),
    superseded, quarantined, grant and cue records never enter, and under
    ``integrity.enabled`` nothing below the admission threshold does (with
    live re-evaluation, judged on the effective trust); the instruction-flag
    and untrusted-note wrappers apply as in assembly.

    ``session_id`` keys the B0 read ledger (as in :meth:`assemble`), for the read
    headers and the routed read alike.

    ``purpose`` (#50): the read's purpose, checked under ``consent.enforce``.
    #53: PASSIVE-session records enter only with ``include_passive=True`` or when
    ``session_id`` names their session (see :meth:`search`).

    Date filters (#37): as in :meth:`search`, for every search of the read (the
    headers' included), the records a ``full`` read lists and the neighbours a
    replay or compose read adds. The pinned persona and the lead section are not
    dated evidence and are not filtered. No bound: unchanged.

    ``as_of`` (W7, plan v3.2): read the world as memory says it stood at that time
    (valid time): only records begun by then (unless the caller's own
    ``valid_from_before`` says otherwise), a fact superseded after it counts as the
    current one, and relative phrases in the question resolve against it. For
    "what did memory know then" add ``recorded_before``. None: unchanged.
    """
    session_id = ledger_id if ledger_id is not None else session_id  # I9
    if focal_entity:  # G-11: an extra graph seed for this read
        _FOCAL_ENTITY.set(focal_entity)
    plan_token = None
    if read_request.v2_flags_on(eng._config().read):  # R3-01/02/03, flag-off: skipped
        query, plan_state = await read_request.plan_read(
            eng,
            query,
            validate_namespace(namespace),
            viewer=viewer,
            budget_tokens=budget_tokens,
            as_of=as_of,
        )
        plan_token = read_request._READ_PLAN.set(plan_state)
    try:
        return await _read_planned(
            eng,
            query,
            namespace,
            mode,
            budget_tokens,
            top_k,
            replay_window,
            compose_pool,
            session_id,
            sessions,
            roles,
            memory_types,
            tags_any,
            purpose,
            valid_from_after,
            valid_from_before,
            valid_to_after,
            valid_to_before,
            recorded_after,
            recorded_before,
            date_filter_mode,
            as_of,
            viewer,
            include_passive=include_passive,
            focal_entity=focal_entity,
        )
    finally:
        if plan_token is not None:
            read_request._READ_PLAN.reset(plan_token)


async def _read_planned(
    eng: Any,
    query: str,
    namespace: str,
    mode: str | None,
    budget_tokens: int,
    top_k: int,
    replay_window: int,
    compose_pool: int,
    session_id: str | None,
    sessions: Sequence[str] | None,
    roles: Sequence[str] | None,
    memory_types: Sequence[str] | None,
    tags_any: Sequence[str] | None,
    purpose: str | None,
    valid_from_after: DateBound | None,
    valid_from_before: DateBound | None,
    valid_to_after: DateBound | None,
    valid_to_before: DateBound | None,
    recorded_after: DateBound | None,
    recorded_before: DateBound | None,
    date_filter_mode: str,
    as_of: DateBound | None,
    viewer: str | None,
    *,
    include_passive: bool = False,
    focal_entity: str | None = None,
) -> ReadResult:
    """:meth:`read` after the v2 request stage (R3-01/02/03); the body of the read."""
    if as_of is not None:
        moment = to_utc(as_of) + timedelta(microseconds=1)
        valid_from_before = valid_from_before if valid_from_before is not None else moment
    date_filter = DateFilter.build(
        valid_from_after=valid_from_after,
        valid_from_before=valid_from_before,
        valid_to_after=valid_to_after,
        valid_to_before=valid_to_before,
        recorded_after=recorded_after,
        recorded_before=recorded_before,
        mode=date_filter_mode,
    )
    with (
        read_scope(purpose) as outer,
        date_filter_scope(date_filter),
        as_of_scope(as_of),
        language_scope(eng._config().read.language_guard == "on"),  # I23
        record_scope(  # I7/I8/G-12, I53 viewer, I48/I52 bars
            RecordScope.build(
                sessions,
                roles,
                memory_types,
                tags_any,
                viewer=viewer,
                check=await eng._scope_check(query),
            )
        ),
    ):
        hit, cache_key = await read_request._cache_get(
            eng,
            query,
            namespace,
            viewer,
            session_id,
            (
                *(mode, budget_tokens, top_k, replay_window, compose_pool, purpose),
                *(include_passive, sessions, roles, memory_types, tags_any, focal_entity, as_of),
                *(date_filter_mode, valid_from_after, valid_from_before, valid_to_after),
                *(valid_to_before, recorded_after, recorded_before),
            ),
        )
        if hit is not None:
            result = hit
        else:
            result = None
            if eng._config().read.decompose_v2:  # R14-M-12 / OBS-10: map-reduce, flag-off skipped
                from memspine.read.pipeline.decompose import decomposed_read

                result = await decomposed_read(
                    eng,
                    query,
                    validate_namespace(namespace),
                    mode,
                    budget_tokens,
                    top_k,
                    replay_window,
                    compose_pool,
                    session_id,
                )
            if result is None:
                result = await eng._read(
                    query,
                    namespace,
                    mode,
                    budget_tokens,
                    top_k,
                    replay_window,
                    compose_pool,
                    session_id,
                )
            if eng._config().read.contract_rules:  # R3-02: the typed [contract] header
                result = ReadResult(
                    result.mode,
                    read_request.add_typed_header(
                        eng, query, validate_namespace(namespace), result.context
                    ),
                )
            result = ReadResult(result.mode, eng._consent_context(result.context))
            read_request._cache_put(eng, cache_key, result)
        if outer:
            ids = [r.record_id for r in result.context.records]
            await eng._audit_read("read", namespace, ids)
    return result


async def _read(
    eng: Any,
    query: str,
    namespace: str,
    mode: str | None,
    budget_tokens: int,
    top_k: int,
    replay_window: int,
    compose_pool: int,
    session_id: str | None,
) -> ReadResult:
    """:meth:`read` without the purpose scope and the read audit."""
    if mode is None:
        mode = eng._config().read.default_mode  # "auto" unless a template pins one
    if mode not in ("auto", "full", "replay", "retrieve", "compose"):
        raise ValueError(f"unknown read mode {mode!r}")
    eng._require_started()
    ns = validate_namespace(namespace)
    budget_tokens = eng._reply_budget(budget_tokens)
    # G1b/G3b: the read headers take their shares first; the routed read gets the
    # rest and leaves out what they carry, so nothing appears twice.
    strong = await eng._raw_evidence_strong(ns, query, top_k, session_id)
    headers = await eng._read_headers(ns, query, budget_tokens, session_id, strong=strong)
    # E3: a count question keeps room for the occurrences block, built afterwards
    # from what the routed read retrieved.
    count_share = eng._count_allowance(query, budget_tokens)
    headers, count_share = eng._cap_lead_blocks(headers, count_share, budget_tokens)
    # ADR-055 addendum: a gated question reads raw turns only, in every mode.
    gated = eng._cards_gated(query, strong=strong)
    routed_budget = budget_tokens - eng._headers_cost(headers) - count_share
    hide = eng._header_hide(headers, hide_facts=gated)
    result = await eng._read_routed(
        query,
        ns,
        mode,
        routed_budget,
        top_k,
        replay_window,
        compose_pool,
        hide=hide,
        full_hide=eng._header_hide(headers, all_facts=False, hide_facts=gated),
        session_id=session_id,
    )
    if eng._config().read.agentic:  # I67: opt-in; step 0 above is today's read
        result = await eng._agentic_extend(
            query, ns, result, routed_budget, hide=hide, session_id=session_id
        )
    if eng._config().read.event_ledger == "on":  # A02: opt-in, read-time only
        result = await eng._event_ledger_extend(query, ns, result, routed_budget)
    if eng._config().read.deep_v2 or eng._config().read.obligations:  # R3-12 / R3-16: opt-in
        result = await eng._deep_extend(
            query, ns, result, routed_budget, hide=hide, session_id=session_id
        )
    headers = eng._count_section(ns, query, result.context, count_share, headers)
    headers = eng._duration_section(ns, query, result.context, headers)
    gated = await eng._relevance_gate(
        query, ReadResult(result.mode, eng._attach_headers(result.context, headers)), ns
    )
    overlaid = await eng._read_overlay(query, ns, gated.context, session_id)
    overlaid = await eng._asset_overlay(query, ns, overlaid)  # E04, off by default
    overlaid = await eng._external_overlay(query, ns, overlaid)  # E03, off by default
    return ReadResult(gated.mode, overlaid)


async def _read_routed(
    eng: Any,
    query: str,
    ns: str,
    mode: str,
    budget_tokens: int,
    top_k: int,
    replay_window: int,
    compose_pool: int,
    *,
    hide: Callable[[MemoryRecord], bool] | None = None,
    full_hide: Callable[[MemoryRecord], bool] | None = None,
    session_id: str | None = None,
) -> ReadResult:
    """:meth:`read` after the reply reserve and the read headers: route and read.

    ``hide`` leaves out what the headers carry (with the cards header, every mined
    fact). ``full_hide`` (default ``hide``) is the narrower rule of a ``full`` read:
    it holds every live record, so only the records a header actually shows leave
    (A-4: the header shows ``cards_top_k`` facts, not all of them)."""
    storage = eng._require_started()
    if full_hide is None:
        full_hide = hide
    scale = eng._budget_factor(budget_tokens)
    if scale < 1.0:  # I20: a tight budget shrinks the candidate pool with the window
        top_k = max(1, round(top_k * scale))
    if mode in ("auto", "full"):
        live = []
        date_filter = active_date_filter()
        for record in await storage.list_records(ns):
            if full_hide is not None and full_hide(record):
                continue
            if date_filter is not None and not date_filter.matches(record):
                continue
            view = await eng._live_view(record)
            if view is not None:
                live.append(view)
        live.sort(key=chrono_key)
        decorated = await eng._decorate(
            ns, [(r, 0.0) for r in eng._inflate_all(live, ns)], hide=full_hide
        )
        records = [r for r, _ in decorated]
        cost = sum(estimate_tokens(r.content) for r in records)
        if records and cost <= budget_tokens:
            # N4: judged again on the rendered text; a full read that the dated
            # render pushes over budget falls back rather than being cut.
            full = eng._render(query, AssembledContext(records=records, tokens_used=cost))
            if full.tokens_used <= budget_tokens:
                return ReadResult("full", full)
        if mode == "full":
            mode = "retrieve"
    read_cfg = eng._config().read
    planner = read_cfg.planner
    routed = mode == "auto"
    probes: list[str] = []
    lookup_probes: list[str] = []
    plan: ReadPlan | None = None
    legs: list[list[LegHit]] = []
    if mode == "auto" and planner == "decision":
        mode = await eng._plan_read_mode(query) or mode
    elif mode == "auto" and planner == "llm":
        plan = await eng._llm_read_plan(query)
        if plan is not None:
            # G2a: lookup and replay both read by replay; aggregate by compose.
            mode = "compose" if plan.mode == "aggregate" else "replay"
            probes = list(plan.subqueries) if plan.mode == "aggregate" else []
            if plan.mode != "aggregate" and read_cfg.planner_version in ("v2", "v3"):
                # #35: a lookup's subqueries join its search as extra RRF legs.
                lookup_probes = list(plan.subqueries[: constants.PLAN_LOOKUP_PROBES])
            if read_cfg.planner_version == "v3":
                # #36: the plan's persons / time expression become a structured leg.
                legs = [await eng._person_time_leg(ns, plan)]
    if mode == "compose" or (mode == "auto" and is_aggregation(query)):
        # G11: a routed aggregation read pools more candidates (budget-capped).
        k = read_cfg.aggregate_top_k if routed and read_cfg.aggregate_top_k else top_k
        aggregate = (plan is not None and plan.mode == "aggregate") or (
            is_aggregation(query) or is_count(query)
        )
        if read_cfg.completeness_check and aggregate:
            return await eng._compose_checked(
                query,
                ns,
                budget_tokens,
                k,
                compose_pool,
                hide=hide,
                extra_probes=probes,
                replay_window=replay_window,
                session_id=session_id,
                legs=legs,
            )
        return await eng._compose(
            query,
            ns,
            budget_tokens,
            k,
            compose_pool,
            hide=hide,
            extra_probes=probes,
            replay_window=replay_window,
            session_id=session_id,
            legs=legs,
        )
    if (
        mode in ("replay", "auto")
        and read_cfg.aggregate_in_replay
        and read_cfg.aggregate_top_k
        and (is_aggregation(query) or is_count(query))
    ):
        # A1 (ADR-055): a list or count question read by replay pools more
        # candidates too; replay rendering, no compose. The budget still caps it.
        top_k = read_cfg.aggregate_top_k
    if read_cfg.list_mode and read_cfg.list_mode_trigger_v2:
        list_fire = await read_request._list_fires_v2(eng, ns, query)  # R14-M-1
    else:
        list_fire = await eng._list_fires(query)
    list_token = _LIST_MODE.set(list_fire)  # B1: the search widens for a list question
    try:
        base = await eng._assemble_core(
            query,
            ns,
            budget_tokens,
            top_k,
            session_id=session_id,
            hide=hide,
            probes=lookup_probes,
            legs=legs,
        )
    finally:
        _LIST_MODE.reset(list_token)
    episodic_hits = [r for r in base.records if r.memory_type == "episodic"]
    # H6: a mined atomic fact replays the source turn it best matches (its
    # derived_from lists the whole session, which would not fit the budget).
    for fact in (r for r in base.records if "atomic_fact" in r.tags and r.source.parents):
        source = await eng._best_source_turn(fact)
        if source is not None and all(source.record_id != h.record_id for h in episodic_hits):
            episodic_hits.append(source)
    if mode == "retrieve" or eng._episodic is None or not episodic_hits:
        return ReadResult("retrieve", eng._render(query, base, budget_tokens))
    sessions = await eng._episodic.sessions(ns, constants.SESSION_GAP_MINUTES)
    where = {rid: s for s in sessions for rid in s.record_ids}
    segment_of: dict[str, list[str]] = {}
    if eng._config().read.replay_topic_segments:
        # H15: restrict each replay window to the hit's topic segment. Only the
        # sessions holding a hit are segmented, from one batched listing.
        hit_sessions = {
            id(where[h.record_id]): where[h.record_id]
            for h in episodic_hits
            if h.record_id in where
        }
        if hit_sessions:
            by_id = {r.record_id: r for r in await storage.list_records(ns, "episodic")}
            for sess in hit_sessions.values():
                members = [by_id[i] for i in sess.record_ids if i in by_id]
                for segment in topic_segments(members):
                    ids_in = [r.record_id for r in segment]
                    for rid in ids_in:
                        segment_of[rid] = ids_in
    chosen: list[MemoryRecord] = [r for r in base.records if r.memory_type != "episodic"]
    seen = {r.record_id for r in chosen}
    used = sum(len(r.content) // 4 + 1 for r in chosen)
    best_window: set[str] = set()
    if read_cfg.replay_hits_first:
        # I20: every hit (best first) is admitted before any neighbour, so the best
        # evidence is never cut for the neighbours of a better-ranked hit.
        for rank, hit in enumerate(episodic_hits):
            cost = len(hit.content) // 4 + 1
            if hit.record_id in seen or used + cost > budget_tokens:
                continue
            chosen.append(hit)
            seen.add(hit.record_id)
            used += cost
            if rank == 0:
                best_window.add(hit.record_id)
    factor = eng._budget_factor(budget_tokens)
    for rank, hit in enumerate(episodic_hits):
        session = where.get(hit.record_id)
        ids = segment_of.get(hit.record_id) or (session.record_ids if session else [hit.record_id])
        at = ids.index(hit.record_id) if hit.record_id in ids else 0
        # B1 tier: hits past ``window_full_hits`` are single turns (no neighbours).
        single = (
            list_fire
            and read_cfg.window_full_hits is not None
            and rank >= read_cfg.window_full_hits
        )
        # The hit first, then its neighbours nearest first (older on a tie): a
        # neighbour that does not fit is skipped, it never costs the hit its place.
        for rid, turn, cost in await eng._window_turns(
            ns,
            ids,
            at,
            replay_window,
            budget_tokens=budget_tokens,
            used=used,
            seen=seen,
            hit=hit,
            single=single,
            factor=factor,
        ):
            chosen.append(turn)
            seen.add(rid)
            used += cost
            if rank == 0:
                best_window.add(rid)
        if read_cfg.reply_links and not single:
            # G27: a reply also shows the message it answers (gated, in budget).
            for answered in _reply_targets(hit):
                if answered in seen:
                    continue
                turn = await eng._replay_neighbour(answered, ns)
                if turn is None or turn.namespace != ns:
                    continue
                cost = len(turn.content) // 4 + 1
                if used + cost > budget_tokens:
                    continue
                chosen.append(turn)
                seen.add(answered)
                used += cost
                if rank == 0:
                    best_window.add(answered)
    if read_cfg.reference_edges == "on":
        used = await eng._attach_antecedents(
            ns, episodic_hits, chosen, seen, best_window, used, budget_tokens, read_cfg
        )
    stable = [r for r in chosen if r.memory_type != "episodic"]
    turns = sorted(
        (r for r in chosen if r.memory_type == "episodic"),
        key=chrono_key,
    )
    if read_cfg.evidence_first and best_window:
        # T10 (plan v3.2): the best hit's window opens the turns (time order inside),
        # then the other windows in time order.
        turns = [r for r in turns if r.record_id in best_window] + [
            r for r in turns if r.record_id not in best_window
        ]
    return ReadResult(
        "replay",
        eng._render(
            query,
            AssembledContext(
                records=[*stable, *turns],
                boundary_index=min(base.boundary_index, len(stable)),
                abstained=base.abstained,
                tokens_used=used,
                evidence=base.evidence,
            ),
            budget_tokens,
        ),
    )
