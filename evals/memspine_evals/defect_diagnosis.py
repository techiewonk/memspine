"""G01: diagnosed-defect checks for the retry (``--verify-slots diagnosed``). Pure, no model.

``slot_verify`` (E06) already names type defects (a date missing from a "when" answer, a country
for a city, a count that disagrees with its own list). G01 adds the three diagnoses a limited
retry needs to be trusted, each decided by code from the question, the answer and the SAME
context the reader saw:

``unsupported_claim``
    The answer names a person / place / title (a capitalised phrase that does not open the
    sentence, or a quoted span) none of whose words appears anywhere in the context or the
    question. A phrase the context holds in part (a longer form of a name it has) is supported.
``operand_result_mismatch``
    The answer or the reader's explanation states an arithmetic fact its own operands do not
    give ("12 - 5 = 8"), or a duration whose two dates (the only two full dates it names) do not
    give that many days / weeks / months / years (tolerance of one unit).
``unjustified_refusal``
    The answer says the information is not there, yet the context holds a line that names every
    subject of the question and at least two of its relation words (one, when the relation has
    only one). Without such a line an abstention is an evidence-based unknown and is left alone.

A retry that follows a diagnosis is kept only when the diagnosed defect is gone; a retry that
answers an unjustified refusal must also be SUPPORTED, i.e. share a non-question content word
with the evidence line(s) the diagnosis cited, so a guess never replaces an unknown.

Generic: no dataset word, no category, no gold. Surface properties of an answer only.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from memspine.core.answer_check import Defect, _full_dates

__all__ = [
    "DIAGNOSIS_VERSION",
    "Diagnosis",
    "arithmetic_mismatch",
    "diagnose",
    "duration_mismatch",
    "refusal_evidence",
    "supported_by",
    "unsupported_claims",
]

DIAGNOSIS_VERSION = "g01/v1"

_WORD = re.compile(r"[A-Za-z0-9À-ɏ]+")
_CAP_RUN = re.compile(r"[A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)*")
_QUOTED = re.compile(r"[\"“]([^\"”]{2,60})[\"”]")
_MONTHS = frozenset(
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec monday tuesday wednesday thursday friday "
    "saturday sunday mon tue tues wed thu thur thurs fri sat sun".split()
)
_STOP = frozenset(
    "the a an of to in on at and or for with by from as is was were that this it its his her "
    "their my our be been being has have had not no yes did does do who what when where which "
    "how why".split()
)
_SENTENCE_END = ".?!:\n*#"


def _fold(text: str) -> str:
    return " ".join(text.casefold().split())


def _words(text: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(text)]


# ---- unsupported claims -----------------------------------------------------------------------


def unsupported_claims(answer: str, context: str, question: str = "") -> list[str]:
    """Capitalised phrases / quoted spans of ``answer`` none of whose words is in the context
    or the question (months, weekdays and sentence-initial words never count)."""
    haystack = set(_words(context)) | set(_words(question))
    found: list[str] = []
    candidates: list[str] = [m.group(1) for m in _QUOTED.finditer(answer)]
    for m in _CAP_RUN.finditer(answer):
        before = answer[: m.start()].rstrip()
        if not before or before[-1] in _SENTENCE_END:
            continue  # sentence-initial: any word can be capitalised there
        candidates.append(re.sub(r"['’]s$", "", m.group(0)))
    for phrase in candidates:
        words = [w for w in _words(phrase) if len(w) >= 3 and w not in _MONTHS and w not in _STOP]
        if not words:
            continue
        if not any(w in haystack or w.rstrip("s") in haystack for w in words):
            if phrase not in found:
                found.append(phrase)
    return found


# ---- operand / result -------------------------------------------------------------------------

_ARITH = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*([+\-−×x*/÷])\s*(\d+(?:\.\d+)?)\s*=\s*(\d+(?:\.\d+)?)(?!\w|\.\d)"
)
_UNIT_DAYS = {"day": 1.0, "week": 7.0, "month": 30.4375, "year": 365.25}
_STATED_DURATION = re.compile(
    r"\b(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|a|an)"
    r"\s+(day|week|month|year)s?\b",
    re.I,
)
_NUM_WORDS = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve".split()
    )
}
_NUM_WORDS.update({"a": 1, "an": 1})


def arithmetic_mismatch(text: str) -> str | None:
    """The first ``a op b = c`` in ``text`` that does not hold, as a sentence; else None."""
    for m in _ARITH.finditer(text):
        a, op, b, c = float(m.group(1)), m.group(2), float(m.group(3)), float(m.group(4))
        if op in "+":
            want = a + b
        elif op in "-−":
            want = a - b
        elif op in "×x*":
            want = a * b
        else:
            if b == 0:
                continue
            want = a / b
        if abs(want - c) > 0.01 * max(1.0, abs(want)):
            return f"{m.group(0).strip()} is not right: {m.group(1)} {op} {m.group(3)} is {want:g}"
    return None


def duration_mismatch(answer: str, explanation: str = "") -> str | None:
    """When the answer and its explanation together name exactly two full dates and the answer
    states a duration, the duration the dates give (tolerance of one unit)."""
    dates = sorted({*_full_dates(answer), *_full_dates(explanation)})
    if len(dates) != 2:
        return None
    m = _STATED_DURATION.search(answer)
    if not m:
        return None
    raw = m.group(1).lower()
    n = float(raw) if raw[0].isdigit() else float(_NUM_WORDS[raw])
    unit = m.group(2).lower()
    try:
        d0, d1 = date(*dates[0]), date(*dates[1])
    except ValueError:
        return None
    gap = (d1 - d0).days
    got = gap / _UNIT_DAYS[unit]
    if abs(got - n) <= 1.0:
        return None
    return (
        f"the answer states {m.group(0)} but {d0:%Y-%m-%d} to {d1:%Y-%m-%d} is {gap} days "
        f"(about {got:.1f} {unit}s)"
    )


# ---- unjustified refusal ----------------------------------------------------------------------

_MARKERS = frozenset({"asker", "assistant", "user"})


def refusal_evidence(
    context: str, subjects: Sequence[str], relation: str, limit: int = 2
) -> list[str]:
    """Context lines that name every subject and enough of the relation's words (stems of five
    letters). Empty when the question has no relation words: nothing can be diagnosed."""
    rel = sorted({w[:5] for w in _words(relation) if len(w) > 2 and w not in _STOP})
    if not rel:
        return []
    need = 1 if len(rel) == 1 else 2
    names = [s.casefold() for s in subjects if s.casefold() not in _MARKERS]
    scored: list[tuple[int, int, str]] = []
    for i, line in enumerate(context.splitlines()):
        folded = _fold(line)
        if not folded or any(n not in folded for n in names):
            continue
        toks = {w[:5] for w in _words(line)}
        hits = sum(1 for r in rel if r in toks)
        if hits >= need:
            scored.append((-hits, i, line.strip()))
    scored.sort()
    return [text for _, _, text in scored[:limit]]


def supported_by(answer: str, lines: Sequence[str], question: str) -> bool:
    """True when most (at least 60%, and at least one) of the answer's content words that the
    question does not already contain are in the cited evidence lines: a retry that brings new
    words the line does not hold is a guess."""
    asked = {w[:5] for w in _words(question)}
    have = {w[:5] for line in lines for w in _words(line)}
    new = {w[:5] for w in _words(answer) if w not in _STOP and (len(w) >= 3 or w.isdigit())} - asked
    return bool(new) and len(new & have) >= 1 and len(new & have) / len(new) >= 0.6


# ---- the diagnosis ----------------------------------------------------------------------------


@dataclass(slots=True)
class Diagnosis:
    defects: list[Defect] = field(default_factory=list)
    #: the evidence lines an ``unjustified_refusal`` cites (a retry must be supported by them)
    evidence: list[str] = field(default_factory=list)


def diagnose(
    question: str,
    context: str,
    answer: str,
    *,
    subjects: Sequence[str],
    relation: str,
    abstained: bool,
    explanation: str = "",
) -> Diagnosis:
    """The G01 defects of ``answer`` against ``context``. An abstention is diagnosed only for
    ``unjustified_refusal``; any other answer for ``unsupported_claim`` and
    ``operand_result_mismatch``."""
    out = Diagnosis()
    if abstained:
        lines = refusal_evidence(context, subjects, relation)
        if lines:
            out.evidence = lines
            out.defects.append(
                Defect(
                    "unjustified_refusal",
                    "the memories do contain this: " + " | ".join(line[:300] for line in lines),
                )
            )
        return out
    bad = unsupported_claims(answer, context, question)
    if bad:
        out.defects.append(
            Defect(
                "unsupported_claim",
                "the answer names " + ", ".join(repr(b) for b in bad[:4]) + " which no memory line contains",
            )
        )
    sentence = arithmetic_mismatch(answer) or arithmetic_mismatch(explanation)
    sentence = sentence or duration_mismatch(answer, explanation)
    if sentence:
        out.defects.append(Defect("operand_result_mismatch", sentence))
    return out
