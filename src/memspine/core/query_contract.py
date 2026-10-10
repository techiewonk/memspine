"""A03: a typed query contract, built from the question alone.

The audit of a full conversational-memory benchmark run found 111 failures that are a
wrong answer TYPE (a country for
a city, a date for a title, a related fact that does not satisfy the predicate) and 47 wrong
details, with the evidence in context. The reader is never told what kind of thing the
question asks for. This module builds that statement as a small structured object:

* ``subjects``: the people / things the question is about (capitalised names; ``I`` / ``you``
  as the markers ``asker`` / ``assistant``);
* ``relation``: the predicate, as the question's content words left after subjects and
  interrogatives are removed;
* ``answer_type`` (+ ``subtype``): person / place (city, country, region, ...) / date (date,
  time, year, month, weekday) / duration / count / quantity / title / name / yesno / choice /
  reason / description / entity, or ``unknown``;
* ``time_scope``: the explicit time phrase of the question, else empty;
* ``cardinality``: ``one`` / ``many`` / ``count``;
* ``request``: ``recall`` / ``inference`` (the answer is invited to be inferred) /
  ``recommendation``.

Rules only: English surface cues, no model, no dataset vocabulary (rule I37). Every rule is a
property of how questions are phrased ("how many", "which city", "where", "why"), never of a
benchmark's content. A question no rule recognises gets ``answer_type="unknown"``, and an
unknown contract adds nothing to the read, so ambiguous questions fall back to the general
evidence-grounded path. An optional resolver (the ``plan@contract`` LLM call, or any callable)
may refine the contract when the rules are unsure (:func:`is_ambiguous`); see
:func:`merge_contract`.

Pure and self-contained: stdlib plus :mod:`memspine.core.query_shape`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from memspine.core.query_shape import is_set_question, is_set_question_wide

__all__ = [
    "ANSWER_TYPES",
    "CARDINALITIES",
    "REQUESTS",
    "QueryContract",
    "build_contract",
    "contract_header",
    "is_ambiguous",
    "merge_contract",
    "parse_contract",
    "rerank_hint",
]

ANSWER_TYPES = (
    "person",
    "place",
    "date",
    "duration",
    "count",
    "quantity",
    "title",
    "name",
    "yesno",
    "choice",
    "reason",
    "description",
    "entity",
    "unknown",
)
CARDINALITIES = ("one", "many", "count")
REQUESTS = ("recall", "inference", "recommendation")

#: Subtypes a type can carry (a contract with another subtype is cleaned by ``parse_contract``).
_SUBTYPES: Mapping[str, tuple[str, ...]] = {
    "place": ("city", "country", "region", "continent"),
    "date": ("time", "year", "month", "weekday"),
}


def _words(text: str) -> list[str]:
    """The whitespace-separated words of a vocabulary block."""
    return text.split()


@dataclass(frozen=True, slots=True)
class QueryContract:
    subjects: tuple[str, ...] = ()
    relation: str = ""
    answer_type: str = "unknown"
    subtype: str = ""
    time_scope: str = ""
    cardinality: str = "one"
    request: str = "recall"
    #: ``rules`` or ``llm`` (which resolver produced the answer type)
    source: str = "rules"

    @property
    def known(self) -> bool:
        return self.answer_type != "unknown"

    @property
    def type_label(self) -> str:
        return f"{self.answer_type}:{self.subtype}" if self.subtype else self.answer_type

    def as_meta(self) -> dict[str, Any]:
        out = asdict(self)
        out["subjects"] = list(self.subjects)
        return out


# ---------------------------------------------------------------------------- surface cues

_AUX = r"(?:did|does|do|is|are|was|were|has|have|had|will|would|can|could|should|shall|may|might)"
_YESNO_OPEN = re.compile(rf"^\W*{_AUX}\b", re.I)
_MODAL = re.compile(r"\b(?:would|could|might|likely|unlikely|probably|may)\b", re.I)
_HYPOTHETICAL = re.compile(r"^\W*if\b|\bwhat if\b|\bsuppose\b|\bimagine\b", re.I)
_RECOMMEND = re.compile(
    r"\b(?:(?:can|could|would|will) you (?:please )?(?:recommend|suggest)|"
    r"(?:recommend|suggest) (?:me|us|some|a|an|any)|any (?:ideas|tips|advice|thoughts)|"
    r"what should i|should i|where should|which should|how should i|"
    r"help me (?:pick|choose|find|plan))\b",
    re.I,
)

_COUNT = re.compile(r"\bhow (?:many|often)\b", re.I)
_UNIT = r"(?:seconds?|minutes?|hours?|days?|nights?|weeks?|months?|years?|decades?)"
_DURATION = re.compile(
    rf"\bhow long\b|\bhow (?:many|much) {_UNIT}\b|\bhow much time\b"
    rf"|\bfor how (?:long|many {_UNIT})\b",
    re.I,
)
_QUANTITY = re.compile(
    r"\bhow (?:much|old|tall|far|big|large|heavy|fast|high|deep|wide|expensive|cheap|"
    r"many (?!times)\w+)\b",
    re.I,
)

#: A wh-word is the question's head only when no other wh-word comes before it
#: ("Who supports X when ..." is a who-question, not a when-question).
_PRE = r"^\W*(?:and |so |but )?(?:(?!(?:who|whom|whose|what|which|where|when|why|how)\b)\w+ ){0,3}?"
_WHEN_HEAD = re.compile(rf"{_PRE}when\b|\bsince when\b|\buntil when\b", re.I)
_DATE_NOUN = re.compile(
    r"\b(?:what|which) (?:exact |specific )?(?P<n>date|day of the week|day|time|year|month|week|"
    r"season|weekday|decade)(?=\s+(?:did|does|do|is|are|was|were|has|have|had|will|would|can|"
    r"could|of|in|on|at|for|to)\b|\s*[?,.]|\s*$)"
    r"|\b(?:on|in|at|during) (?:what|which) (?P<m>date|day|time|year|month|week|season)\b",
    re.I,
)
_WHO = re.compile(rf"{_PRE}(?:who|whom|whose)\b", re.I)
_WHERE = re.compile(rf"{_PRE}where\b", re.I)
_WHY = re.compile(
    rf"{_PRE}why\b|\bhow come\b|\bfor what reason\b|"
    r"\bwhat (?:is|was|were|are) (?:the |his |her |their )?(?:reason|motivation|cause|purpose)\b|"
    r"\bwhat (?:motivated|prompted|led|caused|drove)\b",
    re.I,
)
_HOW_METHOD = re.compile(
    r"^\W*(?:and |so |but )?how\s+(?:did|does|do|can|could|would|will|has|have|is|was|were)\b",
    re.I,
)
_WH_NOUN = re.compile(
    r"\b(?:what|which)\s+(?:(?:kind|type|sort)s? of\s+)?"
    r"(?P<noun>(?:[a-z][a-z'-]*\s+){0,2}[a-z][a-z'-]*)\b",
    re.I,
)
_NAME_OF = re.compile(
    r"\b(?:what|which)\s+(?:is|was|are|were)\s+(?:the\s+)?(?:[\w'-]+\s+){0,3}?(?P<n>name|title)\b"
    r"|\b(?P<n2>name|title) of\b",
    re.I,
)

_PLACE_NOUNS = {
    "city": "city",
    "cities": "city",
    "town": "city",
    "towns": "city",
    "village": "city",
    "metropolis": "city",
    "country": "country",
    "countries": "country",
    "nation": "country",
    "state": "region",
    "states": "region",
    "province": "region",
    "region": "region",
    "county": "region",
    "continent": "continent",
    "place": "",
    "places": "",
    "location": "",
    "locations": "",
    "venue": "",
    "address": "",
    "neighbourhood": "",
    "neighborhood": "",
    "destination": "",
    "destinations": "",
}
_TITLE_NOUNS = frozenset(
    _words(
        """
        book books movie movies film films song songs album albums show shows series
        novel novels poem poems play plays game games painting paintings article
        articles podcast podcasts documentary documentaries magazine magazines track
        tracks story stories episode episodes title titles
        """
    )
)
_PERSON_NOUNS = frozenset(
    _words(
        """
        person people friend friends artist artists author authors singer singers actor
        actors player players teacher teachers family relative relatives
        """
    )
)
#: Nouns after what / which that do not name the answer's kind ("what did", "what is the").
_NOT_NOUN = frozenset(
    _words(
        """
        is was are were did does do has have had will would can could should the a an
        his her their my your our its this that these those other else about if happened
        """
    )
)

_MONTHS = _words(
    """
        january february march april may june july august september october november
        december
        """
)
_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_NOT_SUBJECT = frozenset(
    w.capitalize()
    for w in (
        *_MONTHS,
        *_DAYS,
        "what",
        "when",
        "where",
        "who",
        "whom",
        "whose",
        "which",
        "why",
        "how",
        "did",
        "does",
        "do",
        "is",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "may",
        "might",
        "if",
        "and",
        "or",
        "but",
        "so",
        "in",
        "on",
        "at",
        "the",
        "a",
        "an",
        "i",
        "you",
        "yes",
        "no",
        "tell",
        "list",
        "name",
        "give",
    )
)

_TIME_SCOPE = re.compile(
    rf"\b(?:in|during|since|before|after|by|until|till|on|around|from|as of)\s+(?:the\s+)?"
    rf"(?:(?:year\s+)?\d{{4}}|(?:{'|'.join(_MONTHS)})(?:\s+\d{{1,2}}(?:st|nd|rd|th)?)?(?:,?\s+\d{{4}})?|"
    rf"(?:{'|'.join(_DAYS)})|summer|winter|spring|fall|autumn|"
    rf"(?:last|this|next|past|previous)\s+(?:\w+\s+)?(?:week|month|year|weekend|summer|winter|"
    rf"spring|fall|autumn|night|day|time))\b"
    rf"|\b(?:last|this|next|past|previous)\s+(?:week|month|year|weekend|night|summer|winter)\b"
    rf"|\b(?:yesterday|today|tomorrow|recently|currently|right now|nowadays|these days|"
    rf"the first time|the last time|for the first time|at first|originally|initially|lately)\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{'|'.join(_MONTHS)})\b"
    rf"|\b(?:{'|'.join(m[:3] for m in _MONTHS)})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b",
    re.I,
)

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*|\d+")
_FUNCTION = frozenset(
    _words(
        """
        the a an of to in on at and or for with by from as is was were are be been being
        do does did has have had will would can could should shall may might what when
        where who whom whose which why how that this these those it its he she they his
        her their i you we my your our me him them us not no any some all there than
        then also if but so just about into over after before during since until up down
        out off again more most much many
        """
    )
)


def _subjects(question: str) -> tuple[str, ...]:
    """Capitalised names (runs of capitalised words joined), ``I``/``my`` as ``asker`` and
    ``you``/``your`` as ``assistant``; time words and interrogatives are not names."""
    text = question.strip()
    out: list[str] = []
    tokens = re.findall(r"[A-Za-z][A-Za-z'\u2019-]*|[.,;:!?()]", text)
    run: list[str] = []

    def flush() -> None:
        if run:
            name = " ".join(run)
            if name not in out:
                out.append(name)
            run.clear()

    for i, tok in enumerate(tokens):
        word = re.sub(r"['\u2019]s$", "", tok)
        word = word.rstrip("'\u2019")
        if not word or not word[0].isupper() or word in _NOT_SUBJECT:
            flush()
            continue
        if i == 0 and word.lower() in _FUNCTION:
            flush()
            continue
        run.append(word)
        if tok != word:  # a possessive ends the run ("New York's mayor")
            flush()
    flush()
    low = text.lower()
    if re.search(r"\b(?:i|my|me|mine|myself|i'm|i've|i'd)\b", low):
        out.append("asker")
    if re.search(r"\b(?:you|your|yours|you're|you've)\b", low):
        out.append("assistant")
    return tuple(out)


def _relation(question: str, subjects: tuple[str, ...]) -> str:
    names = {part.lower() for s in subjects for part in s.split()}
    seen: list[str] = []
    for raw in _WORD.findall(question):
        w = re.sub(r"['\u2019]s?$", "", raw.lower())
        if (
            w
            and w not in _FUNCTION
            and w not in names
            and not w.isdigit()
            and w not in _MONTHS
            and w not in seen
        ):
            seen.append(w)
    return " ".join(seen[:6])


def _answer_type(question: str) -> tuple[str, str]:
    q = question.strip()
    low = q.lower()
    # Order matters: the most specific shape first.
    if _WHY.search(q):
        return "reason", ""
    if _DURATION.search(q):
        return "duration", ""
    if _COUNT.search(q):
        return "count", ""
    m = _DATE_NOUN.search(q)
    if m:
        noun = (m.group("n") or m.group("m") or "").lower()
        sub = {"time": "time", "year": "year", "month": "month", "decade": "year"}.get(noun, "")
        if noun == "day of the week" or noun == "weekday":
            sub = "weekday"
        return "date", sub
    if _WHEN_HEAD.search(q):
        return "date", ""
    nm = _NAME_OF.search(q)
    if nm:
        which = (nm.group("n") or nm.group("n2") or "").lower()
        # "the title of the book" is a title; "the name of X's pet" is a name.
        return ("title", "") if which == "title" else ("name", "")
    m = _WH_NOUN.search(q)
    first_noun = ""
    if m:
        # The kind of thing asked for is the first known noun among the words after what /
        # which ("what additional country" -> country); else the first non-function word.
        words = m.group("noun").lower().split()
        for noun in words:
            if noun in _NOT_NOUN:
                break
            if noun in _PLACE_NOUNS:
                return "place", _PLACE_NOUNS[noun]
            if noun in _TITLE_NOUNS:
                return "title", ""
            if noun in _PERSON_NOUNS:
                return "person", ""
        if words and words[0] not in _NOT_NOUN and len(words[0]) > 2:
            first_noun = words[0]
    if _WHO.search(q):
        return "person", ""
    if _WHERE.search(q):
        return "place", ""
    if _QUANTITY.search(q):
        return "quantity", ""
    if _HOW_METHOD.search(q):
        return "description", ""
    if _YESNO_OPEN.search(q):
        # "Is X A or B?" asks for one of the options, not for yes/no.
        if re.search(r"\b\w+(?:,\s*\w+)?\s+or\s+\w+", low) and not re.search(
            r"\b(?:or not|or no)\b", low
        ):
            return "choice", ""
        return "yesno", ""
    if first_noun:
        return "entity", ""
    return "unknown", ""


def _cardinality(question: str, answer_type: str) -> str:
    if answer_type == "count":
        return "count"
    if answer_type in ("yesno", "choice", "date", "duration", "quantity", "reason", "unknown"):
        # "when" / "how long" / yes-no name one value even with a plural noun nearby.
        return "one"
    if is_set_question_wide(question) or is_set_question(question):
        return "many"
    if re.search(r"\b(?:list|all (?:the|of)|every|each)\b", question, re.I):
        return "many"
    return "one"


def _request(question: str, answer_type: str) -> str:
    if _RECOMMEND.search(question):
        return "recommendation"
    if _HYPOTHETICAL.search(question):
        return "inference"
    if _MODAL.search(question) and answer_type in (
        "yesno",
        "choice",
        "description",
        "entity",
        "unknown",
    ):
        return "inference"
    if _MODAL.search(question) and re.search(r"\b(?:would|likely|might)\b", question, re.I):
        return "inference"
    return "recall"


def build_contract(question: str) -> QueryContract:
    """The rules-only contract of ``question`` (never raises; an empty or unreadable question
    gives the ``unknown`` contract)."""
    if not isinstance(question, str) or not question.strip():
        return QueryContract()
    atype, sub = _answer_type(question)
    subjects = _subjects(question)
    scope = _TIME_SCOPE.search(question)
    return QueryContract(
        subjects=subjects,
        relation=_relation(question, subjects),
        answer_type=atype,
        subtype=sub,
        time_scope=scope.group(0).strip() if scope else "",
        cardinality=_cardinality(question, atype),
        request=_request(question, atype),
        source="rules",
    )


def is_ambiguous(contract: QueryContract) -> bool:
    """True when the rules are unsure of the answer type: it is ``unknown``, or only the
    broad ``entity``. These are the only questions an optional resolver is asked about."""
    return contract.answer_type in ("unknown", "entity")


# ------------------------------------------------------------------------- resolver merge


def parse_contract(raw: Mapping[str, Any], base: QueryContract | None = None) -> QueryContract:
    """A contract from a resolver's mapping (the ``plan@contract`` output), validated: an
    unknown type / cardinality / request / subtype falls back to ``base``'s value (the rules')
    rather than being invented. ``base`` supplies subjects, relation and time scope when the
    resolver leaves them out."""
    base = base or QueryContract()
    atype = str(raw.get("answer_type") or "").strip().lower()
    if atype not in ANSWER_TYPES:
        atype = base.answer_type
    sub = str(raw.get("subtype") or "").strip().lower()
    if sub not in _SUBTYPES.get(atype, ()):
        sub = base.subtype if atype == base.answer_type else ""
    card = str(raw.get("cardinality") or "").strip().lower()
    if card not in CARDINALITIES:
        card = base.cardinality
    req = str(raw.get("request") or "").strip().lower()
    if req not in REQUESTS:
        req = base.request
    subjects = raw.get("subjects") or raw.get("persons") or ()
    if isinstance(subjects, str):
        subjects = (subjects,)
    subs = tuple(str(s).strip() for s in subjects if str(s).strip()) or base.subjects
    return QueryContract(
        subjects=subs,
        relation=str(raw.get("relation") or "").strip() or base.relation,
        answer_type=atype,
        subtype=sub,
        time_scope=str(raw.get("time_scope") or "").strip() or base.time_scope,
        cardinality=card,
        request=req,
        source="llm",
    )


def merge_contract(rules: QueryContract, resolved: QueryContract | None) -> QueryContract:
    """The resolver may refine an ambiguous contract; it never blanks a known one and never
    overrides a rule-recognised type (the rules are the fallback AND the guard)."""
    if resolved is None or not is_ambiguous(rules) or resolved.answer_type == "unknown":
        return rules
    return resolved


# ------------------------------------------------------------------------------ rendering


def contract_header(contract: QueryContract) -> str | None:
    """The one-line reader header, or None when the answer type is unknown (an unknown
    contract adds nothing: the general evidence-grounded path)."""
    if not contract.known:
        return None
    expected = {"one": "one", "many": "many (list every one the memories support)"}.get(
        contract.cardinality, "a number"
    )
    line = f"Answer type: {contract.type_label}; expected: {expected}."
    if contract.request == "inference":
        line += " Inference from the memories is invited."
    return line


def rerank_hint(contract: QueryContract) -> str | None:
    """A short suffix for the reranker's query (never the embedder's): the expected answer
    type in words, or None for an unknown type."""
    if not contract.known:
        return None
    label = contract.type_label.replace(":", " ")
    return f" (the answer is a {label})"
