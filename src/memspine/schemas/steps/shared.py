# ruff: noqa: E501
"""Shared step types that are not decided yet.

Every model here is an internal-tier draft: the field layout is taken from the "Shared data types" sections
of ``docs/v2/pipelines/*.md`` and is pending the schema session named in each docstring (see
``docs/v2/SCHEMA_AGENDA.md``). Nothing here is a durable or public promise.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from memspine.schemas.base import InternalModel

# ---------------------------------------------------------------------------------------------------------
# Closed sets used across several models. Kept as plain Literals so an unknown value fails validation in
# the internal tier; the kind set for model output is open (S2-3) and is not modelled here.
# ---------------------------------------------------------------------------------------------------------
Layer = Literal["L0", "L1", "L2", "L3", "L4", "L5"]
SrcClass = Literal[
    "user_stated", "assistant_proposed", "user_confirmed", "tool", "document", "external", "derived"
]
Role = Literal["user", "assistant", "tool", "system", "operator", "document"]
DeriveUse = Literal["proposal", "evidence", "stated", "excluded"]
Mode = Literal["asserted", "planned", "hoped", "asked", "hedged"]
AssertionKind = Literal["state", "event", "fact"]
Family = Literal["dense", "lexical", "structured", "derived"]
Depth = Literal["fast", "standard", "deep"]
JobKind = Literal["full", "reconcile_only", "rederive"]


class Scope(InternalModel):
    """draft: field layout pending schema session 3 (identity)."""

    tenant: str | None = None
    user: str | None = None
    agent: str | None = None
    session: str | None = None
    principal: str | None = None  # TODO(session 3): handle-only in the retrieve spec
    purpose: str | None = None  # TODO(session 3)
    viewer: str | None = None  # TODO(session 10): viewer binding comes from the principal


class AssetRef(InternalModel):
    """draft: field layout pending schema session 4 (evidence)."""

    asset_id: str
    hash: str
    mime: str
    url_or_path: str


class Segment(InternalModel):
    """draft: field layout pending schema session 3 (identity)."""

    seg_index: int
    caller_id: str | None = None
    role: Role
    speaker: str | None = None
    addressee: str | None = None
    source_at: datetime | None = None  # TODO(session 6): never defaulted to now
    tz: str | None = None
    content: str
    reply_to: str | int | None = None
    visibility: Literal["private", "participants", "owner_shared"] | None = None
    purposes: list[str] | None = None
    assets: list[AssetRef] | None = None


class SourceEnvelope(InternalModel):
    """draft: field layout pending schema session 3 (identity)."""

    source_id: str
    kind: Literal["conversation", "document", "tool_log", "asset"]
    scope: Scope
    version: int | None = None
    tz: str | None = None
    participants: list[str] | None = None
    segments: list[Segment]


class Receipt(InternalModel):
    """draft: field layout pending schema session 7 (receipt and readiness)."""

    event_ids: list[str]
    record_ids: list[str]
    intent_id: str
    source_id: str
    source_version: int
    accepted_seq: int | None = None
    readiness_by_capability: dict[str, Any] = Field(
        default_factory=dict
    )  # TODO(session 7): illustrative
    retried: list[int] = Field(default_factory=list)
    quarantined: list[int] = Field(default_factory=list)


class IntentStatus(InternalModel):
    """draft: field layout pending schema session 7 (receipt and readiness)."""

    intent_id: str
    layers: dict[
        str, Literal["pending", "leased", "done", "failed", "underived", "skipped"]
    ]  # keys L0..L5
    failure_class: str | None = None
    covers_seq: int | None = None


class WaitResult(InternalModel):
    """draft: field layout pending schema session 7 (receipt and readiness)."""

    status: str  # TODO(session 7): spec gives only {status, waited_ms}
    waited_ms: int


class Fingerprint(InternalModel):
    """draft: field layout pending schema session 8 (jobs) / P-12 derivation fingerprint."""

    prompt_hash: str
    schema_v: str
    model_id: str
    quant: str
    decoding: str
    input_blocks: str
    runtime_libs: str


class FirewallVerdict(InternalModel):
    """draft: field layout pending schema session 5 (assessment)."""

    gate: Literal[1, 2, 3] | None = None
    trust: float
    instruction_flag: bool = False
    anomalous: bool = False
    quarantine: bool = False
    reasons: list[str] = Field(default_factory=list)
    signals_run: list[str] = Field(default_factory=list)


class LineageEdge(InternalModel):
    """draft: field layout pending schema session 4 (evidence and lineage).

    The insert spec lists roles evidence|context|version_of|supersedes|merge_member; the offline spec lists
    member|evidence|merged_from|cites. Both sets are accepted until session 4 settles one.
    """

    child: str
    parent: str
    parent_version: int | None = None
    role: Literal[
        "evidence",
        "context",
        "version_of",
        "supersedes",
        "merge_member",
        "member",
        "merged_from",
        "cites",
    ]
    span: str | dict[str, Any] | None = (
        None  # TODO(session 4): str (insert) vs {line,start,end} (offline)
    )
    derivation: Fingerprint | None = None


class EvidenceRef(InternalModel):
    """draft: field layout pending schema session 4 (evidence and lineage)."""

    source_id: str
    source_version: int
    line: int | None = None
    span: str | None = None


class EventInterval(InternalModel):
    """draft: field layout pending schema session 6 (time)."""

    start: datetime | str | None = None  # TODO(session 6): resolved date or coarse label
    end: datetime | str | None = None
    precision: Literal["day", "month", "year", "unknown"] = "unknown"


class ValidityWindow(InternalModel):
    """draft: field layout pending schema session 6 (time)."""

    from_: datetime | None = Field(default=None, alias="from")
    to: datetime | None = None


class AssertionKey(InternalModel):
    """draft: field layout pending schema session 3 (identity)."""

    scope: str
    src: str
    rel: str
    dst: str
    kind: str
    key_v: int = 1


class GroundingChecks(InternalModel):
    """draft: field layout pending schema session 5 (assessment)."""

    span: bool | None = None
    numbers: bool | None = None
    polarity: bool | None = None
    names: bool | None = None
    endpoints: bool | None = None


class IntegrationVerdict(InternalModel):
    """draft: field layout pending schema session 5 (assessment)."""

    op: Literal["append", "supersede", "merge", "coexist", "contest", "noop", "defer_kc"]
    rung: str | None = None
    hint: str | None = None


class ItemV1(InternalModel):
    """Model-output wire schema, compact keys (W-15 B, P-06).

    draft: field layout pending schema session 2 follow-up. Session 2 fixed the split (the model authors
    content only, with citations); the compact keys below are not frozen. The kind set is open (S2-3), so
    ``kind`` is a plain string here and the closed ``state|event|fact`` check belongs to the compiler.
    Reserved system names (``reserved.py``) in model output are rejected by the compiler, not here.
    """

    s: str | None = None
    r: str | None = None
    o: str | None = None
    text: str
    kind: str  # TODO(session 2): spec says state|event|fact; S2-3 opens the set
    mode: Mode
    neg: bool
    when: str | None = None
    until: str | None = None
    lines: list[int]
    span: str
    source: Literal["user_stated", "assistant_proposed", "user_confirmed", "tool"] | None = None


class AssertionEnvelope(InternalModel):
    """Stored assertion, compiled in code (W-15 C, M-06).

    draft: field layout pending schema session 2 follow-up and sessions 3-6. ``extras`` holds unknown
    model-authored fields (SESSIONS_01_02 condition 1); it is not indexed.
    """

    v: int = 1
    text: str
    value: Any = None  # TODO(session 3): typed literal or entity id
    evidence: list[EvidenceRef] = Field(default_factory=list)
    asserter: str | None = None
    subject: list[str] = Field(default_factory=list)
    spk: str | None = None
    mode: Mode | None = None
    polarity: bool | None = (
        None  # TODO(session 5): spec gives "polarity" without a type; ItemV1 has neg: bool
    )
    source_at: datetime | None = None
    event: EventInterval | None = None
    valid: ValidityWindow | None = None
    kind: AssertionKind | None = None
    occurrence_id: str | None = None
    key: AssertionKey | None = None
    key_aliases: list[str] = Field(default_factory=list)
    src: SrcClass | None = None
    tau: float | None = None
    parents: list[str] = Field(default_factory=list)
    grounding: Literal["grounded", "uncertain", "rejected"] | None = None
    checks: GroundingChecks | None = None
    prompt_hash: str | None = None
    graph: dict[str, Any] | None = None  # TODO(session 3): {edges:[(src,rel,dst)], nary:{roles}}
    verdict: IntegrationVerdict | None = None
    extras: dict[str, Any] = Field(default_factory=dict)


class Record(InternalModel):
    """One record shape for all layers (M-02..M-10).

    draft: field layout pending schema sessions 3-6; this will become a durable kind later.
    """

    record_id: str
    scope: Scope | None = None
    layer: Layer | None = None
    type: str | None = None  # TODO(session 3): one of nine memory types (M-03)
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


class MemoryEvent(InternalModel):
    """draft: field layout pending schema session 8 (jobs); becomes a durable kind."""

    event_id: str
    seq: int | None = None
    kind: str  # TODO(session 8): EventKind enum
    scope: Scope | None = None
    ts: datetime | None = None
    actor: str | None = None
    schema_version: int = 1
    payload: dict[str, Any] = Field(default_factory=dict)
    fingerprint: str | None = None


class OutboxRow(InternalModel):
    """draft: field layout pending schema session 8 (jobs and progress).

    Insert and offline specs differ (kinds, ``scope`` vs ``namespace``, ``source_ids`` vs ``members``); both
    spellings are accepted until session 8.
    """

    id: str
    scope: Scope | None = None
    namespace: str | None = None
    kind: Literal[
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
    source_id: str | None = None
    source_version: int | None = None
    source_ids: list[dict[str, Any]] = Field(default_factory=list)
    members: list[tuple[str, int]] = Field(default_factory=list)
    fingerprint: str | None = None
    generation: int | None = None
    status: Literal["pending", "leased", "done", "failed", "dead", "underived"] = "pending"
    attempts: int = 0
    lease_owner: str | None = None
    lease_until: datetime | None = None
    not_before: datetime | None = None
    last_error: str | None = None
    failure_class: str | None = None
    epoch: int | None = None  # TODO(session 8): ER-07 claim epoch; spec does not list it on the row


class WindowTurn(InternalModel):
    """draft: field layout pending schema session 8."""

    record_id: str
    source_version: int
    session: str | None = None
    source_at: datetime | None = None
    tokens: int
    vector: list[float] | None = None


class Window(InternalModel):
    """Segmentation window (W-11). draft: pending schema session 8."""

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
    """draft: field layout pending schema session 9 (overload and accounting)."""

    batch_id: str
    windows: list[Window]
    tokens_in: int
    prompt_hash: str | None = None
    created_at: datetime | None = None


class FailureRecord(InternalModel):
    """draft: field layout pending schema session 9 (overload and accounting)."""

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
    """draft: field layout pending schema session 9 (overload and accounting)."""

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
    cost_estimate: float | None = (
        None  # TODO(session 9): cost classes (generative, embedding, rerank, tool)
    )


class Turn(InternalModel):
    """A live conversation turn as read by sleep stages. draft: pending schema session 3."""

    record_id: str
    text: str
    speaker: str | None = None
    source_at: datetime | None = None
    session: str | None = None


class ProfileOp(InternalModel):
    """draft: field layout pending schema session 5 (profile operations, O-18)."""

    op: Literal["add", "update", "delete", "none"]
    entry_type: Literal["identity", "attribute", "relationship", "instruction"]
    key: str
    value: str
    observed_at: datetime | str
    stance: Literal["stated", "inferred"]
    cites: list[str] = Field(default_factory=list)


class CardEntry(InternalModel):
    """draft: pending schema session 5."""

    key: str
    type: str
    value: str
    cites: list[str] = Field(default_factory=list)
    stance: Literal["stated", "inferred"]
    since: datetime | str | None = None


class ProfileCard(InternalModel):
    """draft: pending schema session 5 (O-19, M-20)."""

    entries: list[CardEntry] = Field(default_factory=list)


class StageReport(InternalModel):
    """draft: field layout pending schema session 8 (jobs and progress)."""

    stage: str  # TODO(session 8): StageName enum, see offline.StageName
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
    """draft: field layout pending schema session 9 (overload and accounting)."""

    tokens_total: int | None = None  # None = unlimited
    spent: int = 0
    by_class: dict[str, int] = Field(
        default_factory=dict
    )  # reconcile, summarise, personalise, maintain


class ReadRequest(InternalModel):
    """draft: field layout pending schema session 10 (read and cache)."""

    query: str
    depth: Depth | None = None
    as_of_valid: datetime | None = None
    as_of_known: datetime | None = None
    viewer: str | None = None  # TODO(session 10): bound from the authenticated principal (ER-29)
    budget_tokens: int | None = None  # None -> 2,048
    reference_time: datetime | None = None
    lang: str | None = None


class QueryContract(InternalModel):
    """Typed query contract (R-03). draft: pending schema session 10."""

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
    """draft: pending schema session 10."""

    record_id: str
    score: float  # leg-local scale


class NamedLeg(InternalModel):
    """draft: pending schema session 10. ``list[LegHit]`` plus a name and family."""

    name: str
    family: Family
    hits: list[LegHit] = Field(default_factory=list)


class PoolEntry(InternalModel):
    """draft: pending schema session 10."""

    record_id: str
    fused_score: float
    family: str
    protected: bool = False
    lifted_from: str | None = None


class ScoredHit(InternalModel):
    """draft: pending schema session 10."""

    record_id: str
    rerank_raw: float | None = None
    score: float
    fused_score: float | None = None
    boosts: float = 1.0
    source_rank: int = 0


class NoteItem(InternalModel):
    """draft: pending schema session 10."""

    kind: Literal["fact", "value_history", "profile", "observation"]
    text: str
    cites: list[str] = Field(default_factory=list)
    record_id: str
    score: float = 0.0


class RenderedLine(InternalModel):
    """draft: pending schema session 10."""

    alias: str
    record_id: str
    speaker: str | None = None
    date: str | None = None
    modality: str | None = None
    wrapped: Literal["none", "untrusted", "instruction", "claim"] = "none"
    text: str


class ContextBlock(InternalModel):
    """draft: pending schema session 10."""

    kind: Literal["contract", "profile", "raw", "notes", "public"]
    lines: list[RenderedLine] = Field(default_factory=list)
    tokens: int = 0
    cap: int = 0


class SkipRecord(InternalModel):
    """draft: pending schema session 10. The spec names the type without fields."""

    record_id: str | None = None
    reason: str  # TODO(session 10): spec gives no field list


class EvidenceBundle(InternalModel):
    """draft: pending schema session 10."""

    blocks: list[ContextBlock] = Field(default_factory=list)
    tokens_used: int = 0
    skipped: list[SkipRecord] = Field(default_factory=list)
    exact_accounting: bool | None = None  # TODO(session 10): exact vs estimated flag (ER-24)


class ReadTrace(InternalModel):
    """draft: pending schema session 10. Typed form of today's forensics dict."""

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


class ReadResult(InternalModel):
    """draft: pending schema session 10."""

    depth: str | None = None
    blocks: list[ContextBlock] = Field(default_factory=list)
    aliases: dict[str, str] = Field(default_factory=dict)
    tokens_used: int = 0
    boundary_index: int | None = None
    abstained: bool = False
    reason: str | None = None
    contract: QueryContract | None = None
    trace: ReadTrace | None = None
