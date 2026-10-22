from __future__ import annotations

from typing import Any

from memspine.compose.pack import ranked as rank_pairs
from memspine.config import constants
from memspine.exceptions import MemspineError, StorageError
from memspine.memories.procedural.knn import Exemplar, knn_vote, render_table
from memspine.memories.procedural.lessons import action_signature, failures_dominate, outcome_rank
from memspine.models.encoders.query.shape import content_words
from memspine.observability import tap
from memspine.observability.logging import EVENT_RETRIEVE, get_logger
from memspine.read.pipeline.fusion import _cosine, _jaccard
from memspine.services.embedding.base import embed_queries
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.store.scope import grant_allows, validate_namespace
from memspine.trust.pipeline import gates_v2

_log = get_logger(__name__)


async def classify(
    eng: Any,
    text: str,
    namespace: str = "default",
    group: str = "default",
    k: int = constants.KNN_VOTE_K,
) -> dict[str, Any]:
    """W17f + N29: the kNN label vote for ``text`` over the group's exemplars
    (BM25 and dense cosine fused by reciprocal rank, score-weighted vote).
    Returns ``label`` (None without a match), ``margin``, ``votes``, the voting
    ``exemplars`` (text, label, score), the ``label_table`` (label -> its most
    distinctive terms) and ``table_text``. The caller's reader may override."""
    eng._require_procedural()
    if eng._embedder is None:
        raise MemspineError("retrieval services not constructed — engine not started?")
    ns = validate_namespace(namespace)
    stored = [
        r
        for r in await eng._records(ns, "procedural", f"mapping:{group}")
        if eng._is_live_advisory(r, "mapping") and r.entity
    ]
    exemplars = [Exemplar(r.content, str(r.entity), r.record_id) for r in stored]
    dense: list[float] | None = None
    if exemplars:
        vectors = await eng._embedder.embed([text, *(e.text for e in exemplars)])
        dense = [_cosine(vectors[0], vec) for vec in vectors[1:]]
    result = knn_vote(text, exemplars, dense=dense, k=k)
    return {
        "label": result.label,
        "margin": result.margin,
        "votes": result.votes,
        "exemplars": [(e.text, e.label, score) for e, score in result.exemplars],
        "label_table": result.label_table,
        "table_text": render_table(result.label_table),
    }


async def recall_plan(
    eng: Any,
    task: str,
    namespace: str = "default",
    min_similarity: float = constants.PLAN_RECALL_MIN_SIMILARITY,
) -> MemoryRecord | None:
    """E6 plan cache lookup: the usable plan whose *task* embedding is most
    similar to ``task`` — or None when nothing clears the floor.

    Similarity is computed over the stored plans' task strings (their
    ``entity``), not plan bodies: two different tasks can share plan steps
    without being interchangeable.
    """
    eng._require_started()
    if eng._procedural is None:
        raise MemspineError("procedural memory not enabled — set memories.procedural.enabled: true")
    if eng._embedder is None:
        raise MemspineError("retrieval services not constructed — engine not started?")
    ns = validate_namespace(namespace)
    plans = [
        plan
        for plan in await eng._procedural.list(ns, kind="plan", usable_only=True)
        if plan.entity is not None
    ]
    if not plans:
        # Misses must be visible: "no plans recorded" vs "none cleared the
        # floor" is the first question when plan reuse isn't happening.
        _log.info(EVENT_RETRIEVE, namespace=ns, plan=True, hit=False, candidates=0)
        return None
    vectors = await eng._embedder.embed([task, *(str(plan.entity) for plan in plans)])
    task_vec, plan_vecs = vectors[0], vectors[1:]
    best: tuple[MemoryRecord, float] | None = None
    for plan, plan_vec in zip(plans, plan_vecs, strict=True):
        score = _cosine(task_vec, plan_vec)
        if best is None or score > best[1]:
            best = (plan, score)
    assert best is not None
    if best[1] < min_similarity:
        _log.info(
            EVENT_RETRIEVE,
            namespace=ns,
            plan=True,
            hit=False,
            candidates=len(plans),
            best_similarity=round(best[1], 4),
        )
        return None
    record = eng._inflate.inflate(best[0])
    # Reinforcement (M1): a reused plan's utility signal rides the log.
    await eng._append_and_project(
        MemoryEvent(
            kind=EventKind.RETRIEVE,
            namespace=ns,
            actor="system",
            payload={"record_ids": [record.record_id]},
        )
    )
    _log.info(EVENT_RETRIEVE, namespace=ns, plan=True, similarity=round(best[1], 4))
    return record  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


