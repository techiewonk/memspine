"""Read path: gate moved out of the engine (F-10b)."""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from typing import Any

from memspine.compose.pack import AssembledContext
from memspine.core import trace_sink
from memspine.core.evidence import evidence_signal
from memspine.core.owner_check import first_person, referenced_names
from memspine.core.read_filters import active_as_of
from memspine.core.relevance_probes import OFF_TOPIC_PROBES, Calibration, calibrate
from memspine.exceptions import StorageError
from memspine.models.encoders.embedding.base import embed_queries
from memspine.models.providers.llm.structured import structured_call
from memspine.models.providers.rerank.base import concat_background
from memspine.observability.logging import get_logger, redact_error
from memspine.prompts.models import RelevanceLabels
from memspine.read.pipeline.state import (
    _FORENSICS,
    CUE_TAG,
    ReadResult,
    _json_line,
    _passive_hidden,
)
from memspine.read.pipeline.timeview import _current_at
from memspine.store.records import MemoryRecord, RecordStatus

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
        reranker = eng._rerank_provider()
        if reranker is not None:
            raws = await reranker.rerank(text, [concat_background(r) for r, _ in live])
            out["rerank"] = float(max(raws))
    return out


async def _store_calibration(
    eng: Any, ns: str, legs: Sequence[str]
) -> tuple[dict[str, Calibration], bool]:
    """The namespace's off-topic score level per leg, computed lazily on the first
    gated read and again when the store has grown past ``relevance_gate_regrow`` x the
    record count it was calibrated on. Returns (calibrations, recomputed)."""
    assert eng._embedder is not None
    read = eng._config().read
    n_records = len(await eng._require_started().list_records(ns))
    reranker = eng._rerank_provider() if "rerank" in legs else None
    key = (ns, eng._embedder.embedder_id, getattr(reranker, "reranker_id", None), tuple(legs))
    cache: dict[Any, dict[str, Calibration]] = eng.__dict__.setdefault("_relevance_cal", {})
    cached = cache.get(key)
    if cached and n_records <= read.relevance_gate_regrow * max(
        c.n_records for c in cached.values()
    ):
        return cached, False
    tops: dict[str, list[float]] = {leg: [] for leg in legs}
    for probe in OFF_TOPIC_PROBES:
        for leg, value in (await eng._gate_raw_tops(ns, probe, legs)).items():
            tops[leg].append(value)
    cal = {leg: calibrate(leg, v, n_records) for leg, v in tops.items() if v}
    cache[key] = cal
    return cal, True


async def _calibrated_gate(
    eng: Any, query: str, result: ReadResult, ns: str | None = None
) -> ReadResult:
    """I29/I37 (``relevance_gate: store_calibrated``): a message passes only when its raw
    top score exceeds the namespace's off-topic p95 by ``relevance_gate_margin_sd``
    standard deviations; otherwise an empty abstained context. Fails open."""
    read = eng._config().read
    if result.context.abstained or eng._embedder is None:
        return result
    records = [r for r in result.context.records if r.source.channel != "persona"]
    if not records:
        return result
    ns = ns or records[0].namespace
    entry: dict[str, Any] = {"margin_sd": read.relevance_gate_margin_sd}
    try:
        has_reranker = eng._rerank_provider() is not None
        want = read.relevance_gate_leg
        if want == "auto":
            legs = ["rerank"] if has_reranker else ["vector"]
        elif want == "any":
            legs = ["vector", "rerank"] if has_reranker else ["vector"]
        else:
            legs = [want] if (want == "vector" or has_reranker) else ["vector"]
        cal, recomputed = await eng._store_calibration(ns, legs)
        tops = await eng._gate_raw_tops(ns, query, legs)
        verdicts: dict[str, dict[str, Any]] = {}
        for leg, c in cal.items():
            if leg in tops:
                thr = c.threshold(read.relevance_gate_margin_sd)
                verdicts[leg] = {
                    "raw_top": tops[leg],
                    "threshold": thr,
                    "pass": tops[leg] > thr,
                    **c.as_meta(),
                }
        entry.update(legs=verdicts, recalibrated=recomputed)
        passed = not verdicts or any(v["pass"] for v in verdicts.values())
        entry["decision"] = "inject" if passed else "empty"
    except Exception as exc:  # an enhancer, never a gate: fail open
        _log.warning("read.relevance_gate_failed", error=redact_error(exc))
        entry.update(decision="inject", error=str(exc))
        passed = True
    if (fx := _FORENSICS.get()) is not None:
        fx["relevance_calibration"] = entry
        if not passed:
            for r in records:
                trace_sink.cut("relevance_gate_calibrated", r.record_id)
    return result if passed else ReadResult(result.mode, AssembledContext(abstained=True))


async def _gate_bypassed(eng: Any, query: str, ns: str | None, result: ReadResult) -> bool:
    """I74 (``read.relevance_gate_bypass``): whether the message references the store's own
    people / entities (``named``) or the speaker ("I", "my"; ``named_or_first_person``),
    in which case memory is needed and the relevance gate is skipped. Recorded in the
    forensics as ``relevance_bypass``. Fails closed to "no bypass"."""
    read = eng._config().read
    if read.relevance_gate == "off" or read.relevance_gate_bypass == "none":
        return False
    if result.context.abstained:
        return False
    ns = ns or next((r.namespace for r in result.context.records), None)
    entry: dict[str, Any] = {"mode": read.relevance_gate_bypass, "fired": False}
    try:
        names: list[str] = []
        if ns is not None:
            state = await eng._persp_state_for(ns)
            names = referenced_names(query, await eng._entity_vocabulary(ns), state["known"])
        if names:
            entry.update(fired=True, kind="named", names=names)
        elif read.relevance_gate_bypass == "named_or_first_person":
            tok = first_person(query)
            if tok:
                entry.update(fired=True, kind="first_person", names=[tok])
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("read.relevance_bypass_failed", error=redact_error(exc))
        entry["error"] = str(exc)
    if (fx := _FORENSICS.get()) is not None:
        fx["relevance_bypass"] = entry
    return bool(entry["fired"])


async def _relevance_gate(
    eng: Any, query: str, result: ReadResult, ns: str | None = None
) -> ReadResult:
    """I29 ``read.relevance_gate: store_calibrated``: the per-namespace calibrated raw-score
    gate (off by default; the decider form is retired, G-22).
    I74: a message that names the store's own people / entities skips the gate."""
    if await eng._gate_bypassed(query, ns, result):
        return result
    if eng._config().read.relevance_gate == "store_calibrated":
        return await eng._calibrated_gate(query, result, ns)
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
