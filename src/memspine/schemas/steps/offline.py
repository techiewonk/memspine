"""Internal step types for the offline pipeline (``docs/v2/pipelines/offline.md``), steps 1-22.

Step numbers are the spec heading numbers (``### 4. S1 Retention`` is ``OfflineStep04``; the sleep-stage
number S1..S18 is in each docstring). Internal tier: no compatibility promise.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from memspine.schemas.base import InternalModel
from memspine.schemas.durable import (
    AssertionEnvelope,
    AssertionKey,
    JobLease,
    MemoryEvent,
    OutboxRow,
    Progress,
)
from memspine.schemas.steps.shared import (
    DerivationBudget,
    JobKind,
    Layer,
    ProfileCard,
    ProfileOp,
    Record,
    StageReport,
    Turn,
)

StageName = Literal[
    "retention",
    "catch_up_extract",
    "reconcile_resolve",
    "contradictions",
    "near_duplicates",
    "value_histories",
    "observe_patterns",
    "session_catch_up",
    "entity_summaries",
    "communities",
    "profile",
    "observer_views",
    "standing_answers",
    "watches_cues",
    "procedures",
    "decay",
    "compress",
    "log_prune",
]
TriggerKind = Literal["commit", "idle_timer", "interval", "manual", "open", "backlog", "rederive"]
Tier = Literal["hot", "warm", "cold", "dormant"]


# --- types defined in the offline spec's shared section ------------------------------------------------


class NamespaceTriggerState(InternalModel):
    """Internal step type."""

    ns: str | None = None
    deposits_since_run: int
    last_deposit_at: datetime
    last_run_at: datetime | None = None
    last_reconcile_at: datetime | None = None
    deferred_units: int = 0


class CycleReport(InternalModel):
    """Internal step type."""

    job_id: str
    ns: str
    job_kind: JobKind
    manual: bool = False
    started_at: datetime
    ended_at: datetime
    stages: list[StageReport] = Field(default_factory=list)
    budget: DerivationBudget = Field(default_factory=DerivationBudget)
    deferred_total: int = 0
    freshness: dict[str, int] = Field(
        default_factory=dict
    )  # layer -> newer sources than covers_seq


# --- small stage types ---------------------------------------------------------------------------------


class TriggerInput(InternalModel):
    ns: str
    event: TriggerKind
    seq: int | None = None  # commit only


class TriggerDecision(InternalModel):
    fire: bool
    job_kind: Literal["full", "reconcile_only", "rederive", "none"]
    reason: str
    next_check_at: datetime | None = None


class JobRequest(InternalModel):
    ns: str
    job_kind: JobKind
    manual: bool = False
    requested_by: str


class DirtySet(InternalModel):
    events: list[MemoryEvent] = Field(default_factory=list)
    neighbours: list[str] = Field(default_factory=list)  # records one hop from a changed record


class SessionClosed(InternalModel):
    namespace: str
    session_id: str
    windows: int
    member_ids: list[str]
    members_fp: str
    reason: Literal["explicit", "new_session", "end_of_stream", "idle_fallback"]


class Call2Input(InternalModel):
    turns: list[Turn]
    fragments: list[Record] = Field(default_factory=list)
    previous_summary: Record | None = None
    card: ProfileCard | None = None


class Call2Summary(InternalModel):
    text: str
    folded: bool = False


class Call2Episode(InternalModel):
    text: str
    cites_windows: list[str] = Field(default_factory=list)
    when: str | None = None  # text; the compiler builds a durable TemporalRef (S6)


class Call2Result(InternalModel):
    """Wire (model output; compiled in code). Each section optional."""

    summary: Call2Summary | None = None
    episode: Call2Episode | None = None
    profile_ops: list[ProfileOp] | None = None


class RetentionClass(InternalModel):
    match: str  # TODO: match expression format is not specified
    ttl: str  # TODO: duration format is not specified (a durable Duration fits)
    applies_to_layers: list[Layer] = Field(default_factory=list)


class PendingMerge(InternalModel):
    """Outbox row kind=pending_merge (W-19 B)."""

    id: str
    ns: str
    name: str
    entity_type: str | None = None
    mention_record_id: str
    source_trust: float
    co_mentions: list[str] = Field(default_factory=list)
    evidence_line: str
    created_at: datetime


class ResolutionDecision(InternalModel):
    name: str
    target_entity_id: str | None = None
    method: Literal["exact", "alias", "minhash", "llm", "contested", "possible_same", "new"]
    candidate: str | None = None
    confidence: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    group: int | None = None


class ContradictionCandidate(InternalModel):
    new_fact_id: str
    old_fact_id: str
    source: Literal["same_key", "word_overlap", "predicate_family", "embedding"]


class ContradictionVerdict(InternalModel):
    pair: tuple[str, str]
    verdict: Literal["supersede", "contest", "coexist"]
    reason_code: str
    adjudicator: Literal["rules", "llm"]


class DedupPair(InternalModel):
    keep_id: str
    drop_id: str
    cosine: float
    guard: Literal["pass", "numbers_differ", "dates_differ", "negation_differs"]


class ValueSpan(InternalModel):
    value: Any
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    source_dates: list[datetime] = Field(default_factory=list)
    record_id: str


class ValueHistoryView(InternalModel):
    ns: str
    subject_id: str
    attribute: str
    values: list[ValueSpan] = Field(default_factory=list)
    current: str | None = None
    open_contests: list[str] = Field(default_factory=list)
    covers_seq: int | None = None


class PatternGroup(InternalModel):
    ns: str
    subject_id: str
    predicate_family: str
    member_ids: list[str]
    recurrence: int
    members_fp: str


class SessionGap(InternalModel):
    ns: str
    session_id: str
    members_fp: str
    missing_sections: list[Literal["summary", "episode", "profile_ops"]]
    reason: str


class EntityWork(InternalModel):
    node_id: str
    display_name: str
    member_ids: list[str]
    members_fp: str
    lines: list[str] = Field(default_factory=list)


class PartitionResult(InternalModel):
    ns: str
    anchors: dict[str, str] = Field(default_factory=dict)  # node -> anchor
    mode: Literal["full", "incremental"]
    collapsed: bool = False


class StandingQuestion(InternalModel):
    id: str
    ns: str
    text: str
    sections: list[str] = Field(default_factory=list)


class AnswerSection(InternalModel):
    text: str
    cites: list[str] = Field(default_factory=list)
    covers_seq: int | None = None


class StandingAnswer(InternalModel):
    """Record layer=L5."""

    question_id: str
    sections: list[AnswerSection] = Field(default_factory=list)


class WatchReport(InternalModel):
    due: int = 0
    invalidated: int = 0
    fired: list[str] = Field(default_factory=list)


class Cue(InternalModel):
    text: str
    target_turn_id: str
    trust: float | None = None  # <= target; firewall screened


class OutcomeReceipt(InternalModel):
    """Exists at ``memories/procedural/lessons.py:52``; spec lists no fields."""

    lesson_key: str | None = None  # TODO: spec lists no fields


class LessonCase(InternalModel):
    lesson_key: str
    steps: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    outcome: str | None = None
    stage: str = "ADVISORY"


class TierTransition(InternalModel):
    record_id: str
    from_tier: Tier
    to_tier: Tier
    reason: str = "idle"


class CompressResult(InternalModel):
    record_id: str
    content_zstd: bytes | None = None
    res: dict[str, Any] | None = None  # {gist, note, full_ptr}


# --- steps ---------------------------------------------------------------------------------------------


class OfflineStep01Input(InternalModel):
    """Trigger evaluation."""

    trigger: TriggerInput
    state: NamespaceTriggerState


class OfflineStep01Output(InternalModel):
    decision: TriggerDecision
    state: NamespaceTriggerState


class OfflineStep02Input(InternalModel):
    """Job claim: lease, watermarks, dirty set and budget."""

    request: JobRequest


class OfflineStep02Output(InternalModel):
    lease: JobLease
    watermarks: list[Progress] = Field(default_factory=list)
    dirty: dict[str, DirtySet] = Field(default_factory=dict)  # per stage
    budget: DerivationBudget = Field(default_factory=DerivationBudget)


class OfflineStep03Input(InternalModel):
    """Call 2 at session close."""

    session: SessionClosed
    call2: Call2Input


class OfflineStep03Output(InternalModel):
    result: Call2Result
    records: list[Record] = Field(default_factory=list)  # per-section deposits, independent
    rejected_sections: list[str] = Field(default_factory=list)  # call-2 gaps


class OfflineStep04Input(InternalModel):
    """S1 Retention."""

    classes: list[RetentionClass]
    now: datetime


class OfflineStep04Output(InternalModel):
    expired: list[str] = Field(default_factory=list)
    held: list[str] = Field(default_factory=list)  # legal hold
    rederive_rows: int = 0


class OfflineStep05Input(InternalModel):
    """S2 Catch-up extraction for underived sources."""

    sources: list[str]
    batch_tokens: int


class OfflineStep05Output(InternalModel):
    assertions: list[AssertionEnvelope] = Field(default_factory=list)
    pending_merges: list[PendingMerge] = Field(default_factory=list)
    still_underived: int = 0


class OfflineStep06Input(InternalModel):
    """S3 Reconcile backlog and resolve entities."""

    pending: list[PendingMerge]


class OfflineStep06Output(InternalModel):
    decisions: list[ResolutionDecision]


class OfflineStep07Input(InternalModel):
    """S4 Contradictions and invalidation."""

    candidates: list[ContradictionCandidate]


class OfflineStep07Output(InternalModel):
    verdicts: list[ContradictionVerdict]


class OfflineStep08Input(InternalModel):
    """S5 Near-duplicates."""

    pairs: list[DedupPair]


class OfflineStep08Output(InternalModel):
    merged: int = 0
    blocked_by_guard: int = 0
    reinforcement_updates: int = 0


class OfflineStep09Input(InternalModel):
    """S6 Value histories."""

    touched_keys: list[AssertionKey]  # keys touched since the watermark


class OfflineStep09Output(InternalModel):
    views: list[ValueHistoryView]


class OfflineStep10Input(InternalModel):
    """S7 Pattern observations (opt-in L4)."""

    groups: list[PatternGroup]


class OfflineStep10Output(InternalModel):
    observations: list[Record]  # layer=L4, type=reflective


class OfflineStep11Input(InternalModel):
    """S8 Session catch-up (call-2 fallback)."""

    gaps: list[SessionGap]


class OfflineStep11Output(InternalModel):
    records: list[Record] = Field(default_factory=list)  # as step 3, missing sections only
    passive_sessions: list[str] = Field(default_factory=list)


class OfflineStep12Input(InternalModel):
    """S9 Entity summaries."""

    work: list[EntityWork]


class OfflineStep12Output(InternalModel):
    summaries: list[Record]  # layer=L3, channel=entity_summary


class OfflineStep13Input(InternalModel):
    """S10 Communities."""

    ns: str
    mode: Literal["full", "incremental"] = "incremental"


class OfflineStep13Output(InternalModel):
    partition: PartitionResult
    parents: list[Record] = Field(default_factory=list)  # CommunityParent records, layer=L3


class OfflineStep14Input(InternalModel):
    """S11 Profile operations and reflection."""

    ops: list[ProfileOp] = Field(default_factory=list)
    card: ProfileCard | None = None


class OfflineStep14Output(InternalModel):
    card: ProfileCard
    reflections: list[Record] = Field(default_factory=list)
    rejected_ops: int = 0


class OfflineStep15Input(InternalModel):
    """S12 Observer views."""

    pairs: list[tuple[str, str]]  # (observer_id, subject_id)


class OfflineStep15Output(InternalModel):
    views: list[Record]  # ObserverView records, layer=L4


class OfflineStep16Input(InternalModel):
    """S13 Standing answers."""

    questions: list[StandingQuestion]


class OfflineStep16Output(InternalModel):
    answers: list[StandingAnswer]


class OfflineStep17Input(InternalModel):
    """S14 Watches and cues."""

    ns: str
    closed_sessions: list[str] = Field(default_factory=list)


class OfflineStep17Output(InternalModel):
    watch_report: WatchReport
    cues: list[Cue] = Field(default_factory=list)


class OfflineStep18Input(InternalModel):
    """S15 Procedures."""

    receipts: list[OutcomeReceipt]


class OfflineStep18Output(InternalModel):
    cases: list[LessonCase]


class OfflineStep19Input(InternalModel):
    """S16 Decay."""

    record_ids: list[str]  # not deleted, not quarantined, not pinned
    now: datetime
    decay_policy: dict[str, Any] = Field(
        default_factory=dict
    )  # per type; TODO: shape not specified


class OfflineStep19Output(InternalModel):
    transitions: list[TierTransition]


class OfflineStep20Input(InternalModel):
    """S17 Compression."""

    record_ids: list[str]  # tier=cold
    legal_holds: list[str] = Field(default_factory=list)


class OfflineStep20Output(InternalModel):
    results: list[CompressResult]


class OfflineStep21Input(InternalModel):
    """S18 Event-log prune."""

    mode: Literal["rolling"] = "rolling"
    retention_days: int
    watermarks: list[Progress] = Field(default_factory=list)
    pending_outbox_source_seqs: list[int] = Field(default_factory=list)


class OfflineStep21Output(InternalModel):
    pruned: int
    cutoff: datetime
    min_watermark_seq: int | None = None


class OfflineStep22Input(InternalModel):
    """Job finish: report, ledger, release."""

    job_id: str
    ns: str
    stage_reports: list[StageReport]
    budget: DerivationBudget = Field(default_factory=DerivationBudget)


class OfflineStep22Output(InternalModel):
    report: CycleReport
    trigger_state: NamespaceTriggerState
    released_rows: list[OutboxRow] = Field(
        default_factory=list
    )  # rows handed back: status pending, lease cleared, epoch kept
