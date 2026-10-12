# ruff: noqa: E501
"""Shared internal-tier step types.

The session-decided shared types were promoted out of this module (MOD-01 part 2): the envelope, evidence,
lineage, event, outbox and time types live in :mod:`memspine.schemas.durable`; the receipt, read and cost
types live in :mod:`memspine.schemas.public`. What remains here is step-to-step plumbing with no
compatibility promise; the layout comes from the "Shared data types" sections of
``docs/v2/pipelines/*.md``. A TODO marks only what no session specified.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from memspine.schemas.base import InternalModel
from memspine.schemas.durable import AssertionEnvelope, Fingerprint
from memspine.schemas.durable.common import SrcClass
from memspine.schemas.public import ContextBlock, EvidenceBundle

# ---------------------------------------------------------------------------------------------------------
# Closed sets used across several models. Kept as plain Literals so an unknown value fails validation in
# the internal tier; the kind set for model output is open (S2-3) and is not modelled here.
# ---------------------------------------------------------------------------------------------------------
Layer = Literal["L0", "L1", "L2", "L3", "L4", "L5"]
Role = Literal["user", "assistant", "tool", "system", "operator", "document"]
DeriveUse = Literal["proposal", "evidence", "stated", "excluded"]
Mode = Literal["asserted", "planned", "hoped", "asked", "hedged"]
Family = Literal["dense", "lexical", "structured", "derived"]
Depth = Literal["fast", "standard", "deep"]
JobKind = Literal["full", "reconcile_only", "rederive"]


class Scope(InternalModel):
    """Handle-side scope as read by the retrieve steps (tenant, user, agent, session, principal).

    Durable records use :class:`memspine.schemas.durable.OwnerScope` (tenant plus owner, S3-1); this is the
    caller-facing handle shape the read path resolves into one.
    """

    tenant: str | None = None
    user: str | None = None
    agent: str | None = None
    session: str | None = None
    principal: str | None = None
    purpose: str | None = None  # TODO: purpose vocabulary is not specified by any session
    viewer: str | None = None  # bound to the principal (S10); see public.ReadRequest


class AssetRef(InternalModel):
    """Internal step type."""

    asset_id: str
    hash: str
    mime: str
    url_or_path: str


class Segment(InternalModel):
    """Internal step type."""

    seg_index: int
    caller_id: str | None = None
    role: Role
    speaker: str | None = None
    addressee: str | None = None
    source_at: datetime | None = None  # never defaulted to now (S6-2)
    tz: str | None = None
    content: str
    reply_to: str | int | None = None
    visibility: Literal["private", "participants", "owner_shared"] | None = None
    purposes: list[str] | None = None
    assets: list[AssetRef] | None = None


class SourceEnvelope(InternalModel):
    """Internal step type."""

    source_id: str
    kind: Literal["conversation", "document", "tool_log", "asset"]
    scope: Scope
    version: int | None = None
    tz: str | None = None
    participants: list[str] | None = None
    segments: list[Segment]


class IntentStatus(InternalModel):
    """Internal step type."""

    intent_id: str
    layers: dict[
        str, Literal["pending", "leased", "done", "failed", "underived", "skipped"]
    ]  # keys L0..L5
    failure_class: str | None = None
    covers_seq: int | None = None


class FirewallVerdict(InternalModel):
    """Internal step type."""

    gate: Literal[1, 2, 3] | None = None
    trust: float
    instruction_flag: bool = False
    anomalous: bool = False
    quarantine: bool = False
    reasons: list[str] = Field(default_factory=list)
    signals_run: list[str] = Field(default_factory=list)


class ItemV1(InternalModel):
    """Model-output wire schema, compact keys (W-15 B, P-06).

    Session 2 fixed the split (the model authors content only, with citations); the compact keys below are
    not frozen. The compiler maps ``mode`` and ``neg`` onto the session-5 ``modality`` (asserted, negated,
    hypothetical, planned, reported, question). The kind set is open (S2-3), so
    ``kind`` is a plain string here and the closed ``state|event|fact`` check belongs to the compiler.
    Reserved system names (``reserved.py``) in model output are rejected by the compiler, not here.
    """

    s: str | None = None
    r: str | None = None
    o: str | None = None
    text: str
    kind: str  # open set (S2-3); the registry normalises it
    mode: Mode
    neg: bool
    when: str | None = None
    until: str | None = None
    lines: list[int]
    span: str
    source: Literal["user_stated", "assistant_proposed", "user_confirmed", "tool"] | None = None


class Record(InternalModel):
    """One record shape for all layers (M-02..M-10).

    Internal for now: the record envelope (:class:`~memspine.schemas.durable.AssertionEnvelope`) is durable,
    the row around it becomes a durable kind with the kind registry (MOD-02).
    """

    record_id: str
    scope: Scope | None = None
    layer: Layer | None = None
    type: str | None = (
        None  # TODO: one of nine memory types (M-03); the kind registry (MOD-02) owns the set
    )
    content: str
    content_fingerprint: str | None = None
    source_id: str | None = None
    source_version: int | None = None
    seg_index: int | None = None
    idem_key: str | None = None
    source_at: datetime | None = None
    source_tz: str | None = None
    tz_unknown: bool = False
    recorded_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    superseded_at: datetime | None = None
    invalid_at: datetime | None = None
    spk: str | None = None
    addr: str | None = None
    participants: list[str] = Field(default_factory=list)
    sub: list[str] = Field(default_factory=list)
    ask: str | None = None
    derive_use: DeriveUse | None = None
    echo: bool = False
    src_class: SrcClass | None = None
    trust: float | None = None
    quarantined: bool = False
    instruction_flag: bool = False
    support_count: int | None = None
    derivation: Fingerprint | None = None
    covers_seq: int | None = None
    envelope: AssertionEnvelope | None = None
    status: str | None = None
    version: int | None = None
    pii_tier: str | None = None
    consent_tags: list[str] = Field(default_factory=list)
    generation: int | None = None


class WindowTurn(InternalModel):
    """Internal step type."""

    record_id: str
    source_version: int
    session: str | None = None
    source_at: datetime | None = None
    tokens: int
    vector: list[float] | None = None


class Window(InternalModel):
    """Segmentation window (W-11). Internal step type."""

    window_id: str | None = None
    turns: list[WindowTurn] = Field(default_factory=list)
    reason: Literal[
        "topic",
        "turns",
        "tokens",
        "time_gap",
        "calendar_day",
        "session_end",
        "idle",
        "flush",
        "truncation",
    ]
    session: str | None = None
    source_versions: list[int] = Field(default_factory=list)
    tokens: int | None = None


class CallBatch(InternalModel):
    """Internal step type."""

    batch_id: str
    windows: list[Window]
    tokens_in: int
    prompt_hash: str | None = None
    created_at: datetime | None = None


class FailureRecord(InternalModel):
    """Internal step type."""

    row_id: str
    batch_id: str
    item_index: int | None = None
    failure_class: Literal[
        "raised",
        "malformed",
        "valid_empty",
        "truncated",
        "size_skip",
        "partial_commit",
        "invented_alias",
    ]
    attempts: int = 0
    retry_allowed: bool = False
    validator_errors: list[str] = Field(default_factory=list)


class LedgerEntry(InternalModel):
    """Internal step type."""

    call_id: str
    scope: Scope | None = None
    layer: Layer | None = None
    tier: Literal["worker", "reader"] | None = None
    role: str | None = None
    prompt_hash: str | None = None
    tokenizer: str | None = None
    template: str | None = None
    cache_state: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    retries: int = 0
    queue_delay_ms: int | None = None
    items_grounded: int = 0
    items_uncertain: int = 0
    items_rejected: int = 0
    cost_estimate: float | None = None  # per-class detail is recorded as public.CostRecord (ER-39)


class Turn(InternalModel):
    """A live conversation turn as read by sleep stages. Internal step type."""

    record_id: str
    text: str
    speaker: str | None = None
    source_at: datetime | None = None
    session: str | None = None


class ProfileOp(InternalModel):
    """Internal step type."""

    op: Literal["add", "update", "delete", "none"]
    entry_type: Literal["identity", "attribute", "relationship", "instruction"]
    key: str
    value: str
    observed_at: datetime | str
    stance: Literal["stated", "inferred"]
    cites: list[str] = Field(default_factory=list)


class CardEntry(InternalModel):
    """Internal step type."""

    key: str
    type: str
    value: str
    cites: list[str] = Field(default_factory=list)
    stance: Literal["stated", "inferred"]
    since: datetime | str | None = None


class ProfileCard(InternalModel):
    """Internal step type."""

    entries: list[CardEntry] = Field(default_factory=list)


class StageReport(InternalModel):
    """Internal step type."""

    stage: str  # a StageName value, see offline.StageName
    status: Literal["ok", "partial", "skipped", "error", "noop"]
    reason: str | None = None
    errors: list[str] = Field(default_factory=list)
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    retries: int = 0
    produced: int = 0
    deferred: int = 0
    guard_rejections: int = 0
    p50_ms: float | None = None
    p95_ms: float | None = None
    watermark_from: int | None = None
    watermark_to: int | None = None


class DerivationBudget(InternalModel):
    """Internal step type."""

    tokens_total: int | None = None  # None = unlimited
    spent: int = 0
    by_class: dict[str, int] = Field(
        default_factory=dict
    )  # reconcile, summarise, personalise, maintain


class QueryContract(InternalModel):
    """Typed query contract (R-03). Internal step type."""

    subjects: list[str] = Field(default_factory=list)
    relation: str = ""
    answer_type: Literal[
        "person",
        "place",
        "date",
        "duration",
        "count",
        "quantity",
        "title",
        "name",
        "yesno",
        "choice",
        "reason",
        "description",
        "entity",
        "unknown",
    ] = "unknown"
    subtype: str = ""
    time_scope: str = ""
    cardinality: Literal["one", "many", "count"] = "one"
    request: Literal["recall", "inference", "recommendation"] = "recall"
    source: Literal["rules", "llm"] = "rules"


class LegHit(InternalModel):
    """Internal step type."""

    record_id: str
    score: float  # leg-local scale


class NamedLeg(InternalModel):
    """Internal step type. ``list[LegHit]`` plus a name and family."""

    name: str
    family: Family
    hits: list[LegHit] = Field(default_factory=list)


class PoolEntry(InternalModel):
    """Internal step type."""

    record_id: str
    fused_score: float
    family: str
    protected: bool = False
    lifted_from: str | None = None


class ScoredHit(InternalModel):
    """Internal step type."""

    record_id: str
    rerank_raw: float | None = None
    score: float
    fused_score: float | None = None
    boosts: float = 1.0
    source_rank: int = 0


class NoteItem(InternalModel):
    """Internal step type."""

    kind: Literal["fact", "value_history", "profile", "observation"]
    text: str
    cites: list[str] = Field(default_factory=list)
    record_id: str
    score: float = 0.0


class SkipRecord(InternalModel):
    """Internal step type. The spec names the type without fields."""

    record_id: str | None = None
    reason: str  # TODO: the retrieve spec names SkipRecord without a field list


class PackedEvidence(InternalModel):
    """Packer output (steps 16-17): bundles packed into blocks. The unit bundle is the public ``EvidenceBundle`` (session 10)."""

    bundles: list[EvidenceBundle] = Field(default_factory=list)

    blocks: list[ContextBlock] = Field(default_factory=list)
    tokens_used: int = 0
    skipped: list[SkipRecord] = Field(default_factory=list)
    accounting: Literal["exact", "estimated"] = "estimated"  # ER-24


class ReadTrace(InternalModel):
    """Internal step type. Typed form of today's forensics dict."""

    legs: dict[str, list[tuple[str, float]]] = Field(default_factory=dict)
    dropped_legs: list[dict[str, Any]] = Field(default_factory=list)
    fused: list[str] = Field(default_factory=list)
    pool: list[str] = Field(default_factory=list)
    protected: list[str] = Field(default_factory=list)
    rerank_scores: dict[str, float] = Field(default_factory=dict)
    gate: dict[str, Any] | None = None
    cuts: list[dict[str, str]] = Field(default_factory=list)  # [{id, reason}]
    windows: list[dict[str, Any]] = Field(default_factory=list)
    packer_skips: list[SkipRecord] = Field(default_factory=list)
    model_calls: int = 0
    latency_ms: dict[str, float] = Field(default_factory=dict)  # per stage
    degraded_from: str | None = None
