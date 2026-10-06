"""#29: the date a mined fact HAPPENED, as opposed to the day it was SAID.

In 10 of combo-A's 29 wrong absolute dates on LoCoMo the reader answered with the day
the event was mentioned. A mined fact therefore carries its happened date in a
``happened:<date>`` tag, found deterministically where possible: the H1 resolution
(:mod:`memspine.core.temporal_resolve`) of a relative phrase in the fact or in the
turns it was mined from, each against its own date. Pure functions, no model.

A happened date is written as a day (``2023-07-14``), a month (``2023-07``), a year
(``2023``) or an inclusive span of days (``2023-06-02..2023-06-08``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime

from memspine.core.records import MemoryRecord
from memspine.core.temporal_resolve import WeekMode, resolve

__all__ = [
    "HAPPENED_PREFIX",
    "SAID_PREFIX",
    "anchor_turn",
    "cited_turns",
    "date_anchor",
    "happened_label",
    "happened_of",
    "happened_tag",
    "label_span",
    "label_start",
    "normalise_label",
    "resolve_happened",
    "said_differs",
    "said_tag",
]

#: The tag prefix of a mined fact's happened date.
HAPPENED_PREFIX = "happened:"
#: The tag prefix of the day a happened-tagged fact was said: the anchor its relative
#: phrases were resolved against, so a read resolves them against the same day.
SAID_PREFIX = "said:"

_LABEL = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2}(?:\.\.\d{4}-\d{2}-\d{2})?)?)?$")


def happened_label(first: date, last: date) -> str:
    """A day, a whole calendar month, a whole year, else ``first..last``."""
    if first == last:
        return first.isoformat()
    if (first.month, first.day, last.month, last.day) == (1, 1, 12, 31) and (
        first.year == last.year
    ):
        return f"{first:%Y}"
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    if first.day == 1 and (nxt - last).days == 1 and first.month == last.month:
        return f"{first:%Y-%m}"
    return f"{first.isoformat()}..{last.isoformat()}"


def normalise_label(value: str | None) -> str | None:
    """A model-written date (``YYYY-MM-DD``, ``YYYY-MM``, ``YYYY`` or a ``..`` span)
    as a label, or None when it is not one of those shapes."""
    if not value:
        return None
    text = value.strip()
    return text if _LABEL.match(text) else None


def label_start(label: str) -> date | None:
    """The first day a label denotes (``2023-07`` -> 2023-07-01), None if malformed."""
    head = label.split("..", 1)[0]
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None


def label_span(label: str) -> tuple[date, date] | None:
    """The inclusive ``(first, last)`` days a label denotes (``2023-07`` ->
    2023-07-01..2023-07-31), None if malformed."""
    if not _LABEL.match(label):
        return None
    head, _, tail = label.partition("..")
    first = label_start(head)
    if first is None:
        return None
    if tail:
        try:
            last = date.fromisoformat(tail)
        except ValueError:
            return None
        return (first, last) if last >= first else None
    if len(head) == 4:
        return first, date(first.year, 12, 31)
    if len(head) == 7:
        nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        return first, date.fromordinal(nxt.toordinal() - 1)
    return first, first


def resolve_happened(
    texts: Iterable[tuple[str, date | datetime]], *, week: WeekMode = "calendar"
) -> tuple[date, date] | None:
    """The one span the relative phrases of ``texts`` denote, each text resolved
    against its own anchor; None when there is none, or when they disagree.

    Approximate resolutions ("three weeks ago", "last summer") are ignored: a wrong
    happened date is worse than none.
    """
    spans: set[tuple[date, date]] = set()
    for text, anchor in texts:
        for r in resolve(text, anchor, week=week):
            if not r.approximate:
                spans.add((r.first, r.last))
    return next(iter(spans)) if len(spans) == 1 else None


def happened_tag(label: str) -> str:
    return f"{HAPPENED_PREFIX}{label}"


def said_tag(said: date | datetime) -> str:
    day = said.date() if isinstance(said, datetime) else said
    return f"{SAID_PREFIX}{day.isoformat()}"


def date_anchor(record: MemoryRecord) -> date | datetime | None:
    """The anchor a read resolves ``record``'s relative phrases against, or None
    when they must not be resolved at all.

    A happened-tagged fact's ``valid_from`` may have been moved to the event day, so
    resolving "yesterday" against it would shift the date twice: its ``said:`` tag
    is the anchor. A happened-tagged fact written before that tag existed, whose
    ``valid_from`` is its happened day, is not resolved. Anything else: ``valid_from``.
    """
    happened = happened_of(record)
    if happened is None:
        return record.valid_from
    for tag in record.tags:
        if tag.startswith(SAID_PREFIX):
            try:
                return date.fromisoformat(tag[len(SAID_PREFIX) :])
            except ValueError:
                break
    if label_start(happened) == record.valid_from.date():
        return None
    return record.valid_from


def happened_of(record: MemoryRecord) -> str | None:
    """The happened date a mined fact was tagged with, or None."""
    for tag in record.tags:
        if tag.startswith(HAPPENED_PREFIX):
            return tag[len(HAPPENED_PREFIX) :] or None
    return None


def said_differs(said: date | datetime, happened: str) -> bool:
    """True unless ``happened`` is exactly the day ``said``."""
    day = said.date() if isinstance(said, datetime) else said
    return happened != day.isoformat()


def anchor_turn(
    phrases_of: str, cited: Sequence[MemoryRecord], *, week: WeekMode = "calendar"
) -> MemoryRecord | None:
    """The cited turn a fact's relative phrases were said in: the latest cited turn
    containing every phrase of ``phrases_of`` (case-insensitive), else the latest
    cited turn; None when nothing is cited."""
    if not cited:
        return None
    phrases = [r.phrase.lower() for r in resolve(phrases_of, cited[0].valid_from, week=week)]
    latest = max(cited, key=lambda m: m.valid_from)
    if not phrases:
        return latest
    holding = [m for m in cited if all(p in m.content.lower() for p in phrases)]
    return max(holding, key=lambda m: m.valid_from) if holding else latest


def cited_turns(members: Sequence[MemoryRecord], turns: Sequence[int]) -> list[MemoryRecord]:
    """The cited 1-based ``turns`` of ``members`` (out-of-range numbers ignored)."""
    return [members[n - 1] for n in turns if 1 <= n <= len(members)]
