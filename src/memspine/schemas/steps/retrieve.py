# ruff: noqa: E501
"""Internal step types for the retrieve pipeline (``docs/v2/pipelines/retrieve.md``), steps 1-20.

Internal tier: no compatibility promise. Step 20 is cross-cutting (execution and caching); its types are
drafts pending schema session 10 (read and cache).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from memspine.schemas.base import InternalModel
from memspine.schemas.steps.shared import (
    ContextBlock,
    Depth,
    EvidenceBundle,
    Family,
    Layer,
    NamedLeg,
    NoteItem,
    PoolEntry,
    QueryContract,
    ReadRequest,
    ReadResult,
    ReadTrace,
    Record,
    Scope,
    ScoredHit,
)

TimeView = Literal["current", "history", "as_of"]


# --- types defined in the retrieve spec ----------------------------------------------------------------


class TierConfig(InternalModel):
    """Config keys read.default_depth, read.allowed_depths, read.tiers. draft: pending session 10."""

    default_depth: Depth = "standard"
    allowed_depths: list[Depth] = Field(default_factory=lambda: ["fast", "standard", "deep"])
    tiers: dict[str, dict[str, Any]] = Field(
        default_factory=dict
    )  # TODO(session 10): per-tier knobs


class TierPolicy(InternalModel):
    depth: Depth
    pool_per_leg: int
    pool_size: int
    keep: int
    model_calls: int
    rerank_mode: Literal["always", "gated", "off"]
    allow_external: bool
    slo_p95_ms: int


class DateFilter(InternalModel):
    """Explicit valid/recorded bounds. draft: pending schema session 6 (time)."""

    valid_from: datetime | None = None
    valid_to: datetime | None = None
    recorded_from: datetime | None = None
    recorded_to: datetime | None = None


class ReadScope(InternalModel):
    namespace: str
    multi_party: bool
    viewer: str | None = None  # required when multi_party; bound from the principal (ER-29 C)
    viewer_rule: Literal["full", "viewer"]
    purpose: str | None = None
    as_of_valid: datetime | None = None
    as_of_known: datetime | None = None  # caller-set only
    date_filter: DateFilter | None = None
    sensitivity_bar: str | None = (
        None  # TODO(session 10): spec has Callable[[Record], bool]; not serialisable
    )


class LegSpec(InternalModel):
    name: str
    family: Family
    fetch_k: int
    timeout_ms: int
    reason: str


class LayerSet(InternalModel):
    """Which layers exist in this namespace (step 4 input)."""

    layers: list[Layer] = Field(default_factory=list)


class LegPlan(InternalModel):
    legs: list[LegSpec]
    probes: list[str] = Field(default_factory=list)
    list_mode: bool = False
    time_view: TimeView = "current"
    reasons: list[str] = Field(default_factory=list)


class FusedHit(InternalModel):
    record_id: str
    fused_score: float
    families: list[Family] = Field(default_factory=list)
    best_leg: str | None = None
    best_rank: int | None = None


class Candidate(InternalModel):
    record: Record
    fused_score: float


class Pool(InternalModel):
    entries: list[PoolEntry] = Field(default_factory=list)
    evicted: list[str] = Field(default_factory=list)


class RankedList(InternalModel):
    hits: list[ScoredHit] = Field(default_factory=list)


class StoreCalibration(InternalModel):
    p95: float
    sd: float
    leg: Literal["rerank", "vector"]
    n_probes: int
    store_size: int


class GateVerdict(InternalModel):
    passed: bool
    reason: str
    top: float
    threshold: float
    bypass: str | None = None


class NeighbourWindow(InternalModel):
    """The spec's retrieve-side ``Window``; renamed because insert.md defines a different ``Window``."""

    anchor_id: str
    member_ids: list[str]
    score: float
    tokens: int


class Sufficiency(InternalModel):
    enough: bool
    missing: str = ""


class PublicBlockInput(InternalModel):
    """Step 14 input helper: evidence texts handed to the privacy filter."""

    evidence_texts: list[str] = Field(default_factory=list)


class BlockCaps(InternalModel):
    raw: int
    notes: int
    profile: int
    public: int


class CacheEntryInfo(InternalModel):
    """Step 20 cache description. draft: pending schema session 10 (CacheKey)."""

    cache: str
    hit: bool
    key_fingerprint: str | None = None  # TODO(session 10): CacheKey fields (ER-31)


class LegResult(InternalModel):
    leg: NamedLeg
    timed_out: bool = False
    elapsed_ms: float | None = None


# --- steps ---------------------------------------------------------------------------------------------


class RetrieveStep01Input(InternalModel):
    """Resolve the tier."""

    request: ReadRequest
    tier_config: TierConfig = Field(default_factory=TierConfig)


class RetrieveStep01Output(InternalModel):
    policy: TierPolicy


class RetrieveStep02Input(InternalModel):
    """Bind scope: namespace, viewer, purpose, time bounds."""

    request: ReadRequest
    handle_scope: Scope


class RetrieveStep02Output(InternalModel):
    scope: ReadScope


class RetrieveStep03Input(InternalModel):
    """Build the typed query contract."""

    query: str
    lang: str | None = None


class RetrieveStep03Output(InternalModel):
    contract: QueryContract


