"""Derive: advisory prospective and procedural memory over stored records (O4-10, LIB-28).

Adapters from today's records to the pure libraries ``memory.prospective`` / ``memory.procedural``,
and the two sweeps the sleep cycle runs behind their flags:

* ``memories.prospective.policies.v2`` : stated plans (records tagged ``mod:plan``) become
  intentions with a time window or a cue; later facts close them (done / cancelled); old ones expire;
  the ones worth surfacing now are reminders. Advisory only: nothing is delivered or acted on.
* ``memories.procedural.policies.v2`` : repeated how-to skills and plans of one name become a
  procedure with versions; its counts change only on *explicit* like / dislike feedback (a feedback
  event whose actor is not the engine itself); the engine's own outcome-receipt likes are implicit and
  are refused and counted.

Both outputs are a pure function of the source records (and, for procedures, the FEEDBACK events on
them). They are recomputed, not stored: a stored copy would be a second source of truth that goes
stale when a source is forgotten or erased (G-08). So nothing is written here and there is no lineage
to record; every result carries its source ids (``parents`` / ``source_ids``) and a trust that is at
most the least trusted source. Everything is flag-off by default.

Layering: this module sits in ``derive`` and imports the libraries from ``memory``; ``memory`` never
imports ``derive``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol

from memspine.config import constants
from memspine.config.schema import MemspineConfig
from memspine.memory.procedural import (
    HowTo,
    ImplicitFeedbackError,
    Outcome,
    Procedure,
    Suggestion,
    build_procedures,
    suggest,
    update,
)
from memspine.memory.prospective import (
    Intention,
    ProspectiveRules,
    Reminder,
    derive_intentions,
    detect_resolution,
    due,
    expire,
)
from memspine.observability import tap
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.assessment import Assessment, Support
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import Interval, TemporalRef, TimeAxes
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus, SkillStage

__all__ = [
    "ProceduralReport",
    "ProspectiveReport",
    "build_record_procedures",
    "procedural_report",
    "procedural_v2_on",
    "prospective_report",
    "prospective_v2_on",
    "suggest_for",
]

#: Memory types that are bookkeeping or other mechanisms, never the source of a plan or a later fact.
_SKIP_TYPES = frozenset({"prospective", "shared", "procedural", "resource", "working"})
#: Perspective modalities that are neither a plan nor a plain statement (they close nothing).
_NON_FACT_MODS = frozenset({"hypo", "wish", "opinion", "question", "request"})
#: Explicit feedback signals that move a procedure's counts (a note carries no result).
_RESULT_OF = {"like": "success", "dislike": "failure"}
#: Procedures need this many agreeing sources before they exist (library default).
MIN_REPEATS = 2


class _Storage(Protocol):
    async def list_records(
        self, namespace: str, memory_type: str | None = None
    ) -> list[MemoryRecord]: ...

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]: ...


def _policy_on(config: MemspineConfig, memory_type: str, key: str) -> bool:
    mem = config.memories.get(memory_type)
    return bool(mem is not None and mem.enabled and mem.policies.get(key))


def prospective_v2_on(config: MemspineConfig) -> bool:
    """``memories.prospective.policies.v2``: advisory intentions and reminders (O4-10)."""
    return _policy_on(config, "prospective", "v2")


def procedural_v2_on(config: MemspineConfig) -> bool:
    """``memories.procedural.policies.v2``: advisory procedures from repeated how-tos (O4-10)."""
    return _policy_on(config, "procedural", "v2")


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


# ------------------------------------------------------------------------------------------ prospective


def _vals(tags: Iterable[str], prefix: str) -> list[str]:
    n = len(prefix)
    return [t[n:] for t in tags if t.startswith(prefix) and len(t) > n]


def _modality(record: MemoryRecord) -> tuple[str, bool] | None:
    """(library modality, negated) for a record, or ``None`` when it is neither plan nor statement."""
    mods = set(_vals(record.tags, "mod:"))
    negated = "pol:neg" in record.tags
    if mods & _NON_FACT_MODS:
        return None
    if "plan" in mods:
        return None if "fact" in mods else ("planned", negated)
    return ("negated", False) if negated else ("asserted", False)


def _subject(record: MemoryRecord) -> tuple[str, str]:
    """(subject surface, speaker): the first named ``sub:`` tag, else the speaker, else ``user``."""
    spk = (_vals(record.tags, "spk:") or ["user"])[0]
    for s in _vals(record.tags, "sub:"):
        if s not in ("self", "you", "3p") and "@" not in s:
            return s, spk
    return spk, spk


def _day(text: str) -> datetime | None:
    try:
        d = date.fromisoformat(text)
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def _window(record: MemoryRecord) -> Interval | None:
    """The plan's ``due_from`` / ``due_to`` day tags as an event interval (precision day)."""
    start = _day((_vals(record.tags, "due_from:") or [""])[0])
    end = _day((_vals(record.tags, "due_to:") or [""])[0])
    if start is not None and end is not None and end < start:
        start = None
    if start is None and end is None:
        return None

    def ref(at: datetime) -> TemporalRef:
        return TemporalRef(at=at, precision="day", basis="explicit")

    return Interval(
        start=ref(start) if start is not None else None, end=ref(end) if end is not None else None
    )


