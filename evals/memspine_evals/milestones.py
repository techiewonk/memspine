"""E05: temporal milestone extraction, then deterministic computation (``--milestones``).

Extends I76 (``duration_solve.py``). The regex solver names an event by the words the question
shares with one context line and abstains on every other shape ("how long did it take", "after how
many weeks did X", "the first ... the second ..."): 6 of the 38 duration questions of the full run.
This post-step asks ONE bounded structured model call for the *milestones*, never for the answer,
and does the rest in code:

1. **Extract (one call, I69 repair / retry).** The model reads the cited, dated context lines
   (numbered ``L1``..) and returns candidate milestones: actor, event predicate, an ``event_key``
   (one label per real-world event, shared by its mentions), status (``done`` | ``ongoing`` |
   ``planned`` | ``cancelled`` | ``unknown``), the source line id and the *exact span* of that line
   that carries the time (the relative expression, if any), plus which two milestones the question
   spans, the unit it asks for and the ordinals it names. The model never states a date or a number.
2. **Resolve (code).** Each milestone's date comes from its span: an absolute date, or a relative
   phrase resolved by ``core/temporal_resolve`` against the line's own date; with no time phrase the
   line's date, flagged approximate when the span is vague ("just", "recently").
3. **Validate (code).** Both endpoints must cite a line of the context and quote a span that is on
   that line; status must be consistent with the question (a ``planned`` endpoint only when the
   question asks about a plan; ``cancelled`` / ``unknown`` never); same actor when the question
   needs it; endpoints in chronological order; ``first`` / ``second`` / ``last`` checked against the
   dated mentions of the same event; two reports of one event that disagree by more than a week
   (or by any day when both are exact) are a conflict.
4. **Compute (code).** Elapsed time = ``end - start`` in calendar days (the start day is not
   counted; ``inclusive=True`` counts both), converted to the unit the question asks (calendar
   months, then ``days / 30.4375`` for the remainder), rounded to nearest (or ``floor`` for completed
   units). An approximate endpoint or a non-integer value is stated as "about".

Fail closed: any missing operand, ambiguity or conflict leaves the reader's answer untouched and
records why in ``meta["milestones"]``. The model's own arithmetic is never read. Like I76 it only
rewrites an answer that disagrees with the computed number (or a refusal), and it skips questions
I76 already settled, so the extra call is spent only where the regex solver abstained.

Generic: dated conversational memory only; no dataset word, no category, no gold (I37).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import ReaderAnswer
from .duration_solve import (
    _UNIT_DAYS,
    Endpoint,
    Solution,
    _asof_date,
    _line_dates,
    _lines,
    _pick_unit,
    _question_unit,
    check_answer,
)
from .tokens import HeuristicTokenCounter

__all__ = [
    "MILESTONES_VERSION",
    "MilestoneOut",
    "MilestoneReader",
    "compute_from_milestones",
    "elapsed_in_unit",
    "extract_milestones",
    "is_milestone_question",
    "numbered_dated_lines",
]

MILESTONES_VERSION = "v1"
ROUNDINGS = ("nearest", "floor")
_MAX_LINES = 80
_MAX_LINE_CHARS = 400
_CONFLICT_DAYS = 7  # two reports of one event further apart than this (any day if exact) conflict
_SETTLED = frozenset({"rewritten", "agrees", "unparsed", "hinted"})  # I76 already decided

# ---- question gate (code only; a non-duration question costs no call) --------------------------

_UNIT = r"(?:day|week|month|year)s?"
_HOW_LONG = re.compile(r"\bhow long\b", re.I)
_HOW_MANY_UNIT = re.compile(rf"\bhow many {_UNIT}\b", re.I)
_AFTER_HOW_MANY = re.compile(rf"\bafter how many {_UNIT}\b", re.I)
_HOW_MUCH_TIME = re.compile(r"\bhow much time\b", re.I)
_CONNECTIVE = re.compile(
    r"\b(?:after|before|since|between|until|till|passed|elapsed|lapsed|apart|later|earlier|"
    r"ago|from|to|take|took|taken|did it|does it|would it|will it|had)\b",
    re.I,
)
_FREQUENCY = re.compile(rf"\bhow many {_UNIT} (?:a|per|each|every) ", re.I)
_PLAN_WORDS = re.compile(
    r"\b(?:plan(?:s|ned|ning)?|schedul\w*|expect\w*|upcoming|will|going to|due|until|till|"
    r"intend\w*|book(?:ed|ing)|appointment)\b",
    re.I,
)
_ORDINALS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1, "final": -1,
    "earliest": 1, "latest": -1, "initial": 1,
}  # fmt: skip


def is_milestone_question(question: str) -> bool:
    """A duration-shaped question: how long / how many <unit> with a temporal connective."""
    if _FREQUENCY.search(question):
        return False
    if _AFTER_HOW_MANY.search(question) or _HOW_MUCH_TIME.search(question):
        return True
    if _HOW_LONG.search(question):
        return True
    return bool(_HOW_MANY_UNIT.search(question) and _CONNECTIVE.search(question))


def _ordinals_in(question: str) -> set[int]:
    low = question.lower()
    return {v for k, v in _ORDINALS.items() if re.search(rf"\b{k}\b", low)}


# ---- the model's output ------------------------------------------------------------------------


class Milestone(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    line: int
    actor: str = ""
    event: str = ""
    event_key: str = ""
    status: str = "unknown"
    span: str = ""

    @field_validator("line", mode="before")
    @classmethod
    def _line_no(cls, v: Any) -> Any:
        if isinstance(v, str):
            m = re.search(r"\d+", v)
            return int(m.group(0)) if m else v
        return v

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, v: Any) -> str:
        return str(v).strip()

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v: Any) -> str:
        return str(v or "unknown").strip().lower()


class MilestoneOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    #: ``between`` (two milestones), ``since`` (milestone to the question date), ``until``
    #: (question date to a milestone) or ``other`` (not a duration the milestones answer).
    shape: str = "other"
    unit: str = ""
    start: str = ""
    end: str = ""
    start_ordinal: int | None = None
    end_ordinal: int | None = None
    same_actor: bool = False
    milestones: list[Milestone] = Field(default_factory=list)

    @field_validator("shape", "unit", mode="before")
    @classmethod
    def _lower(cls, v: Any) -> str:
        return str(v or "").strip().lower()

    @field_validator("start", "end", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        return "" if v is None else str(v).strip()

    @field_validator("start_ordinal", "end_ordinal", mode="before")
    @classmethod
    def _ord(cls, v: Any) -> Any:
        if isinstance(v, str):
            return (
                _ORDINALS.get(v.strip().lower()) if not v.strip().lstrip("-").isdigit() else int(v)
            )
        return v


# ---- prompt and extraction call ----------------------------------------------------------------

_SYSTEM = (
    "You extract time milestones from dated conversation lines. You never compute a date, a "
    "duration or any number, and you never answer the question. Reply with one JSON object only."
)

_BODY = """\
Question: {{ question }}

