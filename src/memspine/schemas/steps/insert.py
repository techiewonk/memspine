# ruff: noqa: E501
"""Internal step types for the insert pipeline (``docs/v2/pipelines/insert.md``), steps 1-23.

Names follow ``Insert<NN>Step...`` -> ``InsertStep<NN>Input`` / ``InsertStep<NN>Output``. Field names come
from the Input/Output block of each step. Internal tier: no compatibility promise.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from memspine.schemas.base import InternalModel
from memspine.schemas.steps.shared import (
    AssertionEnvelope,
    AssertionKey,
    AssetRef,
    CallBatch,
    DeriveUse,
    FailureRecord,
    FirewallVerdict,
    IntegrationVerdict,
    IntentStatus,
    ItemV1,
    LedgerEntry,
    LineageEdge,
    OutboxRow,
    Receipt,
    Record,
    Role,
    Segment,
    SourceEnvelope,
    SrcClass,
    WaitResult,
    Window,
    WindowTurn,
)

# --- helper types local to the insert pipeline (internal drafts) ---------------------------------------


class SegmentIdentity(InternalModel):
    """Per-segment output of step 2. draft: pending schema session 3 (identity)."""

    seg_index: int
    idem_key: str
    verdict: Literal["new", "retry", "new_version"]
    source_version: int


class StampedSegment(InternalModel):
    """A segment after steps 3-4 (clock and axes). draft: pending schema sessions 3 and 6."""

    seg_index: int
    role: Role
    content: str
    source_at: datetime | None = None
    source_tz: str = "unknown"
    tz_unknown: bool = False
    recorded_at: datetime | None = None
    spk: str | None = None
    addr: str | None = None
    participants: list[str] = Field(default_factory=list)
    src_class: SrcClass | None = None
    base_trust: float | None = None


class NamespaceContext(InternalModel):
    """Firewall gate-1 context (step 6). draft: pending schema session 5."""

    nearest_neighbour_similarities: list[float] = Field(default_factory=list)
    recent_live_contents: list[str] = Field(default_factory=list)  # about 50
    query_history_zscore: float | None = None


class ExplicitKey(InternalModel):
    """Optional explicit key for routing (step 7). draft: pending schema session 3."""

    entity: str | None = None
    attribute: str | None = None
    skill_name: str | None = None
    plan_task: str | None = None


class RoutedRecord(InternalModel):
    """A routed record with its verdict (input of step 8). draft: pending schema session 7."""

    record: Record
    verdict: FirewallVerdict
    idem_key: str
    source_version: int
    seg_index: int | None = None


class BacklogState(InternalModel):
    """Step 12 backlog state. draft: pending schema session 9."""

    pending_windows: int = 0
    oldest_age_ms: int | None = None


class LedgerBudget(InternalModel):
    """Step 12 ledger budget. draft: pending schema session 9."""

    tokens_total: int | None = None
    spent: int = 0


class LiveMember(InternalModel):
    """A re-read member record for prompt assembly (step 13). draft: pending schema session 4."""

    record_id: str
    source_version: int
    status: str | None = None
    quarantined: bool = False
    instruction_flag: bool = False
    text: str | None = None


class AliasTarget(InternalModel):
    """Alias map value ``n -> (record_id, version)``."""

    record_id: str
    version: int


class RenderedPrompt(InternalModel):
    """Step 13 output. draft: pending schema session 4."""

    system: str  # byte-stable
    user_blocks: list[str] = Field(default_factory=list)  # [w1..wk] of "[n] [date] Speaker: text"
    context_block: str | None = None
    known_line: str | None = None
    prompt_hash: str
    alias_map: dict[str, AliasTarget] = Field(
        default_factory=dict
    )  # alias n -> (record_id, version)
    input_blocks_hash: str | None = None


class DecodingParams(InternalModel):
    """Decoding settings for the extraction call (step 14)."""

    temperature: float = 0.0
    thinking: bool = False
    grammar: bool = True
    max_tokens: int | None = None


class Usage(InternalModel):
    tokens_in: int = 0
    tokens_out: int = 0


class ValidItem(InternalModel):
    """A valid ItemV1 with its batch block (step 15 output)."""

    item: ItemV1
    block: int | None = None  # TODO(session 8): spec says "their batch block", type not given


class ParentState(InternalModel):
    """Parent record live state for gate 3 (step 18). draft: pending schema session 5."""

    record_id: str
    status: str | None = None
    trust: float | None = None


class EntityMention(InternalModel):
    """A name to resolve (step 19 input). draft: pending schema session 3."""

    name: str
    type_hint: str | None = None
    co_mentioned: list[str] = Field(default_factory=list)


class Resolution(InternalModel):
    """Step 19 output. draft: pending schema session 3."""

    name: str
    entity_id: str | None = None
    method: Literal["exact", "alias", "minhash", "new", "pending_merge", "contested"]
    candidate: str | None = None


class RegistryEntry(InternalModel):
    """Cardinality registry entry (step 21). draft: pending schema session 5."""

    cardinality: Literal["one", "many"]
    functional: bool = False
    exclusive: bool = False


class Incumbent(InternalModel):
    """An existing assertion on the key (step 21). draft: pending schema session 5."""

    assertion_id: str
    record_id: str | None = None
    trust: float | None = None


class AttachDecision(InternalModel):
    """``new | support_of(<assertion id>)`` (step 20)."""

    mode: Literal["new", "support_of"]
    assertion_id: str | None = None


# --- steps ---------------------------------------------------------------------------------------------


class InsertStep01Input(InternalModel):
    """Intake. Adapter arguments (E2..E7) or a SourceEnvelope (E1)."""

    adapter_args: dict[str, Any] | None = None  # TODO(session 7): per-adapter argument shapes
    envelope: SourceEnvelope | None = None


class InsertStep01Output(InternalModel):
    envelope: SourceEnvelope


class InsertStep02Input(InternalModel):
    envelope: SourceEnvelope


class InsertStep02Output(InternalModel):
    segments: list[SegmentIdentity]


class InsertStep03Input(InternalModel):
    segment_source_at: datetime | None = None
    segment_tz: str | None = None
    envelope_tz: str | None = None


class InsertStep03Output(InternalModel):
    source_at: datetime | None = None
    source_tz: str  # segment tz, else envelope tz, else "unknown"
    tz_unknown: bool
    recorded_at: datetime
    seq_tiebreak: int


class InsertStep04Input(InternalModel):
    role: Role
    speaker: str | None = None
    addressee: str | None = None
    participants: list[str] | None = None


class InsertStep04Output(InternalModel):
    spk: str | None = None
    addr: str | None = None
    participants: list[str] = Field(default_factory=list)
    participant_tags: list[str] = Field(default_factory=list)  # participant:<name>
    src_class: Literal["user_stated", "assistant_proposed", "tool", "document", "external"]
    base_trust: float


class InsertStep05Input(InternalModel):
    segment: StampedSegment


class InsertStep05Output(InternalModel):
    derive_use: DeriveUse
    echo: bool


class InsertStep06Input(InternalModel):
    record: Record
    context: NamespaceContext = Field(default_factory=NamespaceContext)


class InsertStep06Output(InternalModel):
    verdict: FirewallVerdict
    record: Record  # redacted content, pii_tier, sensitivity tags (when on)
    sensitivity_tags: list[str] = Field(default_factory=list)


class InsertStep07Input(InternalModel):
    envelope_kind: Literal["conversation", "document", "tool_log", "asset"]
    segment: Segment
    explicit_key: ExplicitKey | None = None


class InsertStep07Output(InternalModel):
    route: Literal["raw_episodic", "raw_resource", "keyed_fact", "keyed_procedural"]
    derive: bool


class InsertStep08Input(InternalModel):
    routed: list[RoutedRecord]


class InsertStep08Output(InternalModel):
    """``Receipt`` field names are proposed only (spec: "exact schema to be settled separately")."""

    receipt: Receipt


class InsertStep09Input(InternalModel):
    row: OutboxRow  # kind: projection


class InsertStep09Output(InternalModel):
    applied: bool
    status: Literal["done", "failed", "dead", "pending"]


class InsertStep10Input(InternalModel):
    asset: AssetRef
    prompt_hash: str
    model_id: str


class InsertStep10Output(InternalModel):
    record: Record  # L0 resource record (caption), src_class derived
    parents: list[str]  # [the turn]
    provenance: dict[str, str]  # {asset hash, model, prompt}


class InsertStep11Input(InternalModel):
    turns: list[WindowTurn]


class InsertStep11Output(InternalModel):
    windows: list[Window]


class InsertStep12Input(InternalModel):
    windows: list[Window]
    backlog: BacklogState = Field(default_factory=BacklogState)
    budget: LedgerBudget = Field(default_factory=LedgerBudget)


class InsertStep12Output(InternalModel):
    batch: CallBatch | None = None
    underived: bool = False  # underived marker on the intent row


class InsertStep13Input(InternalModel):
    batch: CallBatch
    live_members: list[LiveMember] = Field(default_factory=list)
    context_lines: list[str] = Field(default_factory=list)  # up to N CONTEXT lines
    known_names: list[str] = Field(default_factory=list)


class InsertStep13Output(InternalModel):
    prompt: RenderedPrompt
    firewall_verdict: FirewallVerdict | None = None  # gate 2


class InsertStep14Input(InternalModel):
    prompt: RenderedPrompt
    json_schema: dict[str, Any]  # JSON schema of [ItemV1], compact keys
    decoding: DecodingParams = Field(default_factory=DecodingParams)


class InsertStep14Output(InternalModel):
    raw_reply: str
    finish_reason: str
    usage: Usage
    latency_ms: float | None = None
    cache_state: str | None = None


class InsertStep15Input(InternalModel):
    raw_reply: str
    alias_map: dict[str, AliasTarget]
    item_schema: dict[str, Any] | None = (
        None  # TODO(session 8): "ItemV1 schema"; may become a class ref
    )


class InsertStep15Output(InternalModel):
    items: list[ValidItem] = Field(default_factory=list)
    failures: list[FailureRecord] = Field(default_factory=list)
    rescheduled: list[Window] = Field(default_factory=list)  # reason "truncation"


class InsertStep16Input(InternalModel):
    item: ItemV1
    alias_map: dict[str, AliasTarget]
    cited_records: list[Record]  # spk, source_at, source_tz, src_class
    prompt_hash: str


class InsertStep16Output(InternalModel):
    envelope: AssertionEnvelope


class InsertStep17Input(InternalModel):
    envelope: AssertionEnvelope
    cited_lines: list[str] = Field(default_factory=list)
    context_lines: list[str] = Field(
        default_factory=list
    )  # reference-binding evidence only (ER-11)
    replied_assistant_turn: str | None = None  # for assent


class InsertStep17Output(InternalModel):
    grounding: Literal["grounded", "uncertain", "rejected"]
    checks: dict[str, bool | None] = Field(
        default_factory=dict
    )  # span, numbers, polarity, names, endpoint_spans
    src: SrcClass | None = None  # possibly promoted or downgraded
    adjudicated: bool = False


class InsertStep18Input(InternalModel):
    envelope: AssertionEnvelope
    parents: list[ParentState] = Field(default_factory=list)


class InsertStep18Output(InternalModel):
    verdict: FirewallVerdict  # gate 3
    tau: float


class InsertStep19Input(InternalModel):
    mentions: list[EntityMention]
    source_trust: float | None = None


class InsertStep19Output(InternalModel):
    resolutions: list[Resolution]


class InsertStep20Input(InternalModel):
    envelope: AssertionEnvelope
    resolutions: list[Resolution]


class InsertStep20Output(InternalModel):
    key: AssertionKey
    occurrence_id: str | None = None  # events only
    attach: AttachDecision


class InsertStep21Input(InternalModel):
    envelope: AssertionEnvelope
    incumbents: list[Incumbent] = Field(default_factory=list)
    registry_entry: RegistryEntry | None = None


class InsertStep21Output(InternalModel):
    verdict: IntegrationVerdict


class DeposableEnvelope(InternalModel):
    """An envelope with its verdict, key and resolutions, as step 22 consumes it."""

    envelope: AssertionEnvelope
    verdict: IntegrationVerdict
    key: AssertionKey
    resolutions: list[Resolution] = Field(default_factory=list)


class InsertStep22Input(InternalModel):
    envelopes: list[DeposableEnvelope]
    outbox_row: OutboxRow  # lease, generation
    claim_epoch: int | None = None  # TODO(session 8): ER-07 conditional commit


class InsertStep22Output(InternalModel):
    records: list[Record]  # L1 records
    lineage: list[LineageEdge]
    events: list[
        dict[str, Any]
    ]  # CONFLICT / MERGE events with rung names; TODO(session 8): MemoryEvent
    projection_rows: list[OutboxRow] = Field(default_factory=list)
    intent_status: IntentStatus | None = None
    outbox_status: Literal["done", "failed", "dead", "pending"] = "done"


class CallUsage(InternalModel):
    tokens_in: int = 0
    tokens_out: int = 0
    retries: int = 0
    latency_ms: float | None = None


class OutcomeCounters(InternalModel):
    items_grounded: int = 0
    items_uncertain: int = 0
    items_rejected: int = 0


class InsertStep23Input(InternalModel):
    call_usage: CallUsage
    counters: OutcomeCounters = Field(default_factory=OutcomeCounters)
    intent_id: str
    wait_timeout_ms: int | None = None  # for the read barrier wait_for_pending(timeout)


class InsertStep23Output(InternalModel):
    ledger_entry: LedgerEntry
    status: IntentStatus
    wait: WaitResult | None = None
