"""Deterministic date-equivalence pre-check for the QA judge (gap A2).

LoCoMo writes many temporal golds relative to the session ("The Friday before 15 March 2021"),
while a reader that resolves dates answers with the absolute day ("Friday, 2021-03-12"). The
9B LLM judge rejects many such exact answers (measured: 5 of 35 wrong answers on two dev
conversations). This check credits an answer only when

* the gold parses to ONE day (an absolute date, or "<weekday> before <date>"), and
* the first date in the answer parses to that same day, and
* the answer is not a refusal or denial ("never", "did not", "not mentioned": a denial can still
  carry the right date, e.g. "X never went to the show; she went biking on 9 April").

Everything else (ranges such as "the weekend before", years, months, non-date golds, answers
whose first date differs) is left to the LLM judge, so the check can only turn a wrong verdict
into a right one on an unambiguous single-day match.

I12: for any gold that is not ONE day (a range, a year, a month, free text)
:func:`date_check` returns ``None`` (not applicable) and the judge decides. A missing date
parser is a loud error (:class:`DateParserUnavailable`), never a silent switch-off.
"""

from __future__ import annotations

import importlib
import logging
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["DateParserUnavailable", "date_check", "date_equivalent", "require_parser"]

log = logging.getLogger(__name__)

_DAY_GRANULARITIES = ("day", "relative_day")
_NEGATION = re.compile(r"\b(never|not|no|didn't|doesn't|wasn't|isn't|cannot|can't|unknown)\b", re.I)
#: ``evals/`` holds ``failure_buckets.py`` (the interval parser); it is the parent of this package.
_EVALS_DIR = Path(__file__).resolve().parents[1]
_PARSER: Callable[[str], Any] | None = None


class DateParserUnavailable(RuntimeError):
    """The interval parser (``evals/failure_buckets.py``) could not be imported."""


def _parser() -> Callable[[str], Any]:
    """The ``parse_interval`` function. Resolved package-relative: when ``failure_buckets`` is not
    importable, ``evals/`` (found from this file) is put on ``sys.path`` and the import retried.
    Failure raises :class:`DateParserUnavailable` and logs an error; it is never swallowed."""
    global _PARSER
    if _PARSER is not None:
        return _PARSER
    try:
        module = importlib.import_module("failure_buckets")
    except ImportError:
        if str(_EVALS_DIR) not in sys.path:
            sys.path.insert(0, str(_EVALS_DIR))
        try:
            module = importlib.import_module("failure_buckets")
        except ImportError as exc:
            message = (
                f"date check unavailable: cannot import failure_buckets (looked in {_EVALS_DIR}): "
                f"{exc}. The judge date check would be silently off; fix the path or drop "
                "--judge-date-check."
            )
            log.error(message)
            raise DateParserUnavailable(message) from exc
    _PARSER = module.parse_interval
    return _PARSER


def require_parser() -> None:
    """Fail fast (at judge construction) when the date check is on but cannot run."""
    _parser()


def date_check(gold: str | None, answer: str | None) -> bool | None:
    """``None`` = not applicable (no gold, or the gold is not a single day: the judge decides);
    ``True`` = the answer names the gold's day; ``False`` = applicable, no credit."""
    if not gold:
        return None
    parse = _parser()
    g = parse(str(gold))
    if g is None or g.granularity not in _DAY_GRANULARITIES or g.lo != g.hi:
        return None
    if not answer:
        return False
    from memspine_evals.refusal import is_refusal

    if is_refusal(answer) or _NEGATION.search(answer):
        return False
    a = parse(str(answer))
    if a is None or a.granularity not in _DAY_GRANULARITIES or a.lo != a.hi:
        return False
    return a.lo == g.lo


def date_equivalent(gold: str | None, answer: str | None) -> bool:
    """True when gold and answer name the same single day (see module docstring)."""
    return date_check(gold, answer) is True
