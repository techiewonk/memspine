"""Universal memory record model (M1) — the one shape every memory type projects into.

Phase-0 DDL contract: every column here lands in the initial Alembic migration so
later phases never need a breaking schema change for:

- bi-temporal validity (M4 conflict ladder operates on these),
- provenance (`source`, D-42) and versioned lifecycle (`status`/`version`/`history`/
  `evolve_to`, D-42) — what makes conflict resolution and `audit taint` auditable,
- governance (`pii_tier`, `consent_tags`, M7),
- scoring state (M1: recency/relevance/importance + utility),
- Memory Firewall columns (`trust`/`quarantined`/`instruction_flag`, E1),
- two-stage dedup fields (`simhash`/`minhash_sig`, D-27).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from fastuuid import uuid4
from pydantic import BaseModel, ConfigDict, Field

from memspine.config.constants import TRUST_DEFAULT
from memspine.core.events import fingerprint_payload

__all__ = [
    "ArchivedVersion",
    "MemoryRecord",
    "PiiTier",
    "RecordStatus",
    "ScoringState",
    "SourceInfo",
    "new_record_id",
]


def new_record_id() -> str:
    """Hot-path record id (fastuuid, D-37)."""
    return str(uuid4())


class RecordStatus(StrEnum):
    """Versioned lifecycle (D-42, MemOS-derived)."""

    ACTIVATED = "activated"
    RESOLVING = "resolving"
    QUARANTINED = "quarantined"
    ARCHIVED = "archived"
    DELETED = "deleted"


class PiiTier(StrEnum):
    """Governance tier propagated from ingest to every projection (M7/M13.9)."""

    NONE = "none"
    LOW = "low"
    HIGH = "high"
    REGULATED = "regulated"


class SourceInfo(BaseModel):
    """First-class provenance object (D-42).

    ``prompt_version`` ties a record to the exact prompt that produced it (D-43),
    which is what lets `audit taint` (E1) trace poisoned writes to their origin.
    """

    role: str = "system"
    channel: str = "internal"
    doc_path: str | None = None
    message_id: str | None = None
    prompt_version: str | None = None


class ScoringState(BaseModel):
    """M1 scoring state; the utility modifier composes with the three base scores."""

    recency: float = 1.0
    relevance: float = 0.0
    importance: float = 0.0
    utility: float = 0.0
    access_count: int = 0
    last_accessed_at: datetime | None = None


class ArchivedVersion(BaseModel):
    """One superseded version kept in ``history`` (D-42)."""

    version: int
    content: str
    archived_at: datetime
    reason: str = ""


class MemoryRecord(BaseModel):
    """The universal record. Memory-type packages specialize behaviour, not shape."""

    # minhash_sig is binary; event payloads are JSON (D-38) — base64 both ways.
    model_config = ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")

    record_id: str = Field(default_factory=new_record_id)
    namespace: str
    memory_type: str
    content: str
    content_fingerprint: str = ""

    # Fact keying (M13.3): semantic records may carry an (entity, attribute)
    # key — the unit the conflict ladder (M4) operates on. None = unkeyed.
    entity: str | None = None
    attribute: str | None = None

    # Bi-temporal columns (M4): event time vs. record time.
    valid_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_to: datetime | None = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    superseded_at: datetime | None = None

    # Provenance + versioned lifecycle (D-42).
    source: SourceInfo = Field(default_factory=SourceInfo)
    status: RecordStatus = RecordStatus.ACTIVATED
    version: int = 1
    history: list[ArchivedVersion] = Field(default_factory=list)
    evolve_to: str | None = None

    # Governance (M7).
    pii_tier: PiiTier = PiiTier.NONE
    consent_tags: list[str] = Field(default_factory=list)

    # Scoring state (M1).
    scoring: ScoringState = Field(default_factory=ScoringState)

    # Memory Firewall columns (E1) — present from day one; ``corroborations``
    # counts independent trusted assertions that promote a quarantined record.
    trust: float = TRUST_DEFAULT
    quarantined: bool = False
    instruction_flag: bool = False
    corroborations: int = 0

    # Two-stage dedup fields (D-27): simhash for cheap distance, MinHash signature
    # bytes for LSH candidate generation; both computed at write time in M5.
    simhash: int | None = None
    minhash_sig: bytes | None = None

    # Decay lifecycle (M3): current tier, advanced by the decay sweep through
    # DECAY_TRANSITION events. Cold-tier compression (M6/D-32) moves ``content``
    # into ``content_zstd``; the fingerprint stays, so round-trips are checkable.
    tier: str = "hot"
    content_zstd: bytes | None = None

    def model_post_init(self, _context: Any) -> None:
        if not self.content_fingerprint:
            self.content_fingerprint = fingerprint_payload({"content": self.content})
