"""Read path: gate moved out of the engine (F-10b)."""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from typing import Any

from memspine.compose.pack import AssembledContext, estimate_tokens
from memspine.config import constants
from memspine.core import trace_sink
from memspine.core.evidence import evidence_signal
from memspine.core.owner_check import first_person, referenced_names
from memspine.core.read_filters import active_as_of
from memspine.core.relevance_probes import OFF_TOPIC_PROBES
from memspine.exceptions import StorageError
from memspine.observability import tap
from memspine.observability.logging import get_logger, redact_error
from memspine.prompts.models import RelevanceLabels
from memspine.read.contract_rules import build_contract as build_typed_contract
from memspine.read.gate import (
    GateConfig,
    GateContract,
    GateItem,
    ProbeObservation,
    StoreCalibration,
    assess,
)
from memspine.read.pipeline.state import (
    _FORENSICS,
    CUE_TAG,
    ReadResult,
    _json_line,
    _passive_hidden,
)
from memspine.read.pipeline.timeview import _current_at
from memspine.services.embedding.base import embed_queries
from memspine.services.llm.structured import structured_call
from memspine.services.rerank.base import concat_background, gated_rerank
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.trust.pipeline import consent as trust_consent
from memspine.trust.pipeline import gates

_log = get_logger(__name__)


async def _gate_hits(
    eng: Any,
    ns: str,
    ranked: list[tuple[str, float]],
    group_id: str | None,
    tags: list[str] | None,
    memory_type: str | None = None,
) -> list[tuple[MemoryRecord, float]]:
    """The E1 / EI-1 / C8' / D2 gates of :meth:`search`, in ranked order."""
    storage = eng._require_started()
    candidates: list[tuple[MemoryRecord, float]] = []
    tracing = _FORENSICS.get() is not None
    gates_on = gates.gates_on(eng)
    if eng._config().consent.enabled:
        await trust_consent.ensure_registry(eng)
    for record_id, relevance in ranked:
        record = await storage.get_record(record_id)
        if record is None or (
            record.status is not RecordStatus.ACTIVATED and not _current_at(record, active_as_of())
        ):
            if tracing:
                trace_sink.cut(
                    "status_or_missing", record_id,
                    status=None if record is None else str(record.status),
                )  # fmt: skip
            # Only live facts reach a context window: DELETED/QUARANTINED are
            # excluded (E1), and ARCHIVED/superseded history never surfaces as
            # current truth — a promoted-then-superseded record cannot re-enter.
            # W7: under an as-of read, a fact superseded AFTER that time was
            # the current one then, and is admitted.
            continue
        if record.quarantined:
            if tracing:
                trace_sink.cut("quarantine", record_id)
            continue  # defense in depth: quarantined never reaches assembly
        if not eng._consent_ok(record):
            if tracing:
                trace_sink.cut("consent_purpose", record_id)
            continue  # #50: the record's purposes do not allow this read
        if gates_on and gates.read_item(record, ns).action != "accept":
            if tracing:
                trace_sink.cut("gate4", record_id)
            continue  # W2-04: the read gate (own namespace: quarantine and trust)
        if record.memory_type == "shared":
            # EI-1: grant/subscription bookkeeping is authorization state, not
            # memory content — it must never occupy retrieval slots (shared_search
            # already hides foreign ones; this hides the reader's own).
            if tracing:
                trace_sink.cut("shared_bookkeeping", record_id)
            continue
        if CUE_TAG in record.tags:
            # C8': a cue is a key, never content. Off, cues are invisible; on,
            # a trusted cue resolves to its live target, which then passes the
            # same gates as any other hit.
            read_cfg = eng._config().read
            if not read_cfg.anticipatory_cues or record.trust < read_cfg.cue_min_trust:
                if tracing:
                    trace_sink.cut("cue_not_content", record_id)
                continue
            parents = record.source.parents
            target = await storage.get_record(parents[0]) if parents else None
            if (
                target is None
                or target.status is not RecordStatus.ACTIVATED
                or target.quarantined
                or not eng._consent_ok(target)
            ):
                if tracing:
                    trace_sink.cut("cue_target_unavailable", record_id)
                continue
            record = target
        # D2 sub-scoping gate: narrow to a group and/or records carrying all tags.
        if group_id is not None and record.group_id != group_id:
            if tracing:
                trace_sink.cut("group_filter", record_id, group=group_id)
            continue
        if _passive_hidden(record, group_id):
            if tracing:
                trace_sink.cut("passive_session", record_id)
            continue  # #53: an archived (PASSIVE) session stays out of default reads
        if tags and not set(tags).issubset(record.tags):
            if tracing:
                trace_sink.cut("tag_filter", record_id)
            continue
        if memory_type is not None and record.memory_type != memory_type:
            if tracing:
                trace_sink.cut("memory_type_filter", record_id)
            continue
        try:
            record = eng._inflate.inflate(record)  # cold-tier content restored (M6)
        except StorageError:
            # One corrupt cold-tier row must not take down the whole query.
            _log.warning("memory.inflate_failed", namespace=ns, record_id=record.record_id)
            continue
        candidates.append((record, relevance))
    return candidates


