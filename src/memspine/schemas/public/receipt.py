"""Receipt, readiness, wait and job handles (schema sessions 7 and 8)."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import AwareDatetime, Field, model_validator

from memspine.schemas.base import PublicModel
from memspine.schemas.durable.common import OwnerScope

__all__ = [
    "CAPABILITIES",
    "Capability",
    "JobHandle",
    "JobState",
    "Readiness",
    "ReadinessState",
    "Receipt",
    "WaitResult",
]

Capability = Literal["raw_search", "facts", "graph", "summaries", "profile"]
ReadinessState = Literal["pending", "ready", "unavailable", "failed"]
JobState = Literal["queued", "running", "succeeded", "failed", "cancelled", "coalesced"]
CAPABILITIES: tuple[str, ...] = ("raw_search", "facts", "graph", "summaries", "profile")


class Readiness(PublicModel):
    """State of each capability for a write. A capability that is not configured is ``unavailable``,
    never ``pending`` (S7)."""

    __semver__: ClassVar[str] = "1.0.0"

    raw_search: ReadinessState
    facts: ReadinessState
    graph: ReadinessState
    summaries: ReadinessState
    profile: ReadinessState

    def get(self, capability: str) -> ReadinessState:
        if capability not in CAPABILITIES:
            raise KeyError(f"unknown capability {capability!r}")
        return getattr(self, capability)  # type: ignore[no-any-return]

    def pending(self, capabilities: list[str] | None = None) -> list[str]:
        names = list(capabilities) if capabilities is not None else list(CAPABILITIES)
        return [c for c in names if self.get(c) == "pending"]


class Receipt(PublicModel):
    """Durable acceptance of a write (ER-06).

    ``durability`` is ``durable``, or ``memory`` in ephemeral mode (the weaker guarantee). A retry, as
    identified by the S3-3 write id, returns the original receipt.
    """

    __semver__: ClassVar[str] = "1.0.0"

    durability: Literal["durable", "memory"]
    event_ids: list[str]
    record_ids: list[str]
    intent_id: str
    source_id: str
    source_version: int = Field(ge=1)
    accepted_seq: int | None = Field(default=None, ge=0)
    readiness: Readiness
    retried: list[int] = Field(default_factory=list)  # seg_index values recognised as retries
    quarantined: list[int] = Field(default_factory=list)  # seg_index values held by gate 1


class WaitResult(PublicModel):
    """Readiness reached by ``wait(capabilities, timeout)``. A read issued after a timed-out wait is
    marked partial with the missing capabilities listed."""

    __semver__: ClassVar[str] = "1.0.0"

    requested: list[Capability] = Field(min_length=1)
    readiness: Readiness
    timed_out: bool
    waited_ms: int = Field(ge=0)

    @property
    def missing(self) -> list[str]:
        return self.readiness.pending(list(self.requested))

    @model_validator(mode="after")
    def _consistent(self) -> WaitResult:
        if len(set(self.requested)) != len(self.requested):
            raise ValueError("duplicate requested capability")
        if self.timed_out and not self.missing:
            raise ValueError("timed_out but every requested capability has settled")
        if not self.timed_out and self.missing:
            raise ValueError("not timed out but requested capabilities are still pending")
        return self


class JobHandle(PublicModel):
    """Handle returned by ``sleep()`` and other job-starting calls; they never block (OF-1)."""

    __semver__: ClassVar[str] = "1.0.0"

    job_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    state: JobState
    scope: OwnerScope | None = None
    created_at: AwareDatetime | None = None
    target_offset: int | None = Field(default=None, ge=-1)  # the immutable snapshot target (ER-26)
    coalesced_into: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _coalesced(self) -> JobHandle:
        if (self.state == "coalesced") != (self.coalesced_into is not None):
            raise ValueError("coalesced_into is set exactly when state is 'coalesced'")
        if self.error is not None and self.state != "failed":
            raise ValueError("error is only set on a failed job")
        return self

    @property
    def terminal(self) -> bool:
        return self.state in ("succeeded", "failed", "cancelled", "coalesced")