#: F12.9: the trigger of a cue-based plan. "next time I'm at the pharmacy" is a place; "when Bram
#: calls" an event. The clause ends at a comma, a sentence end, or the "I will" that starts the action.
_CUE_END = (
    r"(?=\s*(?:,|;|\.|$|\b(?:i|we)\s+(?:will|'ll|shall|should|must|need to|am going to|have to)\b))"
)
_LOCATION_CUE = re.compile(
    r"\b(?:when(?:ever)?|next time|once)\s+(?:i am|i'm|we are|we're|i get|we get)\s+(?:at|to|in)\s+"
    r"(?P<cue>(?:the\s+)?[^,;.]+?)" + _CUE_END,
    re.IGNORECASE,
)
_EVENT_CUE = re.compile(
    r"\b(?:when(?:ever)?|once|as soon as|next time|after)\s+(?P<cue>[^,;.]+?)" + _CUE_END,
    re.IGNORECASE,
)


def plan_cues(text: str) -> tuple[str | None, str | None]:
    """``(event_cue, location_cue)`` of a plan's text, or ``(None, None)`` (F12.9).

    Rule-based, no model call: a place after "when/next time I'm at" is a location cue; any other
    "when / once / as soon as / after <clause>" before the action is an event cue."""
    loc = _LOCATION_CUE.search(text)
    if loc:
        return None, loc.group("cue").strip()
    ev = _EVENT_CUE.search(text)
    return (ev.group("cue").strip() if ev else None), None


def record_assertion(record: MemoryRecord) -> AssertionEnvelope | None:
    """A record as the prospective library sees it (library input only; never stored).

    ``mod:plan`` without ``mod:fact`` is a planned assertion (``pol:neg`` makes it a cancellation);
    a plain statement is asserted, or negated with ``pol:neg``. Hypothetical, wish, opinion, question
    and request records, held, quarantined and bookkeeping records give ``None``.
    """
    if (
        record.memory_type in _SKIP_TYPES
        or record.status is not RecordStatus.ACTIVATED
        or record.quarantined
        or record.instruction_flag
        or not record.content.strip()
        or constants.CUE_TAG in record.tags
    ):
        return None
    verdict = _modality(record)
    if verdict is None:
        return None
    modality, negated = verdict
    surface, speaker = _subject(record)
    window = _window(record) if modality == "planned" else None
    time = TimeAxes(event=window, source_time=_aware(record.valid_from))
    ns = record.namespace
    extras: dict[str, str] = {}
    if (
        modality == "planned"
    ):  # F12.9: the cue the prospective library matches reminders(cues=...) on
        event_cue, location_cue = plan_cues(record.content)
        if event_cue:
            extras["event_cue"] = event_cue
        if location_cue:
            extras["location_cue"] = location_cue
    return AssertionEnvelope(
        write_id=f"w:{record.record_id}",
        assertion_key=AssertionKey(
            src=surface, rel=record.attribute or "note", kind=record.memory_type
        ),
        occurrence_id=f"o:{record.record_id}",
        record_id=record.record_id,
        scope=OwnerScope(tenant=ns, owner=ns),
        text=record.content,
        kind="plan" if modality == "planned" else "fact",
        subject=Mention(surface=surface, mention_id=f"m:{record.record_id}"),
        speaker=speaker,
        time=time,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id=record.record_id, source_version=1, seg_index=0),
                source_family=record.source.principal or record.source.role or "user",
            )
        ],
        assessment=Assessment(
            source_trust=record.trust,
            support=Support(),
            binding="bound",
            time_precision=time.event_precision,  # type: ignore[arg-type]
            modality=modality,  # type: ignore[arg-type]
            negated=negated and modality == "planned",
        ),
        src_class="assistant_proposed" if record.source.role == "assistant" else "user_stated",
        extras=extras,
    )


@dataclass(frozen=True)
class ProspectiveReport:
    """Intentions after resolution and expiry, and the reminders worth surfacing now."""

    intentions: list[Intention] = field(default_factory=list)
    reminders: list[Reminder] = field(default_factory=list)
    planned: int = 0
    pool: int = 0
    opened: int = 0
    done: int = 0
    cancelled: int = 0
    expired: int = 0


