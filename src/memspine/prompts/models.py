"""Output models paired with prompts (D-43 §3): typed LLM responses (D-31).

Each shipped prompt's ``output_model`` frontmatter names one of these; the
structured-output helper validates the (repaired) response against it.
"""

from __future__ import annotations

from datetime import date as _date
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "OUTPUT_MODELS",
    "AnticipatedCue",
    "AnticipatedCues",
    "ConflictVerdictOut",
    "ConsolidatedFact",
    "ConsolidatedFacts",
    "DuplicateVerdictOut",
    "EntityResolutionOut",
    "ExtractedEdge",
    "ExtractedEdges",
    "ExtractedFact",
    "ExtractedFacts",
    "Insight",
    "Insights",
    "InstructionFlagOut",
    "RelevanceLabel",
    "RelevanceLabels",
]


def _as_text(value: Any) -> Any:
    """Scalars YAML did not keep as text: an unquoted ``2023-05-08`` loads as a date,
    ``2023`` or ``32`` as a number. Turn them back into the string the model wrote."""
    if isinstance(value, _date):
        return value.isoformat()
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return value


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

    _scalars_as_text = field_validator("entity", "attribute", "value", "date", mode="before")(
        _as_text
    )

    @field_validator("kind", mode="before")
    @classmethod
    def _kind_or_event(cls, value: Any) -> Any:
        """Tolerate a missing, blank or unknown ``kind`` from the miner: it is an event."""
        text = str(value).strip().lower() if value is not None else ""
        return text if text in ("state", "event") else "event"


class ExtractedFacts(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list)


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

    _valid_from_as_text = field_validator("valid_from", mode="before")(_as_text)


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


class InstructionFlagOut(BaseModel):
    instruction_shaped: bool
    reason: str = ""


OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "ExtractedFacts": ExtractedFacts,
    "ExtractedEdges": ExtractedEdges,
    "ConsolidatedFacts": ConsolidatedFacts,
    "Insights": Insights,
    "ConflictVerdictOut": ConflictVerdictOut,
    "DuplicateVerdictOut": DuplicateVerdictOut,
    "EntityResolutionOut": EntityResolutionOut,
    "InstructionFlagOut": InstructionFlagOut,
    "AnticipatedCues": AnticipatedCues,
    "RelevanceLabels": RelevanceLabels,
}
