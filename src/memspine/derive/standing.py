# ruff: noqa: E501
"""Standing answers and standing instructions (LIB-32; O-21, ER-28; opt-in L5, wired later by O4-08).

Pure and unwired: no I/O, no clock (sequences are injected integers), no model call. Depends on
``memspine.schemas``, ``memspine.derive.histories`` (value histories) and ``memspine.derive.resolve``
(name normalisation).

Standing answers (O-21)
-----------------------
A :class:`StandingAnswer` is a precomputed answer to a registered question pattern
(:class:`QuestionKey`: normalised tenant, owner, subject, attribute and a time view). It is built from value
histories only, so it adds no facts. It records

* the answer value(s) and the supporting assertion ids (never tentative or hidden records);
* the dependency groups (:class:`GroupKey`: tenant, owner, subject, attribute) and a dependency fingerprint of
  those groups' spans at build time (ER-28: "dependency fingerprints");
* ``computed_at_seq`` (an injected sequence), ``freshness`` (``fresh`` or ``stale``) and ``contested``.

A contested value is never flattened: the answer carries every contested value and ``contested=True``.
A question with no value yet still gets an (empty) answer that depends on its group, so a first fact
invalidates it.

Invalidation is by changed group (ER-28 B): :func:`invalidate` returns the ids whose dependency groups are in
the changed set and nothing else; :func:`changed_groups` derives that set from two history snapshots by
comparing per-group fingerprints (tentative-only changes do not count, because they never reach an answer).
A stale answer is never served by :func:`select_for_context`; the reader falls back to live evidence.
:func:`refresh` rebuilds only the stale ones, so unchanged answers stay identical objects.

Usefulness (ER-28): :func:`usefulness` folds a hit log into per-answer :class:`UsageStats`;
:func:`eviction_candidates` lists answers worth dropping (idle, low hit rate, or over a size cap), worst first.

Standing instructions
---------------------
A :class:`StandingInstruction` ("always answer in Dutch") is a different type: user-stated, with a scope
(global, topic, session) and a priority. :func:`propose_instruction` accepts only a firm, bound, asserted
record whose source class is ``user_stated`` or ``user_confirmed`` and whose trust clears ``min_trust``; a
tentative, contested, hedged, assistant-proposed or derived record never becomes an instruction. Instructions
are never derived from value histories and never expire by invalidation of an answer; they drop only when
their source record is no longer live (:func:`live_instructions`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from memspine.derive.histories import KeyHistory, ValueHistory, ValueSpan
from memspine.derive.resolve import normalise_name
from memspine.schemas.durable.assertion import AssertionEnvelope

__all__ = [
    "GroupKey",
    "HitEvent",
    "QuestionKey",
    "Selection",
    "StandingAnswer",
    "StandingInstruction",
    "UsageStats",
    "build",
    "changed_groups",
    "eviction_candidates",
    "group_fingerprints",
    "invalidate",
    "live_instructions",
    "mark_stale",
    "propose_instruction",
    "refresh",
    "render_answer",
    "select_for_context",
    "select_instructions",
    "usefulness",
]

View = Literal["current", "as_of", "history"]
Freshness = Literal["fresh", "stale"]
Cost = Callable[[str], int]


def _words(text: str) -> int:
    return len(text.split())


def _digest(*parts: object) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


# --------------------------------------------------------------------------- keys


@dataclass(frozen=True, order=True)
class GroupKey:
    """One value line: ``(tenant, owner, subject, attribute)``, subject and attribute normalised."""

    tenant: str
    owner: str
    subject: str
    attribute: str

    @staticmethod
    def of(tenant: str, owner: str, subject: str | None, attribute: str) -> GroupKey:
        return GroupKey(tenant, owner, normalise_name(subject or ""), normalise_name(attribute))


@dataclass(frozen=True)
class QuestionKey:
    """A question pattern: normalised subject + attribute + time view (``as_of`` needs a datetime)."""

    tenant: str
    owner: str
    subject: str
    attribute: str
    view: View = "current"
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        if (self.view == "as_of") != (self.as_of is not None):
            raise ValueError("as_of is required for, and only for, the 'as_of' view")
        object.__setattr__(self, "subject", normalise_name(self.subject))
        object.__setattr__(self, "attribute", normalise_name(self.attribute))

    @property
    def group(self) -> GroupKey:
        return GroupKey(self.tenant, self.owner, self.subject, self.attribute)

    @property
    def canonical(self) -> str:
        at = self.as_of.isoformat() if self.as_of is not None else ""
        return "|".join((self.tenant, self.owner, self.subject, self.attribute, self.view, at))

    @property
    def answer_id(self) -> str:
        return "sa_" + _digest(self.canonical)[:16]


# --------------------------------------------------------------------------- standing answers


@dataclass(frozen=True)
class StandingAnswer:
    """A precomputed answer. ``id`` depends only on the question key, so a rebuild replaces in place."""

    id: str
    key: QuestionKey
    values: tuple[str, ...]
    support_ids: tuple[str, ...]
    groups: tuple[GroupKey, ...]
    fingerprint: str
    computed_at_seq: int
    freshness: Freshness = "fresh"
    contested: bool = False
    trust: float = 0.0  # min trust over the supporting spans (0.0 when there is no support)

    @property
    def empty(self) -> bool:
        return not self.values


def _line_for(history: ValueHistory, group: GroupKey) -> KeyHistory | None:
    for line in history.lines:
        k = line.key
        if GroupKey.of(k.tenant, k.owner, k.subject, k.attribute) == group:
            return line
    return None


def _line_fingerprint(line: KeyHistory | None) -> str:
    if line is None:
        return _digest("absent")
    return _digest(
        [
            (s.record_id, s.value, s.status, str(s.effective_from), str(s.effective_to))
            for s in line.spans
        ]
    )


def group_fingerprints(history: ValueHistory) -> dict[GroupKey, str]:
    """Per-group dependency fingerprint of a history snapshot (spans only; tentative entries excluded)."""
    out: dict[GroupKey, str] = {}
    for line in history.lines:
        k = line.key
        out[GroupKey.of(k.tenant, k.owner, k.subject, k.attribute)] = _line_fingerprint(line)
    return out


def changed_groups(before: ValueHistory, after: ValueHistory) -> frozenset[GroupKey]:
    """Groups whose spans differ between two snapshots (added, removed or edited lines)."""
    a, b = group_fingerprints(before), group_fingerprints(after)
    absent = _digest("absent")
    return frozenset(g for g in a.keys() | b.keys() if a.get(g, absent) != b.get(g, absent))


def _select_spans(line: KeyHistory | None, key: QuestionKey) -> tuple[tuple[ValueSpan, ...], bool]:
    if line is None:
        return (), False
    if key.view == "current":
        contests = line.open_contests()
        spans = tuple(line.current()) + tuple(contests)
        return spans, bool(contests)
    if key.view == "as_of":
        assert key.as_of is not None
        spans = line.value_at(key.as_of, include_contested=True)
        return spans, any(s.status == "contested" for s in spans)
    return tuple(line.spans), bool(line.open_contests())


def _build_one(key: QuestionKey, history: ValueHistory, seq: int) -> StandingAnswer:
    line = _line_for(history, key.group)
    spans, contested = _select_spans(line, key)
    values: list[str] = []
    for s in spans:
        if s.value not in values:
            values.append(s.value)
    support = tuple(dict.fromkeys(s.record_id for s in spans))
    return StandingAnswer(
        id=key.answer_id,
        key=key,
        values=tuple(values),
        support_ids=support,
        groups=(key.group,),
        fingerprint=_line_fingerprint(line),
        computed_at_seq=seq,
        contested=contested,
        trust=min((s.trust for s in spans), default=0.0),
    )


def build(
    question_keys: Iterable[QuestionKey], histories: ValueHistory, *, computed_at_seq: int = 0
) -> list[StandingAnswer]:
    """Build one answer per distinct question key, ordered by answer id (input order is irrelevant)."""
    keys = {k.answer_id: k for k in question_keys}
    return [_build_one(keys[i], histories, computed_at_seq) for i in sorted(keys)]


def invalidate(
    standing: Iterable[StandingAnswer], changed_group_keys: Iterable[GroupKey]
) -> list[str]:
    """Ids of the answers that depend on a changed group (sorted); every other answer stays valid."""
    changed = set(changed_group_keys)
    return sorted({a.id for a in standing if changed.intersection(a.groups)})


def mark_stale(
    standing: Iterable[StandingAnswer], stale_ids: Iterable[str]
) -> list[StandingAnswer]:
    """Same list with the given answers flagged stale (others are the same objects)."""
    ids = set(stale_ids)
    return [replace(a, freshness="stale") if a.id in ids else a for a in standing]


def refresh(
    standing: Iterable[StandingAnswer],
    stale_ids: Iterable[str],
    histories: ValueHistory,
    *,
    computed_at_seq: int,
) -> list[StandingAnswer]:
    """Rebuild only the listed answers from ``histories``; the rest are returned untouched."""
    ids = set(stale_ids)
    return [_build_one(a.key, histories, computed_at_seq) if a.id in ids else a for a in standing]


def render_answer(answer: StandingAnswer) -> str:
    """One-line context rendering; a contested answer lists its values and says so."""
    k = answer.key
    when = f" as of {k.as_of.date().isoformat()}" if k.as_of is not None else ""
    head = f"{k.subject} {k.attribute} ({k.view}{when})"
    if answer.empty:
        return f"{head}: no recorded value"
    if answer.contested:
        return f"{head}: contested: " + " | ".join(answer.values)
    return f"{head}: " + "; ".join(answer.values)


# --------------------------------------------------------------------------- usefulness


@dataclass(frozen=True)
class HitEvent:
    """One logged read: the answer was used (``hit``) or consulted but not used / contradicted (``miss``)."""

    answer_id: str
    seq: int
    kind: Literal["hit", "miss"]


@dataclass(frozen=True)
class UsageStats:
    answer_id: str
    hits: int = 0
    misses: int = 0
    last_used_seq: int | None = None

    @property
    def samples(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.samples if self.samples else 0.0


def usefulness(standing: Iterable[StandingAnswer], hit_log: Iterable[HitEvent]) -> list[UsageStats]:
    """Per-answer stats, ordered by answer id; answers never seen in the log appear with zeros.
    Events for unknown ids are ignored. ``last_used_seq`` is the latest hit."""
    hits: dict[str, int] = {}
    misses: dict[str, int] = {}
    last: dict[str, int] = {}
    known = {a.id for a in standing}
    for ev in hit_log:
        if ev.answer_id not in known:
            continue
        if ev.kind == "hit":
            hits[ev.answer_id] = hits.get(ev.answer_id, 0) + 1
            last[ev.answer_id] = max(last.get(ev.answer_id, ev.seq), ev.seq)
        else:
            misses[ev.answer_id] = misses.get(ev.answer_id, 0) + 1
    return [UsageStats(i, hits.get(i, 0), misses.get(i, 0), last.get(i)) for i in sorted(known)]


def eviction_candidates(
    standing: Iterable[StandingAnswer],
    stats: Iterable[UsageStats],
    *,
    now_seq: int,
    idle_after: int,
    min_samples: int = 5,
    min_hit_rate: float = 0.2,
    max_answers: int | None = None,
) -> list[str]:
    """Answers worth evicting, worst first. An answer is a candidate when it was not used for ``idle_after``
    sequence steps (counted from its last hit, or its computation when never hit), or when it has at least
    ``min_samples`` reads and a hit rate below ``min_hit_rate``. If more than ``max_answers`` would remain,
    the lowest ``hits - misses`` of the rest are added until the cap holds."""
    by_stats = {s.answer_id: s for s in stats}
    answers = sorted(standing, key=lambda a: a.id)

    def worth(a: UsageStats) -> tuple[int, int, int]:
        return (a.hits - a.misses, a.last_used_seq if a.last_used_seq is not None else -1, 0)

    flagged: list[str] = []
    for a in answers:
        s = by_stats.get(a.id, UsageStats(a.id))
        ref = s.last_used_seq if s.last_used_seq is not None else a.computed_at_seq
        idle = now_seq - ref >= idle_after
        poor = s.samples >= min_samples and s.hit_rate < min_hit_rate
        if idle or poor:
            flagged.append(a.id)
    rest = [a.id for a in answers if a.id not in set(flagged)]
    if max_answers is not None and len(rest) > max_answers:
        rest.sort(key=lambda i: (*worth(by_stats.get(i, UsageStats(i))), i))
        flagged_extra = rest[: len(rest) - max_answers]
    else:
        flagged_extra = []
    order = flagged + flagged_extra
    return sorted(
        order,
        key=lambda i: (*worth(by_stats.get(i, UsageStats(i))), i),
    )


# --------------------------------------------------------------------------- selection


@dataclass(frozen=True)
class Selection:
    answers: tuple[StandingAnswer, ...]
    skipped_stale: tuple[
        str, ...
    ] = ()  # exact-match answers withheld because stale (use live evidence)
    dropped_over_budget: tuple[str, ...] = ()
    cost: int = 0


def select_for_context(
    standing: Iterable[StandingAnswer],
    query_key: QuestionKey,
    budget: int,
    *,
    cost: Cost = _words,
) -> Selection:
    """Pick answers for a read. The exact-key answer comes first (fresh only; a stale one is reported in
    ``skipped_stale`` and the reader falls back to live evidence), then fresh answers about the same subject
    (same tenant and owner), sorted by attribute and id, while the ``budget`` holds. Empty answers are not
    shown. Never exceeds the budget; an item that does not fit is dropped and later smaller ones may still fit."""
    pool = sorted(standing, key=lambda a: a.id)
    chosen: list[StandingAnswer] = []
    stale: list[str] = []
    over: list[str] = []
    used = 0

    def take(a: StandingAnswer) -> None:
        nonlocal used
        c = cost(render_answer(a))
        if used + c <= budget:
            chosen.append(a)
            used += c
        else:
            over.append(a.id)

    exact = [a for a in pool if a.key.canonical == query_key.canonical]
    for a in exact:
        if a.freshness == "stale":
            stale.append(a.id)
        elif not a.empty:
            take(a)
    related = [
        a
        for a in pool
        if a.key.canonical != query_key.canonical
        and (a.key.tenant, a.key.owner, a.key.subject)
        == (query_key.tenant, query_key.owner, query_key.subject)
        and a.freshness == "fresh"
        and not a.empty
    ]
    for a in sorted(related, key=lambda a: (a.key.attribute, a.key.view, a.id)):
        take(a)
    return Selection(tuple(chosen), tuple(stale), tuple(over), used)


# --------------------------------------------------------------------------- standing instructions

Scope = Literal["global", "topic", "session"]
_SPECIFICITY = {"session": 2, "topic": 1, "global": 0}
_FIRM_SOURCES = frozenset({"user_stated", "user_confirmed"})


@dataclass(frozen=True)
class StandingInstruction:
    id: str
    tenant: str
    owner: str
    text: str
    scope: Scope
    scope_ref: str | None
    priority: int
    source_ids: tuple[str, ...]
    trust: float
    created_seq: int


def propose_instruction(
    assertion: AssertionEnvelope,
    *,
    created_seq: int,
    scope: Scope = "global",
    scope_ref: str | None = None,
    priority: int = 50,
    min_trust: float = 0.7,
) -> StandingInstruction | None:
    """An instruction from one user-stated record, or ``None``. Refused: a tentative, contested, superseded or
    hidden record; a non-asserted, unbound or negated one; an assistant-proposed, tool, document, external or
    derived source; trust below ``min_trust``."""
    a = assertion.assessment
    if (
        assertion.status != "active"
        or a.modality != "asserted"
        or a.binding != "bound"
        or assertion.src_class not in _FIRM_SOURCES
        or a.source_trust < min_trust
    ):
        return None
    if not 0 <= priority <= 100:
        raise ValueError("priority must be in [0, 100]")
    if (scope == "global") != (scope_ref is None):
        raise ValueError("scope_ref is required for, and only for, topic and session scopes")
    text = assertion.text.strip()
    if not text:
        return None
    rid = assertion.record_id or assertion.occurrence_id
    sc = assertion.scope
    return StandingInstruction(
        id="si_" + _digest(sc.tenant, sc.owner, normalise_name(text), scope, scope_ref)[:16],
        tenant=sc.tenant,
        owner=sc.owner,
        text=text,
        scope=scope,
        scope_ref=scope_ref,
        priority=priority,
        source_ids=(rid,),
        trust=a.source_trust,
        created_seq=created_seq,
    )


def live_instructions(
    instructions: Iterable[StandingInstruction], live_record_ids: Iterable[str]
) -> list[StandingInstruction]:
    """Keep instructions whose every source record is still live (not erased, superseded or hidden)."""
    live = set(live_record_ids)
    return [i for i in instructions if i.source_ids and all(s in live for s in i.source_ids)]


def select_instructions(
    instructions: Iterable[StandingInstruction],
    *,
    tenant: str,
    owner: str,
    session: str | None = None,
    topics: Iterable[str] = (),
    budget: int,
    cost: Cost = _words,
) -> list[StandingInstruction]:
    """Applicable instructions for a turn, highest priority first (then session > topic > global, newest,
    id). Other owners' instructions never apply; identical texts collapse to the first; the budget is hard."""
    topic_set = {normalise_name(t) for t in topics}
    ok = [
        i
        for i in instructions
        if (i.tenant, i.owner) == (tenant, owner)
        and (
            i.scope == "global"
            or (i.scope == "session" and session is not None and i.scope_ref == session)
            or (
                i.scope == "topic"
                and i.scope_ref is not None
                and normalise_name(i.scope_ref) in topic_set
            )
        )
    ]
    ok.sort(key=lambda i: (-i.priority, -_SPECIFICITY[i.scope], -i.created_seq, i.id))
    out: list[StandingInstruction] = []
    seen: set[str] = set()
    used = 0
    for i in ok:
        norm = normalise_name(i.text)
        c = cost(i.text)
        if norm in seen or used + c > budget:
            continue
        seen.add(norm)
        out.append(i)
        used += c
    return out
