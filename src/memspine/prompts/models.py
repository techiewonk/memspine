"""Output models paired with prompts (D-43 §3): typed LLM responses (D-31).

Each shipped prompt's ``output_model`` frontmatter names one of these; the
structured-output helper validates the (repaired) response against it.
"""

from __future__ import annotations

import re
from datetime import date as _date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from memspine.config import constants

__all__ = [
    "OUTPUT_MODELS",
    "AnswerVerdictOut",
    "AnticipatedCue",
    "AnticipatedCues",
    "ConflictVerdictOut",
    "ConsolidatedFact",
    "ConsolidatedFacts",
    "DuplicateVerdictOut",
    "EntityMatch",
    "EntityMatches",
    "EntityResolutionOut",
    "EntitySummaries",
    "EntitySummary",
    "ExtractedEdge",
    "ExtractedEdges",
    "ExtractedFact",
    "ExtractedFacts",
    "FactClass",
    "FactClasses",
    "FactDate",
    "FactDates",
    "Insight",
    "Insights",
    "InstructionFlagOut",
    "MissingInfoOut",
    "ReadPlan",
    "RelevanceLabel",
    "RelevanceLabels",
    "SufficiencyOut",
    "view_text",
]


def _as_text(value: Any) -> Any:
    """Scalars YAML did not keep as text: an unquoted ``2023-05-08`` loads as a date,
    ``2023`` or ``32`` as a number. Turn them back into the string the model wrote."""
    if isinstance(value, _date):
        return value.isoformat()
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return value


#: #31 (Graphiti attribute guards): the longest entity / attribute / value a mined fact
#: may carry. A longer entity or attribute is not a key but leaked prose, so the fact
#: is dropped; a longer value is cut at a word boundary.
FACT_FIELD_MAX_CHARS = 250

#: #31: an entity or attribute that opens like model reasoning, not like a key.
_REASONING = re.compile(
    r"^\s*(?:<think>|let me\b|let's\b|i think\b|i need to\b|i will\b|i'll\b|"
    r"i should\b|hmm\b|okay,|ok,|wait,|first,? i\b|step \d|reasoning:|thought:|"
    r"analysis:|the user (?:says|said|mentions|mentioned|is asking)\b)",
    re.IGNORECASE,
)

#: #31: a VALUE that is reasoning as a whole. A value is free text, so only openings
#: that are reasoning and nothing else count: "Let Me Love You" (a song), "step 3 of
#: the adoption process" and "I think therefore I am" are facts.
_VALUE_REASONING = re.compile(
    r"^\s*(?:<think>|(?:let me|let's) (?:think|see|check|analy[sz]e|reason|figure|"
    r"consider|work)\b|step 1:|reasoning:|thought:|analysis:|"
    r"the user (?:says|said|mentions|mentioned|is asking)\b)",
    re.IGNORECASE,
)

#: #31: placeholder entities and attributes a model invents when it has nothing to say.
_PLACEHOLDERS = frozenset(
    [
        "",
        "-",
        "?",
        "??",
        "???",
        "...",
        "…",
        "n/a",
        "na",
        "none",
        "null",
        "nil",
        "unknown",
        "not mentioned",
        "not specified",
        "not stated",
        "not provided",
        "not available",
        "unspecified",
        "tbd",
        "todo",
        "placeholder",
        "value",
        "entity",
        "attribute",
        "example",
    ]
)

#: #31: placeholder values. "none", "nil" and "unknown" are left out: they are real
#: answers ("pets: none").
_VALUE_PLACEHOLDERS = _PLACEHOLDERS - {"none", "nil", "unknown"}


def _is_placeholder(text: str, words: frozenset[str] = _PLACEHOLDERS) -> bool:
    """``"Unknown"``, ``"N/A"``, ``"<value>"``, ``"{entity}"`` (a ``[...]`` list is data)."""
    stripped = text.strip()
    if stripped.lower().strip(" .") in words:
        return True
    return len(stripped) > 1 and stripped[0] + stripped[-1] in ("<>", "{}")


def _cap_words(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters, at a word boundary when one exists."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ,;:")


def view_text(value: Any) -> str | None:
    """#28: one multi-view field (a person, a location or a topic) after the guards.

    None for a missing, non-text, placeholder ("unknown", "N/A", ``<topic>``) or
    reasoning-shaped value; whitespace is collapsed and the text is cut at a word
    boundary to :data:`constants.MULTIVIEW_FIELD_MAX_CHARS`.
    """
    value = _as_text(value)
    if not isinstance(value, str) or "</think>" in value:
        return None
    text = " ".join(value.split())
    if _is_placeholder(text) or _REASONING.match(text):
        return None
    return _cap_words(text, constants.MULTIVIEW_FIELD_MAX_CHARS) or None


