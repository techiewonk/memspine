# ruff: noqa: E501
"""The event-log envelope (M-01, M-26). Payload shapes are per event kind and are not modelled here."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import AwareDatetime, Field

from memspine.schemas.base import DurableModel
from memspine.schemas.durable.common import OwnerScope, Provenance

__all__ = ["EVENT_KINDS", "MemoryEvent"]

# Vocabulary at the time of writing (today's EventKind values plus the planned MERGE/SPLIT/LEDGER kinds).
# The set is open: a newer release may write a kind this one does not know, and it round-trips.
EVENT_KINDS: tuple[str, ...] = (
    "memory.write",
    "memory.retrieve",
    "memory.consolidate",
    "memory.decay_transition",
    "memory.conflict",
    "memory.merge",
    "memory.split",
    "memory.link",
    "memory.forget",
    "memory.rebuild",
    "memory.expose",
    "memory.marker",
    "memory.read_audit",
    "memory.audit",
    "memory.feedback",
    "memory.session",
    "memory.ledger",
)


class MemoryEvent(DurableModel):
    """One immutable entry in the ``memory_events`` log.

    ``seq`` is assigned by the write door at append time (monotonic per store); ``None`` only before.
    The instance field ``kind`` is the *event* kind; the durable kind id of this model is ``memory_event``.
    """

    __kind__: ClassVar[str | None] = "memory_event"

    event_id: str = Field(min_length=1)
    seq: int | None = Field(default=None, ge=0)
    kind: str = Field(min_length=1)
    scope: OwnerScope
    provenance: Provenance | None = None
    ts: AwareDatetime
    actor: str = "system"
    payload: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str | None = None
