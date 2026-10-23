"""Compose: the windows part of context assembly, moved out of the engine (F-11)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from memspine.compose.pack import AssembledContext
from memspine.compose.pipeline.pack import bundle_selected
from memspine.compose.pipeline.render import _gisted, activate_counter, tok
from memspine.config import constants
from memspine.core.concentration import collapse_concentrated
from memspine.core.evidence import evidence_signal, second_round_probe
from memspine.core.language import language_scope
from memspine.core.read_filters import (
    DateBound,
    DateFilter,
    RecordScope,
    as_of_scope,
    date_filter_scope,
    record_scope,
    to_utc,
)
from memspine.derive.prepass import is_filler as _is_filler
from memspine.exceptions import MemspineError
from memspine.models.encoders.query.shape import (
    core_terms,
    feedback_terms,
    is_verbatim,
    split_intents,
    statement_form,
)
from memspine.models.encoders.query.temporal_query import LegHit
from memspine.observability import tap
from memspine.read import neighbours as nb
from memspine.read.pipeline.derived import redate_derived
from memspine.read.pipeline.request import tier_pool
from memspine.read.pipeline.state import (
    _FOCAL_ENTITY,
    _FORENSICS,
    _LIST_MODE,
    _RAW_SCORES,
    _RERANKED,
    ReadResult,
)
from memspine.store.records import MemoryRecord, chrono_key
from memspine.store.scope import validate_namespace
from memspine.trust.privacy import read_scope


async def assemble(
    eng: Any,
    query: str,
    namespace: str = "default",
    budget_tokens: int = constants.ASSEMBLE_BUDGET_TOKENS,
    top_k: int = constants.ASSEMBLE_TOP_K,
    shared: bool = False,
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
    as_of: DateBound | None = None,
    viewer: str | None = None,
) -> AssembledContext:
    """Retrieval + M12/E2 assembly: MMR-selected, cache-aware-ordered context.

    Persona and other stable records sit before ``boundary_index``; volatile
    episodic/working content after it — feed the prefix to provider caching.

    ``shared=True`` (G9) assembles over own + granted records via
    ``shared_search``, so a multi-agent turn sees exactly what its grants allow
    (trust-capped, and under ``integrity.*`` attenuated and admitted at theta).

    ``purpose`` (#50): the read's purpose, checked under ``consent.enforce``.
    #53: PASSIVE-session records enter only with ``include_passive=True`` or when
    ``session_id`` names their session (see :meth:`search`).

    ``as_of`` (W7): as in :meth:`read`.
    """
    session_id = ledger_id if ledger_id is not None else session_id  # I9
    if focal_entity:  # G-11: an extra graph seed for this read
        _FOCAL_ENTITY.set(focal_entity)
    as_of_filter = None
    if as_of is not None:
        moment = to_utc(as_of) + timedelta(microseconds=1)
        as_of_filter = DateFilter.build(valid_from_before=moment)
    with (
        read_scope(purpose) as outer,
        date_filter_scope(as_of_filter),
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
        context = await eng._assemble(
            query, namespace, budget_tokens, top_k, shared=shared, session_id=session_id
        )
        context = eng._consent_context(context)
        if outer:
            ids = [r.record_id for r in context.records]
            await eng._audit_read("assemble", namespace, ids)
    return context  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


async def _assemble(
    eng: Any,
    query: str,
    namespace: str,
    budget_tokens: int,
    top_k: int,
    *,
    shared: bool,
    session_id: str | None,
) -> AssembledContext:
    """:meth:`assemble` without the purpose scope and the read audit."""
    if eng._assembly is None:
        raise MemspineError("assembly policy not bound — engine not started?")
    counter = activate_counter(eng)  # R3-09a
    budget = eng._reply_budget(budget_tokens)
    ns = validate_namespace(namespace)
    strong = False if shared else await eng._raw_evidence_strong(ns, query, top_k, session_id)
    headers = (
        [] if shared else await eng._read_headers(ns, query, budget, session_id, strong=strong)
    )
    count_share = 0 if shared else eng._count_allowance(query, budget)
    headers, count_share = eng._cap_lead_blocks(headers, count_share, budget)
    inner = budget - eng._headers_cost(headers) - count_share
    pinned, lessons = ([], []) if shared else await eng._procedural_blocks(ns, query, session_id)
    inner = max(1, inner - sum(tok(r.content) for r in [*pinned, *lessons]))
    assembled = await eng._assemble_core(
        query,
        ns,
        inner,
        top_k,
        shared=shared,
        session_id=session_id,
        hide=eng._header_hide(headers, hide_facts=eng._cards_gated(query, strong=strong)),
    )
    rendered = eng._render(query, assembled, inner)
    if not shared:
        rendered = await eng._expand_trajectories(ns, rendered, inner)
    if pinned or lessons:
        rendered = eng._place_procedural(rendered, pinned, lessons)
    headers = eng._count_section(ns, query, rendered, count_share, headers)
    headers = eng._duration_section(ns, query, rendered, headers)
    final = await eng._read_overlay(
        query, ns, eng._attach_headers(rendered, headers), session_id, shared=shared
    )
    if tap.enabled():
        tap.event(
            "compose.budget",
            total=budget_tokens,
            reserves={"reply": budget_tokens - budget},
            headers=eng._headers_cost(headers),
            used=sum(tok(r.content) for r in final.records),
            accounting=counter.accounting if counter is not None else "estimated",
            tokenizer=counter.name if counter is not None else "estimate:len//4+1",
            exact_budget=counter is not None,
        )
    return final  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


async def _assemble_core(
    eng: Any,
    query: str,
    ns: str,
    budget_tokens: int,
    top_k: int,
    *,
    shared: bool = False,
    session_id: str | None = None,
    hide: Callable[[MemoryRecord], bool] | None = None,
    probes: Sequence[str] = (),
    legs: Sequence[list[LegHit]] = (),
) -> AssembledContext:
    """:meth:`assemble` without the reply reserve and the final render (callers
    apply both once, so the replay read can extend the context first).

    ``hide`` (G1b/G3b): candidates a read header already carries leave.
    ``probes`` (#35): extra search texts fused into the search as RRF legs.
    ``legs`` (#36): precomputed ranked legs fused into the search by RRF."""
    if eng._assembly is None:
        raise MemspineError("assembly policy not bound — engine not started?")
    counter = activate_counter(eng)  # R3-09a
    pool = eng._config().read.candidate_pool
    if _LIST_MODE.get():  # B1: a list question draws a wider pool
        pool = max(pool, eng._config().read.list_pool)
    want = top_k * pool
    # FU-RRPOOL (model.rerank.search_pool): the read path widens through the same key as
    # search(); _search fetches max(want, pool) before the rerank chain and cuts back to
    # ``want``.
    # 0 = off; a pool <= want changes nothing, so candidate_pool is never widened twice.
    rr_pool = eng._config().model.rerank.search_pool
    if eng._config().read.tiers_v2 and eng._config().model.rerank.chain:
        # FX-READPATH (R3-01 / R-13): the chosen tier sets the rerank pool (fast 32, standard 48,
        # deep 64), so ``tiers_v2`` now reaches the retrieval it was specified to govern.
        rr_pool = max(rr_pool, tier_pool())
    _RERANKED.set(False)
    _RAW_SCORES.set({} if eng._config().read.abstain_on_raw else None)
    if eng._config().read.statement_probe and (said := statement_form(query)):
        # N63 (EverMemOS multi-query, by rules): the question as a statement.
        probes = [*probes, said]
    if eng._config().read.multi_intent_split:
        # G34 (plan v3.2): each request of a multi-part question is its own probe.
        parts = split_intents(query)
        if len(parts) > 1:
            probes = [*probes, *parts]
    if shared:
        scored = await eng.shared_search(query, namespace=ns, top_k=want, session_id=session_id)
        if hide is not None:
            scored = [pair for pair in scored if not hide(pair[0])]
    else:
        # Smoke 2026-10-05 / A-1, A-2: records a header already shows (mined facts)
        # can outrank most raw turns. They leave inside the one search, before the
        # rerank and its ``rerank_keep`` cut, which widen until ``want`` survive.
        scored = await eng._search(
            query,
            ns,
            want,
            session_id=session_id,
            keep_k=top_k,
            hide=hide,
            probes=probes,
            fused_legs=legs,
            search_pool=rr_pool,
        )
        if eng._config().read.prf_expansion:
            # N03 (plan v3.2): words the first-round top hits share and the
            # question lacks become one more fused probe (pseudo-relevance feedback).
            extra = feedback_terms(query, [r.content for r, _ in scored[: constants.PRF_TOP_DOCS]])
            if extra:
                probes = [*probes, extra]
                scored = await eng._search(
                    query,
                    ns,
                    want,
                    session_id=session_id,
                    keep_k=top_k,
                    hide=hide,
                    probes=probes,
                    fused_legs=legs,
                    search_pool=rr_pool,
                )
        read_now = eng._config().read
        if (
            read_now.second_round
            and evidence_signal(query, scored, read_now.evidence_weak_below).weak
        ):
            # N04 (plan v3.2): weak evidence -> the names and dates the first hits
            # mention become a probe, over a doubled pool (one more local search).
            probe = second_round_probe(
                query, [r.content for r, _ in scored[: constants.SECOND_ROUND_TOP]]
            )
            if probe:
                probes = [*probes, probe]
                scored = await eng._search(
                    query,
                    ns,
                    want * 2,
                    session_id=session_id,
                    keep_k=top_k,
                    hide=hide,
                    probes=probes,
                    fused_legs=legs,
                    search_pool=rr_pool,
                )
                scored = scored[:want]
        if read_now.bridge_hop and scored and await eng._bridge_fires(query, read_now, scored):
            # R2-1: key noun phrases of the first-hop hits (not in the question) seed
            # one extra search; its hits join the final search as the "bridge" leg.
            bridge = await eng._bridge_leg(
                query,
                ns,
                scored,
                session_id=session_id,
                keep_k=top_k,
                hide=hide,
                probes=probes,
            )
            if bridge:
                legs = [*legs, bridge]
                scored = await eng._search(
                    query,
                    ns,
                    want,
                    session_id=session_id,
                    keep_k=top_k,
                    hide=hide,
                    probes=probes,
                    fused_legs=legs,
                    search_pool=rr_pool,
                )
        if eng._config().read.cluster_expand and scored:
            # N06 (plan v3.2): the vector neighbourhoods of the top hits (across
            # sessions) join the search as extra legs: the topic cluster around
            # the best evidence, not only what matches the question's wording.
            legs = [*legs, *await eng._cluster_legs(ns, scored, want)]
            scored = await eng._search(
                query,
                ns,
                want,
                session_id=session_id,
                keep_k=top_k,
                hide=hide,
                probes=probes,
                fused_legs=legs,
                search_pool=rr_pool,
            )
        # W19 (read.raw_turn_floor): a derived record (mined fact, card, summary)
        # takes a search slot a raw turn would have had, and in replay each lost
        # turn hit costs its whole window. Widen by the derived records found, so
        # the raw turns keep every slot they had without them.
        if eng._config().read.raw_turn_floor:
            wanted = want
            for _ in range(constants.RAW_TURN_FLOOR_MAX_WIDEN):
                derived = sum(1 for record, _ in scored if record.memory_type != "episodic")
                if want + derived <= wanted:
                    break
                wanted = want + derived
                scored = await eng._search(
                    query,
                    ns,
                    wanted,
                    session_id=session_id,
                    keep_k=top_k + derived,
                    hide=hide,
                    probes=probes,
                    fused_legs=legs,
                    search_pool=rr_pool,
                )
        cap = eng._config().read.session_cap
        if cap is not None:
            scored = await eng._session_capped(
                query,
                ns,
                len(scored),
                cap,
                session_id=session_id,
                hide=hide,
                probes=probes,
                legs=legs,
            )
    read_cfg = eng._config().read
    if read_cfg.concentration_filter:
        # N21 (plan v3.2): a dense near-duplicate cluster (a planted paraphrase
        # set) counts once, its kept member tagged ``concentrated:<n>``.
        scored = collapse_concentrated(scored, jaccard=read_cfg.concentration_jaccard)
    if read_cfg.facts_to_sources and scored:
        # N54 (EverMemOS episode/fact pairing, reversed): a mined fact gives its
        # slot to the source turn it came from, so facts never displace turns.
        scored = await eng._facts_to_sources(ns, scored)
    if read_cfg.type_quotas and scored:
        # N55 (EverMemOS per-source quotas): at most N records of each type.
        seen: dict[str, int] = {}
        capped: list[tuple[MemoryRecord, float]] = []
        for record, score in scored:
            quota = read_cfg.type_quotas.get(record.memory_type)
            if quota is not None and seen.get(record.memory_type, 0) >= quota:
                continue
            seen[record.memory_type] = seen.get(record.memory_type, 0) + 1
            capped.append((record, score))
        scored = capped
    if read_cfg.dedupe != "off" and scored:
        # I31: near-duplicates leave before assembly; the sink says what left.
        scored = await eng._dedupe_candidates(scored)
    integrity = eng._integrity()
    if integrity.enabled and integrity.trust_weighted_ranking and scored:
        # Scores are composite x view trust. Abstention (theta_abstain) judges
        # RELEVANCE, and trust already had its own gate (admission theta), so
        # rescale uniformly: the top candidate sits at its unweighted score,
        # the trust-aware ORDER is preserved.
        best = max(scored, key=lambda pair: pair[1])
        unweighted = best[1] / best[0].trust if best[0].trust > 0 else best[1]
        factor = unweighted / best[1] if best[1] > 0 else 1.0
        scored = [(record, score * factor) for record, score in scored]
    # W3: judged on the search's evidence, before the pinned persona joins.
    signal = (
        evidence_signal(query, scored, read_cfg.evidence_weak_below)
        if read_cfg.evidence_signal
        else None
    )
    # Persona is pinned context (E2): always a candidate, never query-gated,
    # but it passes the same status / quarantine / admission gates as any
    # record (a forgotten persona never comes back). It does not count as
    # evidence for abstention or the relative floor (AssemblyPolicy).
    storage = eng._require_started()
    for record in await storage.list_records(ns, "working"):
        if record.source.channel != "persona" or any(
            record.record_id == candidate.record_id for candidate, _ in scored
        ):
            continue
        live = await eng._live_view(record)
        if live is not None:
            inflated = eng._inflate_all([live], ns)
            if inflated:
                scored.append((inflated[0], 1.0))
    standing: list[MemoryRecord] = []
    timelines: list[MemoryRecord] = []
    if not shared and (read_cfg.topic_timelines or read_cfg.standing_instructions):
        standing, timelines = await eng._lead_section(ns, scored, budget_tokens)
    lead_cost = sum(tok(r.content) for r in [*standing, *timelines])
    if read_cfg.derived_dates and scored:
        scored = await redate_derived(eng, ns, query, scored)  # GTEMP
    scored = await eng._decorate(ns, scored, hide=hide)
    gist_after = read_cfg.gist_after
    if gist_after is not None and not is_verbatim(query):
        # G-22 (SimpleMem pyramid): the best ``gist_after`` hits keep their text;
        # the rest are shown as their one sentence most like the question, so the
        # budget holds more distinct evidence.
        scored = [
            (record if i < gist_after else _gisted(record, query), score)
            for i, (record, score) in enumerate(scored)
        ]
    # E5 (D-51): the compression policy's own master switch decides whether
    # the fit stage runs; with the default options this is a no-op.
    true_budget = max(1, budget_tokens - lead_cost)
    assembled = eng._assembly.assemble(
        scored,
        # R3-09b: bundles are packed under the true budget afterwards, from a wider pool.
        budget_tokens=(
            int(true_budget * read_cfg.bundle_pool_factor)
            if read_cfg.evidence_bundles
            else true_budget
        ),
        compression=eng._assembly_compression,
        apply_floor=not (eng._config().model.rerank.floor == "skip" and _RERANKED.get()),
        raw_scores=_RAW_SCORES.get() if read_cfg.abstain_on_raw else None,
        cost=tok if counter is not None else None,
        max_skips=read_cfg.exact_budget_max_skips if counter is not None else None,
    )
    if read_cfg.evidence_bundles and not assembled.abstained:
        assembled = await bundle_selected(eng, ns, assembled, true_budget)
    if read_cfg.abstain_on_raw and (fx := _FORENSICS.get()) is not None:
        raw_seen = _RAW_SCORES.get() or {}
        fx["abstain_on_raw"] = {
            "n_raw": len(raw_seen),
            "raw_top": max(raw_seen.values(), default=None),
            "abstained": assembled.abstained,
        }
    if standing or timelines:
        assembled = eng._place_lead(assembled, standing, timelines)
    assembled.evidence = signal
    if read_cfg.evidence_line and signal is not None and signal.weak and assembled.records:
        # W3 step 2 (plan v3.2): say so when the evidence is weak, instead of
        # letting the reader treat a near miss as an answer.
        note = eng._lead_record(ns, constants.WEAK_EVIDENCE_LINE, assembled.records[:3])
        note = note.model_copy(update={"tags": [constants.LEAD_TAG, constants.WEAK_EVIDENCE_TAG]})
        records = list(assembled.records)
        records.insert(min(assembled.boundary_index, len(records)), note)
        assembled.records = records
        assembled.tokens_used += tok(note.content)
    return assembled  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


async def _compose(
    eng: Any,
    query: str,
    ns: str,
    budget_tokens: int,
    top_k: int,
    pool: int,
    *,
    hide: Callable[[MemoryRecord], bool] | None = None,
    extra_probes: Sequence[str] = (),
    replay_window: int = 2,
    session_id: str | None = None,
    legs: Sequence[list[LegHit]] = (),
    rewrites: Sequence[str] | None = None,
) -> ReadResult:
    """H3: session-diverse, wider-recall read for aggregation questions.

    ``extra_probes`` (G2a: the LLM planner's subqueries) join the query, its
    core terms and the P4 rewrites; every probe's hits are rank-fused.
    ``legs`` (#36) are fused into the query's own search. ``rewrites`` (#38): the
    P4 rewrites already fetched for this query (None: fetch them here)."""
    assert eng._assembly is not None
    probes = [query]
    terms = core_terms(query)
    if terms and terms.lower() != query.lower():
        probes.append(terms)
    probes += await eng._query_rewrite_probes(query) if rewrites is None else list(rewrites)
    for probe in extra_probes:
        if probe.strip() and probe.strip().lower() not in (p.lower() for p in probes):
            probes.append(probe.strip())
    rrf_k = eng._config().read.rrf_k or constants.RRF_K
    fused: dict[str, float] = {}
    records: dict[str, MemoryRecord] = {}
    best_score: dict[str, float] = {}
    for probe in probes:
        fetch = max(1, top_k * pool)
        # A-1: what the headers show leaves inside the search, before the rerank
        # and its ``rerank_keep`` cut (the legs widen until ``fetch`` survive).
        hits = await eng._search(
            probe,
            ns,
            fetch,
            session_id=session_id,
            keep_k=fetch,
            hide=hide,
            fused_legs=legs if probe is query else (),
        )
        for rank, (record, score) in enumerate(hits, start=1):
            rid = record.record_id
            fused[rid] = fused.get(rid, 0.0) + 1.0 / (rrf_k + rank)
            records[rid] = record
            best_score[rid] = max(score, best_score.get(rid, score))
    # M12 / H4 on the pooled evidence, exactly as assembly judges it.
    pooled = [(records[rid], best_score[rid]) for rid in records]
    read_cfg = eng._config().read
    signal = (
        evidence_signal(query, pooled, read_cfg.evidence_weak_below)
        if read_cfg.evidence_signal
        else None
    )
    if eng._assembly.abstains(pooled):
        return ReadResult("compose", AssembledContext(abstained=True, evidence=signal))
    kept = {r.record_id for r, _ in eng._assembly.apply_floor(pooled)}
    decorated = {
        r.record_id: r
        for r, _ in await eng._decorate(
            ns, [(records[rid], 0.0) for rid in records], expand_claims=False
        )
    }
    ranked = sorted(
        (rid for rid in fused if rid in kept and rid in decorated),
        key=lambda rid: (-fused[rid], chrono_key(records[rid])),
    )
    sessions: dict[str, list[str]] = {}
    session_ids: dict[str, list[str]] = {}
    if eng._episodic is not None:
        for s in await eng._episodic.sessions(ns, constants.SESSION_GAP_MINUTES):
            for rid in s.record_ids:
                sessions.setdefault(rid, []).append(s.session_key)
                session_ids.setdefault(rid, s.record_ids)
    queues: dict[str, list[str]] = {}
    order: list[str] = []
    for rid in ranked:
        key = (sessions.get(rid) or [records[rid].group_id or rid])[0]
        if key not in queues:
            queues[key] = []
            order.append(key)
        queues[key].append(rid)
    chosen: list[MemoryRecord] = []
    chosen_raw: list[MemoryRecord] = []
    used = 0
    limit = 2 * top_k
    while len(chosen) < limit and any(queues[k] for k in order):
        for key in order:
            if not queues[key] or len(chosen) >= limit:
                continue
            rid = queues[key].pop(0)
            if eng._assembly.is_near_duplicate(records[rid], chosen_raw):
                continue  # H23: a near-duplicate wastes budget
            record = decorated[rid]
            cost = len(record.content) // 4 + 1
            if used + cost > budget_tokens:
                queues[key].clear()
                continue
            chosen.append(record)
            chosen_raw.append(records[rid])
            used += cost
    if read_cfg.compose_replay and replay_window > 0:
        used = await eng._expand_neighbours(
            ns, chosen, session_ids, replay_window, budget_tokens, used
        )
    chosen.sort(key=chrono_key)
    return ReadResult(
        "compose",
        eng._render(
            query,
            AssembledContext(records=chosen, tokens_used=used, evidence=signal),
            budget_tokens,
        ),
    )


async def _compose_checked(
    eng: Any,
    query: str,
    ns: str,
    budget_tokens: int,
    top_k: int,
    pool: int,
    *,
    hide: Callable[[MemoryRecord], bool] | None,
    extra_probes: Sequence[str],
    replay_window: int,
    session_id: str | None,
    legs: Sequence[list[LegHit]],
) -> ReadResult:
    """#38: a compose read, checked once for completeness (``read.completeness_check``).

    The first compose result goes to the ``sufficiency`` role (+1 call); when it is
    judged incomplete, ``sufficiency@missing`` writes up to
    :data:`constants.COMPLETENESS_MAX_QUERIES` missing-information queries (+1 call)
    and the compose read runs once more with them as extra probes. One round by default
    (``read.completeness_rounds``, G-17, allows up to 3);
    an abstained read, a complete verdict, no query or any failure keeps the first
    result. The P4 rewrites are fetched once for both reads."""
    rewrites = await eng._query_rewrite_probes(query)
    first = await eng._compose(
        query,
        ns,
        budget_tokens,
        top_k,
        pool,
        hide=hide,
        extra_probes=extra_probes,
        replay_window=replay_window,
        session_id=session_id,
        legs=legs,
        rewrites=rewrites,
    )
    if first.context.abstained or not first.context.records:
        return first  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
    result = first
    probes = list(extra_probes)
    # G-17 (SimpleMem multi-round reflection): up to ``read.completeness_rounds``
    # rounds; each adds the new missing-information queries and stops at the first
    # complete verdict (or when no new query appears).
    for _ in range(eng._config().read.completeness_rounds):
        missing = [
            q for q in await eng._missing_info_queries(query, result.context) if q not in probes
        ]
        if not missing:
            break
        probes = [*probes, *missing]
        result = await eng._compose(
            query,
            ns,
            budget_tokens,
            top_k,
            pool,
            hide=hide,
            extra_probes=probes,
            replay_window=replay_window,
            session_id=session_id,
            legs=legs,
            rewrites=rewrites,
        )
        if result.context.abstained or not result.context.records:
            return first  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
    return result  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


def _window_sides(eng: Any, window: int) -> tuple[int, int]:
    """The neighbour window as (turns before, turns after) a hit: ``window`` on both
    sides unless ``read.replay_window_before`` / ``replay_window_after`` override one."""
    read_cfg = eng._config().read
    before = read_cfg.replay_window_before
    after = read_cfg.replay_window_after
    return (window if before is None else before, window if after is None else after)


async def _expand_neighbours(
    eng: Any,
    ns: str,
    chosen: list[MemoryRecord],
    session_ids: dict[str, list[str]],
    window: int,
    budget_tokens: int,
    used: int,
) -> int:
    """G2c: add each chosen turn's +-``window`` session neighbours, in place.

    Hits are expanded in selection order, neighbours nearest first (older on a
    tie). A neighbour passes the same gates and decoration as in replay mode;
    one that does not fit the budget is skipped. Returns the new token count.
    """
    if eng._config().read.neighbours_v2:
        return await _expand_neighbours_v2(
            eng, ns, chosen, session_ids, window, budget_tokens, used
        )
    seen = {r.record_id for r in chosen}
    factor = eng._budget_factor(budget_tokens)
    for hit in list(chosen):
        ids = session_ids.get(hit.record_id)
        if not ids:
            continue
        at = ids.index(hit.record_id)
        for rid, turn, cost in await eng._window_turns(
            ns,
            ids,
            at,
            window,
            budget_tokens=budget_tokens,
            used=used,
            seen=seen,
            factor=factor,
        ):
            chosen.append(turn)
            seen.add(rid)
            used += cost
    return used


class _CharCounter:
    """The pipeline's own token estimate (chars // 4 + 1), as a LIB-24 counter."""

    name = "estimate:chars/4+1"
    accounting = "estimated"

    def count(self, text: str) -> int:
        return len(text) // 4 + 1


async def _expand_neighbours_v2(
    eng: Any,
    ns: str,
    chosen: list[MemoryRecord],
    session_ids: dict[str, list[str]],
    window: int,
    budget_tokens: int,
    used: int,
) -> int:
    """R3-10 (``read.neighbours_v2``, LIB-24): neighbour windows through the library.

    The chosen turns are the reranked hits (best first). Every turn inside a hit's reach is
    prefetched through ``eng._replay_neighbour`` (the viewer, consent, status and date
    gates), so the library's visibility predicate is exactly "the gates let it through".
    The neighbour share is what is left of ``budget_tokens``; neighbours join ``chosen``
    window by window, hit-adjacent turns nearest first within a window's conversation order."""
    factor = eng._budget_factor(budget_tokens)
    before, after = eng._window_sides(window)
    if factor < 1.0:
        before = max(1 if before else 0, round(before * factor))
        after = max(1 if after else 0, round(after * factor))
    hits_in: list[nb.Hit] = []
    convs: dict[str, list[str]] = {}
    visible: dict[str, MemoryRecord] = {r.record_id: r for r in chosen}
    probed: set[str] = set()
    for rank, hit in enumerate(chosen):
        ids = session_ids.get(hit.record_id)
        if not ids:
            continue
        conv = ids[0]
        at = ids.index(hit.record_id)
        hits_in.append(nb.Hit(hit.record_id, conv, 1.0 / (1 + rank)))
        for i in range(max(0, at - before), min(len(ids), at + after + 1)):
            rid = ids[i]
            if rid in visible or rid in probed:
                continue
            probed.add(rid)
            turn = await eng._replay_neighbour(rid, ns)
            if turn is not None:
                visible[rid] = turn
        convs.setdefault(conv, ids)
    index = {
        c: [nb.Turn(rid, c, visible[rid].content if rid in visible else "") for rid in ids]
        for c, ids in convs.items()
    }
    windows = nb.expand(
        hits_in,
        index,
        lambda t: t.turn_id in visible,
        before=before,
        after=after,
        budget=max(0, budget_tokens - used),
        counter=_CharCounter(),  # type: ignore[arg-type]  # CI-GREEN: pre-existing typing debt
    )
    seen = {r.record_id for r in chosen}
    added = 0
    for w in windows:
        for rid in w.neighbour_ids:
            if rid in seen:
                continue
            turn = visible[rid]
            chosen.append(turn)
            seen.add(rid)
            used += len(turn.content) // 4 + 1
            added += 1
    if tap.enabled():
        skipped: dict[str, int] = {}
        for w in windows:
            for sk in w.skipped:
                skipped[sk.reason] = skipped.get(sk.reason, 0) + 1
        tap.event(
            "compose.neighbours",
            windows=len(windows),
            merged=sum(len(w.anchor_ids) - 1 for w in windows),
            added=added,
            skipped=skipped,
            tokens=sum(w.neighbour_tokens for w in windows),
        )
    if (fx := _FORENSICS.get()) is not None:
        for w in windows:
            fx.setdefault("window", []).append(
                {
                    "anchor": w.anchor_id,
                    "neighbours": list(w.neighbour_ids),
                    "skipped": {sk.turn_id: sk.reason for sk in w.skipped},
                }
            )
    return used


async def _window_turns(
    eng: Any,
    ns: str,
    ids: list[str],
    at: int,
    window: int,
    *,
    budget_tokens: int,
    used: int,
    seen: set[str],
    hit: MemoryRecord | None = None,
    single: bool = False,
    factor: float = 1.0,
) -> list[tuple[str, MemoryRecord, int]]:
    """The turns of one hit's window to add: ``(record id, turn, token cost)``, hit
    first (when ``hit`` is given), then neighbours nearest first (older on a tie).

    ``turns`` unit (default): +-``window`` turns per side (:meth:`_window_sides`,
    scaled by ``factor`` under I20). ``tokens`` unit (I6): each side takes neighbours
    until its token allowance is used; a neighbour that would pass it closes the
    side. Either way a turn that is gated, already seen, or does not fit what is left
    of ``budget_tokens`` is skipped; it never costs the hit its place."""
    read_cfg = eng._config().read
    by_tokens = read_cfg.replay_window_unit == "tokens"
    allowance = {
        -1: int(read_cfg.replay_window_tokens_before * factor),
        1: int(read_cfg.replay_window_tokens_after * factor),
    }
    pre: dict[str, MemoryRecord | None] = {}  # neighbours fetched while sizing the window
    if single:
        order = [at]
    elif by_tokens:
        order = sorted(range(len(ids)), key=lambda i: (i != at, abs(i - at), i))
    else:
        before, after = eng._window_sides(window)
        if factor < 1.0:
            before = max(1 if before else 0, round(before * factor))
            after = max(1 if after else 0, round(after * factor))
        if read_cfg.neighbours_v2:
            # FX-READPATH (R3-10 in replay): filler neighbours (greetings, "ok", "haha") do not
            # take a window slot; the reach stretches over them, by at most as many turns
            # again as the side's quota. Off: the fixed -before/+after span below.
            span = []
            for step, quota in ((-1, before), (1, after)):
                taken_n, reach, i = 0, 0, at + step
                while 0 <= i < len(ids) and taken_n < quota and reach < 2 * quota:
                    reach += 1
                    turn_i = await eng._replay_neighbour(ids[i], ns)
                    pre[ids[i]] = turn_i
                    if turn_i is None or not _is_filler(turn_i.content):
                        span.append(i)
                        taken_n += 1
                    i += step
            span.append(at)
        else:
            span = range(max(0, at - before), min(len(ids), at + after + 1))  # type: ignore[assignment]  # CI-GREEN: pre-existing typing debt
        order = sorted(span, key=lambda i: (i != at, abs(i - at), i))
    spent = {-1: 0, 1: 0}
    closed: set[int] = set()
    taken: set[str] = set()
    out: list[tuple[str, MemoryRecord, int]] = []
    fx = _FORENSICS.get()
    skipped: dict[str, str] = {}
    for index in order:
        side = 0 if index == at else (-1 if index < at else 1)
        if by_tokens and side in closed:
            if closed >= {-1, 1}:
                break
            continue
        rid = ids[index]
        if rid in seen or rid in taken:
            if fx is not None:
                skipped[rid] = "already in the context"
            continue
        turn = hit if hit is not None and rid == hit.record_id else None
        if turn is None:
            turn = pre[rid] if rid in pre else await eng._replay_neighbour(rid, ns)
        if turn is None:
            if fx is not None:
                skipped[rid] = "not visible (gates: status, quarantine, consent, scope)"
            continue
        cost = len(turn.content) // 4 + 1
        if by_tokens and side != 0:  # noqa: SIM102
            if spent[side] + cost > allowance[side]:
                closed.add(side)
                if fx is not None:
                    skipped[rid] = "side token allowance used"
                continue
        if used + cost > budget_tokens:
            if fx is not None:
                skipped[rid] = f"over budget ({used}+{cost}>{budget_tokens})"
            continue
        if by_tokens and side != 0:
            spent[side] += cost
        taken.add(rid)
        out.append((rid, turn, cost))
        used += cost
    if fx is not None:
        fx.setdefault("window", []).append(
            {
                "anchor": ids[at],
                "neighbours": [rid for rid, _, _ in out if rid != ids[at]],
                "skipped": skipped,
            }
        )
    return out
