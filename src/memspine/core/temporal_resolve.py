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
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

__all__ = ["Resolution", "annotate", "resolve"]

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
    """One resolved phrase: character span in the text, inclusive date range, label."""

    start: int
    end: int
    phrase: str
    first: date
    last: date
    approximate: bool = False

    @property
    def label(self) -> str:
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


def _span(name: str, m: re.Match[str], d: date) -> tuple[date, date, bool] | None:
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


def resolve(text: str, anchor: datetime | date) -> list[Resolution]:
    """All non-overlapping relative-time phrases in ``text``, resolved against ``anchor``."""
    d = anchor.date() if isinstance(anchor, datetime) else anchor
    found: list[Resolution] = []
    taken: list[tuple[int, int]] = []
    for name, rx in _PATTERNS:  # ordered most specific first
        for m in rx.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            span = _span(name, m, d)
            if span is None:
                continue
            first, last, approx = span
            found.append(Resolution(m.start(), m.end(), m.group(0), first, last, approx))
            taken.append((m.start(), m.end()))
    return sorted(found, key=lambda r: r.start)


def annotate(text: str, anchor: datetime | date) -> str:
    """``text`` with ``[= <absolute date>]`` after every resolved relative phrase."""
    out, pos = [], 0
    for r in resolve(text, anchor):
        out.append(text[pos : r.end])
        out.append(f" [= {r.label}]")
        pos = r.end
    out.append(text[pos:])
    return "".join(out)