def prospective_report(
    records: Sequence[MemoryRecord],
    now: datetime,
    cues: Sequence[str] = (),
    rules: ProspectiveRules | None = None,
) -> ProspectiveReport:
    """Intentions, resolution, expiry and due reminders over ``records`` of one namespace.

    Planned (not negated) assertions give intentions; every later statement, negation and
    planned-and-negated assertion is a candidate resolver. Pure: ``now`` is a parameter.
    """
    envelopes = [e for e in (record_assertion(r) for r in records) if e is not None]
    planned = [
        e for e in envelopes if e.assessment.modality == "planned" and not e.assessment.negated
    ]
    intentions = derive_intentions(planned, now=now)
    resolved = detect_resolution(intentions, envelopes, now=now, rules=rules)
    final = expire(resolved, now, rules)
    reminders = due(final, now, cues, rules)
    count = {
        s: sum(1 for i in final if i.status == s) for s in ("open", "done", "cancelled", "expired")
    }
    return ProspectiveReport(
        intentions=final,
        reminders=reminders,
        planned=len(planned),
        pool=len(envelopes) - len(planned),
        opened=count["open"],
        done=count["done"],
        cancelled=count["cancelled"],
        expired=count["expired"],
    )


async def prospective_sweep(
    storage: _Storage, namespaces: Sequence[str], now: datetime
) -> dict[str, int]:
    """The sleep-cycle pass: report per namespace on the wiretap; returns the summed counts."""
    totals = {"intentions": 0, "due": 0, "done": 0, "cancelled": 0, "expired": 0, "open": 0}
    for ns in namespaces:
        records = await storage.list_records(ns)
        report = prospective_report(records, now)
        totals["intentions"] += len(report.intentions)
        totals["due"] += len(report.reminders)
        totals["done"] += report.done
        totals["cancelled"] += report.cancelled
        totals["expired"] += report.expired
        totals["open"] += report.opened
        if tap.enabled():
            tap.event(
                "job.prospective.sweep",
                intentions=len(report.intentions),
                planned=report.planned,
                candidates=report.pool,
                open=report.opened,
                due=len(report.reminders),
                done=report.done,
                cancelled=report.cancelled,
                expired=report.expired,
                reasons=sorted({r for rem in report.reminders for r in rem.reasons}),
            )
            for i in report.intentions:
                if i.status != "open":
                    tap.event(
                        "job.prospective.closed",
                        intention=i.intention_id,
                        status=i.status,
                        resolved_by=i.resolved_by,
                        parents=list(i.parents),
                    )
    return totals


# ------------------------------------------------------------------------------------------- procedural

_BULLET = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s*")
_ARROWS = re.compile(r"\s*(?:->|→|;)\s*")


def _steps_of(content: str) -> tuple[str, ...]:
    lines = [_BULLET.sub("", ln).strip() for ln in content.splitlines()]
    steps = [ln for ln in lines if ln]
    if len(steps) == 1:
        steps = [s.strip() for s in _ARROWS.split(steps[0]) if s.strip()]
    return tuple(steps)


def howto_of_record(record: MemoryRecord) -> HowTo | None:
    """A live skill or plan as one how-to statement: ``entity`` = the name, ``content`` = the steps
    (lines, a numbered list, or ``a -> b -> c``); plain tags (no ``:``) are its tags and ``pre:<text>``
    tags its preconditions. Deprecated, deleted, quarantined and lesson records are not evidence."""
    if (
        record.memory_type != "procedural"
        or record.attribute not in ("skill", "plan")
        or record.quarantined
        or record.status is RecordStatus.DELETED
        or record.skill_stage is SkillStage.DEPRECATED
    ):
        return None
    name = (record.entity or "").strip()
    steps = _steps_of(record.content)
    if not name or not steps:
        return None
    ns = record.namespace
    return HowTo(
        source_id=record.record_id,
        scope_key=f"{ns}/{ns}",
        name=name,
        steps=steps,
        preconditions=tuple(_vals(record.tags, "pre:")),
        tags=tuple(t for t in record.tags if ":" not in t and t != constants.LESSON_TAG),
        trust=record.trust,
        stamp=_aware(record.recorded_at).isoformat(),
    )


def build_record_procedures(
    howtos: Iterable[HowTo], *, min_repeats: int = MIN_REPEATS
) -> list[Procedure]:
    """Procedures from how-tos, replayed per name in source order so a change of the agreed steps
    opens a new version (the old counts move to history). Deterministic; independent of input order."""
    groups: dict[str, list[HowTo]] = {}
    for h in howtos:
        groups.setdefault(" ".join(h.name.casefold().split()), []).append(h)
    out: list[Procedure] = []
    for _, group in sorted(groups.items()):
        acc: list[HowTo] = []
        existing: list[Procedure] = []
        for h in sorted(group, key=lambda m: (m.stamp, m.source_id)):
            acc.append(h)
            existing = build_procedures(acc, existing=existing, min_repeats=min_repeats)
        out.extend(existing)
    return sorted(out, key=lambda p: p.procedure_id)