async def _gate_raw_tops(eng: Any, ns: str, text: str, legs: Sequence[str]) -> dict[str, float]:
    """I29: the top RAW score of ``text`` against the namespace's live records, per leg
    (``vector``: cosine of the best live hit; ``rerank``: the reranker's raw score over
    the best vector candidates). Read-only: no events, no ledger, no forensics of its own.
    Probes and real messages both go through here, so the two are always comparable."""
    assert eng._embedder is not None
    read = eng._config().read
    [qv] = await embed_queries(eng._embedder, [text])
    hits = await eng._vector_leg(ns, qv, read.relevance_gate_candidates)
    live = await eng._gate_hits(ns, [(h.record_id, h.score) for h in hits], None, None)
    if not live:
        return {}
    out: dict[str, float] = {}
    if "vector" in legs:
        out["vector"] = max(score for _, score in live)
    if "rerank" in legs:
        reranker = await eng._rerank_provider()
        if reranker is not None:
            raws = await gated_rerank(reranker, text, [concat_background(r) for r, _ in live])
            scored = [float(s) for s in raws if s is not None]  # a None is unscored (R3-07)
            if scored:
                out["rerank"] = max(scored)
    return out


async def _relevance_gate(
    eng: Any, query: str, result: ReadResult, ns: str | None = None
) -> ReadResult:
    """Apply the current five-state gate when the optional policy is enabled."""
    if eng._config().read.gate:
        return await _gate(eng, query, result, ns)
    return result


async def _raw_evidence_strong(
    eng: Any, ns: str, query: str, top_k: int, session_id: str | None
) -> bool:
    """F4 (plan v3.2, ``read.cards_when_weak``): True when the raw turns alone give
    strong evidence (the W3 signal of a raw-only search is not ``weak``), so the
    cards header and the mined facts stay out of the read. One extra local search,
    only with ``read.cards: header`` and the key on; otherwise False, no search."""
    read_cfg = eng._config().read
    if read_cfg.cards != "header" or not read_cfg.cards_when_weak:
        return False
    raw = await eng._search(
        query,
        ns,
        top_k,
        session_id=session_id,
        keep_k=top_k,
        hide=lambda record: record.memory_type != "episodic",
    )
    return not evidence_signal(query, raw, read_cfg.evidence_weak_below).weak


async def _relevance_filter(
    eng: Any, query: str, candidates: list[tuple[MemoryRecord, float]]
) -> list[tuple[MemoryRecord, float]]:
    """H17: drop only candidates the ``relevance`` role labels irrelevant.

    The ``relevance_safety_net`` best-scored candidates are always kept. Any failure,
    or no bound role, leaves the candidates unchanged (an enhancer, never a gate).
    """
    if eng._llm is None or eng._prompts is None or "relevance" not in eng._llm.roles:
        return candidates
    keep_n = eng._config().read.relevance_safety_net
    ranked = sorted(range(len(candidates)), key=lambda i: candidates[i][1], reverse=True)
    safe = set(ranked[:keep_n])
    # #10: each note is one JSON object on one line (quotes, newlines and line
    # separators escaped) inside markers carrying a fresh nonce, so stored text
    # can neither start a forged label line nor close the notes block.
    shown = eng._remote_view("relevance", [r for r, _ in candidates])  # #50
    notes = "\n".join(
        _json_line({"index": i, "text": r.content[:400]}) for i, r in enumerate(shown)
    )
    try:
        result = await structured_call(
            eng._llm.for_role("relevance"),
            eng._prompts.select("relevance"),
            {"question": query, "notes": notes, "nonce": secrets.token_hex(6)},
            RelevanceLabels,
        )
    except Exception as exc:
        _log.warning("read.relevance_filter_failed", error=redact_error(exc))
        return candidates
    drop = {item.index for item in result.labels if item.label.strip().lower() == "irrelevant"}
    return [pair for i, pair in enumerate(candidates) if i in safe or i not in drop]


