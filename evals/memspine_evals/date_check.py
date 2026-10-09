"""Deterministic date-equivalence pre-check for the QA judge (gap A2).

LoCoMo writes many temporal golds relative to the session ("The Friday before 15 July 2023"),
while a reader that resolves dates answers with the absolute day ("Friday, 2023-07-14"). The
9B LLM judge rejects many such exact answers (measured: 5 of 35 wrong answers on two dev
conversations). This check credits an answer only when

* the gold parses to ONE day (an absolute date, or "<weekday> before <date>"), and
* the first date in the answer parses to that same day, and
* the answer is not a refusal or denial ("never", "did not", "not mentioned": a denial can still
  carry the right date, e.g. "X never went to the show; she went biking on 9 April").

Everything else (ranges such as "the weekend before", years, months, non-date golds, answers
whose first date differs) is left to the LLM judge, so the check can only turn a wrong verdict
into a right one on an unambiguous single-day match.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["date_equivalent"]

_DAY_GRANULARITIES = ("day", "relative_day")
_NEGATION = re.compile(r"\b(never|not|no|didn't|doesn't|wasn't|isn't|cannot|can't|unknown)\b", re.I)


def _parser() -> Any:
    try:
        from failure_buckets import parse_interval  # evals/failure_buckets.py

        return parse_interval
    except ImportError:  # harness used without the evals folder on the path
        return None


def date_equivalent(gold: str | None, answer: str | None) -> bool:
    """True when gold and answer name the same single day (see module docstring)."""
    if not gold or not answer:
        return False
    from memspine_evals.refusal import is_refusal

    if is_refusal(answer) or _NEGATION.search(answer):
        return False
    parse = _parser()
    if parse is None:
        return False
    g = parse(str(gold))
    if g is None or g.granularity not in _DAY_GRANULARITIES or g.lo != g.hi:
        return False
    a = parse(str(answer))
    if a is None or a.granularity not in _DAY_GRANULARITIES or a.lo != a.hi:
        return False
    return a.lo == g.lo
