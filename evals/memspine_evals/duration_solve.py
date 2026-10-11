"""I76: a duration / interval solver as an opt-in reader post-step (``--duration-solve``).

A 9B reader gets the endpoints of a "how many months passed between X and Y" question right
(every relative phrase on the cited lines carries a resolved date) and the subtraction wrong:
16 of the 61 full-persp-loc temporal failures. This step does the subtraction in code. No model
call, no gold, no category: question shape, context lines and the reader's answer only (I37).

Shapes it reads (generic English, no dataset word):

* **interval**: ``how many <unit> ... between A and B`` / ``how long ... between A and B``;
* **elapsed**: ``how long has it been since A`` / ``how many <unit> since A``, the end being a
  date in the question (``as of March 2021``) or the question date.

Anything else ("how long did it take", "after how many weeks did X", one event with two
mentions) abstains: the second endpoint cannot be named from the question.

Endpoints. Each event clause is matched to the context line that shares the most stems with it
(a distinguishing word of the clause is required; a tie between lines of different dates, or one
line for both events, abstains). The line's event date is the single date it states (an absolute
date, or a relative phrase resolved by ``core/temporal_resolve`` against the line's own date), else
the line's date; a line with several different dates abstains. Week and month dates are
approximate and make the answer an "about".

Two modes (one design each, both fail closed):

* ``rewrite`` (default): after the reader answers, compare its number, in the unit asked, with
  the computed one; on disagreement, or when the reader refused, replace the answer. Zero extra
  calls, and the reader is untouched whenever the solver abstains or agrees.
* ``hint``: before the reader answers, put one computed line at the top of the context
  ("Computed by calendar arithmetic: ... = 3 months (92 days)") and let the reader answer.
  Reader keeps the wording; a wrong hint can mislead it, so this mode is only for a screen that
  measures it (the offline estimate cannot).

The decision (rewritten / agrees / hinted / abstain reason, the endpoints, the arithmetic and the
reader's own value) is recorded in ``meta["duration_solve"]``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .contracts import ReaderAnswer

__all__ = [
    "DURATION_SOLVE_MODES",
    "DURATION_SOLVE_VERSION",
    "DurationSolveReader",
    "Solution",
    "check_answer",
    "solve_duration",
]

DURATION_SOLVE_VERSION = "v1"
DURATION_SOLVE_MODES = ("rewrite", "hint")

_UNIT_DAYS = {"day": 1.0, "week": 7.0, "month": 30.4375, "year": 365.25}
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "half": 0.5,
}  # fmt: skip
_NUM = r"\d+(?:\.\d+)?|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True))
_AMOUNT = re.compile(rf"\b(?P<n>{_NUM})\s*(?:and a half )?(?P<u>day|week|month|year)s?\b", re.I)
_BARE_NUMBER = re.compile(rf"^\W*(?P<n>{_NUM})\b", re.I)

_HOW_MANY = re.compile(r"\bhow many (?P<u>day|week|month|year)s?\b", re.I)
_HOW_LONG = re.compile(r"\bhow long\b", re.I)
_BETWEEN = re.compile(r"\bbetween\b(?P<rest>.+?)\??\s*$", re.I)
_SINCE = re.compile(r"\bsince\b(?P<rest>.+?)(?:,?\s+as of\s+(?P<asof>[^?]+?))?\??\s*$", re.I)
_LINE = re.compile(r"^\s*(?:\*\s*|\[hit \d+\]\s*)?\[(\d{4}-\d{2}-\d{2})(?: [A-Za-z]{3})?\]\s*(.*)$")
_ANNOTATION = re.compile(r"\s*\[=[^\]]*\]")
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
_MON = "|".join(_MONTHS)
_MONTH_YEAR = re.compile(rf"\b(?P<m>{_MON})\s+(?P<y>(?:19|20)\d\d)\b", re.I)
_STOP = frozenset(
    ["the", "a", "an", "of", "to", "in", "on", "at", "and", "or", "for", "with", "by", "from", "as", "is", "was", "were", "did", "do", "does", "what", "when", "which", "who", "how", "where", "why", "that", "this", "it", "his", "her", "their", "many", "long", "much", "has", "have", "had", "been", "passed", "pass", "lapsed", "elapse", "elapsed", "take", "took", "time", "between", "since", "after", "before", "after", "first", "second", "he", "she", "they", "them", "him", "i", "you", "we", "my", "me", "our", "us", "be", "being", "not", "no"]
)
_WORD = re.compile(r"[A-Za-z][A-Za-z']*")


@dataclass(frozen=True, slots=True)
class Endpoint:
    clause: str
    day: date
    approx: bool
    line: str  # the line's text, trimmed
    basis: str = ""  # verb_line | first_mention | question


@dataclass(frozen=True, slots=True)
class Solution:
    unit: str  # unit of the answer
    n: int  # rounded amount in ``unit``
    days: int
    approx: bool
    first: Endpoint
    second: Endpoint

    @property
    def phrase(self) -> str:
        return f"{self.n} {self.unit}{'' if self.n == 1 else 's'}"

    def text(self) -> str:
        lead = "About " if self.approx else ""
        return (
            f"{lead}{self.phrase} (from {self.first.day.isoformat()} to "
            f"{self.second.day.isoformat()}, {self.days} days)"
        )

    def hint(self) -> str:
        return (
            f'Computed by calendar arithmetic: "{self.first.clause}" is on {self.first.day.isoformat()}'
            f' and "{self.second.clause}" is on {self.second.day.isoformat()}, {self.days} days '
            f"apart = {'about ' if self.approx else ''}{self.phrase}."
        )


# ---- question side ---------------------------------------------------------------------------


def _stems(text: str) -> list[str]:
    out = []
    for w in _WORD.findall(text):
        low = w.lower().strip("'")
        if low in _STOP or len(low) < 2:
            continue
        out.append(low[:5] if len(low) > 5 else low)
    return out


def _lowercase_words(text: str) -> list[str]:
    """Content words that are not capitalised (the verbs and nouns, not the names)."""
    return [w for w in _WORD.findall(text) if w[0].islower() and w.lower() not in _STOP]


def _split_events(question: str) -> tuple[str, str, str] | None:
    """``(kind, clause 1, clause 2)`` from the question, or None. ``kind``: interval | elapsed."""
    m = _BETWEEN.search(question)
    if m:
        parts = re.split(r"\s+and\s+", m["rest"].strip(" ?.,"), maxsplit=1, flags=re.I)
        if len(parts) == 2 and all(p.strip() for p in parts):
            a, b = parts[0].strip(), parts[1].strip()
            if len(_stems(b)) <= 2 and not _lowercase_words(b):  # "Rex and Max": share the verb
                shared = " ".join(_lowercase_words(a))
                b = f"{shared} {b}".strip()
            return "interval", a, b
        return None
    m = _SINCE.search(question)
    if m:
        asof = (m["asof"] or "").strip()
        return "elapsed", m["rest"].strip(" ?.,"), asof
    return None


def _question_unit(question: str) -> str:
    m = _HOW_MANY.search(question)
    return m["u"].lower() if m else ""


def is_duration_question(question: str) -> bool:
    return bool(_HOW_MANY.search(question) or _HOW_LONG.search(question)) and (
        _split_events(question) is not None
    )


# ---- context side ----------------------------------------------------------------------------


def _lines(context: str) -> list[tuple[date, str]]:
    out = []
    for raw in context.splitlines():
        m = _LINE.match(raw)
        if m:
            out.append((datetime.strptime(m.group(1), "%Y-%m-%d").date(), m.group(2)))
    return out


def _line_dates(text: str, said: date) -> list[tuple[date, bool]]:
    """Distinct ``(date, approximate)`` a line states: absolute dates and resolved phrases."""
    from memspine.core.temporal_resolve import resolve

    from .date_repair import _abs_dates

    bare = _ANNOTATION.sub("", text)  # re-resolve from the raw wording, not the printed label
    found: dict[date, bool] = {}
    for _, _, d in _abs_dates(bare):
        found[d] = False
    for r in resolve(bare, said, weekdays=True):
        if r.kind != "date":
            continue
        if r.first == r.last:
            found.setdefault(r.first, r.approximate)
        else:  # a span (week, month, season): its middle, approximate
            mid = r.first + (r.last - r.first) / 2
            found.setdefault(mid, True)
    for m in _MONTH_YEAR.finditer(bare):
        mon = _MONTHS.index(m["m"].lower()) + 1
        found.setdefault(date(int(m["y"]), mon, 15), True)
    return sorted(found.items())


_SPEAKER = re.compile(r"^([A-Z][\w'-]*):")


def _speaker_stems(lines: list[tuple[date, str]]) -> set[str]:
    out = set()
    for _, t in lines:
        m = _SPEAKER.match(t)
        if m:
            low = m.group(1).lower()
            out.add(low[:5] if len(low) > 5 else low)
    return out


def _endpoint(clause: str, said: date, text: str, basis: str) -> Endpoint | str:
    dates = _line_dates(text, said)
    if len(dates) > 1:
        return "line_has_several_dates"
    day, approx = dates[0] if dates else (said, False)
    return Endpoint(clause, day, approx, text[:160], basis)


def _locate(clause: str, lines: list[tuple[date, str]]) -> Endpoint | str:
    """The endpoint of one event clause, or the reason it cannot be named.

    The clause must name its event by a capitalised word that is not a speaker ("Rex" in
    "Ana adopting Rex"); the rarest such word in the context anchors the event. Lines that
    hold the anchor AND a verb stem of the clause (``adopt``) are the event lines; when no line
    holds a verb stem, the first mention of the anchor is the event ("meet Rex, my puppy").
    Event lines that disagree by more than a week abstain.
    """
    speakers = _speaker_stems(lines)
    names = [
        (w.lower()[:5] if len(w) > 5 else w.lower())
        for w in _WORD.findall(clause)
        if w[0].isupper()
    ]
    names = [n for n in names if n not in speakers and n not in _STOP]
    verbs = {(w.lower()[:5] if len(w) > 5 else w.lower()) for w in _lowercase_words(clause)}
    if not names:
        return "event_has_no_name"
    stem_sets = [(d, t, set(_stems(t))) for d, t in lines]
    df = {n: sum(n in s for _, _, s in stem_sets) for n in names}
    present = [n for n in names if df[n] > 0]
    if not present:
        return "event_not_found"
    anchor = min(present, key=lambda n: df[n])
    holders = [(d, t, s) for d, t, s in stem_sets if anchor in s]
    with_verb = [(d, t) for d, t, s in holders if verbs & s]
    if with_verb:
        eps = [_endpoint(clause, d, t, "verb_line") for d, t in with_verb]
        good = [e for e in eps if isinstance(e, Endpoint)]
        if not good:
            return str(eps[0])
        good.sort(key=lambda e: e.day)
        if (good[-1].day - good[0].day).days > 7:
            return "event_lines_disagree"
        return good[0]
    d, t, _ = min(holders, key=lambda h: h[0])
    return _endpoint(clause, d, t, "first_mention")


def _asof_date(asof: str, question_date: str | None) -> tuple[date, bool] | None:
    if asof:
        m = _MONTH_YEAR.search(asof)
        if m:
            return date(int(m["y"]), _MONTHS.index(m["m"].lower()) + 1, 15), True
        from .date_repair import _abs_dates

        found = _abs_dates(asof)
        if found:
            return found[0][2], False
        return None
    if question_date:
        try:
            return datetime.strptime(question_date[:10], "%Y-%m-%d").date(), False
        except ValueError:
            return None
    return None


def _pick_unit(days: int) -> str:
    if days < 14:
        return "day"
    if days <= 7 * 7:
        return "week"
    if days <= 700:
        return "month"
    return "year"


def solve_duration(
    question: str, context: str, question_date: str | None = None
) -> tuple[Solution | None, dict[str, Any]]:
    """``(solution, meta)``; the solution is None (and ``meta["decision"]`` says why) to abstain."""
    split = _split_events(question)
    if split is None or not (_HOW_MANY.search(question) or _HOW_LONG.search(question)):
        return None, {}  # not a duration question: no record at all
    kind, c1, c2 = split
    meta: dict[str, Any] = {"version": DURATION_SOLVE_VERSION, "kind": kind}
    lines = _lines(context)
    if not lines:
        return None, {**meta, "decision": "abstain:no_dated_lines"}
    e1 = _locate(c1, lines)
    if isinstance(e1, str):
        return None, {**meta, "decision": f"abstain:event1_{e1}", "clause1": c1}
    if kind == "interval":
        e2 = _locate(c2, lines)
        if isinstance(e2, str):
            return None, {**meta, "decision": f"abstain:event2_{e2}", "clause2": c2}
        if e2.line == e1.line:
            return None, {**meta, "decision": "abstain:same_line", "clause2": c2}
    else:
        end = _asof_date(c2, question_date)
        if end is None:
            return None, {**meta, "decision": "abstain:no_end_date"}
        e2 = Endpoint(c2 or "the question date", end[0], end[1], "", "question")
    days = abs((e2.day - e1.day).days)
    if days == 0:
        return None, {**meta, "decision": "abstain:zero_days"}
    unit = _question_unit(question) or _pick_unit(days)
    value = days / _UNIT_DAYS[unit]
    n = int(value + 0.5)
    if n < 1:
        return None, {**meta, "decision": "abstain:rounds_to_zero", "days": days, "unit": unit}
    approx = e1.approx or e2.approx or abs(value - n) > 0.25
    sol = Solution(unit, n, days, approx, *((e1, e2) if e1.day <= e2.day else (e2, e1)))
    meta.update(
        {
            "decision": "solved",
            "unit": unit,
            "n": n,
            "days": days,
            "approx": approx,
            "endpoints": [
                {
                    "clause": e.clause,
                    "date": e.day.isoformat(),
                    "approx": e.approx,
                    "basis": e.basis,
                    "line": e.line,
                }
                for e in (e1, e2)
            ],
        }
    )
    return sol, meta


# ---- answer side -----------------------------------------------------------------------------


def _amount(raw: str) -> float:
    low = raw.lower()
    return float(low) if low.replace(".", "", 1).isdigit() else float(_NUMBER_WORDS[low])


def reader_days(answer: str, unit_hint: str) -> float | None:
    """The reader's duration in days (compound answers summed), or None when it states none."""
    total, found = 0.0, False
    for m in _AMOUNT.finditer(answer):
        total += _amount(m["n"]) * _UNIT_DAYS[m["u"].lower()]
        found = True
    if found:
        return total
    m = _BARE_NUMBER.match(answer)
    if m and unit_hint:
        return _amount(m["n"]) * _UNIT_DAYS[unit_hint]
    return None