# --------------------------------------------------------------------------------------------
# R3-11 (``read.gate``): the LIB-12 five-state gate, scored per candidate record.
# --------------------------------------------------------------------------------------------

_TIME_VIEW_KINDS = {"current": "current", "as_of": "as_of", "history": "history"}


def _gate_contract(query: str, named: bool, supported: bool) -> GateContract:
    """Map the typed query contract (LIB-16 ``TypedContract``) onto the LIB-12 ``GateContract``:
    its personal flag, request kind and time view; ``named`` is decided by the caller.

    A bypass must be backed by evidence: enumeration / show-all requests always bypass, but an
    as-of / history request bypasses only when ``supported`` (some candidate scores above the
    weak threshold); otherwise it is read as a plain lookup, so a time phrase alone ("in 1998")
    can never wave unrelated memories through."""
    typed = build_typed_contract(query)
    kind = typed.request_kind
    view = _TIME_VIEW_KINDS.get(typed.time_view.kind, "current")
    if not supported:
        if kind == "as_of":
            kind = "lookup"
        view = "current"
    return GateContract(
        personal=typed.personal,
        request_kind=kind,
        named_reference=named,
        time_view=view,  # type: ignore[arg-type]
    )


async def _gate_leg(eng: Any) -> str:
    read = eng._config().read
    has_reranker = await eng._rerank_provider() is not None
    want = read.relevance_gate_leg
    if want in ("auto", "any"):
        return "rerank" if has_reranker else "vector"
    return want if (want == "vector" or has_reranker) else "vector"


async def _gate_calibration(eng: Any, ns: str, leg: str) -> tuple[StoreCalibration, bool]:
    """The store's off-topic level for ``leg`` as a LIB-12 calibration (probe tops kept as
    samples); cached per namespace/embedder/reranker and recomputed once the store has grown
    past ``relevance_gate_regrow`` x the record count it was calibrated on."""
    read = eng._config().read
    n_records = len(await eng._require_started().list_records(ns))
    reranker = await eng._rerank_provider() if leg == "rerank" else None
    key = (ns, eng._embedder.embedder_id, getattr(reranker, "reranker_id", None), leg)
    cache: dict[Any, tuple[StoreCalibration, int]] = eng.__dict__.setdefault("_gate_cal", {})
    cached = cache.get(key)
    if cached and n_records <= read.relevance_gate_regrow * max(cached[1], 1):
        return cached[0], False
    observations = []
    for probe in OFF_TOPIC_PROBES:
        top = (await eng._gate_raw_tops(ns, probe, [leg])).get(leg)
        observations.append(ProbeObservation(query=probe, scorer=leg, top_score=top))
    cal = StoreCalibration.from_observations(observations)
    cache[key] = (cal, n_records)
    return cal, True


async def _gate_scores(
    eng: Any, ns: str, query: str, records: Sequence[MemoryRecord], leg: str
) -> dict[str, float]:
    """The raw ``leg`` score of each candidate record (absent = unscored, never 0)."""
    read = eng._config().read
    if leg == "rerank":
        reranker = await eng._rerank_provider()
        raws = await gated_rerank(reranker, query, [concat_background(r) for r in records])
        return {r.record_id: float(s) for r, s in zip(records, raws, strict=True) if s is not None}
    [qv] = await embed_queries(eng._embedder, [query])
    n = max(read.relevance_gate_candidates, 4 * len(records))
    hits = await eng._vector_leg(ns, qv, n)
    return {h.record_id: float(h.score) for h in hits}


def _gate_item(record: MemoryRecord, score: float | None, leg: str) -> GateItem:
    keyed = bool(record.entity and record.attribute and record.memory_type == "semantic")
    return GateItem(
        item_id=record.record_id,
        score=score,
        scorer=leg,
        personal=True,  # every record of a memory store is about the user or their people
        key=f"{record.entity}|{record.attribute}" if keyed else None,
        value=record.content if keyed else None,
        current=record.status is RecordStatus.ACTIVATED and record.valid_to is None,
    )


