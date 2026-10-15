"""Prospective memory mechanism (O-23 A, package O4-10): stated plans become advisory reminders.

Pure and deterministic: no I/O, no clock reads (``now`` is always a parameter), no model calls. The module
only *surfaces*: :func:`due` returns :class:`Reminder` values and never acts, never writes, never delivers.
It is opt-in and unwired (wiring is O4-10).

Decisions encoded:

* Intentions come only from ``planned`` assertions (S5-1: planned and hypothetical content is tentative,
  and only a stated plan makes a watch). A planned-and-negated assertion ("won't go") is a cancellation
  signal, not an intention.
* Trigger times come from the assertion's event interval as already resolved by the temporal anchoring
  step (S6). This module never parses dates; an unresolved interval end simply yields no time trigger.
* Event and location cues are plain text. They match a caller-supplied list of current cue strings.
* A derived intention carries lineage (``parents`` = source assertion ids) and ``trust`` no higher than the
  minimum trust of its parents (G1).
* Completion and cancellation come only from *later* assertions about the same subject whose content
  matches the plan; nothing is inferred from the passage of time except expiry.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field

from memspine.schemas.base import InternalModel
from memspine.schemas.durable.assertion import AssertionEnvelope, inherit_trust
from memspine.schemas.durable.temporal import Interval, TemporalRef

__all__ = [
    "Intention",
    "IntentionStatus",
    "ProspectiveRules",
    "Reminder",
    "ReminderReason",
    "Trigger",
    "derive_intentions",
    "detect_resolution",
    "due",
    "effective_status",
    "expire",
]

IntentionStatus = Literal["open", "done", "cancelled", "expired"]
ReminderReason = Literal["in_window", "overdue", "deadline_near", "upcoming", "cue_match"]

#: Reasons in surfacing priority order (earlier = more urgent).
_REASON_ORDER: tuple[str, ...] = ("in_window", "overdue", "deadline_near", "upcoming", "cue_match")

_PRECISION_SPAN: dict[str, timedelta] = {
    "instant": timedelta(minutes=1),
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(days=7),
    "month": timedelta(days=31),
    "season": timedelta(days=92),
    "year": timedelta(days=366),
    "decade": timedelta(days=3653),
    "unknown": timedelta(days=1),
}

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
#: Words that carry no content for plan matching (plan verbs, fillers, done/cancel markers).
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "by",
        "with",
        "and",
        "or",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "will",
        "would",
        "shall",
        "should",
        "going",
        "gonna",
        "plan",
        "plans",
        "planned",
        "planning",
        "intend",
        "intends",
        "intended",
        "want",
        "wants",
        "wanted",
        "need",
        "needs",
        "needed",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "done",
        "not",
        "no",
        "never",
        "wont",
        "cant",
        "cancel",
        "cancelled",
        "canceled",
        "cancels",
        "dropped",
        "abandoned",
        "skip",
        "skipped",
        "skips",
        "scrap",
        "scrapped",
        "finished",
        "completed",
        "complete",
        "finish",
        "went",
        "go",
        "goes",
        "gone",
        "it",
        "its",
        "my",
        "our",
        "their",
        "his",
        "her",
        "this",
        "that",
        "then",
        "later",
        "soon",
        "tomorrow",
        "today",
        "tonight",
    ]
)
_CANCEL_WORDS = frozenset(
    [
        "cancel",
        "cancelled",
        "canceled",
        "cancels",
        "dropped",
        "abandoned",
        "skip",
        "skipped",
        "skips",
        "scrap",
        "scrapped",
    ]
)


#: Connective words that introduce a cue ("when Bram calls") and are not part of what to look for.
_CUE_FILLER = frozenset(
    [
        "when",
        "whenever",
        "if",
        "once",
        "as",
        "soon",
        "after",
        "before",
        "while",
        "at",
        "in",
        "on",
        "near",
        "by",
        "the",
        "a",
        "an",
    ]
)


def _tokens(text: str) -> frozenset[str]:
    return frozenset(
        w for w in (m.group(0).casefold() for m in _WORD.finditer(text)) if w not in _STOP
    )


def _all_words(text: str) -> frozenset[str]:
    return frozenset(m.group(0).casefold() for m in _WORD.finditer(text))


def _norm_phrase(text: str) -> str:
    return " ".join(m.group(0).casefold() for m in _WORD.finditer(text))


def _digest(parts: list[object]) -> str:
    blob = json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"int_{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:32]}"


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


class ProspectiveRules(InternalModel):
    """Tunable, explicit rules. Defaults are conservative; every value is deterministic data."""

    model_config = ConfigDict(frozen=True)

    lead: timedelta = timedelta(hours=24)  # surface "upcoming" this long before a window opens
    overdue_grace: timedelta = timedelta(
        days=7
    )  # a missed window is "overdue" this long, then expires
    ttl_cue_only: timedelta = timedelta(days=90)  # cue-triggered plan with no time
    ttl_no_trigger: timedelta = timedelta(days=30)  # plan with no usable trigger at all
    match_overlap: float = Field(default=0.5, gt=0.0, le=1.0)  # content-word overlap for completion


class Trigger(InternalModel):
    """When an intention should surface. Any combination may be present; all absent = no trigger."""

    model_config = ConfigDict(frozen=True)

    starts_at: AwareDatetime | None = None  # window opens (None with ends_at = a deadline)
    ends_at: AwareDatetime | None = None  # window closes (exclusive end of the period)
    event_cue: str | None = None  # text, e.g. "when Bram calls"
    location_cue: str | None = None  # text, e.g. "at the pharmacy"

    @property
    def has_time(self) -> bool:
        return self.starts_at is not None or self.ends_at is not None

    @property
    def has_cue(self) -> bool:
        return bool(self.event_cue or self.location_cue)

    @property
    def kind(self) -> str:
        kinds = [k for k, on in (("time", self.has_time), ("cue", self.has_cue)) if on]
        return "+".join(kinds) if kinds else "none"


class Intention(InternalModel):
    """A stated plan, tentative by construction (S5-1), with lineage and bounded trust (G1)."""

    model_config = ConfigDict(frozen=True)

    intention_id: str
    scope_key: str  # tenant/owner the plan belongs to
    who: str
    what: str
    trigger: Trigger = Field(default_factory=Trigger)
    status: IntentionStatus = "open"
    tentative: bool = True
    trust: float = Field(ge=0.0, le=1.0)
    parents: tuple[str, ...]  # source assertion ids (lineage)
    created_at: AwareDatetime  # earliest source time
    updated_at: AwareDatetime  # latest source time
    resolved_by: str | None = None  # id of the assertion that closed it
    resolved_at: AwareDatetime | None = None

    @property
    def source_ids(self) -> tuple[str, ...]:
        return self.parents


class Reminder(InternalModel):
    """An advisory surfacing. It never acts; ``advisory`` is always True."""

    model_config = ConfigDict(frozen=True)

    intention_id: str
    who: str
    what: str
    reasons: tuple[ReminderReason, ...]
    details: tuple[str, ...]
    due_at: AwareDatetime | None
    trust: float
    tentative: bool = True
    advisory: Literal[True] = True
    parents: tuple[str, ...] = ()


# ------------------------------------------------------------------------------------------ derive


def _assertion_id(a: AssertionEnvelope) -> str:
    return a.record_id or a.occurrence_id


def _stamp(a: AssertionEnvelope, fallback: datetime) -> datetime:
    return a.time.source_time or a.time.recorded_at or fallback


def _who(a: AssertionEnvelope) -> str:
    if a.subject is not None:
        return a.subject.surface
    return a.speaker or ""


def _subject_key(a: AssertionEnvelope) -> str:
    if a.subject is not None and a.subject.entity_id:
        return f"e:{a.subject.entity_id}"
    return f"s:{_norm_phrase(_who(a))}"


def _end_of(ref: TemporalRef) -> datetime | None:
    if ref.at is None:
        return None
    return ref.at + _PRECISION_SPAN[ref.precision]


def _time_trigger(interval: Interval | None) -> tuple[datetime | None, datetime | None]:
    """Window from an already-resolved event interval: (starts_at, ends_at), ends exclusive."""
    if interval is None:
        return None, None
    start = interval.start.at if interval.start is not None else None
    if interval.end is not None and interval.end.at is not None:
        end = _end_of(interval.end)
    elif interval.start is not None and start is not None:
        end = _end_of(interval.start)
    else:
        end = None
    return start, end


def _cue_text(a: AssertionEnvelope, key: str) -> str | None:
    v = a.extras.get(key)
    return v.strip() if isinstance(v, str) and v.strip() else None


def derive_intentions(
    assertions: Iterable[AssertionEnvelope],
    *,
    now: datetime,
    cue_of: Callable[[AssertionEnvelope], tuple[str | None, str | None]] | None = None,
) -> list[Intention]:
    """Build intentions from ``planned`` assertions.

    Plans about the same subject with the same content merge into one intention (a restated or
    rescheduled plan): the latest source wins the trigger, every source stays in ``parents``.

    ``cue_of`` optionally supplies ``(event_cue, location_cue)`` for an assertion; by default the cues
    are read from ``extras["event_cue"]`` and ``extras["location_cue"]``. ``now`` is only the timestamp
    fallback for an assertion carrying no source or recorded time.
    """
    _require_aware(now, "now")
    groups: dict[tuple[str, str, frozenset[str]], list[AssertionEnvelope]] = {}
    for a in assertions:
        if a.assessment.modality != "planned" or a.assessment.negated:
            continue
        if a.status in ("hidden", "superseded"):
            continue
        what_key = _tokens(a.text)
        if not what_key:
            continue
        gk = (f"{a.scope.tenant}/{a.scope.owner}", _subject_key(a), what_key)
        groups.setdefault(gk, []).append(a)

    out: list[Intention] = []
    for (scope_key, subj, what_key), members in groups.items():
        members.sort(key=lambda m: (_stamp(m, now), _assertion_id(m)))
        latest = members[-1]
        starts, ends = _time_trigger(latest.time.event)
        if cue_of is not None:
            event_cue, loc_cue = cue_of(latest)
        else:
            event_cue, loc_cue = _cue_text(latest, "event_cue"), _cue_text(latest, "location_cue")
        parents = tuple(sorted({_assertion_id(m) for m in members}))
        trust = inherit_trust(
            [m.assessment.source_trust for m in members],
            latest.assessment.source_trust,
        )
        out.append(
            Intention(
                intention_id=_digest([scope_key, subj, sorted(what_key)]),
                scope_key=scope_key,
                who=_who(latest),
                what=latest.text,
                trigger=Trigger(
                    starts_at=starts, ends_at=ends, event_cue=event_cue, location_cue=loc_cue
                ),
                trust=trust,
                parents=parents,
                created_at=_stamp(members[0], now),
                updated_at=_stamp(latest, now),
            )
        )
    out.sort(key=lambda i: (i.created_at, i.intention_id))
    return out


# ------------------------------------------------------------------------- completion / cancellation


def _classify(a: AssertionEnvelope) -> Literal["done", "cancelled"] | None:
    mod = a.assessment.modality
    if mod == "negated" or (mod == "planned" and a.assessment.negated):
        return "cancelled"
    if mod == "asserted":
        return "cancelled" if _all_words(a.text) & _CANCEL_WORDS else "done"
    return None


def _matches(i: Intention, a: AssertionEnvelope, rules: ProspectiveRules) -> bool:
    # Same subject: equal normalised surfaces, or the shorter is a whole-word prefix of the longer
    # ("Aiko" vs "Aiko Tanaka"). Empty subjects never match.
    mine_who, theirs_who = _norm_phrase(i.who).split(), _norm_phrase(_who(a)).split()
    if not mine_who or not theirs_who:
        return False
    n = min(len(mine_who), len(theirs_who))
    if mine_who[:n] != theirs_who[:n]:
        return False
    subject_words = _all_words(i.who) | _all_words(_who(a))
    mine = _tokens(i.what) - subject_words
    theirs = _tokens(a.text) - subject_words
    shared = mine & theirs
    if not shared or not mine or not theirs:
        return False
    return len(shared) / min(len(mine), len(theirs)) >= rules.match_overlap


def detect_resolution(
    intentions: Sequence[Intention],
    later: Iterable[AssertionEnvelope],
    *,
    now: datetime,
    rules: ProspectiveRules | None = None,
) -> list[Intention]:
    """Close open intentions from *later* assertions on the same subject.

    * ``asserted`` content matching the plan -> ``done``; with a cancel word -> ``cancelled``.
    * ``negated`` (or planned-and-negated) matching content -> ``cancelled``.
    * ``hypothetical``, ``reported`` and ``question`` never close anything.

    "Later" means strictly after the intention's latest source time. The earliest qualifying assertion
    (by source time, then id) wins. Returns a list in the input order; only open intentions change.
    """
    _require_aware(now, "now")
    rules = rules or ProspectiveRules()
    pool = sorted(
        (a for a in later if a.status not in ("hidden", "superseded")),
        key=lambda a: (_stamp(a, now), _assertion_id(a)),
    )
    result: list[Intention] = []
    for i in intentions:
        if i.status != "open":
            result.append(i)
            continue
        closed = i
        for a in pool:
            when = _stamp(a, now)
            if when <= i.updated_at or _assertion_id(a) in i.parents:
                continue
            verdict = _classify(a)
            if verdict is None or not _matches(i, a, rules):
                continue
            closed = i.model_copy(
                update={"status": verdict, "resolved_by": _assertion_id(a), "resolved_at": when}
            )
            break
        result.append(closed)
    return result


# --------------------------------------------------------------------------------------------- expiry


def _expires_at(i: Intention, rules: ProspectiveRules) -> datetime:
    t = i.trigger
    if t.ends_at is not None:
        return t.ends_at + rules.overdue_grace
    if t.starts_at is not None:
        return t.starts_at + rules.overdue_grace
    base = i.updated_at
    return base + (rules.ttl_cue_only if t.has_cue else rules.ttl_no_trigger)


def effective_status(
    i: Intention, now: datetime, rules: ProspectiveRules | None = None
) -> IntentionStatus:
    """Status at ``now``: an open intention past its expiry reads as ``expired`` (no mutation)."""
    _require_aware(now, "now")
    if i.status != "open":
        return i.status
    return "expired" if now >= _expires_at(i, rules or ProspectiveRules()) else "open"


def expire(
    intentions: Sequence[Intention], now: datetime, rules: ProspectiveRules | None = None
) -> list[Intention]:
    """Mark open intentions past their expiry as ``expired``. A time window expires ``overdue_grace``
    after it closes; a cue-only plan after ``ttl_cue_only``; a trigger-less plan after ``ttl_no_trigger``
    (measured from the latest source time). Done and cancelled intentions are never touched."""
    rules = rules or ProspectiveRules()
    return [
        i.model_copy(update={"status": "expired"})
        if effective_status(i, now, rules) == "expired" and i.status == "open"
        else i
        for i in intentions
    ]


# ---------------------------------------------------------------------------------------------- due


def _cue_hit(cue: str | None, current: Sequence[str]) -> str | None:
    """A cue matches when its content words all appear in one current cue string (or the phrase does)."""
    if not cue:
        return None
    want = (_all_words(cue) - _CUE_FILLER) or _all_words(cue)
    phrase = _norm_phrase(cue)
    for c in current:
        if phrase and phrase in _norm_phrase(c):
            return c
        if want and want <= (_all_words(c)):
            return c
    return None


def due(
    intentions: Sequence[Intention],
    now: datetime,
    cues: Sequence[str] = (),
    rules: ProspectiveRules | None = None,
) -> list[Reminder]:
    """Intentions worth surfacing at ``now``, with the reasons. Advisory only.

    Reasons: ``in_window`` (inside the time window), ``overdue`` (window closed, still within grace),
    ``deadline_near`` (a deadline-only plan within ``lead`` of its end), ``upcoming`` (window opens within
    ``lead``), ``cue_match`` (an event or location cue appears in ``cues``). Done, cancelled and expired
    intentions never surface. Output order: most urgent reason, then due time, then id.
    """
    _require_aware(now, "now")
    rules = rules or ProspectiveRules()
    scored: list[tuple[int, datetime, str, Reminder]] = []
    for i in intentions:
        if effective_status(i, now, rules) != "open":
            continue
        t = i.trigger
        reasons: list[ReminderReason] = []
        details: list[str] = []
        due_at: datetime | None = None
        if t.has_time:
            s, e = t.starts_at, t.ends_at
            if s is not None and e is not None:
                if s <= now < e:
                    reasons.append("in_window")
                    details.append(f"window {s.isoformat()} to {e.isoformat()}")
                elif now >= e:
                    reasons.append("overdue")
                    details.append(f"window closed {e.isoformat()}")
                elif now >= s - rules.lead:
                    reasons.append("upcoming")
                    details.append(f"opens {s.isoformat()}")
                due_at = s
            elif e is not None:
                if now >= e:
                    reasons.append("overdue")
                    details.append(f"deadline passed {e.isoformat()}")
                elif now >= e - rules.lead:
                    reasons.append("deadline_near")
                    details.append(f"deadline {e.isoformat()}")
                due_at = e
            elif s is not None:
                if s <= now:
                    reasons.append("in_window")
                    details.append(f"started {s.isoformat()}")
                elif now >= s - rules.lead:
                    reasons.append("upcoming")
                    details.append(f"opens {s.isoformat()}")
                due_at = s
        for label, cue in (("event cue", t.event_cue), ("location cue", t.location_cue)):
            hit = _cue_hit(cue, cues)
            if hit is not None:
                if "cue_match" not in reasons:
                    reasons.append("cue_match")
                details.append(f"{label} {cue!r} matched {hit!r}")
        if not reasons:
            continue
        reasons.sort(key=_REASON_ORDER.index)
        reminder = Reminder(
            intention_id=i.intention_id,
            who=i.who,
            what=i.what,
            reasons=tuple(reasons),
            details=tuple(details),
            due_at=due_at,
            trust=i.trust,
            tentative=i.tentative,
            parents=i.parents,
        )
        far = datetime.max.replace(tzinfo=now.tzinfo)
        scored.append((_REASON_ORDER.index(reasons[0]), due_at or far, i.intention_id, reminder))
    scored.sort(key=lambda r: (r[0], r[1], r[2]))
    return [r[3] for r in scored]