async def recall_plans(
    eng: Any,
    task: str,
    namespace: str = "default",
    k: int | None = None,
    min_similarity: float = constants.PLAN_RECALL_MIN_SIMILARITY,
    include_lessons: bool = False,
) -> list[tuple[MemoryRecord, float]]:
    """W17b: the top-``k`` usable plans for ``task`` (default
    ``memories.procedural.policies.recall_k``, else ``PLAN_RECALL_K``).

    Candidates clear ``min_similarity`` on the task embedding, are ranked by
    ``cos x (1 + helpful) / (1 + harmful)`` (N28; plain cosine with no
    outcomes), and plans whose failures dominate (at least
    ``OUTCOME_DEMOTE_MIN_HARMFUL`` harmful, more harmful than helpful) are
    pruned. One plan per action signature is kept (diversity). With
    ``include_lessons`` the live lessons compete in the same ranking. Each
    hit's use rides the log as a RETRIEVE."""
    procedural = eng._require_procedural()
    if eng._embedder is None:
        raise MemspineError("retrieval services not constructed — engine not started?")
    ns = validate_namespace(namespace)
    if k is None:
        k = int(eng._procedural_policy().get("recall_k") or constants.PLAN_RECALL_K)
    plans = [p for p in await procedural.list(ns, kind="plan", usable_only=True) if p.entity]
    candidates: list[tuple[MemoryRecord, float]] = []
    if plans:
        vectors = await eng._embedder.embed([task, *(str(p.entity) for p in plans)])
        candidates = [
            (plan, _cosine(vectors[0], vec)) for plan, vec in zip(plans, vectors[1:], strict=True)
        ]
        candidates = [(p, s) for p, s in candidates if s >= min_similarity]
    if include_lessons:
        candidates += await eng._similar_advisory(ns, task, "lesson", min_similarity)
    ranked = sorted(
        (
            (r, outcome_rank(s, r.scoring.likes, r.scoring.dislikes))
            for r, s in candidates
            if not failures_dominate(r.scoring.likes, r.scoring.dislikes)
        ),
        key=lambda pair: (-pair[1], pair[0].recorded_at),
    )
    picked: list[tuple[MemoryRecord, float]] = []
    signatures: set[str] = set()
    for record, score in ranked:
        signature = f"{record.attribute}:{action_signature(record.content)}"
        if signature in signatures:
            continue
        signatures.add(signature)
        picked.append((record, score))
        if len(picked) >= k:
            break
    _log.info(EVENT_RETRIEVE, namespace=ns, plan=True, hits=len(picked))
    if picked:
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.RETRIEVE,
                namespace=ns,
                actor="system",
                payload={"record_ids": [r.record_id for r, _ in picked]},
            )
        )
    return [(eng._inflate.inflate(r), s) for r, s in picked]


async def _similar_advisory(
    eng: Any, ns: str, query: str, kind: str, min_similarity: float
) -> list[tuple[MemoryRecord, float]]:
    """Live advisory records of ``kind`` by cosine of ``query`` to their trigger."""
    storage = eng._require_started()
    if eng._embedder is None:
        raise MemspineError("retrieval services not constructed — engine not started?")
    found = [
        r
        for r in await storage.list_records(ns, "procedural")
        if eng._is_live_advisory(r, kind) and r.entity
    ]
    if not found:
        return []
    vectors = await eng._embedder.embed([query, *(str(r.entity) for r in found)])
    out = [
        (record, _cosine(vectors[0], vec)) for record, vec in zip(found, vectors[1:], strict=True)
    ]
    return [(r, s) for r, s in out if s >= min_similarity]


