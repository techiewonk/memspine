"""I57: deterministic repair of the reader's date answer (``--date-repair``).

The reader resolves a relative date badly even when the line it reads carries the right
anchor: "Friday, 2021-03-14" for the Friday before 14 March 2021 (14 March 2021 is a Sunday), "last
Friday" left unresolved, a weekday with no date. This post-step runs on the answer of a "when"
question and never calls a model (0 extra calls):

* **relative phrase** ("last week", "yesterday", "two weeks ago", "last Friday"): found by the
  engine's own rules (``memspine.core.temporal_resolve``), resolved against the date of the
  context line it was read from, and rewritten to the absolute date or range the engine would
  print (``the Friday before 2021-03-15 (Fri 2021-03-12)``). The original wording is kept in
  parentheses and in ``meta["date_repair"]``.
* **bare weekday** ("on Friday", no date): same, as ``last <weekday>`` (the most recent such
  weekday before the line's date).
* **weekday and date that disagree** ("Friday, 2021-03-14" when 14 March is a Sunday): if the date
  is the date of a context line, the reader copied the line's date and named the weekday of the
  event, so the weekday wins (the Friday before that date); otherwise the date wins and the
  weekday name is corrected.

Which line? The one that contains the phrase and shares the most content words with the question
and the answer. Fail closed: no line, or a tie between lines of different dates, leaves the answer
untouched. An answer that already holds an absolute date next to the phrase, a refusal, and a
duration question ("how long") are not touched. Question shape, answer text and context only;
never the gold or the category (rule I37).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from .contracts import ReaderAnswer

__all__ = ["DATE_REPAIR_VERSION", "DateRepairReader", "repair_date_answer"]

DATE_REPAIR_VERSION = "v1"

_LINE = re.compile(r"^\s*(?:\*\s*|\[hit \d+\]\s*)?\[(\d{4}-\d{2}-\d{2})\]\s*(.*)$")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = (
    "january february march april may june july august september october november december"
).split()
_MON = "|".join(m for m in _MONTHS) + "|" + "|".join(m[:3] for m in _MONTHS)
_ABS = re.compile(
    rf"\b(?P<iso>\d{{4}}-\d{{2}}-\d{{2}})\b"
    rf"|\b(?P<dmy>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<m1>{_MON})\.?,?\s+(?P<y1>\d{{4}})\b"
    rf"|\b(?P<m2>{_MON})\.?\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y2>\d{{4}})\b",
    re.I,
)
_WD = "|".join(_WEEKDAYS)
_WD_RE = re.compile(rf"\b(?P<wd>{_WD})\b", re.I)
_DURATION_Q = re.compile(r"\bhow (?:long|many)\b", re.I)
_STOP = frozenset(
    "the a an of to in on at and or for with by from as is was were did do does what when "
    "which who how where why that this it his her their".split()
)
_WORD = re.compile(r"[a-z0-9]+")
_DUMMY = date(2000, 1, 3)


def _month_no(name: str) -> int:
    n = name.lower()[:3]
    return next(i + 1 for i, m in enumerate(_MONTHS) if m[:3] == n)


def _abs_dates(text: str) -> list[tuple[int, int, date]]:
    out: list[tuple[int, int, date]] = []
    for m in _ABS.finditer(text):
        try:
            if m["iso"]:
                d = datetime.strptime(m["iso"], "%Y-%m-%d").date()
            elif m["dmy"]:
                d = date(int(m["y1"]), _month_no(m["m1"]), int(m["dmy"]))
            else:
                d = date(int(m["y2"]), _month_no(m["m2"]), int(m["d2"]))
        except ValueError:
            continue
        out.append((m.start(), m.end(), d))
    return out


def _content(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1}


def _lines(context: str) -> list[tuple[date, str]]:
    out = []
    for raw in context.splitlines():
        m = _LINE.match(raw)
        if m:
            out.append((datetime.strptime(m.group(1), "%Y-%m-%d").date(), m.group(2)))
    return out


def _anchor(
    phrase: str, question: str, answer: str, lines: list[tuple[date, str]]
) -> date | None:
    """The date of the line the phrase was read from, or None when it cannot be told."""
    needle = phrase.lower()
    with_phrase = [(d, t) for d, t in lines if needle in t.lower()]
    pool, floor = (with_phrase, 1) if with_phrase else (lines, 2)
    if not pool:
        return None
    q, a = _content(question), _content(answer) - _content(phrase)
    scored = sorted(
        ((len(q & _content(t)) * 2 + len(a & _content(t)), d) for d, t in pool),
        key=lambda x: -x[0],
    )
    best, d0 = scored[0]
    if best < floor:
        return None
    if any(s == best and d != d0 for s, d in scored[1:]):
        return None  # a tie between lines of different dates: fail closed
    return d0


def _label(phrase: str, anchor: date) -> tuple[str, date, date] | None:
    from memspine.core.temporal_resolve import resolve

    found = resolve(phrase, anchor, anchored=True)
    if len(found) != 1 or found[0].phrase.lower() != phrase.lower():
        return None
    r = found[0]
    return r.label, r.first, r.last


def repair_date_answer(question: str, answer: str, context: str) -> tuple[str, dict[str, Any]] | None:
    """``(new answer, meta)`` or None when nothing is to be repaired (see the module docstring)."""
    from memspine.core.query_shape import is_temporal
    from memspine.core.temporal_resolve import resolve

    from .refusal import is_refusal

    if not answer.strip() or not is_temporal(question) or _DURATION_Q.search(question):
        return None
    if is_refusal(answer):
        return None
    lines = _lines(context)
    absolutes = _abs_dates(answer)

    # weekday that disagrees with the date next to it
    for m in _WD_RE.finditer(answer):
        for s, e, d in absolutes:
            near = (0 <= s - m.end() <= 4) or (0 <= m.start() - e <= 4)
            if not near or _WEEKDAYS[d.weekday()] == m["wd"].lower():
                continue
            lo, hi = min(m.start(), s), max(m.end(), e)
            line_dates = {ld for ld, _ in lines}
            if d in line_dates:
                lab = _label(f"last {m['wd']}", d)
                if lab is None:
                    continue
                new = f'{lab[0]} (originally "{answer[lo:hi]}")'
                rule = "weekday_vs_line_date"
            else:
                new = f"{d.strftime('%A')}, {d.isoformat()}"
                rule = "weekday_fixed"
            return answer[:lo] + new + answer[hi:], {
                "rule": rule,
                "original": answer,
                "anchor": d.isoformat(),
                "version": DATE_REPAIR_VERSION,
            }

    if absolutes:  # an absolute date is already there: nothing relative to resolve
        return None

    # a relative phrase the engine resolves, or a bare weekday
    spans = [(r.start, r.end, r.phrase) for r in resolve(answer, _DUMMY, anchored=True)]
    for m in _WD_RE.finditer(answer):
        if not any(s <= m.start() < e for s, e, _ in spans):
            spans.append((m.start(), m.end(), m["wd"]))
    if len(spans) != 1:  # none, or several: too ambiguous to rewrite
        return None
    start, end, phrase = spans[0]
    anchor = _anchor(phrase, question, answer, lines)
    if anchor is None:
        return None
    is_weekday = phrase.lower() in _WEEKDAYS
    lab = _label(f"last {phrase}" if is_weekday else phrase, anchor)
    if lab is None:
        return None
    new = f'{lab[0]} (originally "{phrase}")'
    return answer[:start] + new + answer[end:], {
        "rule": "bare_weekday" if is_weekday else "relative_phrase",
        "original": answer,
        "phrase": phrase,
        "anchor": anchor.isoformat(),
        "version": DATE_REPAIR_VERSION,
    }


class DateRepairReader:
    """Wraps a reader: the answer to a "when" question is repaired by code (no model call)."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.guard = getattr(inner, "guard", None)
        self.reader_id = f"{inner.reader_id}+daterepair"
        self.model = inner.model
        self.makes_model_calls = getattr(inner, "makes_model_calls", True)
        self.repaired = 0

    def describe(self) -> Mapping[str, Any]:
        return {**self.inner.describe(), "date_repair": DATE_REPAIR_VERSION}

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        first: ReaderAnswer = await self.inner.answer(question, context, question_date)
        try:
            fixed = repair_date_answer(question, first.text, context)
        except Exception:  # an enhancer: the reader's answer stands
            fixed = None
        if fixed is None:
            return first
        self.repaired += 1
        text, meta = fixed
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
            extra_meta={**dict(first.extra_meta), "date_repair": meta},
        )
