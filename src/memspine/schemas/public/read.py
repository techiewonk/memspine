# ruff: noqa: E501
"""The read surface (schema session 10): request, bundles, blocks, result."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import AwareDatetime, Field, model_validator

from memspine.schemas.base import PublicModel
from memspine.schemas.durable.assertion import AssertionEnvelope
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence
from memspine.schemas.durable.temporal import TemporalRef
from memspine.schemas.public.receipt import Capability

__all__ = [
    "ContextBlock",
    "EvidenceBundle",
    "ReadRequest",
    "ReadResult",
    "RenderedLine",
    "ScoredItem",
]

Accounting = Literal["exact", "estimated"]


class ReadRequest(PublicModel):
    """A read. A multi-party read must carry a viewer, bound to the authenticated principal (ER-29).

    ``principal`` is set by the handle from authentication, never by the caller's payload; ``viewer`` must
    be that principal. A multi-party read without a viewer is rejected.
    """

    __semver__: ClassVar[str] = "1.0.0"

    query: str = Field(min_length=1)
    scope: OwnerScope
    multi_party: bool = False
    principal: str | None = None
    viewer: str | None = None
    depth: Literal["fast", "standard", "deep"] | None = None  # None -> config default
    as_of_valid: AwareDatetime | None = None  # "what was true on X": event time
    as_of_known: AwareDatetime | None = None  # "what did we know on X": source and recorded time
    budget_tokens: int | None = Field(default=None, gt=0)  # None -> 2,048
    reference_time: AwareDatetime | None = None
    lang: str | None = None

    @model_validator(mode="after")
    def _viewer(self) -> ReadRequest:
        if self.multi_party and self.viewer is None:
            raise ValueError("a multi-party read requires a viewer")
        if self.viewer is not None:
            if self.principal is None:
                raise ValueError("a viewer must be bound to an authenticated principal")
            if self.viewer != self.principal:
                raise ValueError("the viewer must be the authenticated principal")
        return self


class EvidenceBundle(PublicModel):
    """An assertion with its support evidence, binding evidence and time anchors. It enters the context
    whole or not at all (ER-24): the bundle must carry every support and binding item of the assertion."""

    __semver__: ClassVar[str] = "1.0.0"

    record_id: str = Field(min_length=1)
    assertion: AssertionEnvelope
    support: list[Evidence] = Field(default_factory=list)
    binding: list[Evidence] = Field(default_factory=list)
    time_anchors: list[TemporalRef] = Field(default_factory=list)
    tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _whole(self) -> EvidenceBundle:
        if any(e.role != "support" for e in self.support):
            raise ValueError("'support' holds support evidence only")
        if any(e.role != "binding" for e in self.binding):
            raise ValueError("'binding' holds binding evidence only")
        for role, given in (("support", self.support), ("binding", self.binding)):
            want = [e.model_dump() for e in self.assertion.evidence if e.role == role]
            have = [e.model_dump() for e in given]
            if want != have:
                raise ValueError(
                    f"a bundle must carry all {role} evidence of its assertion, or none of it"
                )
        return self


class RenderedLine(PublicModel):
    """One rendered line of a context block. ``alias`` is the citation handle ("[3]")."""

    __semver__: ClassVar[str] = "1.0.0"

    alias: str = Field(min_length=1)
    record_id: str = Field(min_length=1)
    speaker: str | None = None
    date: str | None = None
    modality: str | None = None
    wrapped: Literal["none", "untrusted", "instruction", "claim"] = "none"
    text: str


class ContextBlock(PublicModel):
    """A typed section of the context. ``accounting`` says how ``tokens`` was counted: exactly with the
    reader model's tokenizer (headers and reserves included), or estimated when none was available."""

    __semver__: ClassVar[str] = "1.0.0"

    kind: Literal["contract", "profile", "raw", "notes", "public"]
    lines: list[RenderedLine] = Field(default_factory=list)
    tokens: int = Field(default=0, ge=0)
    cap: int | None = Field(default=None, ge=0)
    accounting: Accounting = "estimated"
    tokenizer: str | None = None

    @model_validator(mode="after")
    def _accounting(self) -> ContextBlock:
        if self.accounting == "exact" and not self.tokenizer:
            raise ValueError("exact accounting names the tokenizer")
        if self.cap is not None and self.tokens > self.cap:
            raise ValueError("block exceeds its cap")
        return self


class ScoredItem(PublicModel):
    """A hit with its score. An item that was not scored is marked ``scored=False`` with ``score=None``;
    it never carries a placeholder score of 0 (R-Q4)."""

    __semver__: ClassVar[str] = "1.0.0"

    record_id: str = Field(min_length=1)
    scored: bool = True
    score: float | None = None

    @model_validator(mode="after")
    def _explicit(self) -> ScoredItem:
        if self.scored and self.score is None:
            raise ValueError("a scored item needs a score")
        if not self.scored and self.score is not None:
            raise ValueError("an unscored item must not carry a score (never 0)")
        return self


class ReadResult(PublicModel):
    """The answer to a read. ``partial`` with ``missing_capabilities`` marks a read issued before every
    needed capability was ready (S7). ``contract`` and ``trace`` are diagnostic and carry no compatibility
    promise beyond being JSON objects."""

    __semver__: ClassVar[str] = "1.0.0"

    depth: Literal["fast", "standard", "deep"] | None = None
    blocks: list[ContextBlock] = Field(default_factory=list)
    items: list[ScoredItem] = Field(default_factory=list)
    aliases: dict[str, str] = Field(default_factory=dict)
    tokens_used: int = Field(default=0, ge=0)
    accounting: Accounting = "estimated"
    boundary_index: int | None = None
    abstained: bool = False
    reason: str | None = None
    partial: bool = False
    missing_capabilities: list[Capability] = Field(default_factory=list)
    revalidated: bool = False  # visibility and erasure re-checked just before return (POL-1)
    store_generation: int | None = Field(default=None, ge=0)
    erasure_generation: int | None = Field(default=None, ge=0)
    contract: dict[str, Any] | None = None
    trace: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _partial(self) -> ReadResult:
        if self.partial != bool(self.missing_capabilities):
            raise ValueError("partial is set exactly when missing_capabilities is non-empty")
        if self.accounting == "exact" and any(b.accounting != "exact" for b in self.blocks):
            raise ValueError("exact result accounting needs every block counted exactly")
        return self