async def _independent_roots(eng: Any, held: MemoryRecord, incoming: MemoryRecord) -> bool:
    """W13 (plan v3.2, ``integrity.corroboration_roots``): the corroborator's
    lineage roots must not overlap the held record's, nor any earlier
    corroborator's (read from the log). A summary of, or a re-statement derived
    from, the poison's own source turns is then not a second opinion."""
    mine = await eng._lineage_roots(incoming)
    if mine & await eng._lineage_roots(held):
        return False
    # N25 (plan v3.2, CPB): a near-copy of an earlier corroborator's wording is
    # the same source propagated, not a second opinion.
    words = content_words(incoming.content)
    storage = eng._require_started()
    after = 0
    while True:
        events = await storage.read_events(after_seq=after, limit=1000)
        if not events:
            return True
        for event in events:
            if (
                event.kind is not EventKind.DECAY_TRANSITION
                or event.payload.get("record_id") != held.record_id
            ):
                continue
            if mine & set(event.payload.get("roots") or []):
                return False
            earlier = set(event.payload.get("words") or [])
            if earlier and words and _jaccard(words, earlier) >= constants.COPY_JACCARD:
                return False
        after = max(event.seq for event in events if event.seq is not None)


def _observer_results(
    ns: str,
    candidates: list[tuple[MemoryRecord, float, float]],
    grants: Any,
    integrity: Any,
) -> list[tuple[MemoryRecord, float]]:
    """O4-11: the observer's view of the foreign hits ``candidates`` = (record, score, home trust).

    Each direct grant ``grantor -> ns`` becomes a LIB-21 grant per memory type it covers (horizon 1,
    kappa from ``integrity.kappa``/``edge_kappa``, which the library multiplies along the chain). A
    hit the view includes crosses at ``min(view trust, TRUST_RETRIEVED_CAP)`` (E1) or, under
    ``integrity.enabled``, at the view trust, still subject to the admission threshold; the rest are
    dropped with a reason code. The result is read-only: records are copies, nothing is written.
    """
    from memspine.derive import feedback as fb
    from memspine.trust.horizon import Grant

    types = {rec.memory_type for rec, _, _ in candidates}
    edges: list[Grant] = []
    for grantor, scope in sorted(grants.items()):
        kappa = integrity.kappa_for(grantor, ns)
        for mem_type in sorted(types if scope is None else types & scope):
            edges.append(Grant(grantor, ns, mem_type, "read", 1, kappa))
    observable = [
        fb.ObservableRecord(
            record_id=rec.record_id,
            owner=rec.namespace,
            scope=rec.memory_type,
            trust=home,
            text=rec.content or " ",
        )
        for rec, _, home in candidates
    ]
    items = {item.record_id: item for item in fb.observer_view(ns, observable, edges)}
    out: list[tuple[MemoryRecord, float]] = []
    excluded: dict[str, int] = {}
    for rec, score, _ in candidates:
        item = items[rec.record_id]
        view = item.trust
        reason = item.reason
        if item.included:
            if not integrity.enabled:
                view = min(view, constants.TRUST_RETRIEVED_CAP)
            elif not integrity.admits(view):
                reason = "below_threshold"  # type: ignore[assignment]  # CI-GREEN: pre-existing typing debt
        if not item.included or reason is not None:
            excluded[reason or "excluded"] = excluded.get(reason or "excluded", 0) + 1
            continue
        out.append(
            (
                rec.model_copy(update={"trust": view}),
                integrity.ranked(score, view) if integrity.enabled else score,
            )
        )
    if tap.enabled():
        tap.event(
            "read.observer_view",
            candidates=len(candidates),
            included=len(out),
            excluded=sum(excluded.values()),
            reasons=excluded,
            included_ids=[r.record_id for r, _ in out],
            hops=sorted({items[r.record_id].hops for r, _ in out}),
        )
    return out