def fact_guard(item: Any) -> Any | None:
    """#31: one raw mined fact after the attribute guards, or None to drop it.

    Dropped: an entity or attribute that opens like reasoning text ("Let me ...",
    ``<think>``) or is an invented placeholder ("unknown", "N/A", ``<value>``); a value
    that is reasoning as a whole ("Let me think ...", ``<think>``, any ``</think>``) or
    a placeholder ("N/A", ``<value>``; not "none" or "unknown", real answers); and an
    entity or attribute over :data:`FACT_FIELD_MAX_CHARS`. A value over the cap is cut
    at a word boundary. Items that are not mappings pass through for the model to
    reject, as before.
    """
    if not isinstance(item, dict):
        return item
    fields = {k: _as_text(item.get(k)) for k in ("entity", "attribute", "value")}
    if not all(isinstance(v, str) for v in fields.values()):
        return item  # a missing or non-text field fails validation as before
    texts: dict[str, str] = {k: str(v) for k, v in fields.items()}
    for key, text in texts.items():
        if "</think>" in text:
            return None
        if key == "value":
            if _is_placeholder(text, _VALUE_PLACEHOLDERS) or _VALUE_REASONING.match(text):
                return None
        elif _is_placeholder(text) or _REASONING.match(text):
            return None
    if any(len(texts[k].strip()) > FACT_FIELD_MAX_CHARS for k in ("entity", "attribute")):
        return None
    value = " ".join(texts["value"].split())
    if len(value) > FACT_FIELD_MAX_CHARS:
        return {**item, "value": _cap_words(value, FACT_FIELD_MAX_CHARS)}
    return item


class ExtractedFact(BaseModel):
    entity: str
    attribute: str
    value: str
    confidence: float = 1.0
    #: H2: the date the fact refers to (YYYY-MM-DD, YYYY-MM or YYYY), resolved by the
    #: session-mining prompt from the line's date; None when no time is involved.
    date: str | None = None
    #: G1a: a ``state`` is single-valued and current (where someone lives, their job,
    #: relationship status, a pet's name), so a newer value supersedes it; an
    #: ``event`` (something that happened, a preference, hobby or plan) is one of
    #: many that hold at once and is never superseded. Missing => ``event``.
    kind: Literal["state", "event"] = "event"
    #: #29: the 1-based transcript lines the fact comes from, when the miner was
    #: shown numbered lines (``consolidation.mine_evidence_turns``); empty otherwise.
    turns: list[int] = Field(default_factory=list)
    #: #28 (multi-view fields, ``extract@session4``): the people the fact involves,
    #: where it happened and its topic or class ("activities", "books read"). Each
    #: passes the #31 guards (:func:`view_text`); empty when the miner gave none.
    persons: list[str] = Field(default_factory=list)
    location: str | None = None
    topic: str | None = None

    _scalars_as_text = field_validator("entity", "attribute", "value", "date", mode="before")(
        _as_text
    )

    @field_validator("persons", mode="before")
    @classmethod
    def _person_list(cls, value: Any) -> Any:
        """#28: ``"Melanie, Caroline"`` or a list; guarded, deduplicated, capped."""
        if value is None:
            return []
        items = value.split(",") if isinstance(value, str) else value
        if not isinstance(items, list | tuple):
            items = [items]
        out: list[str] = []
        for item in items:
            text = view_text(item)
            if text is not None and text.casefold() not in {o.casefold() for o in out}:
                out.append(text)
        return out[: constants.MULTIVIEW_MAX_PERSONS]

    @field_validator("location", "topic", mode="before")
    @classmethod
    def _view(cls, value: Any) -> Any:
        """#28: a guarded location or topic; junk becomes None, never a failure."""
        return view_text(value)

    @field_validator("kind", mode="before")
    @classmethod
    def _kind_or_event(cls, value: Any) -> Any:
        """Tolerate a missing, blank or unknown ``kind`` from the miner: it is an event."""
        text = str(value).strip().lower() if value is not None else ""
        return text if text in ("state", "event") else "event"

    @field_validator("turns", mode="before")
    @classmethod
    def _line_numbers(cls, value: Any) -> Any:
        """Tolerate ``3``, ``"3, 4"``, ``["[3]", 4]`` and junk: keep the positive ints."""
        if value is None:
            return []
        items = re.findall(r"-?\d+", value) if isinstance(value, str) else value
        if not isinstance(items, list | tuple):
            items = [items]
        out: list[int] = []
        for item in items:
            digits = re.findall(r"-?\d+", str(item))
            if digits and int(digits[0]) > 0 and int(digits[0]) not in out:
                out.append(int(digits[0]))
        return out