async def _gate(eng: Any, query: str, result: ReadResult, ns: str | None = None) -> ReadResult:
    """R3-11 (``read.gate``): classify the read's candidate records against the store's
    calibrated off-topic level (LIB-12) and act on the state.

    * ``missing``: an empty, abstained context (a recorded absence, ER-23).
    * ``weak``: ``read.gate_weak`` (``keep``: pass, flagged low-confidence; ``abstain``).
    * ``conflicting`` / ``answerable``: the context stays whole minus records the gate dropped
      (a personal record on a generic question); conflicts are recorded, nothing is resolved.
    * ``bypass``: enumeration / show-all / as-of / history requests, and a named reference
      that is real: the name resolves in the store AND a candidate scores above the weak
      threshold. An unknown name never bypasses.

    Lead/header blocks and persona records are not judged and ride along. Fails open."""
    read = eng._config().read
    if result.context.abstained or eng._embedder is None:
        return result
    records = [r for r in result.context.records if r.source.channel != "persona"]
    judged = [r for r in records if constants.LEAD_TAG not in r.tags]
    if not judged:
        return result
    ns = ns or judged[0].namespace
    entry: dict[str, Any] = {"margin_sd": read.relevance_gate_margin_sd}
    try:
        leg = await _gate_leg(eng)
        cal, recomputed = await _gate_calibration(eng, ns, leg)
        scores = await _gate_scores(eng, ns, query, judged, leg)
        items = [_gate_item(r, scores.get(r.record_id), leg) for r in judged]
        cfg = GateConfig(
            margin_sd=read.relevance_gate_margin_sd,
            min_samples=min(5, max(1, len(OFF_TOPIC_PROBES))),
        )
        plain = assess(items, cal, contract=_gate_contract(query, False, False), config=cfg)
        supported = any(a.band in ("strong", "weak") for a in plain.assessments)
        names: list[str] = []
        first: list[str] = []
        if read.relevance_gate_bypass != "none":
            state = await eng._persp_state_for(ns)
            names = referenced_names(query, await eng._entity_vocabulary(ns), state["known"])
            if read.relevance_gate_bypass == "named_or_first_person" and not names:
                tok = first_person(query)
                first = [tok] if tok else []
        # A name bypasses only when it is real: resolved in the store (referenced_names only
        # returns names the store knows) AND hit, a candidate scores above the weak threshold
        # or mentions the name. A first-person reference needs a scored candidate.
        folded = [n.casefold() for n in names]
        mentioned = read.gate_named == "hit" and any(
            n in r.content.casefold() for r in judged for n in folded
        )
        real = (bool(names) and (supported or mentioned)) or (bool(first) and supported)
        decision = assess(items, cal, contract=_gate_contract(query, real, supported), config=cfg)
        names = names or first
        entry.update(
            leg=leg,
            state=decision.state,
            reason=decision.reason,
            bypass_reason=decision.bypass_reason,
            names=names,
            name_resolved_with_evidence=real,
            recalibrated=recomputed,
        )
    except Exception as exc:  # an enhancer, never a gate: fail open
        _log.warning("read.gate_failed", error=redact_error(exc))
        tap.event("read.gate.decision", state="error", error=redact_error(exc))
        return result
    dropped = set(decision.dropped_ids)
    keep = decision.state in ("answerable", "conflicting", "bypass") or (
        decision.state == "weak" and read.gate_weak == "keep"
    )
    entry["decision"] = "inject" if keep else "empty"
    if tap.enabled():
        tap.event(
            "read.gate.decision",
            state=decision.state,
            leg=leg,
            thresholds={
                a.item_id: [a.weak_threshold, a.strong_threshold] for a in decision.assessments[:20]
            },
            scores={a.item_id: [a.score, a.band] for a in decision.assessments[:20]},
            admitted=list(decision.admitted_ids),
            dropped=list(decision.dropped_ids),
            conflicts=[{"key": c.key, "ids": list(c.item_ids)} for c in decision.conflicts],
            bypass_reason=decision.bypass_reason,
            low_confidence=decision.low_confidence,
            decision=entry["decision"],
        )
    else:
        tap.event("read.gate.decision", state=decision.state, decision=entry["decision"])
    if (fx := _FORENSICS.get()) is not None:
        fx["relevance_gate"] = {
            **entry,
            "conflicts": [c.model_dump() for c in decision.conflicts],
            "low_confidence": decision.low_confidence,
        }
        if not keep:
            for r in records:
                trace_sink.cut("relevance_gate", r.record_id)
    if not keep:
        return ReadResult(result.mode, AssembledContext(abstained=True))
    if not dropped:
        return result
    context = result.context
    kept = [r for r in context.records if r.record_id not in dropped]
    return ReadResult(
        result.mode,
        AssembledContext(
            records=kept,
            boundary_index=min(context.boundary_index, len(kept)),
            abstained=False,
            tokens_used=sum(estimate_tokens(r.content) for r in kept),
            evidence=context.evidence,
        ),
    )