async def _shared_search(
    eng: Any,
    query: str,
    namespace: str = "default",
    top_k: int = constants.SEARCH_TOP_K,
    session_id: str | None = None,
) -> list[tuple[MemoryRecord, float]]:
    """Own-namespace search plus granted foreign results (R2/E1).

    Foreign records are LIVE VIEWS into the grantor namespace — never
    copied (no second source of truth) — and are clearly marked by their
    differing ``record.namespace``. Their trust is capped at
    ``TRUST_RETRIEVED_CAP``: content crossing a grant is foreign, and its
    home-namespace trust must never ride along (E1). Quarantined or
    non-ACTIVATED records never cross, ``shared`` bookkeeping records
    never cross, and foreign reads append NO events — a reader must not
    mutate grantor state (no reinforcement across the boundary).
    """
    storage = eng._require_started()
    shared = eng._require_shared()
    if eng._embedder is None or eng._vector is None or eng._scoring is None:
        raise MemspineError("retrieval services not constructed — engine not started?")
    ns = validate_namespace(namespace)
    results = await eng.search(query, namespace=ns, top_k=top_k, session_id=session_id)
    grants = await shared.grants_to(ns)
    if not grants:
        return results  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt
    [query_vector] = await embed_queries(eng._embedder, [query])
    integrity = eng._integrity()
    # O4-11 (``memories.shared.policies.observer_views_v2``, off): foreign hits are judged by the
    # observer view (LIB-44 over the LIB-21 grant chain) instead of the per-edge attenuation below.
    observer_v2 = bool(eng._memory_policy(eng._config(), "shared").get("observer_views_v2"))
    candidates: list[tuple[MemoryRecord, float, float]] = []
    for grantor in sorted(grants):
        # SF-7/ADR-018: one grantor's broken vector index must not sink the
        # reader's own results or the other grantors' — contain per grantor.
        try:
            hits = await eng._vector.query(
                grantor, query_vector, embedder_id=eng._embedder.embedder_id, top_k=top_k
            )
            for hit in hits:
                record = await storage.get_record(hit.record_id)
                if (
                    record is None
                    or record.namespace != grantor
                    or record.status is not RecordStatus.ACTIVATED
                    or record.quarantined
                ):
                    continue  # same E1 gate as search — held content never crosses
                # The ONE enforcement point (ADR-016): scope + bookkeeping gate.
                if not grant_allows(ns, record.namespace, record.memory_type, grants):
                    continue
                try:
                    record = eng._inflate.inflate(record)
                except StorageError:
                    _log.warning(
                        "memory.inflate_failed", namespace=grantor, record_id=hit.record_id
                    )
                    continue
                score = eng._scoring.composite_score(record, relevance=hit.score)
                if observer_v2:
                    home = (
                        await eng.effective_trust(record.record_id)
                        if integrity.enabled and integrity.live_reevaluation
                        else record.trust
                    )
                    candidates.append((record, score, home))
                    continue
                if integrity.enabled:
                    # MTI read door: attenuate per grant edge, rank by
                    # score x view trust, admit only at >= threshold.
                    home = (
                        await eng.effective_trust(record.record_id)
                        if integrity.live_reevaluation
                        else record.trust
                    )
                    if gates_v2.gates_on(eng) and integrity.horizon_v2:
                        # W2-04 gate 4 over the T5-01 grant: a visible chain, attenuated trust
                        gate = gates_v2.read_item(
                            record.model_copy(update={"trust": min(1.0, max(0.0, home))}),
                            ns,
                            grants=[integrity.grant(grantor, ns)],
                            min_view_trust=integrity.admission_threshold,
                        )
                        if gate.action != "accept":
                            continue
                        view = gate.trust
                    else:
                        view = integrity.view_trust(home, grantor, ns)
                        if not integrity.admits(view):
                            continue
                    record = record.model_copy(update={"trust": view})
                    results.append((record, integrity.ranked(score, view)))
                    continue
                # E1: foreign content is retrieved content — trust-capped,
                # never its home-namespace trust.
                record = record.model_copy(
                    update={"trust": min(record.trust, constants.TRUST_RETRIEVED_CAP)}
                )
                results.append((record, score))
        except Exception as exc:
            _log.warning(
                "shared_search.grantor_failed",
                namespace=ns,
                grantor=grantor,
                error=str(exc),
                exc_info=True,
            )
    if observer_v2:
        results.extend(_observer_results(ns, candidates, grants, integrity))
    results = rank_pairs(results)
    # COR-2/ADR-018: the per-grantor loop appended up to top_k EACH — a final
    # truncation keeps the contract that shared_search returns at most top_k.
    results = results[:top_k]
    foreign_ids = [record.record_id for record, _ in results if record.namespace != ns]
    if integrity.enabled and foreign_ids:
        # G4: the reader-side exposure trail that makes blast radius
        # computable from the log — in the reader's namespace, never the
        # grantor's (reads must not mutate grantor state, E1).
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.EXPOSE,
                namespace=ns,
                actor="system",
                payload={"reader_namespace": ns, "record_ids": foreign_ids},
            )
        )
    _log.info(
        EVENT_RETRIEVE, namespace=ns, shared=True, grantors=sorted(grants), count=len(results)
    )
    eng._record_reads(ns, session_id, results)
    return results
