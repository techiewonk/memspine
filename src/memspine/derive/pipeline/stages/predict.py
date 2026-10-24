"""Derive stage: predict_calibrate (F-15)."""

from __future__ import annotations

from datetime import date, timedelta

from memspine.config import constants
from memspine.core.escaping import escape_markers
from memspine.core.event_date import happened_of, label_start, normalise_label
from memspine.core.policies.consolidation import ConsolidationPolicy
from memspine.derive.pipeline.stages.context import PipelineContext, _policy_options, live_records
from memspine.derive.pipeline.stages.mining import _fact_date
from memspine.derive.pipeline.stages.session_stage import _run_session_stage
from memspine.models.encoders.query.shape import content_words
from memspine.prompts.models import ExtractedFact
from memspine.store.records import MemoryRecord, RecordStatus


def _prompt_line(text: str) -> str:
    """#62: one prompt input as a single line: whitespace collapsed (a stored newline
    cannot open a new prompt section) and the engine's markers escaped."""
    return escape_markers(" ".join(text.split()))


def _label_span(label: str | None) -> tuple[date, date] | None:
    """#62: the inclusive days a date label (day, month, year or ``a..b``) denotes."""
    if not label:
        return None
    first = label_start(label)
    if first is None:
        return None
    if ".." in label:
        last = label_start(label.split("..", 1)[1])
        return (first, last) if last is not None and last >= first else None
    if len(label) == 4:  # a year
        return first, date(first.year, 12, 31)
    if len(label) == 7:  # a month
        nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        return first, nxt - timedelta(days=1)
    return first, first


def _record_span(record: MemoryRecord) -> tuple[date, date] | None:
    """#62: when a stored statement's event happened: its ``happened:`` label, else
    the day of its ``valid_from``."""
    return _label_span(happened_of(record) or record.valid_from.date().isoformat())


def _in_memory(fact: ExtractedFact, known: list[MemoryRecord]) -> bool:
    """#62 (ADR-049): is ``fact`` already in memory? One known statement must cover
    its content words (``PREDICT_CALIBRATE_COVERED``) and, for an event with a date,
    happen on an overlapping date: the same event on another date is a new
    occurrence (a repeat camping trip still counts for "how many times")."""
    words = content_words(fact.value)
    if not words:
        return True
    need = constants.PREDICT_CALIBRATE_COVERED
    kind = getattr(fact, "kind", "event") or "event"
    span = None if kind == "state" else _label_span(normalise_label(fact.date))
    for record in known:
        if len(words & content_words(record.content)) / len(words) < need:
            continue
        if span is not None:
            other = _record_span(record)
            if other is None or other[1] < span[0] or span[1] < other[0]:
                continue
        return True
    return False


async def _known_statements(
    ctx: PipelineContext, namespace: str, members: list[MemoryRecord]
) -> list[MemoryRecord]:
    """#62: what memory already holds, for the prediction and the coverage check:
    live semantic records at or above ``PREDICT_CALIBRATE_KNOWN_MIN_TRUST`` (a
    low-trust record pre-stating a fact must not suppress the true one) not derived
    from this session's turns (no summaries, cues or list cards), the best lexical
    overlap with the session first, ``PREDICT_CALIBRATE_KNOWLEDGE_K``."""
    member_ids = {m.record_id for m in members}
    session_words = content_words(" ".join(m.content for m in members))
    scored: list[tuple[int, str, MemoryRecord]] = []
    for record in await live_records(ctx, namespace, "semantic"):
        if (
            record.status is not RecordStatus.ACTIVATED
            or record.quarantined
            or record.instruction_flag
            or record.trust < constants.PREDICT_CALIBRATE_KNOWN_MIN_TRUST
            or record.source.channel == "consolidation"
            or constants.CUE_TAG in record.tags
            or constants.LIST_CARD_TAG in record.tags
            or member_ids & set(record.source.parents)
        ):
            continue
        overlap = len(content_words(record.content) & session_words)
        scored.append((-overlap, record.record_id, record))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [record for _, _, record in scored[: constants.PREDICT_CALIBRATE_KNOWLEDGE_K]]


async def predict_calibrate(ctx: PipelineContext) -> dict[str, object]:
    """#62 (Nemori predict-calibrate, research-grade, ADR-049): store what is new.

    Per consolidated session, once (done marker): the ``predict_episode`` role
    predicts the session's facts from what memory already holds plus the session's
    opening; the ``calibrate`` role compares the transcript with the prediction and
    the known statements and returns the facts memory does not hold yet (a fact the
    prediction guessed right is still new). A returned fact is dropped only when it
    is already IN MEMORY (:func:`_in_memory`: a known statement at or above the
    trust floor covers it, on an overlapping date for a dated event); a predicted
    line never suppresses storage. The rest go through the write door as derived
    semantic facts whose parents are the session's turns (erasure cascades, trust
    capped at the turns). Every prompt input is collapsed to single lines with the
    engine's markers escaped.
    """
    policy = ConsolidationPolicy.bind(_policy_options(ctx, "episodic", "consolidation"))
    if not getattr(policy.options, "predict_calibrate", False):
        return {"status": "skipped", "reason": "consolidation.predict_calibrate is off"}
    if ctx.predict_episode is None or ctx.calibrate is None or ctx.deposit_surprise is None:
        return {"status": "skipped", "reason": "no predict_episode/calibrate LLM role bound"}
    predictor, calibrator, deposit = ctx.predict_episode, ctx.calibrate, ctx.deposit_surprise

    async def work(namespace: str, key: str, members: list[MemoryRecord]) -> tuple[int, list[str]]:
        known = await _known_statements(ctx, namespace, members)
        knowledge = [_prompt_line(r.content) for r in known]
        opening = members[0]
        cue = _prompt_line(opening.content)[: constants.PREDICT_CALIBRATE_CUE_CHARS]
        prediction = await predictor(cue, f"{opening.valid_from:%Y-%m-%d}", knowledge)
        predicted = "\n".join(
            line for line in (_prompt_line(x) for x in prediction.splitlines()) if line
        )
        transcript = "\n".join(
            f"[{m.valid_from:%Y-%m-%d}] {_prompt_line(m.content)}" for m in members
        )
        surprises = await calibrator(predicted, transcript, knowledge)
        parents = [m.record_id for m in members]
        written = 0
        errors: list[str] = []
        for fact in surprises:
            text = f"{fact.entity} {fact.attribute}: {fact.value}"
            if not fact.value.strip() or _in_memory(fact, known):
                continue  # already in memory: not new
            kind = getattr(fact, "kind", "event") or "event"
            when = _fact_date(getattr(fact, "date", None), members[-1].valid_from)
            try:
                stored = await deposit(
                    namespace,
                    text,
                    fact.entity or None,
                    fact.attribute or None,
                    parents,
                    when or opening.valid_from,
                    key,
                    kind=kind,
                )
            except Exception as exc:  # one bad fact must not lose the rest
                errors.append(f"{namespace}:{key}: surprise deposit failed: {exc}")
                continue
            if stored is None:
                continue  # I72: a parent turn was forgotten meanwhile; skipped, audited
            written += 1
        return written, errors

    return await _run_session_stage(ctx, "predict_calibrate", "surprises", work)
