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

import threading
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from fastuuid import uuid4
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SerializerFunctionWrapHandler,
    model_serializer,
)

from memspine.config.constants import REFLECTION_DEPTH_CAP, TRUST_DEFAULT
from memspine.schemas.durable.temporal import TimeBasis, TimePrecision
from memspine.store.events import fingerprint_payload
from memspine.store.layers import (
    Layer,
    ProvenanceClass,
    derive_axes,
    derive_layer,
    derive_provenance_class,
)

__all__ = [
    "ArchivedVersion",
    "MemoryRecord",
    "PiiTier",
    "RecordAxes",
    "RecordFingerprint",
    "RecordStatus",
    "ScoringState",
    "SkillStage",
    "SourceInfo",
    "chrono_key",
    "new_record_id",
    "record_time",
]


_clock_lock = threading.Lock()
_last_record_time = datetime.min.replace(tzinfo=UTC)


def record_time() -> datetime:
    """#87: the default ``recorded_at``, strictly increasing within the process.

    Two records built in the same clock tick (a batched ``ingest_messages``) would
    otherwise share a record time, and their order would fall to the random
    ``record_id``. A tie is bumped by one microsecond, so record time follows
    construction order and identical inputs give identical orderings."""
    global _last_record_time
    now = datetime.now(UTC)
    with _clock_lock:
        if now <= _last_record_time:
            now = _last_record_time + timedelta(microseconds=1)
        _last_record_time = now
    return now


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


