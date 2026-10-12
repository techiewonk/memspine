# ruff: noqa: E501
"""The stored assertion envelope (schema sessions 2-6; W-15 C, M-06).

The model authors content only, with citations (S2-1). Everything else here is system-set. Unknown model
fields live in ``extras`` (not indexed, not used by supersede, graph or profile logic).
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field, model_validator

from memspine.schemas.base import DurableModel, DurablePart
from memspine.schemas.durable.assessment import Assessment
from memspine.schemas.durable.common import Fingerprint, OwnerScope, Provenance, SrcClass
from memspine.schemas.durable.evidence import Evidence
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import TimeAxes
from memspine.schemas.reserved import reserved_hits

__all__ = [
    "AssertionEnvelope",
    "AssertionStatus",
    "GroundingChecks",
    "IntegrationVerdict",
    "Mention",
    "inherit_trust",
]

AssertionStatus = Literal["active", "tentative", "contested", "superseded", "hidden"]
Grounding = Literal["grounded", "uncertain", "rejected"]
Scalar = str | int | float | bool


class Mention(DurablePart):
    """A surface mention as written, with its system-assigned mention id and resolved entity (if any)."""

    surface: str = Field(min_length=1)
    mention_id: str = Field(min_length=1)
    entity_id: str | None = None  # None until resolved; a provisional id may be set (I-5)


class GroundingChecks(DurablePart):
    """Per-check grounding results (W-17); ``None`` = check not run."""

    span: bool | None = None
    numbers: bool | None = None
    polarity: bool | None = None
    names: bool | None = None
    endpoints: bool | None = None


class IntegrationVerdict(DurablePart):
    """How the conflict ladder integrated this assertion (G0 operator split), set at integration."""

    op: Literal["append", "supersede", "merge", "coexist", "contest", "noop", "defer_kc"]
    rung: str | None = None
    hint: str | None = None


class AssertionEnvelope(DurableModel):
    """One stored assertion.

    Identities are separate fields (ER-13, S3): ``write_id`` (retry id of the write that produced it),
    mention ids (on :class:`Mention`), ``assertion_key``, ``occurrence_id`` and ``record_id`` (``None``
    only until the write door assigns it). ``scope`` is tenant plus owner; the session is provenance.
    """

    __kind__: ClassVar[str | None] = "assertion_envelope"

    # --- identities (system-authored) ---
    write_id: str = Field(min_length=1)
    assertion_key: AssertionKey
    key_aliases: list[str] = Field(default_factory=list)
    occurrence_id: str = Field(min_length=1)
    record_id: str | None = None
    scope: OwnerScope
    provenance: Provenance = Field(default_factory=Provenance)

    # --- content (model-authored, S2-1) ---
    text: str = Field(min_length=1)
    kind: str = Field(min_length=1)  # open set (S2-3); normalised by the kind registry (MOD-02)
    subject: Mention | None = None
    object: Mention | None = None
    attribute: str | None = None
    value: Scalar | None = None
    speaker: str | None = None
    change_hint: bool = (
        False  # I-3: extraction marked that the cited lines say the earlier value no longer holds
    )

    # --- time, evidence, assessment ---
    time: TimeAxes = Field(default_factory=TimeAxes)
    evidence: list[Evidence] = Field(default_factory=list)
    assessment: Assessment

    # --- system state ---
    src_class: SrcClass
    parents: list[str] = Field(default_factory=list)
    status: AssertionStatus = "active"
    verified: bool = False
    epoch: int | None = Field(default=None, ge=1)  # fencing epoch of the job that wrote it (ER-07)
    erasure_generation: int = Field(default=0, ge=0)
    grounding: Grounding | None = None
    checks: GroundingChecks | None = None
    prompt_hash: str | None = None
    derivation: Fingerprint | None = None
    graph: dict[str, Any] | None = None  # code-compiled projection, never model output
    verdict: IntegrationVerdict | None = None

    extras: dict[str, Any] = Field(default_factory=dict)

    @property
    def mention_ids(self) -> list[str]:
        return [m.mention_id for m in (self.subject, self.object) if m is not None]

    @model_validator(mode="after")
    def _invariants(self) -> AssertionEnvelope:
        # ER-32 / S2-1: nothing is stored without a citation (derived records cite through lineage).
        cited = [e for e in self.evidence if e.counts_as_evidence]
        derived_with_parents = self.src_class == "derived" and bool(self.parents)
        if not cited and not derived_with_parents:
            raise ValueError("an assertion needs at least one support or binding evidence")
        if (
            self.assessment.modality == "asserted"
            and not derived_with_parents
            and not any(e.role == "support" for e in self.evidence)
        ):
            raise ValueError("an asserted item requires support evidence")
        # S5-1: time_precision is the precision of the event time, not a second source of truth.
        if self.assessment.time_precision != self.time.event_precision:
            raise ValueError(
                f"assessment.time_precision {self.assessment.time_precision!r} disagrees with the "
                f"event time precision {self.time.event_precision!r}"
            )
        # Session 2 condition 1: reserved system names are never stored as extras.
        hits = reserved_hits(self.extras)
        if hits:
            raise ValueError(f"extras may not contain reserved names: {hits}")
        ids = self.mention_ids
        if len(set(ids)) != len(ids):
            raise ValueError("mention ids must be distinct")
        return self


def inherit_trust(parent_trusts: list[float], proposed: float) -> float:
    """G1: a deposit inherits ``tau <= min over its parents``. Returns the bounded trust."""
    if not parent_trusts:
        return proposed
    return min(proposed, min(parent_trusts))
