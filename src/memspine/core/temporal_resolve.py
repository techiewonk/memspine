"""Deterministic resolution of relative-time phrases against an anchor date (H1).

A turn written on 15 July 2023 that says "last Friday" refers to 14 July 2023. Readers
resolve such phrases badly: on LoCoMo, 88 of memspine's 120 wrong temporal answers gave a
date, just the wrong one, with the evidence in context. :func:`annotate` appends the absolute
date each phrase denotes, ``last Friday [= Fri 2023-07-14]``, so the reader does not have to
compute it. Rules only, English only, no model. Vague phrases ("recently", "the other day",
"a few days ago") are deliberately left alone: a wrong resolution is worse than none.

Conventions (stated, not inferred):
- ``last <weekday>``: the most recent such weekday strictly before the anchor day.
- ``next <weekday>``: the first such weekday strictly after the anchor day.
- ``this <weekday>``: that weekday in the anchor's Monday-to-Sunday week.
- ``last/this/next week``: Monday-to-Sunday calendar weeks.
- ``last weekend``: the most recent Saturday-Sunday that ended before the anchor day.
- ``last/this/next month|year``: calendar months and years.
- ``N weeks ago``: the anchor minus 7N days. ``N months/years ago``: the month or year only,
  marked approximate (``≈``).
- Seasons (northern hemisphere, meteorological): summer = Jun-Aug, and so on; marked ``≈``.

G13, ``anchored=True`` (``read.relative_dates_anchored``): LoCoMo's gold labels state week-
level phrases relative to the day they were said ("The week before 9 June 2023", "The
weekend before 17 July 2023", "A few days before 24 May 2023"), and the calendar week of
"last week" misses that span by up to six days. Anchored mode names the relation to the
anchor day and gives the span it denotes:

- ``last/past week``: ``the week before <d>``, the seven days before the anchor day;
  ``next week``: ``the week after <d>``, the seven days after it; ``this week``: ``the week
  of <d>`` (the calendar week, unchanged).
- ``last/this/next weekend``: ``the weekend before / of / after <d>`` (spans unchanged).
- ``last/next <weekday>``: ``the Friday before / Saturday after <d>`` (day unchanged).
- ``N weeks ago``: ``N weeks before <d>`` (day unchanged, ``≈``).
- ``a few days ago`` (left alone otherwise): ``a few days before <d>``, with no span:
  restating the relation is not a guess at a day.

Months, years, seasons and single days are calendar units in the gold too, so they are
rendered as without ``anchored``.

#58, ``week="preceding_7_days"`` (``read.relative_week``): only the span of ``last/past
week`` and ``next week`` changes, to the seven days before (after) the anchor day; the label
stays an absolute span (``[= 2023-06-02..2023-06-08]``) with no relation phrase, and every
other phrase is resolved as in the default ``calendar`` mode. ``this week`` stays the
calendar week. Anchored mode already uses these spans.

C5, ``durations=True`` (``read.resolve_durations``): an LLM reader is bad at duration
arithmetic ("how long has she painted?"), so an unambiguous number+unit duration is
resolved against the anchor day, always marked ``about``:

- ``for N years|months`` (ongoing: "has been ... for 3 years"), ``N years|months now``,
  ``a month now``: ``[= since about 2020]`` / ``[= since about 2023-04]``.
- ``N weeks|days now``: ``[= since about 2023-05-06]``.
- ``since 2019`` / ``since March 2019``: ``[= about 4 years as of 2023-05-20]``.

A bare "for 3 years" with no ongoing cue ("stayed for 3 years") is left alone, as are
"for years" and "for a while". ``N years ago`` is resolved by the base rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

__all__ = ["Resolution", "WeekMode", "annotate", "resolve"]

_MONTHS = (
    *("january", "february", "march", "april", "may", "june"),
    *("july", "august", "september", "october", "november", "december"),
)
_NUMBERS_D = {"eleven": 11, "twelve": 12}
_DNUM = (
    r"(?P<n>\d{1,2}|a couple of|couple of|an?|one|two|three|four|five|six|seven|eight|nine"
    r"|ten|eleven|twelve)"
)
_DURATIONS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rx, re.I))
    for name, rx in (
        ("dur_now", rf"\b(?:for )?{_DNUM} (?P<unit>year|month|week|day)s? now\b"),
        ("dur_for", rf"\bfor {_DNUM} (?P<unit>year|month)s?\b(?! ago)"),
        ("since", rf"\bsince (?:(?P<mon>{'|'.join(_MONTHS)}) )?(?P<y>(?:19|20)\d\d)\b"),
    )
)
#: an ongoing-state cue before a bare "for N years": "has been ... for 3 years".
_ONGOING = re.compile(r"\b(?:been|have|has|'ve|am|is|are|do|does)\b(?! to\b)", re.I)

#: #58: how ``last/next week`` resolve (see the module docstring).
WeekMode = Literal["calendar", "preceding_7_days"]

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_NUMBERS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "a couple of": 2,
    "couple of": 2,
}
_SEASONS = {
    "spring": (3, 5),
    "summer": (6, 8),
    "fall": (9, 11),
    "autumn": (9, 11),
    "winter": (12, 2),
}

_NUM = r"(?P<n>\d{1,2}|a couple of|couple of|an?|one|two|three|four|five|six|seven|eight|nine|ten)"
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rx, re.I))
    for name, rx in (
        ("day_before_yesterday", r"\bthe day before yesterday\b"),
        ("day_after_tomorrow", r"\bthe day after tomorrow\b"),
        ("ago", rf"\b{_NUM} (?P<unit>day|week|month|year)s? ago\b"),
        ("few_days_ago", r"\b(?:a )?few days ago\b"),  # anchored mode only
        ("rel_weekday", rf"\b(?P<rel>last|this|next|past) (?P<wd>{'|'.join(_WEEKDAYS)})\b"),
        ("rel_span", r"\b(?P<rel>last|this|next|past) (?P<unit>week|weekend|month|year)\b"),
        (
            "rel_season",
            r"\b(?P<rel>last|this|next|past) (?P<season>spring|summer|fall|autumn|winter)\b",
        ),
        ("last_night", r"\blast night\b"),
        ("yesterday", r"\byesterday\b"),
        ("today", r"\b(?:today|tonight)\b"),
        ("tomorrow", r"\btomorrow\b"),
    )
)


@dataclass(frozen=True, slots=True)
class Resolution:
    """One resolved phrase: character span in the text, inclusive date range, label.

    ``relation`` (G13 anchored mode) names the phrase relative to its anchor day ("the
    week before 2023-06-09"); the label is then the relation, followed by the span in
    parentheses unless ``bounded`` is false (the span is only a rough reading).
    """

    start: int
    end: int
    phrase: str
    first: date
    last: date
    approximate: bool = False
    relation: str = ""
    bounded: bool = True

    @property
    def label(self) -> str:
        if not self.relation:
            return self.span_label
        if not self.bounded:
            return self.relation
        return f"{self.relation} ({self.span_label})"

    @property
    def span_label(self) -> str:
        """The absolute span alone: a day, a month, a year or ``first..last``."""
        mark = "≈ " if self.approximate else ""
        if self.first == self.last:
            return f"{mark}{self.first:%a %Y-%m-%d}"
        if (
            self.first.day == 1
            and self.first.month == 1
            and self.last.month == 12
            and self.last.day == 31
            and self.first.year == self.last.year
        ):
            return f"{mark}{self.first:%Y}"
        if self.first.day == 1 and _month_end(self.first) == self.last:
            return f"{mark}{self.first:%Y-%m}"
        return f"{mark}{self.first:%Y-%m-%d}..{self.last:%Y-%m-%d}"


def _month_end(d: date) -> date:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    return date(d.year + m // 12, m % 12 + 1, 1)


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _span(
    name: str, m: re.Match[str], d: date, week: WeekMode = "calendar"
) -> tuple[date, date, bool] | None:
    if name == "yesterday" or name == "last_night":
        x = d - timedelta(days=1)
        return x, x, False
    if name == "today":
        return d, d, False
    if name == "tomorrow":
        x = d + timedelta(days=1)
        return x, x, False
    if name == "day_before_yesterday":
        x = d - timedelta(days=2)
        return x, x, False
    if name == "day_after_tomorrow":
        x = d + timedelta(days=2)
        return x, x, False
    if name == "few_days_ago":  # a rough reading; anchored mode shows the relation only
        return d - timedelta(days=6), d - timedelta(days=1), True
    if name == "ago":
        n = _NUMBERS.get(m["n"].lower()) if not m["n"].isdigit() else int(m["n"])
        if n is None:
            return None
        unit = m["unit"].lower()
        if unit == "day":
            x = d - timedelta(days=n)
            return x, x, False
        if unit == "week":
            x = d - timedelta(days=7 * n)
            return x, x, True
        if unit == "month":
            first = _add_months(date(d.year, d.month, 1), -n)
            return first, _month_end(first), True
        return date(d.year - n, 1, 1), date(d.year - n, 12, 31), True
    rel = m["rel"].lower() if "rel" in m.re.groupindex else ""
    if name == "rel_weekday":
        target = _WEEKDAYS.index(m["wd"].lower())
        if rel in ("last", "past"):
            delta = (d.weekday() - target) % 7 or 7
            x = d - timedelta(days=delta)
        elif rel == "next":
            delta = (target - d.weekday()) % 7 or 7
            x = d + timedelta(days=delta)
        else:  # this
            x = _week_start(d) + timedelta(days=target)
        return x, x, False
    if name == "rel_span":
        unit = m["unit"].lower()
        step = {"last": -1, "past": -1, "this": 0, "next": 1}[rel]
        if unit == "week":
            if week == "preceding_7_days" and step < 0:
                return d - timedelta(days=7), d - timedelta(days=1), False
            if week == "preceding_7_days" and step > 0:
                return d + timedelta(days=1), d + timedelta(days=7), False
            start = _week_start(d) + timedelta(days=7 * step)
            return start, start + timedelta(days=6), False
        if unit == "weekend":
            if step < 0:  # the most recent Saturday-Sunday whose Sunday is before the anchor day
                back = (d.weekday() - 6) % 7 or 7
                sun = d - timedelta(days=back)
            else:  # this weekend: Sat-Sun of the anchor's week; next weekend: the one after
                sun = _week_start(d) + timedelta(days=6 + 7 * step)
            return sun - timedelta(days=1), sun, False
        if unit == "month":
            first = _add_months(date(d.year, d.month, 1), step)
            return first, _month_end(first), False
        y = d.year + step
        return date(y, 1, 1), date(y, 12, 31), False
    if name == "rel_season":
        season = m["season"].lower()
        if season == "winter":  # December of year y to February of year y+1
            in_winter = d.month in (12, 1, 2)
            this = d.year - 1 if d.month <= 2 else d.year  # current, or the coming one
            if rel in ("last", "past"):  # the most recent winter that has ended
                y = this - 1 if in_winter else d.year - 1
            elif rel == "next":
                y = this + 1
            else:
                y = this
            return date(y, 12, 1), _month_end(date(y + 1, 2, 1)), True
        a, b = _SEASONS[season]
        if rel in ("last", "past"):
            y = d.year if d.month > b else d.year - 1
        elif rel == "next":
            y = d.year if d.month < a else d.year + 1
        else:
            y = d.year
        return date(y, a, 1), _month_end(date(y, b, 1)), True
    return None


def _relate(
    name: str, m: re.Match[str], d: date, first: date, last: date
) -> tuple[str, date, date, bool]:
    """G13: ``(relation, first, last, bounded)`` of one phrase in anchored mode.

    ``relation`` is empty for the phrases anchored mode leaves as they are (days,
    months, years, seasons, ``this <weekday>``).
    """
    on = f"{d:%Y-%m-%d}"
    rel = m["rel"].lower() if "rel" in m.re.groupindex else ""
    word = {"last": "before", "past": "before", "this": "of", "next": "after"}.get(rel, "")
    if name == "few_days_ago":
        return f"a few days before {on}", first, last, False
    if name == "ago" and m["unit"].lower() == "week":
        n = m["n"].lower()
        unit = "week" if n in ("a", "an", "one", "1") else "weeks"
        return f"{n} {unit} before {on}", first, last, True
    if name == "rel_span" and m["unit"].lower() == "week":
        if word == "before":
            return f"the week before {on}", d - timedelta(days=7), d - timedelta(days=1), True
        if word == "after":
            return f"the week after {on}", d + timedelta(days=1), d + timedelta(days=7), True
        return f"the week of {on}", first, last, True
    if name == "rel_span" and m["unit"].lower() == "weekend":
        return f"the weekend {word} {on}", first, last, True
    if name == "rel_weekday" and word in ("before", "after"):
        return f"the {m['wd'].capitalize()} {word} {on}", first, last, True
    return "", first, last, True


def _duration(name: str, m: re.Match[str], text: str, d: date) -> str | None:
    """C5: the ``[= ...]`` label of one duration phrase, or None when it is not clear."""
    if name == "since":
        y = int(m["y"])
        mon = _MONTHS.index(m["mon"].lower()) + 1 if m["mon"] else None
        months = (d.year - y) * 12 + (d.month - mon if mon else 0)
        if months <= 0 or (mon is None and d.year - y < 1):
            return None  # this month / year or the future: no span to state
        yrs, rem = divmod(months, 12)
        if mon is None:
            rem = 0
        parts = []
        if yrs:
            parts.append(f"{yrs} year{'s' if yrs != 1 else ''}")
        if rem:
            parts.append(f"{rem} month{'s' if rem != 1 else ''}")
        return f"about {' '.join(parts)} as of {d:%Y-%m-%d}"
    n_raw = m["n"].lower()
    n = int(n_raw) if n_raw.isdigit() else {**_NUMBERS, **_NUMBERS_D}.get(n_raw)
    if not n:
        return None
    unit = m["unit"].lower()
    if name == "dur_for":
        before = re.split(r"[.!?;]", text[max(0, m.start() - 50) : m.start()])[-1]
        if not _ONGOING.search(before):
            return None  # "stayed for 3 years": a finished span, not a start
    if unit == "year":
        return f"since about {d.year - n}"
    if unit == "month":
        return f"since about {_add_months(date(d.year, d.month, 1), -n):%Y-%m}"
    days = n * (7 if unit == "week" else 1)
    return f"since about {d - timedelta(days=days):%Y-%m-%d}"


def resolve(
    text: str,
    anchor: datetime | date,
    *,
    anchored: bool = False,
    week: WeekMode = "calendar",
    durations: bool = False,
) -> list[Resolution]:
    """All non-overlapping relative-time phrases in ``text``, resolved against ``anchor``.

    ``anchored`` (G13) states week-level phrases relative to the anchor day, LoCoMo's
    convention (see the module docstring); off, the output is unchanged. ``week`` (#58)
    picks the span of ``last/next week``: the calendar week (default) or the seven days
    before / after the anchor day.
    """
    d = anchor.date() if isinstance(anchor, datetime) else anchor
    found: list[Resolution] = []
    taken: list[tuple[int, int]] = []
    for name, rx in _PATTERNS:  # ordered most specific first
        if name == "few_days_ago" and not anchored:
            continue  # vague: left alone unless the relation itself is what is shown
        for m in rx.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            span = _span(name, m, d, week)
            if span is None:
                continue
            first, last, approx = span
            relation, bounded = "", True
            if anchored:
                relation, first, last, bounded = _relate(name, m, d, first, last)
            found.append(
                Resolution(m.start(), m.end(), m.group(0), first, last, approx, relation, bounded)
            )
            taken.append((m.start(), m.end()))
    if durations:  # C5: after the base phrases, so "3 years ago" is never re-read
        for name, rx in _DURATIONS:
            for m in rx.finditer(text):
                if any(m.start() < e and s < m.end() for s, e in taken):
                    continue
                label = _duration(name, m, text, d)
                if label is None:
                    continue
                found.append(Resolution(m.start(), m.end(), m.group(0), d, d, True, label, False))
                taken.append((m.start(), m.end()))
    return sorted(found, key=lambda r: r.start)


def annotate(
    text: str,
    anchor: datetime | date,
    *,
    anchored: bool = False,
    week: WeekMode = "calendar",
    durations: bool = False,
) -> str:
    """``text`` with ``[= <absolute date>]`` after every resolved relative phrase."""
    out, pos = [], 0
    for r in resolve(text, anchor, anchored=anchored, week=week, durations=durations):
        out.append(text[pos : r.end])
        out.append(f" [= {r.label}]")
        pos = r.end
    out.append(text[pos:])
    return "".join(out)