@dataclass
class ProceduralReport:
    procedures: list[Procedure] = field(default_factory=list)
    howtos: int = 0
    explicit_applied: int = 0
    implicit_refused: int = 0
    stale_refused: int = 0
    versioned: int = 0
    events_available: bool = True


async def _feedback_events(
    storage: _Storage, ns: str, record_ids: set[str]
) -> list[tuple[int, str, str, bool]]:
    """(seq, record id, result, explicit) of each like / dislike on ``record_ids``. An event the
    engine wrote itself (actor ``system``: an outcome receipt) is implicit; anything else a person
    or the host chose to send is explicit."""
    found: list[tuple[int, str, str, bool]] = []
    after = 0
    while True:
        batch = await storage.read_events(after_seq=after)
        if not batch:
            return found
        for ev in batch:
            if ev.kind is EventKind.FEEDBACK and ev.namespace == ns:
                rid = str(ev.payload.get("record_id", ""))
                result = _RESULT_OF.get(str(ev.payload.get("signal", "")))
                if rid in record_ids and result is not None:
                    found.append((ev.seq or 0, rid, result, ev.actor != "system"))
        last = batch[-1].seq
        if last is None:
            return found
        after = last


async def procedural_report(
    storage: _Storage, ns: str, config: MemspineConfig, *, min_repeats: int = MIN_REPEATS
) -> ProceduralReport:
    """Procedures of one namespace with their counts from explicit feedback.

    A like is a success and a dislike a failure of the procedure its record sourced, counted only for
    the record's current version and only when explicit. Implicit ones (the engine's own receipts)
    go through ``memory.procedural.update`` with ``explicit=False`` and are refused there.
    """
    records = await storage.list_records(ns, "procedural")
    howtos = [h for h in (howto_of_record(r) for r in records) if h is not None]
    procs = build_record_procedures(howtos, min_repeats=min_repeats)
    report = ProceduralReport(howtos=len(howtos), versioned=sum(1 for p in procs if p.version > 1))
    if not procs:
        return report
    available = config.event_log.mode is not EventLogMode.EPHEMERAL
    report.events_available = available
    events: list[tuple[int, str, str, bool]] = []
    if available:
        events = await _feedback_events(storage, ns, {h.source_id for h in howtos})
    by_source = {s: p for p in procs for s in p.source_ids}
    howto_ids = {h.source_id for h in howtos}
    current = {p.procedure_id: p for p in procs}
    for seq, rid, result, explicit in sorted(events):
        proc = by_source.get(rid)
        if proc is None:
            if rid in howto_ids:  # a source of an older version, or below the repeat threshold
                report.stale_refused += 1
                if tap.enabled():
                    tap.event("job.procedural.update_refused", reason="stale_version", record=rid)
            continue
        p = current[proc.procedure_id]
        try:
            current[p.procedure_id] = update(
                p,
                Outcome(
                    outcome_id=f"ev{seq}",
                    procedure_id=p.procedure_id,
                    result=result,  # type: ignore[arg-type]
                    explicit=explicit,
                    version=p.version,
                ),
            )
            report.explicit_applied += 1
        except ImplicitFeedbackError:
            report.implicit_refused += 1
            if tap.enabled():
                tap.event(
                    "job.procedural.update_refused",
                    reason="implicit",
                    procedure=p.procedure_id,
                    record=rid,
                )
    report.procedures = sorted(current.values(), key=lambda p: p.procedure_id)
    if tap.enabled():
        for p in report.procedures:
            tap.event(
                "job.procedural.built",
                procedure=p.procedure_id,
                version=p.version,
                sources=len(p.source_ids),
                success=p.success,
                failure=p.failure,
                trust=p.trust,
            )
            if p.version > 1:
                tap.event(
                    "job.procedural.versioned",
                    procedure=p.procedure_id,
                    version=p.version,
                    closed=[v.version for v in p.history],
                )
    return report


def suggest_for(
    procedures: Iterable[Procedure], context_tags: Iterable[str], limit: int | None = None
) -> list[Suggestion]:
    """Advisory ranking (``memory.procedural.suggest``); never executes anything."""
    return suggest(procedures, context_tags, limit=limit)


async def procedural_sweep(
    storage: _Storage, namespaces: Sequence[str], config: MemspineConfig
) -> dict[str, int]:
    totals = {"procedures": 0, "howtos": 0, "versioned": 0, "explicit": 0, "implicit_refused": 0}
    for ns in namespaces:
        report = await procedural_report(storage, ns, config)
        totals["procedures"] += len(report.procedures)
        totals["howtos"] += report.howtos
        totals["versioned"] += report.versioned
        totals["explicit"] += report.explicit_applied
        totals["implicit_refused"] += report.implicit_refused
    return totals