class ExtractedFacts(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list)

    @field_validator("facts", mode="before")
    @classmethod
    def _guarded(cls, value: Any) -> Any:
        """#31: drop the facts the attribute guards reject (:func:`fact_guard`)."""
        if not isinstance(value, list):
            return value
        return [kept for kept in (fact_guard(item) for item in value) if kept is not None]


class FactDate(BaseModel):
    """#29: the date one numbered mined fact happened (``extract@dates``)."""

    index: int
    date: str | None = None

    _date_as_text = field_validator("date", mode="before")(_as_text)


class FactDates(BaseModel):
    dates: list[FactDate] = Field(default_factory=list)


class FactClass(BaseModel):
    """#30: the list class of one numbered event fact (``extract@classes``)."""

    index: int
    label: str | None = None

    @field_validator("label", mode="before")
    @classmethod
    def _guarded(cls, value: Any) -> Any:
        return view_text(value)


class FactClasses(BaseModel):
    classes: list[FactClass] = Field(default_factory=list)


class RelevanceLabel(BaseModel):
    """H17: one candidate's 3-way relevance label (Hindsight-style)."""

    index: int
    label: str  # relevant | related | irrelevant


class RelevanceLabels(BaseModel):
    labels: list[RelevanceLabel] = Field(default_factory=list)


class AnticipatedCue(BaseModel):
    """H8: a likely future question/need and the transcript line that answers it."""

    line: int
    cue: str


class AnticipatedCues(BaseModel):
    cues: list[AnticipatedCue] = Field(default_factory=list)


class ConsolidatedFact(BaseModel):
    entity: str
    attribute: str
    value: str
    source_count: int = 1  # how many episodes supported this durable fact (M2)


class ConsolidatedFacts(BaseModel):
    facts: list[ConsolidatedFact] = Field(default_factory=list)


class Insight(BaseModel):
    insight: str
    evidence: list[int] = Field(default_factory=list)  # episode indices (M13.7)


class Insights(BaseModel):
    insights: list[Insight] = Field(default_factory=list)


class ExtractedEdge(BaseModel):
    """A relationship edge between two entities (C1, graphiti-style writes)."""

    src_entity: str
    rel: str  # a short verb-phrase slug, e.g. "works_at"
    dst_entity: str
    fact: str  # the sentence asserting the edge (provenance for the context window)
    valid_from: str | None = None  # ISO date if the text states one
    confidence: float = 1.0
    #: GP-1: a ``state`` edge is single-valued and current (lives_in, works_at), so
    #: a newer ``(src, rel)`` edge supersedes it; an ``event`` edge (read, visited,
    #: attended) is one of many that hold at once, keyed ``(src, rel, dst)`` and
    #: add-only. Missing or unknown => ``event``, mirroring ``ExtractedFact`` (G1a).
    kind: Literal["state", "event"] = "event"

    _valid_from_as_text = field_validator("valid_from", mode="before")(_as_text)

    @field_validator("kind", mode="before")
    @classmethod
    def _kind_or_event(cls, value: Any) -> Any:
        """Tolerate a missing, blank or unknown ``kind`` from the extractor: it is an event."""
        text = str(value).strip().lower() if value is not None else ""
        return text if text in ("state", "event") else "event"


class ExtractedEdges(BaseModel):
    edges: list[ExtractedEdge] = Field(default_factory=list)


class EntityResolutionOut(BaseModel):
    """Coreference/aliasing verdict for two entity mentions (C1)."""

    same_entity: bool
    canonical: str = ""  # preferred name when same_entity is true
    reason: str = ""


class ConflictVerdictOut(BaseModel):
    verdict: str  # add | update | invalidate | noop
    reason: str = ""


class DuplicateVerdictOut(BaseModel):
    duplicate: bool
    reason: str = ""


