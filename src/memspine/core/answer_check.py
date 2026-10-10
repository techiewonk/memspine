"""E06: code-first checks of a final answer against its query contract.

:func:`check_answer` looks at the answer text only (never the gold, never a benchmark's
vocabulary) and reports the DEFECTS it can name from surface properties of the answer type
the question asked for (:mod:`memspine.core.query_contract`):

* ``missing_date`` / ``missing_year`` / ``missing_time``: a "when" question answered without
  any date, year or clock time (a relative phrase such as "last week" counts as a date);
* ``missing_duration``: a "how long" question answered without a quantity of time;
* ``missing_number``: a "how many / how much" question answered without a number;
* ``wrong_granularity``: a "which city" question answered with a country (or a state);
* ``wrong_type``: a date or a bare number given for a title / person / name / place question;
* ``no_polarity``: a yes/no question whose answer says neither yes nor no;
* ``count_list_mismatch``: the answer states N but lists M items;
* ``dropped_items``: a many-valued question whose evidence items (the caller's evidence table,
  or the bullet list in the reader's own explanation) are missing from the final answer.

An abstention ("not mentioned") is never a defect and never repaired: an evidence-based
unknown must not turn into a guess. The checks are conservative by construction: each fires
only on a positive sign of the defect, and the caller treats a flag as a candidate for ONE
bounded repair, not as a verdict.

Pure and self-contained (stdlib plus the contract type).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from memspine.core.query_contract import QueryContract

__all__ = [
    "Defect",
    "check_answer",
    "defect_instruction",
    "is_abstention",
    "is_country",
]


def _words(text: str) -> list[str]:
    """The whitespace-separated words of a vocabulary block."""
    return text.split()


@dataclass(frozen=True, slots=True)
class Defect:
    kind: str
    detail: str

    def as_meta(self) -> dict[str, str]:
        return {"kind": self.kind, "detail": self.detail}


_ABSTAIN = re.compile(
    r"\b(?:i|you) do(?: not|n't) know\b"
    r"|\bnot (?:mentioned|specified|stated|provided|clear|known|available|certain)\b"
    r"|\bno (?:mention|specific information|information|record|evidence)\b"
    r"|\b(?:does|do|did) not (?:mention|specify|contain|say|state|indicate|include|provide)\b"
    r"|\bcannot (?:be )?(?:determine|answer|tell)\b|\bcan(?:'|no)t (?:be )?(?:determined|tell)\b"
    r"|\bthere is no\b|\bunknown\b|\bunclear\b|\bn/?a\b|\bunable to (?:determine|find|answer)\b",
    re.I,
)


def is_abstention(answer: str) -> bool:
    """True when the answer says the information is not available."""
    return bool(_ABSTAIN.search(answer or ""))


# ------------------------------------------------------------------ world-knowledge lists

#: Sovereign states and common short forms (world knowledge, not dataset vocabulary).
_COUNTRIES = frozenset(
    _words(
        """
    afghanistan albania algeria andorra angola argentina armenia australia austria
    azerbaijan bahamas bahrain bangladesh barbados belarus belgium belize benin bhutan
    bolivia bosnia botswana brazil brunei bulgaria burundi cambodia cameroon canada chad
    chile china colombia comoros congo croatia cuba cyprus czechia denmark dominica ecuador
    egypt eritrea estonia eswatini ethiopia fiji finland france gabon gambia georgia germany
    ghana greece grenada guatemala guinea guyana haiti honduras hungary iceland india
    indonesia iran iraq ireland israel italy jamaica japan jordan kazakhstan kenya kiribati
    kosovo kyrgyzstan laos latvia lebanon lesotho liberia libya liechtenstein lithuania
    madagascar malawi malaysia maldives mali malta mauritania mauritius mexico micronesia
    moldova mongolia montenegro morocco mozambique myanmar burma namibia nauru nepal
    netherlands holland nicaragua niger nigeria norway oman pakistan palau palestine
    paraguay peru philippines poland portugal qatar romania russia rwanda samoa senegal
    serbia seychelles slovakia slovenia somalia spain sudan suriname sweden switzerland
    syria taiwan tajikistan tanzania thailand togo tonga tunisia turkey turkmenistan tuvalu
    uganda ukraine uruguay uzbekistan vanuatu venezuela vietnam yemen zambia zimbabwe
    england scotland wales usa uk uae america
    united-states united-kingdom united-arab-emirates south-africa south-korea north-korea
    new-zealand sri-lanka saudi-arabia costa-rica el-salvador dominican-republic
    czech-republic ivory-coast cape-verde papua-new-guinea
    """
    )
)
_US_STATES = frozenset(
    _words(
        """
    alabama alaska arizona arkansas california colorado connecticut delaware florida hawaii
    idaho illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts
    michigan minnesota mississippi missouri montana nebraska nevada new-hampshire new-jersey
    new-mexico north-carolina north-dakota ohio oklahoma oregon pennsylvania rhode-island
    south-carolina south-dakota tennessee texas utah vermont virginia west-virginia
    wisconsin wyoming ontario quebec alberta manitoba bavaria tuscany catalonia
    """
    )
)
_LEAD = re.compile(r"^(?:in|to|at|from|near|around|the|a|an|of|visited|visiting|went to)\s+", re.I)


def _place_key(text: str) -> str:
    t = re.sub(r"[^A-Za-z' -]", " ", text).strip().lower()
    t = re.sub(r"\s+", " ", t)
    while True:
        stripped = _LEAD.sub("", t)
        if stripped == t:
            break
        t = stripped
    return t.replace(" ", "-")


def is_country(text: str) -> bool:
    """True when ``text`` (an answer's place phrase) is a bare country name."""
    return _place_key(text) in _COUNTRIES


def _bare_places(answer: str) -> list[str]:
    """The place-like phrases of a short answer: split at commas / "and" / "or" / ";"."""
    return [p for p in re.split(r"\s*(?:,|;|/|\band\b|\bor\b)\s*", answer.strip().rstrip(".")) if p]


# ----------------------------------------------------------------------------- surface cues

_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DAY = r"(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?"
_YEAR = re.compile(r"\b(?:1[0-9]|20)\d\d\b")
_DATE_TOKEN = re.compile(
    rf"\b(?:1[0-9]|20)\d\d\b|\b{_MONTH}\b|\b{_DAY}\b|\b\d{{1,2}}[/.-]\d{{1,2}}(?:[/.-]\d{{2,4}})?\b"
    r"|\b(?:yesterday|today|tomorrow|tonight|ago|earlier|later|recently|currently|now|"
    r"(?:last|this|next|past|previous|the following|the same) (?:week|weekend|month|year|"
    r"season|summer|winter|spring|fall|autumn|night|morning|evening|day|decade)|"
    r"the (?:day|week|month|year|weekend) (?:before|after)|spring|summer|winter|autumn)\b",
    re.I,
)
#: An answer that is itself a time clause ("When she was 10", "After the move").
_TIME_CLAUSE = re.compile(
    r"^\W*(?:when|while|after|before|during|since|until|once|as soon as)\b", re.I
)
#: Frequency words that answer "how often" / "how many" without a numeral.
_QUANTIFIER = re.compile(
    r"\b(?:multiple|several|many|few|couple|daily|weekly|monthly|yearly|annually|nightly|"
    r"every|each|always|often|regularly|frequently|sometimes|rarely|occasionally|constantly|"
    r"times a|a lot|most)\b",
    re.I,
)
_CLOCK = re.compile(
    r"\b\d{1,2}(?::\d\d)?\s*(?:a\.?m\.?|p\.?m\.?|o'?clock)\b|\b\d{1,2}:\d\d\b"
    r"|\b(?:noon|midnight|morning|afternoon|evening|night|dawn|dusk)\b",
    re.I,
)
_NUMBER_WORDS = _words(
    """
        zero one two three four five six seven eight nine ten eleven twelve thirteen
        fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty
        sixty seventy eighty ninety hundred thousand million dozen once twice thrice
        never none no
        """
)
_NUMBER = re.compile(r"\b\d[\d,.]*\b|\b(?:" + "|".join(_NUMBER_WORDS) + r")\b", re.I)
_UNIT = r"(?:seconds?|minutes?|hours?|days?|nights?|weeks?|months?|years?|decades?)"
_DURATION = re.compile(
    rf"\b(?:\d[\d,.]*|{'|'.join(_NUMBER_WORDS)}|an?|couple|few|several|half|many|some)[\s-]+"
    rf"(?:\w+[\s-]+)?{_UNIT}\b|\ball (?:day|week|month|year|night)\b|\b(?:since|from|until|"
    rf"for) (?:the )?\w+",
    re.I,
)
_POLARITY = re.compile(
    r"\b(?:yes|yeah|yep|no|nope|not|never|true|false|correct|incorrect|indeed|likely|unlikely|"
    r"probably|possibly|maybe|definitely|certainly|absolutely|sure|affirmative|negative|"
    r"neither|both|none|can't|cannot|won't|didn't|doesn't|isn't|aren't|wasn't|weren't|"
    r"hasn't|haven't|hadn't|wouldn't|couldn't|shouldn't|don't)\b|n't\b",
    re.I,
)
_FULL_DATE = re.compile(
    rf"\b(?P<y1>(?:1[0-9]|20)\d\d)-(?P<m1>\d{{2}})-(?P<d1>\d{{2}})\b"
    rf"|\b(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m2>{_MONTH})\.?,?\s+(?P<y2>(?:1[0-9]|20)\d\d)\b"
    rf"|\b(?P<m3>{_MONTH})\.?\s+(?P<d3>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y3>(?:1[0-9]|20)\d\d)\b",
    re.I,
)
_RANGE = re.compile(r"\b(?:between|from|through|until|till|to|or|and|-|\u2013)\b", re.I)
_STATED_N = re.compile(
    r"\b(?P<n>\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b", re.I
)
_WORD_N = {w: i for i, w in enumerate(
    _words(
        """
        zero one two three four five six seven eight nine ten eleven twelve
        """
    )
)}  # fmt: skip
_FUNCTION = frozenset(
    _words(
        """
        the a an of to in on at and or for with by from as is was were are be been his
        her their my our its that this it he she they them
        """
    )
)


def _month_no(name: str) -> int:
    return (
        _words(
            """
        jan feb mar apr may jun jul aug sep oct nov dec
        """
        ).index(name.lower()[:3])
        + 1
    )


def _full_dates(text: str) -> set[tuple[int, int, int]]:
    out: set[tuple[int, int, int]] = set()
    for m in _FULL_DATE.finditer(text):
        if m.group("y1"):
            out.add((int(m.group("y1")), int(m.group("m1")), int(m.group("d1"))))
        elif m.group("y2"):
            out.add((int(m.group("y2")), _month_no(m.group("m2")), int(m.group("d2"))))
        else:
            out.add((int(m.group("y3")), _month_no(m.group("m3")), int(m.group("d3"))))
    return out


def _content(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1 and w not in _FUNCTION}


def _stem(w: str) -> str:
    return w[:-1] if w.endswith("s") and len(w) > 3 else w


def _date_only(answer: str) -> bool:
    rest = _DATE_TOKEN.sub(" ", answer)
    rest = re.sub(r"\b\d+(?:st|nd|rd|th)?\b", " ", rest)
    return bool(_DATE_TOKEN.search(answer)) and len(_content(rest)) == 0


def _number_only(answer: str) -> bool:
    rest = _NUMBER.sub(" ", answer)
    return bool(_NUMBER.search(answer)) and len(_content(rest)) == 0


def _stated_and_listed(answer: str) -> tuple[int, int] | None:
    """(N stated before a list, M items listed) for "3 places: A, B, C, D" shapes, else None."""
    text = answer.strip()
    marks = [int(n) for n in re.findall(r"(?:^|\s)\**(\d{1,2})[.)]\**\s", text)]
    listed = 0
    head = text
    if marks[:2] == [1, 2]:
        listed = 0
        for want, got in enumerate(marks, start=1):  # consecutive 1, 2, 3 ...
            if got != want:
                break
            listed = want
        first = re.search(r"(?:^|\s)\**1[.)]\**\s", text)
        head = text[: first.start()] if first else text
    else:
        m = re.search(r"^(?P<head>[^:\n]{0,80}?):\s*(?P<items>.+)$", text, re.S)
        if not m:
            return None
        body = re.sub(r"\([^)]*\)", " ", m.group("items"))  # asides hold commas and dates
        items = [
            i
            for i in re.split(r"\s*(?:,|;|\n|\band\b)\s*", body.strip().rstrip("."))
            if i.strip(" -*")
        ]
        if any(len(i.split()) > 6 for i in items):
            return None
        listed, head = len(items), m.group("head")
    head = _FULL_DATE.sub(" ", head)
    head = re.sub(r"\b\d{4}-\d{2}(?:-\d{2})?\b|\b(?:1[0-9]|20)\d\d\b", " ", head)
    if re.search(
        r"\b(?:at least|at most|more than|over|about|around|approximately|nearly)\b", head, re.I
    ):
        return None
    n = re.search(
        r"\b(?P<n>\d{1,2}|once|twice|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b",
        head,
        re.I,
    )
    if not n or listed < 2:
        return None
    word = n.group("n").lower()
    stated = int(word) if word.isdigit() else {"once": 1, "twice": 2}.get(word, _WORD_N.get(word))
    return (stated, listed) if stated is not None else None


def _explanation_items(explanation: str) -> list[str]:
    """Bullet / numbered items of a reader's own explanation: short lines only."""
    items: list[str] = []
    for line in explanation.splitlines():
        m = re.match(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$", line)
        if m and 1 <= len(m.group(1).split()) <= 10:
            items.append(m.group(1))
    return items if len(items) >= 2 else []


def _missing_items(answer: str, items: Iterable[str]) -> list[str]:
    have = {_stem(w) for w in _content(answer)}
    missing: list[str] = []
    for item in items:
        words = {_stem(w) for w in _content(item)}
        if words and len(words & have) * 2 < len(words):
            missing.append(item.strip())
    return missing


_CAP_WORD = re.compile(r"[A-Z][\w'\u2019-]*")


def _has_proper_name(text: str, subjects: Sequence[str]) -> bool:
    """A capitalised word that is not sentence-initial, a subject, a month or a weekday, or a
    quoted span (a title)."""
    if re.search(r"[\"“][^\"”]{2,}[\"”]", text):
        return True
    skip = {p.lower() for s in subjects for p in s.split()}
    for m in _CAP_WORD.finditer(text):
        word = re.sub(r"['\u2019]s$", "", m.group(0))
        before = text[: m.start()].rstrip()
        if not before or before[-1] in ".?!:\n*#":
            continue  # sentence-initial
        if word.lower() in skip or re.fullmatch(f"{_MONTH}|{_DAY}", word, re.I):
            continue
        return True
    return False


_PROPER_RUN = re.compile(r"[A-Z][\w'\u2019-]*(?:\s+(?:of\s+)?[A-Z][\w'\u2019-]*)*")


def _proper_runs(text: str, subjects: Sequence[str]) -> list[str]:
    """Capitalised phrases of an answer ("the United States", "Rome, Italy"), leaving out the
    question's subjects, months and weekdays. A sentence-initial word is kept: a short answer
    opens with its place, and a pronoun or article is not a country, so it only ever makes the
    check more conservative."""
    skip = {p.lower() for s in subjects for p in s.split()}
    out: list[str] = []
    for m in _PROPER_RUN.finditer(text):
        phrase = re.sub(r"['\u2019]s$", "", m.group(0))
        if phrase.lower() in skip or re.fullmatch(f"{_MONTH}|{_DAY}", phrase, re.I):
            continue
        out.append(phrase)
    return out


def _soft_defects(contract: QueryContract, text: str) -> list[Defect]:
    out: list[Defect] = []
    kind, sub = contract.answer_type, contract.subtype
    if kind in ("place", "person", "title", "name") and not _has_proper_name(
        text, contract.subjects
    ):
        out.append(Defect("soft_unnamed", f"the answer names no specific {kind}"))
    if kind == "place" and sub in ("city", "region") and contract.cardinality == "many":
        parts = _bare_places(text)
        if any(is_country(p) for p in parts) and not all(is_country(p) for p in parts):
            out.append(
                Defect("soft_mixed_granularity", f"a country is listed where {sub}s were asked")
            )
    return out


# ------------------------------------------------------------------------------ the checks


def check_answer(
    contract: QueryContract,
    answer: str,
    *,
    explanation: str = "",
    evidence_items: Sequence[str] = (),
    abstained: bool | None = None,
    soft: bool = False,
) -> list[Defect]:
    """The defects :mod:`this module <memspine.core.answer_check>` can name for ``answer``.

    ``soft``: also report the ``soft_*`` signals (an unnamed place / person / title; a
    country listed among cities). They are noisier than the strict checks: the repair step
    ignores them unless asked, and the offline estimate counts them separately.

    ``explanation``: the reader's reasoning before its final answer, when it kept one.
    ``evidence_items``: the caller's evidence table (one string per supported item) for
    many-valued questions. ``abstained``: the caller's own abstention decision (default:
    :func:`is_abstention`). An unknown contract, an empty answer and an abstention give no
    defects.
    """
    text = (answer or "").strip()
    if not text or not contract.known:
        return []
    if abstained is None:
        abstained = is_abstention(text)
    if abstained:
        return []
    kind, sub = contract.answer_type, contract.subtype
    out: list[Defect] = []

    if kind == "date":
        if sub == "time":
            if not _CLOCK.search(text) and not _DATE_TOKEN.search(text):
                out.append(Defect("missing_time", "the question asks for a time"))
        elif sub == "year":
            if not _YEAR.search(text) and not re.search(
                r"\b(?:ago|last year|this year)\b", text, re.I
            ):
                out.append(Defect("missing_year", "the question asks for a year"))
        elif not _DATE_TOKEN.search(text) and not _TIME_CLAUSE.search(text):
            out.append(Defect("missing_date", "the question asks for a date or a time"))
    elif kind == "duration":
        if not _DURATION.search(text) and not _YEAR.search(text):
            out.append(Defect("missing_duration", "the question asks for a length of time"))
    elif kind in ("count", "quantity"):
        if not _NUMBER.search(text) and not _QUANTIFIER.search(text):
            out.append(Defect("missing_number", "the question asks for a number"))
        pair = _stated_and_listed(text) if kind == "count" else None
        if pair and pair[0] != pair[1]:
            out.append(
                Defect(
                    "count_list_mismatch",
                    f"the answer states {pair[0]} but lists {pair[1]} items",
                )
            )
    elif kind == "place":
        parts = _bare_places(text)
        named = _proper_runs(text, contract.subjects)
        only_countries = (parts and all(is_country(p) for p in parts)) or (
            named and all(is_country(p) for p in named)
        )
        if sub in ("city", "region") and only_countries:
            out.append(Defect("wrong_granularity", f"a country was given where a {sub} was asked"))
        elif sub == "city" and parts and all(_place_key(p) in _US_STATES for p in parts):
            out.append(Defect("wrong_granularity", "a state was given where a city was asked"))
        elif _date_only(text) or _number_only(text):
            out.append(Defect("wrong_type", "a date or number was given where a place was asked"))
    elif kind in ("title", "person", "name") and (_date_only(text) or _number_only(text)):
        out.append(Defect("wrong_type", f"a date or number was given where a {kind} was asked"))
    elif kind == "yesno" and not _POLARITY.search(text):
        out.append(Defect("no_polarity", "the question is yes/no and the answer is neither"))

    if soft:
        out.extend(_soft_defects(contract, text))

    if contract.cardinality in ("many", "count"):
        items = list(evidence_items) or _explanation_items(explanation)
        if items:
            missing = _missing_items(text, items)
            if missing:
                out.append(Defect("dropped_items", "the answer omits: " + "; ".join(missing[:8])))
    return out


def defect_instruction(defects: Sequence[Defect]) -> str:
    """The one repair sentence naming the specific defects (used by the repair call)."""
    return " ".join(f"{d.detail.rstrip('.')}." for d in defects)
