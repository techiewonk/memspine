"""Shared lifecycle axes (RM-57) for commitments, work status and event lifecycle.

Two axes, selected 2026-10-22:

* ``status``: proposed, planned, scheduled, open, in_progress, blocked,
  postponed, closed, cancelled, superseded. Terminal: closed, cancelled,
  superseded.
* ``outcome``: done, missed, failed, declined. Required when status is closed,
  absent otherwise.

There is no predicate per status. Transitions are explicit-only and every
transition cites evidence; a first mention or an intention never closes an
item. Rescheduling is *not* a status: it is a new scheduled time plus a
``rescheduled_from`` link to the previous one (:class:`RescheduleLink`); the
earlier time is kept as history. Achievement of a goal is ``closed`` + ``done``.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

LifecycleStatus = Literal[
    "proposed",
    "planned",
    "scheduled",
    "open",
    "in_progress",
    "blocked",
    "postponed",
    "closed",
    "cancelled",
    "superseded",
]
LifecycleOutcome = Literal["done", "missed", "failed", "declined"]

LIFECYCLE_STATUSES: tuple[str, ...] = get_args(LifecycleStatus)
LIFECYCLE_OUTCOMES: tuple[str, ...] = get_args(LifecycleOutcome)
TERMINAL_STATUSES: frozenset[str] = frozenset({"closed", "cancelled", "superseded"})


class LifecycleState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: LifecycleStatus
    outcome: LifecycleOutcome | None = None
    evidence_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _outcome_axis(self) -> LifecycleState:
        if self.status == "closed" and self.outcome is None:
            raise ValueError("closed status requires an outcome")
        if self.status != "closed" and self.outcome is not None:
            raise ValueError("outcome is only allowed with closed status")
        if any(not item for item in self.evidence_ids):
            raise ValueError("evidence ids must be non-empty")
        return self

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def validate_transition(previous: LifecycleState | None, new: LifecycleState) -> LifecycleState:
    """Check an explicit transition; never infers or auto-advances a status."""
    if previous is None:
        return new
    if previous.terminal:
        raise ValueError(f"no transition from terminal status {previous.status}")
    if previous.status == new.status:
        raise ValueError("repeated status is not a transition")
    if set(new.evidence_ids) <= set(previous.evidence_ids):
        raise ValueError("transition requires new supporting evidence")
    return new


def is_achieved(state: LifecycleState) -> bool:
    """Goal achievement is exactly closed + done."""
    return state.status == "closed" and state.outcome == "done"


class RescheduleLink(BaseModel):
    """A new scheduled time linked to the previous one; not a lifecycle status."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    link_kind: Literal["rescheduled_from"] = "rescheduled_from"
    item_id: str = Field(min_length=1)
    previous_time_ref: str = Field(min_length=1)
    new_time_ref: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _distinct(self) -> RescheduleLink:
        if self.previous_time_ref == self.new_time_ref:
            raise ValueError("rescheduling requires a different time")
        return self
