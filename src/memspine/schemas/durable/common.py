"""Shared durable value types: owner scope (S3-1), provenance, derivation fingerprint, source refs."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from memspine.schemas.base import DurablePart

__all__ = ["Fingerprint", "OwnerScope", "Provenance", "SourceRef", "SrcClass"]

SrcClass = Literal[
    "user_stated", "assistant_proposed", "user_confirmed", "tool", "document", "external", "derived"
]


class OwnerScope(DurablePart):
    """Memory scope: tenant plus memory owner (S3-1).

    The owner is a user or a shared group; every agent and session serving that owner sees the same
    entities. The session is *not* part of the scope: it is recorded as :class:`Provenance` only.
    """

    tenant: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    owner_kind: Literal["user", "group"] = "user"


class Provenance(DurablePart):
    """Where a write came from. Provenance only: never part of identity or entity keys (S3-1)."""

    session_id: str | None = None
    agent_id: str | None = None
    principal: str | None = None
    source_id: str | None = None
    source_version: int | None = Field(default=None, ge=1)


class Fingerprint(DurablePart):
    """Derivation fingerprint (P-12): what produced a derived item, for staleness and re-derivation."""

    prompt_hash: str
    schema_v: str
    model_id: str
    quant: str
    decoding: str
    input_blocks: str
    runtime_libs: str


class SourceRef(DurablePart):
    """A pinned source: id plus version."""

    id: str = Field(min_length=1)
    version: int = Field(ge=1)