def check_answer(sol: Solution, answer: str) -> tuple[str, float | None]:
    """``("agrees"|"disagrees"|"refusal"|"unparsed", reader's amount in the solution's unit)``."""
    from .refusal import is_refusal

    if is_refusal(answer):
        return "refusal", None
    days = reader_days(answer, sol.unit)
    if days is None:
        return "unparsed", None
    theirs = days / _UNIT_DAYS[sol.unit]
    return ("agrees" if int(theirs + 0.5) == sol.n else "disagrees"), theirs


# ---- the reader wrapper ----------------------------------------------------------------------


class DurationSolveReader:
    """Wraps a reader: a duration / interval question is solved by code (no model call)."""

    def __init__(self, inner: Any, mode: str = "rewrite") -> None:
        if mode not in DURATION_SOLVE_MODES:
            raise ValueError(f"duration_solve must be one of {DURATION_SOLVE_MODES}, got {mode!r}")
        self.inner = inner
        self.mode = mode
        self.guard = getattr(inner, "guard", None)
        self.reader_id = f"{inner.reader_id}+durationsolve-{mode}"
        self.model = inner.model
        self.makes_model_calls = getattr(inner, "makes_model_calls", True)
        self.rewritten = 0
        self.hinted = 0

    def describe(self) -> Mapping[str, Any]:
        return {**self.inner.describe(), "duration_solve": f"{DURATION_SOLVE_VERSION}/{self.mode}"}

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        try:
            sol, meta = solve_duration(question, context, question_date)
        except Exception:  # an enhancer: the reader's answer stands
            sol, meta = None, {}
        if self.mode == "hint" and sol is not None:
            self.hinted += 1
            first = await self.inner.answer(question, f"{sol.hint()}\n{context}", question_date)
            return _with_meta(first, first.text, {**meta, "mode": "hint", "decision": "hinted"})
        first = await self.inner.answer(question, context, question_date)
        if not meta:
            return first
        if sol is None:  # abstained: record why, leave the answer
            return _with_meta(first, first.text, {**meta, "mode": self.mode})
        verdict, theirs = check_answer(sol, first.text)
        meta = {**meta, "mode": self.mode, "reader_value": theirs, "reader_check": verdict}
        if verdict in ("agrees", "unparsed"):  # nothing to correct, or too unclear to overwrite
            return _with_meta(first, first.text, {**meta, "decision": verdict})
        self.rewritten += 1
        return _with_meta(
            first, sol.text(), {**meta, "decision": "rewritten", "original": first.text}
        )


def _with_meta(first: ReaderAnswer, text: str, meta: dict[str, Any]) -> ReaderAnswer:
    return ReaderAnswer(
        text=text,
        prompt_tokens=first.prompt_tokens,
        completion_tokens=first.completion_tokens,
        latency_ms=first.latency_ms,
        model_calls=first.model_calls,
        truncated=first.truncated,
        cached_prompt_tokens=first.cached_prompt_tokens,
        finish_reason=first.finish_reason,
        raw_text=first.raw_text,
        prompt_variant=first.prompt_variant,
        extra_meta={**dict(first.extra_meta), "duration_solve": meta},
    )


__all__ += ["is_duration_question", "reader_days"]