Dated lines (each line starts with its id, then the date the line was written):
{{ context }}

List the milestones that the question's start and end points could be, then say which two it spans.

A milestone is one event mention: the beginning, a transition, or the end of something
(started, signed, moved, finished, got the offer, opened). Rules:
- "line": the id of the line it comes from (for example "L3"). Use only ids shown above.
- "span": the exact words copied from that line that say WHEN it happened (for example "two weeks
  ago", "last Friday", "in March 2023", "on 4 May"). Copy characters exactly. If the line gives no
  time words, use "" (the line's own date is then used).
- "actor": who did it, as written in the line. "event": a short predicate. "event_key": one short
  lowercase label per real-world event, THE SAME label for every mention of the same event.
- "status": done, ongoing, planned (not yet happened, scheduled or intended), cancelled, or unknown.
  A planned or merely discussed event is not done. An interview is not an acceptance.
- Include every mention of a repeated event (all visits, all trips) as its own milestone.

Then fill:
- "shape": "between" if the question spans two events (also "how long did X take": the start
  and the end of X), "since" if it spans one event and the present/question date, "until" if it
  spans the present and a future event, else "other".
- "start" and "end": milestone ids ("" for the present).
- "unit": the unit the question asks for (day, week, month or year), "" if none.
- "start_ordinal" / "end_ordinal": the number if the question names which occurrence of that
  event ("first" = 1, "second" = 2, "last" = -1), else null.
- "same_actor": true if both ends must be the same person's.

JSON shape:
{"shape": "between", "unit": "week", "start": "m1", "end": "m2", "start_ordinal": null,
 "end_ordinal": null, "same_actor": true,
 "milestones": [{"id": "m1", "line": "L3", "actor": "...", "event": "...", "event_key": "...",
                 "status": "done", "span": "..."}]}
"""


def _prompt() -> Any:
    from memspine.prompts.base import Prompt, PromptFormat

    return Prompt(
        id="temporal_milestones",
        version=1,
        role="milestone_extraction",
        format=PromptFormat.JSON,
        system=_SYSTEM,
        body=_BODY,
    )


Chat = Callable[..., Awaitable[str]]


def numbered_dated_lines(context: str) -> list[tuple[date, str]]:
    """The dated context lines the prompt shows (and the validator indexes): at most
    ``_MAX_LINES``, text clipped to ``_MAX_LINE_CHARS``, resolver annotations removed."""
    from .duration_solve import _ANNOTATION

    out = []
    for day, text in _lines(context)[:_MAX_LINES]:
        out.append((day, _ANNOTATION.sub("", text).strip()[:_MAX_LINE_CHARS]))
    return out


def render_lines(lines: Sequence[tuple[date, str]]) -> str:
    return "\n".join(f"L{i} [{d.isoformat()}] {t}" for i, (d, t) in enumerate(lines, start=1))


async def extract_milestones(
    chat: Chat, question: str, lines: Sequence[tuple[date, str]]
) -> MilestoneOut:
    """ONE structured call (``structured_call``: format-aware parse, JSON repair, opt-in retry)."""
    from memspine.services.llm.structured import structured_call

    class _ChatLLM:
        provider_id = "harness:milestones"

        async def chat(self, messages: list[dict[str, str]], **_: Any) -> str:
            system = next((m["content"] for m in messages if m["role"] == "system"), None)
            user = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
            return await chat(user, system=system)

    return await structured_call(
        _ChatLLM(),  # type: ignore[arg-type]
        _prompt(),
        {"question": question, "context": render_lines(lines)},
        MilestoneOut,
    )


# ---- elapsed-time convention -------------------------------------------------------------------


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 + months, 12)
    m += 1
    last = [
        31,
        29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ][m - 1]
    return date(y, m, min(d.day, last))


def elapsed_in_unit(
    start: date, end: date, unit: str, *, inclusive: bool = False, rounding: str = "nearest"
) -> tuple[float, int, int]:
    """``(exact value in unit, rounded n, days)`` for ``end - start`` (``start <= end``).

    Days: calendar days between the two dates (the start day is not counted); ``inclusive`` counts
    both end days (+1). Months: whole calendar months from ``start`` plus the remaining days over
    30.4375; years: months / 12; weeks: days / 7. ``rounding``: ``nearest`` (half up) or ``floor``
    (completed units only).
    """
    if rounding not in ROUNDINGS:
        raise ValueError(f"rounding must be one of {ROUNDINGS}, got {rounding!r}")
    days = (end - start).days + (1 if inclusive else 0)
    if unit == "day":
        value = float(days)
    elif unit == "week":
        value = days / 7.0
    elif unit in ("month", "year"):
        whole = (end.year - start.year) * 12 + end.month - start.month
        if _add_months(start, whole) > end:
            whole -= 1
        rest = (end - _add_months(start, whole)).days + (1 if inclusive else 0)
        value = whole + rest / _UNIT_DAYS["month"]
        if unit == "year":
            value /= 12.0
    else:
        raise ValueError(f"unit must be day, week, month or year, got {unit!r}")
    n = int(value) if rounding == "floor" else int(value + 0.5)
    return value, n, days


# ---- resolve and validate (code) ---------------------------------------------------------------

_VAGUE = re.compile(
    r"\b(?:just|recently|lately|earlier|soon|these days|the other day|a while|a bit ago|"
    r"a few|some time|sometime|around|about|roughly|approximately|ish)\b",
    re.I,
)
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def _in_line(span: str, line: str) -> bool:
    return bool(span) and _norm(span) in _norm(line)


@dataclass(frozen=True, slots=True)
class _Resolved:
    ms: Milestone
    day: date
    approx: bool
    basis: str  # span | line_date
    line_text: str
    line_day: date


def _resolve_one(
    ms: Milestone, lines: Sequence[tuple[date, str]], allow_planned: bool
) -> _Resolved | str:
    if not 1 <= ms.line <= len(lines):
        return "line_not_in_context"
    line_day, text = lines[ms.line - 1]
    status = ms.status
    if status in ("cancelled", "canceled", "unknown", ""):
        return f"status_{status or 'unknown'}"
    if status == "planned" and not allow_planned:
        return "planned_not_done"
    span = ms.span.strip()
    if span and not _in_line(span, text):
        return "span_not_on_line"
    if span:
        found = _line_dates(span, line_day)
        if len(found) > 1:
            return "span_has_several_dates"
        if found:
            day, approx = found[0]
            return _Resolved(ms, day, approx, "span", text, line_day)
    if status == "planned":
        return "planned_without_date"  # a plan's date is never the day it was mentioned
    vague = bool(span and _VAGUE.search(span))  # "just", "recently": not an exact timestamp
    return _Resolved(ms, line_day, vague, "line_date", text, line_day)


def _actor(a: str) -> str:
    return _norm(a).strip(" .:")


def _conflicts(
    chosen: _Resolved, all_resolved: Sequence[_Resolved], ordinal: int | None
) -> str | None:
    """A reason when ``chosen`` is not the one reading of its event."""
    key = _norm(chosen.ms.event_key)
    if not key:
        return None
    same = [
        r
        for r in all_resolved
        if _norm(r.ms.event_key) == key
        and _actor(r.ms.actor) == _actor(chosen.ms.actor)
        and (r.ms.status == "planned") == (chosen.ms.status == "planned")
    ]
    days = sorted({r.day for r in same})
    if ordinal is not None:
        if ordinal < 0:
            want = days[ordinal] if -ordinal <= len(days) else None
        else:
            want = days[ordinal - 1] if ordinal <= len(days) else None
        if want is None:
            return "ordinal_out_of_range"
        # an ordinal is read over distinct occurrences: mentions of one occurrence agree
        return None if chosen.day == want else "ordinal_mismatch"
    approx = any(r.approx for r in same)
    tol = _CONFLICT_DAYS if approx else 0
    if days and (days[-1] - days[0]).days > tol:
        return "conflicting_reports"
    return None


def _abstain(meta: dict[str, Any], reason: str, **extra: Any) -> tuple[None, dict[str, Any]]:
    return None, {**meta, "decision": f"abstain:{reason}", **extra}


def compute_from_milestones(
    question: str,
    context: str,
    out: MilestoneOut,
    question_date: str | None = None,
    *,
    inclusive: bool = False,
    rounding: str = "nearest",
    lines: Sequence[tuple[date, str]] | None = None,
) -> tuple[Solution | None, dict[str, Any]]:
    """``(solution, meta)``; the solution is None (``meta["decision"]`` says why) to abstain.

    Pure code over the model's milestones: it validates, resolves and subtracts. The model's
    ``shape`` / ``start`` / ``end`` / ``unit`` / ordinals are claims that are all re-checked here.
    """
    lines = list(lines) if lines is not None else numbered_dated_lines(context)
    meta: dict[str, Any] = {
        "version": MILESTONES_VERSION,
        "shape": out.shape,
        "convention": {
            "inclusive": inclusive,
            "rounding": rounding,
            "month_days": _UNIT_DAYS["month"],
        },
    }
    if out.shape not in ("between", "since", "until"):
        return _abstain(meta, "shape_other")
    if not lines:
        return _abstain(meta, "no_dated_lines")
    by_id = {m.id: m for m in out.milestones}
    if len(by_id) != len(out.milestones):
        return _abstain(meta, "duplicate_milestone_ids")
    allow_planned = bool(_PLAN_WORDS.search(question))
    asked = _ordinals_in(question)
    for o in (out.start_ordinal, out.end_ordinal):
        if o is not None and o not in asked:
            return _abstain(meta, "ordinal_not_in_question", ordinal=o)

    resolved: dict[str, _Resolved] = {}
    for m in out.milestones:
        r = _resolve_one(m, lines, allow_planned)
        if isinstance(r, _Resolved):
            resolved[m.id] = r
    need = {"between": (out.start, out.end), "since": (out.start,), "until": (out.end,)}[out.shape]
    ends: dict[str, _Resolved] = {}
    for mid in need:
        if not mid or mid not in by_id:
            return _abstain(meta, "endpoint_missing", endpoint=mid)
        if mid not in resolved:
            reason = _resolve_one(by_id[mid], lines, allow_planned)
            return _abstain(meta, f"endpoint_{reason}", endpoint=mid)
        ends[mid] = resolved[mid]
    if out.shape == "between" and out.start == out.end:
        return _abstain(meta, "same_milestone")

    pool = list(resolved.values())
    ordinals = {out.start: out.start_ordinal, out.end: out.end_ordinal}
    for mid, r in ends.items():
        why = _conflicts(r, pool, ordinals.get(mid))
        if why:
            return _abstain(meta, why, endpoint=mid)
    if out.shape == "between":
        a, b = ends[out.start], ends[out.end]
        if out.same_actor and (not _actor(a.ms.actor) or _actor(a.ms.actor) != _actor(b.ms.actor)):
            return _abstain(meta, "actor_mismatch", actors=[a.ms.actor, b.ms.actor])
        if (
            a.line_text == b.line_text
            and a.line_day == b.line_day
            and a.day == b.day
            and a.ms.span == b.ms.span
        ):
            return _abstain(meta, "same_evidence")
        first, second = a, b
        if second.day < first.day:  # asked "after ... before ..." in either order
            first, second = b, a
            meta["swapped"] = True
        e1 = Endpoint(first.ms.event, first.day, first.approx, first.line_text[:160], first.basis)
        e2 = Endpoint(
            second.ms.event, second.day, second.approx, second.line_text[:160], second.basis
        )
    else:
        end = _asof_date("", question_date)
        if end is None:
            return _abstain(meta, "no_question_date")
        r = next(iter(ends.values()))
        pres = Endpoint("the question date", end[0], False, "", "question")
        ev = Endpoint(r.ms.event, r.day, r.approx, r.line_text[:160], r.basis)
        if out.shape == "since":
            e1, e2 = ev, pres
            if e2.day < e1.day:
                return _abstain(meta, "event_after_question_date")
        else:
            e1, e2 = pres, ev
            if e2.day < e1.day:
                return _abstain(meta, "event_before_question_date")
    unit = _question_unit(question)
    if not unit:
        m = re.search(r"\b(day|week|month|year)s?\b", question, re.I)
        unit = m.group(1).lower() if m else ""
    if not unit and out.unit in _UNIT_DAYS and re.search(rf"\b{out.unit}s?\b", question, re.I):
        unit = out.unit
    exact_days = (e2.day - e1.day).days
    if not unit:
        unit = _pick_unit(exact_days)
        meta["unit_basis"] = "picked"
    value, n, days = elapsed_in_unit(e1.day, e2.day, unit, inclusive=inclusive, rounding=rounding)
    if days == 0:
        return _abstain(meta, "zero_days")
    if n < 1:
        return _abstain(meta, "rounds_to_zero", days=days, unit=unit)
    approx = e1.approx or e2.approx or abs(value - n) > 0.25
    sol = Solution(unit, n, days, approx, e1, e2)
    meta.update(
        {
            "decision": "solved",
            "unit": unit,
            "n": n,
            "value": round(value, 3),
            "days": days,
            "approx": approx,
            "endpoints": [
                {
                    "event": e.clause,
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


# ---- the reader wrapper ------------------------------------------------------------------------


class MilestoneReader:
    """Wraps a reader: a duration-shaped question gets one milestone-extraction call, then code."""

    def __init__(
        self, inner: Any, chat: Chat, *, inclusive: bool = False, rounding: str = "nearest"
    ) -> None:
        if rounding not in ROUNDINGS:
            raise ValueError(f"milestones rounding must be one of {ROUNDINGS}, got {rounding!r}")
        self.inner = inner
        self._chat = chat
        self.inclusive = inclusive
        self.rounding = rounding
        self.guard = getattr(inner, "guard", None)
        self.reader_id = f"{inner.reader_id}+milestones"
        self.model = inner.model
        self.makes_model_calls = True
        self._counter = HeuristicTokenCounter()
        self.calls = 0
        self.rewritten = 0
        self.failures = 0
        # I69: repair + one retry of a malformed extraction (process-wide, opt-in switch).
        from memspine.services.llm import structured

        if not structured._OPTIONS.retry_on_error:
            structured.configure(
                structured.StructuredOptions(
                    retry_on_error=True, constrained_retry=structured._OPTIONS.constrained_retry
                )
            )

    def describe(self) -> Mapping[str, Any]:
        return {
            **self.inner.describe(),
            "milestones": (
                f"{MILESTONES_VERSION}/{'inclusive' if self.inclusive else 'exclusive'}"
                f"/{self.rounding}"
            ),
        }

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        first: ReaderAnswer = await self.inner.answer(question, context, question_date)
        if not is_milestone_question(question):
            return first
        settled = (first.extra_meta or {}).get("duration_solve", {}).get("decision")
        if settled in _SETTLED:
            return _with_meta(first, first.text, {"decision": f"skipped:i76_{settled}"}, 0, 0)
        lines = numbered_dated_lines(context)
        if not lines:
            return _with_meta(first, first.text, {"decision": "abstain:no_dated_lines"}, 0, 0)
        prompt_tokens = (
            self._counter.count(render_lines(lines)) + self._counter.count(question) + 700
        )
        self.calls += 1
        try:
            out = await extract_milestones(self._chat, question, lines)
        except Exception as exc:  # an enhancer: the reader's answer stands
            self.failures += 1
            return _with_meta(
                first, first.text, {"decision": "abstain:extract_failed", "error": str(exc)[:200]},
                prompt_tokens, 0,
            )  # fmt: skip
        completion = self._counter.count(out.model_dump_json())
        try:
            sol, meta = compute_from_milestones(
                question, context, out, question_date,
                inclusive=self.inclusive, rounding=self.rounding, lines=lines,
            )  # fmt: skip
        except Exception as exc:
            return _with_meta(
                first, first.text, {"decision": "abstain:compute_failed", "error": str(exc)[:200]},
                prompt_tokens, completion,
            )  # fmt: skip
        meta["extracted"] = out.model_dump()
        if sol is None:
            return _with_meta(first, first.text, meta, prompt_tokens, completion)
        verdict, theirs = check_answer(sol, first.text)
        meta = {**meta, "reader_value": theirs, "reader_check": verdict}
        if verdict in ("agrees", "unparsed"):
            return _with_meta(
                first, first.text, {**meta, "decision": verdict}, prompt_tokens, completion
            )
        self.rewritten += 1
        return _with_meta(
            first, sol.text(), {**meta, "decision": "rewritten", "original": first.text},
            prompt_tokens, completion,
        )  # fmt: skip


def _with_meta(
    first: ReaderAnswer, text: str, meta: dict[str, Any], prompt_tokens: int, completion_tokens: int
) -> ReaderAnswer:
    extra_call = 1 if prompt_tokens else 0
    return ReaderAnswer(
        text=text,
        prompt_tokens=first.prompt_tokens + prompt_tokens,
        completion_tokens=first.completion_tokens + completion_tokens,
        latency_ms=first.latency_ms,
        model_calls=first.model_calls + extra_call,
        truncated=first.truncated,
        cached_prompt_tokens=first.cached_prompt_tokens,
        finish_reason=first.finish_reason,
        raw_text=first.raw_text,
        prompt_variant=first.prompt_variant,
        extra_meta={**dict(first.extra_meta), "milestones": meta},
    )
