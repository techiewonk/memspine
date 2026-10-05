"""Append-only ``memory_events`` model — the single source of truth (D0.1).

This module defines the event *shape* and its canonical serialization; persistence
lives in ``services/storage`` behind the single write door. Vector/graph/FTS/cache
stores are rebuildable projections of this log, never a second source of truth.

Storage-cost control (D-45) is a property of the *log*, not of the event: the same
``MemoryEvent`` flows to projectors in all three ``event_log.mode`` settings
(``full`` / ``rolling`` / ``ephemeral``); only its at-rest lifetime differs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import orjson
import xxhash
from fastuuid import uuid4
from pydantic import BaseModel, Field

__all__ = [
    "EventKind",
    "EventLogMode",
    "MemoryEvent",
    "canonical_payload",
    "fingerprint_payload",
    "new_event_id",
]


class EventLogMode(StrEnum):
    """At-rest lifetime of the ``memory_events`` log (D-45).

    - ``FULL``: append-only forever — full rebuild + audit (default).
    - ``ROLLING``: prune events every projector has applied once they age past
      the retention window; rebuild limited to the window.
    - ``EPHEMERAL``: events flow to projectors but are never persisted;
      rebuild and ``audit taint`` are unavailable (hard warning at startup).
    """

    FULL = "full"
    ROLLING = "rolling"
    EPHEMERAL = "ephemeral"


class EventKind(StrEnum):
    """M11 event vocabulary. One kind per state transition the log can record."""

    WRITE = "memory.write"
    RETRIEVE = "memory.retrieve"
    CONSOLIDATE = "memory.consolidate"
    DECAY_TRANSITION = "memory.decay_transition"
    CONFLICT = "memory.conflict"
    MERGE = "memory.merge"
    #: Associative link between two records (M13.6/ADR-015). Payload:
    #: ``{"src", "dst", "rel", "weight", "reason"}`` — new relational
    #: information rides the log like any other write; the graph store is
    #: only ever a projection of these events.
    LINK = "memory.link"
    FORGET = "memory.forget"
    REBUILD = "memory.rebuild"
    #: Reader-side exposure record (integrity.enabled only). Payload:
    #: ``{"reader_namespace", "record_ids"}`` — foreign records a grantee's
    #: ``shared_search`` returned. Appended in the READER's namespace so the
    #: grantor is never mutated; no projector materializes it (audit-only).
    EXPOSE = "memory.expose"
    #: Bookkeeping marker (audit-only, no projector materializes it). Payload:
    #: ``{"marker": ..., ...}``. ``stage_done`` / ``stage_cleared`` carry
    #: ``{"stage", "session_key"}`` and make the background stages idempotent per
    #: session even when a session yields nothing (it survives rebuild because it
    #: is in the log); ``recall_skipped`` traces a message the H21 filter dropped.
    MARKER = "memory.marker"
    #: #49 durable read audit (``audit.reads``, audit-only, no projector reads it).
    #: Payload: ``{"verb", "principal", "purpose", "record_ids", "at", "chain"}``.
    READ_AUDIT = "memory.read_audit"
    #: #49 action audit (``audit.actions``, audit-only): forget, correct, retention
    #: expiry and export with ``{"action", "actor", "reason", "record_ids", "at",
    #: "chain"}``. Both audit kinds share one hash chain (``chain.prev``/``chain.hash``).
    AUDIT = "memory.audit"
    #: #54 user feedback on one record. Payload: ``{"record_id", "signal"}``
    #: (``like`` / ``dislike`` / ``note``) plus ``"content"`` holding the note text
    #: when one was given (the key erasure scrubs, so a hard forget of the record
    #: also erases the note). The record projector keeps per-record counts.
    FEEDBACK = "memory.feedback"
    #: #53 session lifecycle. Payload: ``{"session_id", "state", "record_ids",
    #: "reason"}`` with ``state`` ``passive`` (idle past the configured horizon,
    #: left out of default reads) or ``active`` (reopened). The record projector
    #: sets ``scoring.passive`` on each listed record, so a rebuild replays it.
    SESSION = "memory.session"


def new_event_id() -> str:
    """Hot-path event id (fastuuid, D-37)."""
    return str(uuid4())


def canonical_payload(payload: dict[str, Any]) -> bytes:
    """Canonical orjson encoding (sorted keys) used for storage and fingerprints (D-38)."""
    return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)


def fingerprint_payload(payload: dict[str, Any]) -> str:
    """Stable xxh64 content fingerprint of a payload (D-37)."""
    return xxhash.xxh64_hexdigest(canonical_payload(payload))


class MemoryEvent(BaseModel):
    """One immutable entry in the ``memory_events`` log.

    ``seq`` is assigned by the write door at append time (monotonic per store);
    it is ``None`` only before the event has passed through the door.
    """

    event_id: str = Field(default_factory=new_event_id)
    seq: int | None = None
    kind: EventKind
    namespace: str
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor: str = "system"
    schema_version: int = 1
    payload: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str = ""

    def model_post_init(self, _context: Any) -> None:
        if not self.fingerprint:
            self.fingerprint = fingerprint_payload(self.payload)