class RetrieveStep04Input(InternalModel):
    """Route: leg plan, list mode, time view, deep-tier rewrites."""

    contract: QueryContract
    policy: TierPolicy
    scope: ReadScope
    layers: LayerSet = Field(default_factory=LayerSet)


class RetrieveStep04Output(InternalModel):
    plan: LegPlan


class RetrieveStep05Input(InternalModel):
    """Embed the query texts: ``[query, *probes]``."""

    texts: list[str]
    embedder_id: str | None = None


class RetrieveStep05Output(InternalModel):
    vectors: list[list[float]]
    embedder_id: str | None = None


class RetrieveStep06Input(InternalModel):
    """Run the legs."""

    plan: LegPlan
    vectors: list[list[float]]
    scope: ReadScope


class RetrieveStep06Output(InternalModel):
    legs: list[NamedLeg]
    dropped_legs: list[dict[str, str]] = Field(default_factory=list)  # {name, reason}


class RetrieveStep07Input(InternalModel):
    """Fuse: within family, then RRF across families."""

    legs: list[NamedLeg]


class RetrieveStep07Output(InternalModel):
    fused: list[FusedHit]


class RetrieveStep08Input(InternalModel):
    """Admit candidates (record gates)."""

    fused: list[FusedHit]
    scope: ReadScope


class RetrieveStep08Output(InternalModel):
    candidates: list[Candidate]
    cuts: list[dict[str, str]] = Field(default_factory=list)  # {id, reason}, to the trace


class RetrieveStep09Input(InternalModel):
    """Protected pool, sized by tier."""

    candidates: list[Candidate]
    policy: TierPolicy


class RetrieveStep09Output(InternalModel):
    pool: Pool


class RetrieveStep10Input(InternalModel):
    """Lift derived hits to source turns."""

    pool: Pool
    scope: ReadScope | None = None  # parents pass the step 8 gates again


class RetrieveStep10Output(InternalModel):
    pool: Pool
    note_candidates: list[NoteItem] = Field(default_factory=list)


class RetrieveStep11Input(InternalModel):
    """Rerank the single hit, bounded boosts."""

    pool: Pool
    rerank_query: str  # query + contract hint
    policy: TierPolicy | None = None


class RetrieveStep11Output(InternalModel):
    ranked: RankedList


class RetrieveStep12Input(InternalModel):
    """Relevance gate."""

    ranked: RankedList
    calibration: StoreCalibration | None = None


class RetrieveStep12Output(InternalModel):
    verdict: GateVerdict


class RetrieveStep13Input(InternalModel):
    """Time view and as-of."""

    ranked: RankedList
    time_view: TimeView
    scope: ReadScope


class RetrieveStep13Output(InternalModel):
    ranked: RankedList
    notes: list[NoteItem] = Field(default_factory=list)  # kind="value_history"


class RetrieveStep14Input(InternalModel):
    """External public-knowledge block (opt-in)."""

    query: str
    evidence: PublicBlockInput = Field(default_factory=PublicBlockInput)


class RetrieveStep14Output(InternalModel):
    public_query: str | None = None  # privacy-filtered
    block: ContextBlock | None = None  # kind="public"


class RetrieveStep15Input(InternalModel):
    """Neighbour windows."""

    ranked: RankedList
    scope: ReadScope | None = None


class RetrieveStep15Output(InternalModel):
    windows: list[NeighbourWindow]


class RetrieveStep16Input(InternalModel):
    """Pack under the budget."""

    windows: list[NeighbourWindow]
    notes: list[NoteItem] = Field(default_factory=list)
    public_block: ContextBlock | None = None
    budget_tokens: int
    reply_reserve: int = 0
    block_caps: BlockCaps
    tokenizer: str | None = None  # reader tokenizer id


class RetrieveStep16Output(InternalModel):
    bundle: EvidenceBundle


class RetrieveStep17Input(InternalModel):
    """Render: typed sections, header, aliases, provenance, trust wrapping."""

    bundle: EvidenceBundle
    contract: QueryContract | None = None


class RetrieveStep17Output(InternalModel):
    blocks: list[ContextBlock]
    aliases: dict[str, str] = Field(default_factory=dict)


class RetrieveStep18Input(InternalModel):
    """Deep tier: sufficiency check and one rewrite."""

    rendered_context: str
    query: str


class RetrieveStep18Output(InternalModel):
    sufficiency: Sufficiency
    rerun: bool = False  # steps 6-17 rerun once with the rewrite as an extra probe leg


class RetrieveStep19Input(InternalModel):
    """Finish: trace, ledger, audit, return."""

    blocks: list[ContextBlock]
    aliases: dict[str, str] = Field(default_factory=dict)
    trace: ReadTrace
    contract: QueryContract | None = None
    depth: Depth | None = None
    read_id: str | None = None


class RetrieveStep19Output(InternalModel):
    result: ReadResult
    dropped_after_revalidation: list[str] = Field(default_factory=list)  # POL-1


class RetrieveStep20Input(InternalModel):
    """Execution, caching and latency budgets (cross-cutting)."""

    plan: LegPlan
    policy: TierPolicy | None = None


class RetrieveStep20Output(InternalModel):
    results: list[LegResult]
    caches: list[CacheEntryInfo] = Field(default_factory=list)