class ReadPlan(BaseModel):
    """G2a (JustMem planner): how ``read(mode="auto")`` should read for one question.

    ``lookup`` (one fact) and ``replay`` (the surrounding conversation) both read
    by replay; ``aggregate`` reads by compose, with ``subqueries`` as extra probes.
    """

    mode: Literal["lookup", "aggregate", "replay"]
    temporal: bool = False
    entities: list[str] = Field(default_factory=list)
    #: At most three; a longer list is cut, blank entries dropped.
    subqueries: list[str] = Field(default_factory=list)
    #: #36 (``plan@v3``): the people the question is about, and its time expression
    #: copied verbatim ("in May 2023", "last week"); None when it names no time.
    persons: list[str] = Field(default_factory=list)
    time_expr: str | None = None

    @field_validator("mode", mode="before")
    @classmethod
    def _mode_lower(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("time_expr", mode="before")
    @classmethod
    def _time_text(cls, value: Any) -> Any:
        if value is None:
            return None
        text = str(_as_text(value)).strip()
        return None if text.lower() in ("", "none", "null") else text

    @field_validator("entities", "subqueries", "persons", mode="before")
    @classmethod
    def _text_list(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return [str(_as_text(v)).strip() for v in value if v is not None and str(v).strip()]

    @field_validator("subqueries")
    @classmethod
    def _at_most_three(cls, value: list[str]) -> list[str]:
        return value[:3]


class InstructionFlagOut(BaseModel):
    instruction_shaped: bool
    reason: str = ""


class EntitySummary(BaseModel):
    """GP-6 (#17): the summary of one numbered entity (``summarize_entity``)."""

    index: int
    summary: str = ""

    _summary_as_text = field_validator("summary", mode="before")(_as_text)


class EntitySummaries(BaseModel):
    summaries: list[EntitySummary] = Field(default_factory=list)


class EntityMatch(BaseModel):
    """GP-7 (#18): the known entity one numbered name refers to, or empty for a
    new entity (``resolve_entity@batch``)."""

    index: int
    match: str = ""

    _match_as_text = field_validator("match", mode="before")(_as_text)


class EntityMatches(BaseModel):
    matches: list[EntityMatch] = Field(default_factory=list)


def _query_list(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(_as_text(v)).strip() for v in value if v is not None and str(v).strip()]


class SufficiencyOut(BaseModel):
    """#38 (``sufficiency``): does the context hold every item an aggregate question needs?"""

    complete: bool
    reason: str = ""


class MissingInfoOut(BaseModel):
    """#38 (``sufficiency@missing``): searches for the information the context lacks.

    At most three; a longer list is cut, blank entries dropped."""

    queries: list[str] = Field(default_factory=list)

    @field_validator("queries", mode="before")
    @classmethod
    def _text_list(cls, value: Any) -> Any:
        return _query_list(value)

    @field_validator("queries")
    @classmethod
    def _at_most_three(cls, value: list[str]) -> list[str]:
        return value[:3]


class AnswerVerdictOut(BaseModel):
    """#39 (``verify_answer``): is the answer supported by the numbered context lines?

    ``evidence`` lists the 1-based numbers of the supporting lines; ``revised_answer``
    is a corrected answer when the given one is not supported (None to keep it)."""

    supported: bool
    evidence: list[int] = Field(default_factory=list)
    revised_answer: str | None = None

    @field_validator("evidence", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, int | str):
            value = [value]
        return [int(n) for v in value for n in re.findall(r"\d+", str(v))]

    @field_validator("revised_answer", mode="before")
    @classmethod
    def _revised_text(cls, value: Any) -> Any:
        if value is None:
            return None
        text = str(_as_text(value)).strip()
        return None if text.lower() in ("", "none", "null") else text


OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "ExtractedFacts": ExtractedFacts,
    "FactDates": FactDates,
    "FactClasses": FactClasses,
    "ExtractedEdges": ExtractedEdges,
    "ConsolidatedFacts": ConsolidatedFacts,
    "Insights": Insights,
    "ConflictVerdictOut": ConflictVerdictOut,
    "DuplicateVerdictOut": DuplicateVerdictOut,
    "EntityResolutionOut": EntityResolutionOut,
    "EntityMatches": EntityMatches,
    "EntitySummaries": EntitySummaries,
    "InstructionFlagOut": InstructionFlagOut,
    "AnticipatedCues": AnticipatedCues,
    "RelevanceLabels": RelevanceLabels,
    "ReadPlan": ReadPlan,
    "SufficiencyOut": SufficiencyOut,
    "MissingInfoOut": MissingInfoOut,
    "AnswerVerdictOut": AnswerVerdictOut,
}