class SkillStage(StrEnum):
    """Procedural ladder (M13.4): draft→staged→verified→active→deprecated.

    Lives in core so ``MemoryRecord.skill_stage`` is enum-typed (an illegal
    stage string fails validation at the model boundary, not deep in a
    caller); the transition rules stay in ``memories/procedural/lifecycle``.
    """

    DRAFT = "draft"
    STAGED = "staged"
    VERIFIED = "verified"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    #: W17a/W17f (ADR-060): lessons and exemplars. Retrievable through their own
    #: verbs, never executable, never promotable to a plan.
    ADVISORY = "advisory"


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
    #: Source-message envelope for canonical conversation ingestion. ``message_id`` above remains
    #: the internal session provenance key used by existing derivation stages.
    message: dict[str, Any] | None = None
    prompt_version: str | None = None
    # MTI provenance (integrity.*, opt-in): the authenticated principal behind
    # the write, and the records the writer had in context when it produced
    # this content (``derived_from``). Both default empty, so records written
    # without them behave exactly as before.
    principal: str | None = None
    parents: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _omit_message(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if self.message is None:
            data.pop("message", None)
        return data


#: ScoringState fields added after v0.2, left out of a dump while at their default,
#: so records that never use them serialize (and fingerprint) exactly as before.
_SCORING_SPARSE: dict[str, object] = {"likes": 0, "dislikes": 0, "notes": 0, "passive": False}


class ScoringState(BaseModel):
    """M1 scoring state; the utility modifier composes with the three base scores.

    ``likes`` / ``dislikes`` / ``notes`` count user feedback (#54, ``Engine.feedback``)
    and feed the utility term through a bounded transform. ``passive`` marks a record
    of a session the session lifecycle (#53) archived: left out of default reads.
    All four are omitted from a dump while at their defaults.
    """

    recency: float = 1.0
    relevance: float = 0.0
    importance: float = 0.0
    utility: float = 0.0
    access_count: int = 0
    last_accessed_at: datetime | None = None
    likes: int = Field(default=0, ge=0)
    dislikes: int = Field(default=0, ge=0)
    notes: int = Field(default=0, ge=0)
    passive: bool = False

    @model_serializer(mode="wrap")
    def _omit_unused(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for key, default in _SCORING_SPARSE.items():
            if key in data and data[key] == default:
                del data[key]
        return data


class ArchivedVersion(BaseModel):
    """One superseded version kept in ``history`` (D-42)."""

    version: int
    content: str
    archived_at: datetime
    reason: str = ""


class RecordAxes(BaseModel):
    """Speaker / subject / asker axes (M-08), typed. Derived from the perspective tags
    (``spk:``, ``sub:``, ``ask:``, ``addr:``) when not given."""

    speaker: str | None = None
    subject: list[str] = Field(default_factory=list)
    addressee: str | None = None
    asker: str | None = None
    #: W2-02 (W-05, M-08 note): everyone present for the turn, normalised principal ids, speaker
    #: first. Empty = not stamped (the v1 ``participant:`` tags still carry it). Left out of a dump
    #: while empty, so a record that never set it serialises as before.
    participants: list[str] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _omit_empty_participants(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict) and not data.get("participants"):
            data.pop("participants", None)
        return data


class RecordFingerprint(BaseModel):
    """Content fingerprint plus derivation fingerprint (P-12): what produced the record.

    ``derivation`` is a digest of (prompt, model) where both are known; ``prompt_hash`` defaults to
    ``source.prompt_version``. ``model_id`` stays ``None`` until a deposit path records it."""

    content: str = ""
    derivation: str | None = None
    prompt_hash: str | None = None
    model_id: str | None = None
    #: O4-01b (O-06): the highest source event seq the derivation covered; ``None`` = unknown (a v1
    #: record, or ``offline.budget_v2`` off). Kept in this envelope blob, so no column is needed.
    covers_seq: int | None = None
    #: FU-GAPS (``write.envelope_v2``): the compiled ``AssertionEnvelope`` as JSON (time axes, evidence
    #: roles, verdict), kept in this blob like ``covers_seq`` so no column or migration is needed.
    #: ``None`` = the record was not written from an envelope. Read it with ``Engine.get_envelope``.
    envelope: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def _omit_unset(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict):
            if data.get("covers_seq") is None:
                data.pop("covers_seq", None)  # a record that never set it dumps as before
            if data.get("envelope") is None:
                data.pop("envelope", None)
        return data


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

    # Sub-scoping facet over the namespace (D2). ``group_id`` groups records
    # (a conversation, a document, a project) within a namespace; ``tags`` are
    # free-form labels. Both are orthogonal to namespace isolation — a filter,
    # never a security boundary — and default empty so unkeyed records behave
    # exactly as in v0.1.
    group_id: str | None = None
    tags: list[str] = Field(default_factory=list)

    # Bi-temporal columns (M4): event time vs. record time.
    valid_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_to: datetime | None = None
    recorded_at: datetime = Field(default_factory=record_time)
    superseded_at: datetime | None = None
    # #19 interval arithmetic (``conflict.interval_order``): when the fact stopped
    # being true in the world, set by the conflict ladder from the next statement
    # on its key. Distinct from ``valid_to``, which also closes zero-length
    # contenders and archived records the store keeps out of "current". None =
    # not known (every record written without interval_order); a None value is
    # left out of serialised payloads, so event logs stay byte-identical.
    invalid_at: datetime | None = None

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
    # Bounds are enforced at the type: an out-of-range trust (e.g. a config
    # override boosting an external channel past 1.0) must fail loudly, never
    # silently defeat the cap.
    trust: float = Field(default=TRUST_DEFAULT, ge=0.0, le=1.0)
    quarantined: bool = False
    instruction_flag: bool = False
    corroborations: int = Field(default=0, ge=0)

    # Two-stage dedup fields (D-27): simhash for cheap distance, MinHash signature
    # bytes for LSH candidate generation; both computed at write time in M5.
    simhash: int | None = None
    minhash_sig: bytes | None = None

    # Decay lifecycle (M3): current tier, advanced by the decay sweep through
    # DECAY_TRANSITION events. Cold-tier compression (M6/D-32) moves ``content``
    # into ``content_zstd``; the fingerprint stays, so round-trips are checkable.
    tier: str = "hot"
    content_zstd: bytes | None = None

    # Procedural lifecycle (M13.4): a skill/plan rides a draft→staged→verified→
    # active→deprecated ladder; None for non-procedural records. Enum-typed so
    # an illegal stage is a ValidationError at construction, never a ValueError
    # deep in a transition.
    skill_stage: SkillStage | None = None
    # Reflective depth (M13.7): 0 = raw memory, 1 = reflection, 2 = meta.
    # The cap is enforced at the type too — a hand-built depth-3 record must
    # fail loudly even if it never went through guards.reflection_depth_for.
    reflection_depth: int = Field(default=0, ge=0, le=REFLECTION_DEPTH_CAP)

    # Record envelope (F-02a; M-02/M-03/M-06/M-07/M-08/P-12). Additive and derived when absent: a
    # record built without them gets the values the mapping in ``store.layers`` gives, so no call
    # site changes. A dump omits a field while it equals its derivation (``_omit_derived``), except
    # ``layer`` (MF-05): that is always written, so a replay never re-derives it.
    schema_version: int = Field(default=1, ge=1)
    layer: Layer = "L0"
    type_tag: str = ""
    provenance_class: ProvenanceClass = "external"
    axes: RecordAxes = Field(default_factory=RecordAxes)
    fingerprint: RecordFingerprint = Field(default_factory=RecordFingerprint)

    # Tri-temporal (F-02b; M-10, S6). Optional and additive: ``valid_from`` / ``valid_to`` stay the
    # event interval, ``recorded_at`` the record axis; these say when it was SAID (``source_at``)
    # and how well the event interval is known. None = not known: a date is never invented. A dump
    # omits a field while it is None (``event_approximate`` while False), so records that
    # never set them serialise as before.
    source_at: datetime | None = None
    source_precision: TimePrecision | None = None
    event_precision: TimePrecision | None = None
    event_approximate: bool = False
    event_basis: TimeBasis | None = None
    #: W2-02 (W-04): the zone the source time was said in: an IANA name, a fixed offset such as
    #: ``+09:00``, or ``"unknown"`` (a naive time nobody gave a zone for: kept as wall clock, never
    #: shifted). None = not stamped.
    source_tz: str | None = None

    # A context block the engine built at read time (lead section, cards and
    # profile headers). Private: it is never serialised and no payload, tag or
    # field a caller controls can set it, so stored text cannot claim to be one.
    _engine_block: bool = PrivateAttr(default=False)

    def _derived_envelope(self) -> dict[str, Any]:
        """The envelope fields when nobody set them (the shared mapping in ``store.layers``)."""
        return {
            "layer": derive_layer(self.memory_type, self.source.channel),
            "type_tag": self.memory_type,
            "provenance_class": derive_provenance_class(
                self.source.role, self.source.channel, bool(self.source.parents)
            ),
            "axes": RecordAxes.model_validate(derive_axes(self.tags)),
            "fingerprint": RecordFingerprint(
                content=self.content_fingerprint, prompt_hash=self.source.prompt_version
            ),
        }

    @model_serializer(mode="wrap")
    def _omit_derived(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if not isinstance(data, dict):
            return data
        if self.invalid_at is None:
            data.pop("invalid_at", None)
        for key in ("source_at", "source_precision", "event_precision", "event_basis", "source_tz"):
            if data.get(key) is None:
                data.pop(key, None)
        if not data.get("event_approximate"):
            data.pop("event_approximate", None)
        # Envelope fields equal to their derivation are redundant: left out, so the payload (and the
        # event-log fingerprint) of a record that never customised them is what it was before F-02a.
        if data.get("schema_version") == 1:
            del data["schema_version"]
        for key, value in self._derived_envelope().items():
            if key == "layer":
                # MF-05: the layer is always written, so a replay never depends on what
                # ``derive_layer`` returns later.
                data["layer"] = self.layer
                continue
            if getattr(self, key) == value:
                data.pop(key, None)
        return data

    def model_post_init(self, _context: Any) -> None:
        if not self.content_fingerprint:
            self.content_fingerprint = fingerprint_payload({"content": self.content})
        given = self.model_fields_set
        for key, value in self._derived_envelope().items():
            if key not in given:
                # not setattr: keeps model_fields_set as the caller gave it
                self.__dict__[key] = value

    @property
    def is_engine_block(self) -> bool:
        """True only for a read-time block the engine built (see ``_engine_block``)."""
        return self._engine_block

    def as_engine_block(self) -> MemoryRecord:
        """A copy marked as an engine-built read-time block. Engine use only."""
        block = self.model_copy()
        block._engine_block = True
        return block


def chrono_key(record: MemoryRecord) -> tuple[datetime, datetime, str, str]:
    """#87: the deterministic chronological order of records.

    Event time first, then record time (strictly increasing in write order, see
    :func:`record_time`), then the content fingerprint. ``record_id`` comes last
    and only separates true duplicates: it is a random uuid4, so ordering by it
    before anything else made equal-time records (every turn of one session)
    come out in a different order on every run."""
    return (record.valid_from, record.recorded_at, record.content_fingerprint, record.record_id)
