# ruff: noqa: E501
"""Evidence (schema session 4): roles, turn reference, span, source family."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from memspine.schemas.base import DurablePart

__all__ = ["EVIDENCE_ROLES", "Evidence", "EvidenceRole", "Span", "TurnRef"]

EvidenceRole = Literal["support", "binding", "context"]
EVIDENCE_ROLES: tuple[str, ...] = ("support", "binding", "context")


class TurnRef(DurablePart):
    """A cited turn of a pinned source version."""

    source_id: str = Field(min_length=1)
    source_version: int = Field(ge=1)
    seg_index: int = Field(ge=0)


class Span(DurablePart):
    """Character range inside one turn, plus the verbatim excerpt when kept."""

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Span:
        if self.end < self.start:
            raise ValueError("span end is before its start")
        return self


class Evidence(DurablePart):
    """One piece of evidence for an assertion.

    * ``support`` states the fact.
    * ``binding`` resolves a pronoun, relative date or reference (ER-11); it must be cited.
    * ``context`` is shown to the model but never counts as evidence and never raises trust.

    ``source_family`` is the asserting principal (S4-2): speaker id, author or document, tool name, or a
    web page's registrable domain. Derived records inherit parents' families through lineage.
    """

    role: EvidenceRole
    turn: TurnRef | None = None
    span: Span | None = None
    source_family: str = Field(min_length=1)

    @model_validator(mode="after")
    def _cited(self) -> Evidence:
        if self.role in ("support", "binding") and self.turn is None:
            raise ValueError(f"{self.role} evidence must cite a turn")
        return self

    @property
    def counts_as_evidence(self) -> bool:
        """Context lines never count (S4-1)."""
        return self.role != "context"
