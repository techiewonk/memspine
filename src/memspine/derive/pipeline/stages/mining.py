"""Derive stages: mine_facts and project_facts (F-15)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any

from memspine.config import constants
from memspine.core.event_date import (
    anchor_turn,
    cited_turns,
    happened_label,
    normalise_label,
    resolve_happened,
)
from memspine.core.fact_chain import fact_projection_on, validate_assertions
from memspine.core.policies.consolidation import ConsolidationPolicy
from memspine.derive.pipeline.stages.context import DateFacts, PipelineContext, _policy_options
from memspine.derive.pipeline.stages.session_stage import _run_session_stage
from memspine.memories.episodic.sessions import topic_segments
from memspine.models.encoders.query.temporal_resolve import WeekMode
from memspine.observability.logging import get_logger
from memspine.prompts.models import ExtractedFact
from memspine.store.records import MemoryRecord
from memspine.workers.list_cards import derive_list_cards

_log = get_logger(__name__)


def _fact_date(value: str | None, latest: datetime | None = None) -> datetime | None:
    """H2: YYYY-MM-DD / YYYY-MM / YYYY from a mined fact, as an aware datetime.

    R2-7: the date is an LLM output, so it is range-checked. A year before
    ``MINED_FACT_MIN_YEAR`` or a date past ``latest`` (the session's last turn,
    default now) plus ``MINED_FACT_FUTURE_SLACK_DAYS`` yields None, and the
    caller falls back to the session start. Unchecked, ``9999-12-31`` would be
    the newest statement on its key and win every later conflict.
    """
    if not value:
        return None
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(value.strip(), fmt).replace(tzinfo=UTC)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    ceiling = (latest or datetime.now(UTC)) + timedelta(days=constants.MINED_FACT_FUTURE_SLACK_DAYS)
    if parsed.year < constants.MINED_FACT_MIN_YEAR or parsed > ceiling:
        return None
    return parsed


def _mining_transcript(segment: list[MemoryRecord], numbered: bool) -> str:
    """The miner's transcript: ``[YYYY-MM-DD] turn`` lines, numbered ``[n]`` (1-based)
    when the miner may cite its evidence turns (#29)."""
    lines = [f"[{m.valid_from:%Y-%m-%d}] {m.content}" for m in segment]
    if not numbered:
        return "\n".join(lines)
    return "\n".join(f"[{n}] {line}" for n, line in enumerate(lines, 1))


@dataclass
class _Happened:
    """#29: one mined fact's happened date: the label, the event time a
    deterministic resolution gives (None: keep the H2 event time), and the time the
    fact was said (the anchor of its relative phrases; tagged ``said:<date>``)."""

    said: datetime
    label: str | None = None
    start: datetime | None = None


def _happened(
    fact: ExtractedFact, segment: list[MemoryRecord], numbered: bool, week: WeekMode
) -> _Happened:
    """#29: deterministic first. A relative phrase in the fact's statement, resolved
    against the cited turn it was said in (else the latest cited turn), or the
    segment's day when it has only one; else in its cited turns (each against its own
    date); else the miner's own ``date``.

    The said time is that anchor, else the latest cited turn, else the segment's last
    turn. A span starting after the said day is a plan, not an event: it keeps its
    label but never becomes the event time (a future ``valid_from`` would win every
    later conflict on its key).
    """
    cited = cited_turns(segment, getattr(fact, "turns", [])) if numbered else []
    days = {m.valid_from.date() for m in segment}
    turn = anchor_turn(fact.value, cited, week=week)
    anchor = (
        turn.valid_from if turn is not None else (segment[0].valid_from if len(days) == 1 else None)
    )
    said = anchor or segment[-1].valid_from
    span = resolve_happened([(fact.value, anchor)], week=week) if anchor is not None else None
    if span is None and cited:
        span = resolve_happened(((m.content, m.valid_from) for m in cited), week=week)
    if span is not None:
        start = datetime.combine(span[0], time(), tzinfo=UTC)
        past = span[0] <= said.date()
        return _Happened(said, happened_label(*span), start if past else None)
    label = normalise_label(getattr(fact, "date", None))
    if label is not None and _fact_date(label.split("..", 1)[0], segment[-1].valid_from) is None:
        label = None  # R2-7: out of range, as for the event time
    return _Happened(said, label)


async def _fill_dates(
    dater: DateFacts,
    transcript: str,
    mined: list[ExtractedFact],
    happened: list[_Happened],
    latest: datetime,
) -> None:
    """#29: one call dates the facts no rule or miner did; a failure leaves them undated.

    R2-7: a date past ``latest`` (the session's last turn) plus the slack is dropped."""
    missing = [i for i, h in enumerate(happened) if h.label is None]
    if not missing:
        return
    try:
        dates = await dater(transcript, [mined[i].value for i in missing])
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("mine_facts.date_fill_failed", error=str(exc))
        return
    for position, index in enumerate(missing, 1):
        label = normalise_label(dates.get(position))
        if label is not None and _fact_date(label.split("..", 1)[0], latest) is not None:
            happened[index].label = label


async def mine_facts(ctx: PipelineContext) -> dict[str, object]:
    """C6': mine atomic, dated facts from each consolidated session, once.

    Idempotent through a per-session ``stage_done`` marker in the log. The
    transcript given to the miner carries each turn's event date, so relative
    dates ("yesterday") can be resolved to absolute ones. Raw turns are kept:
    facts are an index over them, not a replacement (M6 views).
    """
    policy = ConsolidationPolicy.bind(_policy_options(ctx, "episodic", "consolidation"))
    options = policy.options
    if not getattr(options, "mine_facts", False):
        return {"status": "skipped", "reason": "consolidation.mine_facts is off"}
    if ctx.mine_facts is None or ctx.deposit_fact is None:
        return {"status": "skipped", "reason": "no extract LLM role bound"}
    miner, deposit = ctx.mine_facts, ctx.deposit_fact
    by_topic = bool(getattr(options, "mine_by_topic", False))
    # W5: the rule miner always cites its line, so a fact's parent is its own turn.
    numbered = bool(getattr(options, "mine_evidence_turns", False)) or (
        getattr(options, "miner", "llm") == "rules"
    )
    event_dates = bool(getattr(options, "mine_event_dates", False))
    dater = ctx.date_facts if getattr(options, "mine_event_dates_llm", False) else None
    week = ctx.config.read.relative_week
    multiview = bool(getattr(options, "mine_multiview", False))

    async def work(namespace: str, key: str, members: list[MemoryRecord]) -> tuple[int, list[str]]:
        # H15: one call per topic segment. Every call runs before any deposit, so a
        # failed call retries the whole session next cycle without duplicates.
        segments = topic_segments(members) if by_topic else [members]
        batches = []
        for segment in segments:
            transcript = _mining_transcript(segment, numbered)
            mined = await miner(transcript)
            happened: list[_Happened] = []
            if event_dates:
                happened = [_happened(fact, segment, numbered, week) for fact in mined]
                if dater is not None:  # #29: one batched call fills the undated facts
                    await _fill_dates(dater, transcript, mined, happened, segment[-1].valid_from)
            batches.append((segment, mined, happened))
        written = 0
        errors: list[str] = []
        for segment, mined, happened in batches:
            start, latest = segment[0].valid_from, segment[-1].valid_from
            for index, fact in enumerate(mined):
                text = f"{fact.entity} {fact.attribute}: {fact.value}"
                when = _fact_date(getattr(fact, "date", None), latest) or start
                # #29: a cited fact's parents are its evidence turns, not the segment.
                cited = cited_turns(segment, getattr(fact, "turns", [])) if numbered else []
                parents = [m.record_id for m in (cited or segment)]
                # G1a: only a state is keyed by (entity, attribute) and may supersede;
                # the deposit drops an event's attribute, so the ladder ADDs it.
                kind = getattr(fact, "kind", "event") or "event"
                extra: dict[str, Any] = {}
                if multiview:  # #28: the view fields ride along as tags
                    extra.update(persons=list(fact.persons), location=fact.location)
                    extra["topic"] = fact.topic
                if happened and happened[index].label:
                    h = happened[index]
                    extra["happened"] = str(h.label)
                    extra["said"] = f"{h.said:%Y-%m-%d}"
                    when = h.start or when
                    if when.date() > h.said.date():
                        when = h.said  # a plan is not the newest statement on its key
                try:
                    stored = await deposit(
                        namespace,
                        text,
                        fact.entity or None,
                        fact.attribute or None,
                        parents,
                        when,
                        key,
                        kind=kind,
                        **extra,
                    )
                except Exception as exc:  # one bad fact must not lose the rest
                    errors.append(f"{namespace}:{key}: deposit failed: {exc}")
                    continue
                if stored is None:
                    continue  # I72: a parent turn was forgotten meanwhile; skipped, audited
                written += 1
        return written, errors

    stats = await _run_session_stage(ctx, "mine_facts", "facts", work)
    if getattr(options, "list_cards", False) and stats.get("status") != "skipped":
        # #30: the list cards follow the facts, every cycle (a forgotten fact
        # re-derives its card even when no new session was mined).
        stats["list_cards"] = await derive_list_cards(ctx)
    return stats


def projection_on(ctx: PipelineContext) -> bool:
    """E01: ``memories.semantic.policies.fact_projection`` is ``on`` (a bad value is a
    config error, raised at engine start by the builder before any cycle runs)."""
    mem = ctx.config.memories.get("semantic")
    return fact_projection_on(mem.policies if mem is not None else {})


async def project_facts(ctx: PipelineContext) -> dict[str, object]:
    """E01 write-time arm: project atomic subject-relation-object assertions from each
    consolidated session, once, as a REBUILDABLE index over the raw turns.

    Reuses the mined-fact path end to end: the deposit is ``deposit_fact`` (firewall, ladder,
    trust capped at the source turn, the I72 parent-liveness re-check under the namespace
    lock), the per-session done marker, and the ``atomic_fact`` tag, so cards, ``facts_to_sources``
    and the forget cascade treat a projected assertion like any mined fact. Each assertion has
    exactly ONE parent, the turn whose text contains its quoted span (an assertion whose span
    is not in its line is dropped); a negated, hypothetical or uncertain assertion is not
    projected; every projected fact is an event (never supersedes anything). It is tagged
    ``fact_projection`` and ``projected:<session>`` and is archived with its parent when the
    parent is forgotten, so it never outlives it and never crosses a namespace."""
    if not projection_on(ctx):
        return {"status": "skipped", "reason": "memories.semantic.policies.fact_projection is off"}
    if ctx.project_assertions is None or ctx.deposit_fact is None:
        return {"status": "skipped", "reason": "no extract LLM role bound"}
    project, deposit = ctx.project_assertions, ctx.deposit_fact

    async def work(namespace: str, key: str, members: list[MemoryRecord]) -> tuple[int, list[str]]:
        raw = await project(_mining_transcript(members, True))
        valid = validate_assertions(raw, [m.content for m in members])
        written = 0
        errors: list[str] = []
        for a in valid:
            if not a.joinable:
                continue
            parent = members[a.line - 1]
            when = f" [{a.time}]" if a.time else ""
            try:
                stored = await deposit(
                    namespace,
                    f"{a.subject} {a.relation}: {a.object}{when}",
                    a.subject,
                    a.relation,
                    [parent.record_id],
                    parent.valid_from,
                    f"proj:{key}",
                    kind="event",
                    extra_tags=[constants.FACT_PROJECTION_TAG, f"projected:{key}"],
                )
            except Exception as exc:  # one bad assertion must not lose the rest
                errors.append(f"{namespace}:{key}: deposit failed: {exc}")
                continue
            if stored is not None:  # None: I72, the parent turn was forgotten meanwhile
                written += 1
        return written, errors

    return await _run_session_stage(ctx, "project_facts", "projected", work)
