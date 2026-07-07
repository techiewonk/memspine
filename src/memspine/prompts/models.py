"""Output models paired with prompts (D-43 §3): typed LLM responses (D-31).

Each shipped prompt's ``output_model`` frontmatter names one of these; the
structured-output helper validates the (repaired) response against it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "OUTPUT_MODELS",
    "ConflictVerdictOut",
    "DuplicateVerdictOut",
    "ExtractedFact",
    "ExtractedFacts",
    "InstructionFlagOut",
]


class ExtractedFact(BaseModel):
    entity: str
    attribute: str
    value: str
    confidence: float = 1.0


class ExtractedFacts(BaseModel):
    facts: list[ExtractedFact] = Field(default_factory=list)


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
    "ConflictVerdictOut": ConflictVerdictOut,
    "DuplicateVerdictOut": DuplicateVerdictOut,
    "InstructionFlagOut": InstructionFlagOut,
}
