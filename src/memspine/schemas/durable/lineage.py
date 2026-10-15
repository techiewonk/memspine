"""Lineage edges (schema session 4, M-09): child/parent links with a role."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field, model_validator

from memspine.schemas.base import DurableModel
from memspine.schemas.durable.common import Fingerprint
from memspine.schemas.durable.evidence import Span

__all__ = ["LINEAGE_ROLES", "LineageEdge", "LineageRole"]

# support/binding/context mirror the evidence roles (S4-1). The rest are structural: version_of and
# supersedes (value history), merge_member (entity or record merge), member (summary membership), cites.
# The insert spec's "evidence" is split into support/binding; the offline spec's "merged_from" is
# merge_member.
LineageRole = Literal[
    "support", "binding", "context", "version_of", "supersedes", "merge_member", "member", "cites"
]
LINEAGE_ROLES: tuple[str, ...] = (
    "support",
    "binding",
    "context",
    "version_of",
    "supersedes",
    "merge_member",
    "member",
    "cites",
)


class LineageEdge(DurableModel):
    """One edge of the lineage DAG. A derived record inherits families and trust bounds through these."""

    __kind__: ClassVar[str | None] = "lineage_edge"

    child: str = Field(min_length=1)
    parent: str = Field(min_length=1)
    parent_version: int | None = Field(default=None, ge=1)
    role: LineageRole
    span: Span | None = None
    source_family: str | None = (
        None  # the parent's asserting family, carried so a summary cannot vouch for itself
    )
    derivation: Fingerprint | None = None

    @model_validator(mode="after")
    def _no_self_loop(self) -> LineageEdge:
        if self.child == self.parent:
            raise ValueError("a record cannot be its own parent")
        return self
