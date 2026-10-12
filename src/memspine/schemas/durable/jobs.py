# ruff: noqa: E501
"""Jobs and progress (schema session 8): outbox rows, leases with epochs, watermark plus holes."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import AwareDatetime, Field, model_validator

from memspine.schemas.base import DurableModel, DurablePart
from memspine.schemas.durable.common import Fingerprint, OwnerScope, SourceRef

__all__ = [
    "FAILURE_CLASSES",
    "Hole",
    "JobLease",
    "OutboxKind",
    "OutboxRow",
    "OutboxStatus",
    "Progress",
    "commit_allowed",
]

OutboxKind = Literal[
    "projection",
    "derivation",
    "session_close",
    "sleep_stage",
    "rederive",
    "pending_merge",
    "asset",
    "window",
    "heavy_projection",
]
OutboxStatus = Literal["pending", "leased", "done", "failed", "dead", "underived"]
FailureClass = Literal[
    "raised",
    "malformed",
    "valid_empty",
    "truncated",
    "size_skip",
    "partial_commit",
    "invented_alias",
    "budget_exhausted",
]
FAILURE_CLASSES: tuple[str, ...] = (
    "raised",
    "malformed",
    "valid_empty",
    "truncated",
    "size_skip",
    "partial_commit",
    "invented_alias",
    "budget_exhausted",
)


class OutboxRow(DurableModel):
    """One unit of derived work, claimed under a lease. The insert and offline spellings are unified.

    ``sources`` replaces insert's ``source_id``+``source_version`` and offline's ``source_ids``/``members``;
    ``scope`` replaces offline's ``namespace``. ``erasure_generation`` is the generation at enqueue (G-13).
    ``epoch`` is the claim epoch of the current lease (ER-07); a commit is refused if it is stale.
    """

    __kind__: ClassVar[str | None] = "outbox_row"

    id: str = Field(min_length=1)
    scope: OwnerScope
    kind: OutboxKind
    sources: list[SourceRef] = Field(default_factory=list)
    fingerprint: str | None = None
    derivation: Fingerprint | None = None
    erasure_generation: int = Field(default=0, ge=0)
    status: OutboxStatus = "pending"
    attempts: int = Field(default=0, ge=0)
    lease_owner: str | None = None
    lease_until: AwareDatetime | None = None
    epoch: int | None = Field(default=None, ge=1)
    not_before: AwareDatetime | None = None
    last_error: str | None = None
    failure_class: FailureClass | None = None

    @model_validator(mode="after")
    def _leased(self) -> OutboxRow:
        if self.status == "leased" and (
            self.lease_owner is None or self.lease_until is None or self.epoch is None
        ):
            raise ValueError("a leased row needs lease_owner, lease_until and epoch")
        if (
            self.status in ("failed", "dead")
            and self.failure_class is None
            and self.last_error is None
        ):
            raise ValueError("a failed row records a failure_class or last_error")
        return self


class JobLease(DurableModel):
    """A claim on a job or namespace. ``epoch`` strictly increases per (scope, job) with each claim."""

    __kind__: ClassVar[str | None] = "job_lease"

    scope: OwnerScope
    job_id: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    epoch: int = Field(ge=1)
    acquired_at: AwareDatetime
    heartbeat_at: AwareDatetime
    lease_until: AwareDatetime

    @model_validator(mode="after")
    def _times(self) -> JobLease:
        if self.heartbeat_at < self.acquired_at:
            raise ValueError("heartbeat before acquisition")
        if self.lease_until <= self.heartbeat_at:
            raise ValueError("lease must extend past the last heartbeat")
        return self


def commit_allowed(lease_epoch: int, current_epoch: int) -> bool:
    """ER-07: a commit is conditional on the epoch still being current. A stale worker is refused."""
    return lease_epoch == current_epoch


class Hole(DurablePart):
    """An offset that the watermark passed without a successful result (ER-09)."""

    offset: int = Field(ge=0)
    reason: FailureClass
    attempts: int = Field(default=0, ge=0)
    last_error: str | None = None
    waived: bool = False  # the owner may waive a hole; otherwise it stays visible until filled


class Progress(DurableModel):
    """Contiguous watermark plus an explicit list of holes.

    Every offset up to and including ``watermark`` was handled; the ``holes`` among them were not
    successfully derived and stay listed until filled (the hole is removed) or waived.
    """

    __kind__: ClassVar[str | None] = "progress"

    scope: OwnerScope
    job_kind: str = Field(min_length=1)
    watermark: int = Field(ge=-1)  # -1 = nothing handled yet
    target_offset: int | None = Field(
        default=None, ge=-1
    )  # immutable snapshot target of the running job (ER-26)
    holes: list[Hole] = Field(default_factory=list)

    @model_validator(mode="after")
    def _holes(self) -> Progress:
        offsets = [h.offset for h in self.holes]
        if len(set(offsets)) != len(offsets):
            raise ValueError("duplicate hole offsets")
        if offsets != sorted(offsets):
            raise ValueError("holes must be sorted by offset")
        if any(o > self.watermark for o in offsets):
            raise ValueError("a hole cannot lie beyond the watermark")
        if self.target_offset is not None and self.watermark > self.target_offset:
            raise ValueError("watermark is past the job's target offset")
        return self

    @property
    def open_holes(self) -> list[Hole]:
        return [h for h in self.holes if not h.waived]

    @property
    def complete(self) -> bool:
        """No unwaived holes remain."""
        return not self.open_holes
