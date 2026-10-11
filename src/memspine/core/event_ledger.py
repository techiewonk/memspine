"""A02: a read-time event ledger (pure; the engine makes the one structured call).

For a question about events or state ("when did she attend ...", "did the trip happen", "how
many times ...") the retrieved lines mention events, plans, cancellations and repeats of all of
them, and a reader conflates the four: a plan is read as a fact, a repeated mention as a second
event, a mention date as the event date. The engine asks ONE bounded ``extract@events`` call
over the numbered evidence lines for *candidate event mentions* and this module does the rest in
code:

* :func:`validate_events` keeps a mention only when its line exists and its quoted span is on
  that line (case, punctuation and spacing aside); its status is normalised to ``planned`` /
  ``done`` / ``cancelled`` (anything else is dropped with a reason);
* the event time is resolved from the *quoted time phrase* by calendar rules (an absolute date,
  or a relative phrase resolved against the line's own date, ``temporal_resolve``); with no
  phrase the interval is unknown, never the mention date. The mention time is the date of the
  line, kept apart;
* :func:`link_mentions` joins two mentions into one event only on IDENTITY EVIDENCE: the later
  mention names the earlier one (``same_as``), the same actor, a quoted identity cue that is on
  its line, an exact normalised action/object overlap, and event-time intervals that do not
  conflict (two done reports on distant days are two events; a plan may slip). Same words on another day, a different object, or an interval that does not overlap
  stay two events. The model's claim alone never links;
* the latest mention's status is the event's state; every mention stays in its history;
* :func:`render_ledger` writes a compact block: one line per event with the status, the event
  time (or ``time unknown``), the mention times and the source lines.

Nothing here stores anything and no dataset word appears: the cues are the grammar of repetition
("the same", "again", "as planned", "that trip"), the statuses are the three of the spec.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from memspine.core.temporal_resolve import resolve

__all__ = [
    "EventMention",
    "LedgerEvent",
    "STATUSES",
    "is_event_question",
    "link_mentions",
    "normalise_status",
    "render_ledger",
    "validate_events",
]

STATUSES = ("planned", "done", "cancelled")
_STATUS_ALIASES = {
    "planned": "planned",
    "plan": "planned",
    "scheduled": "planned",
    "upcoming": "planned",
    "intended": "planned",
    "future": "planned",
    "done": "done",
    "completed": "done",
    "complete": "done",
    "happened": "done",
    "occurred": "done",
    "past": "done",
    "finished": "done",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "called_off": "cancelled",
    "postponed": "cancelled",
    "abandoned": "cancelled",
}
_PUNCT = re.compile(r"[^\w\s]+", re.U)
_IDENTITY_CUE = re.compile(
    r"\b(?:the same|same (?:one|trip|event|thing)|again|as (?:planned|scheduled|promised|mentioned)|"
    r"that (?:trip|event|meeting|visit|party|class|game|concert|appointment|plan)|the (?:trip|event|"
    r"meeting|visit|party|class|game|concert|appointment) (?:i|we|she|he|they) "
    r"(?:mentioned|planned)|finally|it (?:finally )?(?:happened|went ahead)|got cancel+ed|fell through)\b",
    re.I,
)
_ISO = re.compile(r"\b((?:19|20)\d\d)-(\d{2})-(\d{2})\b")
_MONTHS = {
    m: i
    for i, ms in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for m in ms
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DMY = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_RE})\b\.?,?(?:\s+((?:19|20)\d\d))?", re.I)
_MDY = re.compile(rf"\b({_MONTH_RE})\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+((?:19|20)\d\d))?", re.I)

_EVENT_CUE = re.compile(
    r"\b(?:when|how (?:many times|often|long ago|long)|plan(?:ned|s)?|cancel+ed|postpone[d]?|"
    r"schedul\w+|still|already|yet|finally|first time|last time|latest|most recent|again|"
    r"did (?:\w+ ){1,3}(?:go|attend|visit|happen|take place|finish|start)|"
    r"has (?:\w+ )?(?:happened|occurred)|before|after)\b",
    re.I,
)
_EVENT_TYPES = frozenset({"date", "duration", "count"})


def is_event_question(query: str, answer_type: str = "unknown") -> bool:
    """True for a question about events or state: a date / duration / count question, or one
    that uses the grammar of plans and outcomes. Surface cues only; no dataset vocabulary."""
    return answer_type in _EVENT_TYPES or bool(_EVENT_CUE.search(query))


def normalise_status(value: str) -> str | None:
    key = re.sub(r"[\s-]+", "_", (value or "").strip().lower())
    return _STATUS_ALIASES.get(key)


def _fold(text: str) -> str:
    return " ".join(_PUNCT.sub(" ", text.casefold().replace("_", " ")).split())


_STEM = re.compile(r"(?:ing|ed|es|s)$")


def _head(text: str) -> frozenset[str]:
    """The content words of an action / object, stemmed lightly, for an exact overlap test."""
    stop = {"the", "a", "an", "to", "of", "for", "in", "on", "at", "and", "her", "his", "their", "my"}
    return frozenset(_STEM.sub("", w) if len(w) > 4 else w for w in _fold(text).split() if w not in stop)


@dataclass(frozen=True, slots=True)
class EventMention:
    index: int  # 1-based, in the model's order
    actor: str
    action: str
    obj: str
    status: str
    line: int
    span: str
    when: str  # the quoted time phrase, "" if none
    mention: date | None  # the date of the line
    start: date | None  # event-time interval, resolved by code
    end: date | None
    same_as: int = 0
    identity: str = ""


@dataclass(slots=True)
class LedgerEvent:
    id: str
    mentions: list[EventMention] = field(default_factory=list)
    linked_on: list[str] = field(default_factory=list)

    @property
    def last(self) -> EventMention:
        return max(self.mentions, key=lambda m: (m.mention or date.min, m.index))

    @property
    def status(self) -> str:
        return self.last.status

    @property
    def interval(self) -> tuple[date, date] | None:
        for m in sorted(self.mentions, key=lambda m: (m.mention or date.min, m.index), reverse=True):
            if m.start is not None and m.end is not None:
                return m.start, m.end
        return None


def _span_on_line(span: str, line: str) -> bool:
    s, ln = _fold(span), _fold(line)
    return bool(s) and s in ln


def _absolute(text: str) -> tuple[date, date] | None:
    m = _ISO.search(text)
    try:
        if m:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d, d
        m = _DMY.search(text)
        if m and m.group(3):
            d = date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
            return d, d
        m = _MDY.search(text)
        if m and m.group(3):
            d = date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
            return d, d
    except ValueError:
        return None
    return None


def event_interval(when: str, anchor: date | datetime | None) -> tuple[date, date] | None:
    """The event-time interval a quoted time phrase denotes: an absolute date, or a relative
    phrase resolved against ``anchor`` (the line's date); None for anything else. Approximate
    resolutions ("three weeks ago", a season) are accepted as intervals but never as a day."""
    if not when.strip():
        return None
    absolute = _absolute(when)
    if absolute is not None:
        return absolute
    if anchor is None:
        return None
    found = resolve(when, anchor)
    if not found:
        return None
    first = found[0]
    return first.first, first.last


def validate_events(
    raw: Iterable[object],
    lines: Sequence[str],
    line_dates: Sequence[date | datetime | None],
) -> tuple[list[EventMention], list[dict[str, object]]]:
    """Keep the mentions whose line exists, whose span is on it, with a known status and a
    time phrase (if any) that is on the same line. Returns ``(mentions, dropped)``; every drop
    carries its reason."""
    kept: list[EventMention] = []
    dropped: list[dict[str, object]] = []
    for i, ev in enumerate(raw, start=1):
        line_no = int(getattr(ev, "line", 0) or 0)
        drop = None
        if not 1 <= line_no <= len(lines):
            drop = "no_such_line"
        elif not _span_on_line(getattr(ev, "span", ""), lines[line_no - 1]):
            drop = "span_not_on_line"
        status = normalise_status(getattr(ev, "status", ""))
        if drop is None and status is None:
            drop = "status"
        when = (getattr(ev, "when", "") or "").strip()
        if drop is None and when and not _span_on_line(when, lines[line_no - 1]):
            when = ""  # a time phrase not on the line is not used: the interval stays unknown
        actor = (getattr(ev, "actor", "") or "").strip()
        action = (getattr(ev, "action", "") or "").strip()
        if drop is None and not (actor and action):
            drop = "incomplete"
        if drop is not None or status is None:
            dropped.append({"index": i, "reason": drop or "status"})
            continue
        anchor = line_dates[line_no - 1]
        interval = event_interval(when, anchor)
        mention = anchor.date() if isinstance(anchor, datetime) else anchor
        kept.append(
            EventMention(
                index=i,
                actor=actor,
                action=action,
                obj=(getattr(ev, "object", "") or "").strip(),
                status=status,
                line=line_no,
                span=getattr(ev, "span", ""),
                when=when,
                mention=mention,
                start=interval[0] if interval else None,
                end=interval[1] if interval else None,
                same_as=int(getattr(ev, "same_as", 0) or 0),
                identity=(getattr(ev, "identity", "") or "").strip(),
            )
        )
    return kept, dropped


def _intervals_compatible(a: LedgerEvent, b: EventMention) -> bool:
    """Two reports of a DONE event can be one unless both have an event-time interval and the
    intervals are apart by more than a week (an event that happened on two distant days is two
    events). A plan may slip or be called off, so the check is not made against a planned or
    cancelled mention."""
    iv = a.interval
    if iv is None or b.start is None or b.end is None:
        return True
    gap = max(iv[0], b.start) - min(iv[1], b.end)
    return gap <= timedelta(days=7)


def link_mentions(
    mentions: Sequence[EventMention], lines: Sequence[str]
) -> tuple[list[LedgerEvent], list[dict[str, object]]]:
    """Group mentions into events. A mention joins an earlier event only on identity evidence:
    its ``same_as`` names an earlier mention, same actor, the action or object words overlap
    exactly, the intervals do not conflict, and the quoted ``identity`` phrase (or the span) holds
    an identity cue that is on the mention's own line. Anything less starts a new event; the
    refused links are returned with their reason."""
    events: list[LedgerEvent] = []
    owner: dict[int, LedgerEvent] = {}
    refused: list[dict[str, object]] = []
    for m in mentions:
        target: LedgerEvent | None = None
        if m.same_as and m.same_as in owner and m.same_as != m.index:
            candidate = owner[m.same_as]
            prev = next(x for x in candidate.mentions if x.index == m.same_as)
            reason = None
            if _fold(prev.actor) != _fold(m.actor):
                reason = "actor"
            elif not (_head(prev.action) & _head(m.action)) and not (
                _head(prev.obj) and _head(prev.obj) == _head(m.obj)
            ):
                reason = "no_overlap"
            elif _head(prev.obj) and _head(m.obj) and not (_head(prev.obj) & _head(m.obj)):
                reason = "other_object"
            elif prev.status == "done" and m.status == "done" and not _intervals_compatible(
                candidate, m
            ):
                reason = "interval_conflict"
            else:
                cue_text = m.identity or m.span
                line = lines[m.line - 1]
                if not _IDENTITY_CUE.search(cue_text) or (
                    m.identity and not _span_on_line(m.identity, line)
                ):
                    reason = "no_identity_cue"
            if reason is None:
                target = candidate
                candidate.linked_on.append(f"{m.index}->{m.same_as}")
            else:
                refused.append({"index": m.index, "same_as": m.same_as, "reason": reason})
        if target is None:
            target = LedgerEvent(id=f"E{len(events) + 1}")
            events.append(target)
        target.mentions.append(m)
        owner[m.index] = target
    return events, refused


def render_ledger(
    events: Sequence[LedgerEvent],
    *,
    heading: str,
    max_events: int,
    fits: Callable[[str], bool] = lambda _t: True,
) -> tuple[str, list[LedgerEvent]]:
    """The compact ledger block and the events it shows (fewest events that fit ``fits``)."""
    shown = list(events[:max_events])
    while shown:
        rows = [heading]
        for ev in shown:
            last = ev.last
            iv = ev.interval
            when = (
                "time unknown"
                if iv is None
                else f"{iv[0]:%Y-%m-%d}" if iv[0] == iv[1] else f"{iv[0]:%Y-%m-%d}..{iv[1]:%Y-%m-%d}"
            )
            said = ", ".join(
                f"{m.mention:%Y-%m-%d} {m.status} (L{m.line})" if m.mention else f"{m.status} (L{m.line})"
                for m in sorted(ev.mentions, key=lambda m: (m.mention or date.min, m.index))
            )
            obj = f" {last.obj}" if last.obj else ""
            rows.append(
                f"{ev.id} [{ev.status}] {last.actor} {last.action}{obj}; event time: {when}; "
                f"mentions: {said}"
            )
        text = "\n".join(rows)
        if fits(text):
            return text, shown
        shown = shown[:-1]
    return "", []
