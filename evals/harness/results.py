"""Result schema for the memspine evals harness.

Two rules shape this module.

**The result file is self-contained.** Everything a judge needs — question, gold answers,
dataset-native metadata, the prediction, and the evidence that was retrieved — is stored
per item, so ``--score-only`` can re-judge a stored run with a different judge, model or
rubric without loading the dataset and without touching the engine. Re-judging is the
single largest cost saver in an LLM-judged evaluation, and it is what turns judge
sensitivity into a *result* rather than a caveat in the limitations section.

**Cost is not optional.** Every item carries ``(accuracy, tokens, latency)``. Token and
latency accounting is per loop stage (R/C/G/D/K), so the summary can price a governance
layer rather than assert that it is cheap. MAGMA reports no cost at all; inheriting that
omission would forfeit both memspine's stated discipline and the framework's cost-per-cycle
metric.

Per-category and per-memory-type breakdowns are **computed at write time and stored**, not
left to be derived later by whoever reads the file. T-Mem omits exactly this and we
criticise it for that; a breakdown that is nobody's job does not get reported.

Nothing in this module invents a number. Where a statistic is undefined — no items, no
judged items, no recorded turns — the field is ``None``, never ``0.0``.
"""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

import memspine

from .accounting import CostReport
from .accounting import percentile as _percentile
from .config import HARNESS_VERSION, HarnessError, RunConfig

__all__ = [
    "BUILD_STAGES",
    "QUERY_STAGES",
    "SCHEMA_VERSION",
    "BuildRecord",
    "CostPerCycle",
    "Environment",
    "ItemResult",
    "JudgeResult",
    "LexicalScores",
    "LoopStage",
    "MemoryTypeBreakdown",
    "RetrievedItem",
    "RunResults",
    "RunSummary",
    "SchemaVersionError",
    "ScoreBreakdown",
    "StageCost",
    "TokenCounts",
    "build_results",
    "stage_costs_from_report",
    "summarise",
    "utcnow",
]

#: Result-file schema version, ``major.minor.patch``.
#:
#: Bump **major** when an existing field changes meaning or disappears — readers refuse a
#: foreign major. Bump **minor** for additive, backward-compatible fields.
#:
#: ``1.1.0`` added ``RunSummary.resolved_system_config`` and switched the latency
#: percentiles from linear interpolation to the nearest-rank definition in
#: :mod:`evals.harness.accounting`, so a p95 is always an observed measurement and the
#: live ledger and the persisted summary cannot disagree about the same latencies.
#: Both are additive/behavioural, not breaking, and no run had been recorded.
SCHEMA_VERSION: Final[str] = "1.1.0"


class SchemaVersionError(HarnessError):
    """A result file was written by an incompatible schema major version."""


class LoopStage(StrEnum):
    """Loop stages, used as the key for all cost accounting.

    Retrieve/Compose/Generate/Deposit/Synthesise, per the framework's operator set. The
    judge is **not** a loop stage: it is the measuring instrument, its cost is tracked
    separately, and it never enters cost per cycle.
    """

    RETRIEVE = "retrieve"
    COMPOSE = "compose"
    GENERATE = "generate"
    DEPOSIT = "deposit"
    SYNTHESISE = "synthesise"


#: Stages incurred answering a question (the read half of the loop).
QUERY_STAGES: Final[tuple[LoopStage, ...]] = (
    LoopStage.RETRIEVE,
    LoopStage.COMPOSE,
    LoopStage.GENERATE,
)

#: Stages incurred constructing the memory (the write half, amortised over turns).
BUILD_STAGES: Final[tuple[LoopStage, ...]] = (LoopStage.DEPOSIT, LoopStage.SYNTHESISE)


class TokenCounts(BaseModel):
    """Tokens for one stage. ``cached`` counts prompt tokens served from a provider
    prefix cache — reported alongside ``prompt``, never subtracted from it."""

    model_config = ConfigDict(extra="forbid")

    prompt: int = 0
    completion: int = 0
    cached: int = 0

    @model_validator(mode="before")
    @classmethod
    def _drop_computed(cls, data: Any) -> Any:
        """``total`` is serialised for the benefit of anyone reading the file with
        ``jq``, but it is derived, so a reload must discard it rather than reject it.
        Without this, a saved run cannot be loaded back for ``--score-only``."""
        if isinstance(data, dict) and "total" in data:
            data = {key: value for key, value in data.items() if key != "total"}
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def __add__(self, other: TokenCounts) -> TokenCounts:
        return TokenCounts(
            prompt=self.prompt + other.prompt,
            completion=self.completion + other.completion,
            cached=self.cached + other.cached,
        )


class StageCost(BaseModel):
    """What one stage cost on one item or one build."""

    model_config = ConfigDict(extra="forbid")

    tokens: TokenCounts = Field(default_factory=TokenCounts)
    latency_ms: float = 0.0
    calls: int = 0
    """Number of service invocations (LLM, embedder, store) attributed to the stage."""

    def __add__(self, other: StageCost) -> StageCost:
        return StageCost(
            tokens=self.tokens + other.tokens,
            latency_ms=self.latency_ms + other.latency_ms,
            calls=self.calls + other.calls,
        )


class RetrievedItem(BaseModel):
    """One piece of evidence that reached the context.

    ``text`` is stored so a reference-free judge — which scores whether the answer is
    linked to the evidence, with no gold answer at all — can run from the file alone.
    ``memory_type`` is what makes the per-memory-type breakdown possible; without it that
    breakdown is unrecoverable after the run.
    """

    model_config = ConfigDict(extra="forbid")

    record_id: str
    memory_type: str
    rank: int
    score: float | None = None
    score_components: dict[str, float] = Field(default_factory=dict)
    """Named contributions to the composite score (``vector``, ``bm25``, ``recency``…)."""

    text: str = ""
    tokens: int | None = None
    trust: float | None = None
    quarantined: bool | None = None
    namespace: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    """Session/message ids and any other dataset-linkable origin, for evidence scoring."""


class LexicalScores(BaseModel):
    """Reference-based surface metrics, when a scorer computes them. All optional: a run
    that did not compute them stores ``None`` rather than a misleading zero."""

    model_config = ConfigDict(extra="forbid")

    exact_match: float | None = None
    f1: float | None = None
    bleu1: float | None = None
    rouge_l: float | None = None


class JudgeResult(BaseModel):
    """The verdict, and the instrument that produced it.

    Separate from the item so that a generation-only run leaves it ``None`` and a later
    ``--score-only`` pass fills it in. The rubric identity travels with the score because
    a score without its rubric is not comparable to anything.
    """

    model_config = ConfigDict(extra="forbid")

    score: float
    passed: bool | None = None
    label: str | None = None
    kind: str
    """The :class:`~evals.harness.config.JudgeKind` value used."""

    judge_model: str
    rubric_id: str
    rubric_version: str
    rubric_sha256: str | None = None
    rationale: str | None = None
    raw_output: str | None = None
    cost: StageCost = Field(default_factory=StageCost)
    judged_at: datetime | None = None
    judge_run_id: str | None = None
    """Set when this verdict came from a re-judge pass rather than the original run."""

    error: str | None = None


class ItemResult(BaseModel):
    """One question, one answer, one price.

    Mutable by design: ``--score-only`` writes :attr:`judge` onto a stored item.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str
    sample_id: str
    question: str
    gold_answers: list[str] = Field(default_factory=list)
    gold_evidence: list[str] = Field(default_factory=list)
    category: str | None = None
    """Dataset-native question category. Drives the per-category breakdown."""

    question_meta: dict[str, Any] = Field(default_factory=dict)
    """Dataset-specific fields a judge may need (question date, evidence ids, session
    dates). Stored so re-judging never has to reopen the corpus."""

    prediction: str | None = None
    raw_generation: str | None = None
    """Pre-normalisation model output, kept because answer extraction is itself a
    protocol choice that changes scores."""

    context: str | None = None
    """The assembled context sent to the backbone. Large; store it when the judge or the
    analysis needs it, otherwise leave ``None`` and rely on :attr:`retrieved`."""

    context_tokens: int | None = None
    retrieved: list[RetrievedItem] = Field(default_factory=list)
    costs: dict[LoopStage, StageCost] = Field(default_factory=dict)
    latency_ms: float | None = None
    """Wall-clock for the whole item, which is not the sum of stage latencies when
    stages overlap."""

    attempts: int = 1
    seed: int | None = None
    error: str | None = None
    """Set when generation failed. Such items are counted, never silently dropped."""

    judge: JudgeResult | None = None
    lexical: LexicalScores | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def memory_types(self) -> set[str]:
        """Memory types that contributed at least one retrieved item."""
        return {item.memory_type for item in self.retrieved}

    def stage_tokens(self, stages: Iterable[LoopStage]) -> int:
        return sum(self.costs[stage].tokens.total for stage in stages if stage in self.costs)

    def total_tokens(self) -> int:
        """Loop-stage tokens only. Judge tokens are excluded: the instrument's cost is
        not the system's cost."""
        return sum(cost.tokens.total for cost in self.costs.values())


class BuildRecord(BaseModel):
    """What it cost to construct one sample's memory.

    Without this the write path is invisible and the firewall's price is unknowable — the
    whole point of the cost-versus-capability frontier. ``cache_hit`` records whether the
    build was reused, so a run's reported build cost is never mistaken for work done.
    """

    model_config = ConfigDict(extra="forbid")

    sample_id: str
    cache_key: str | None = None
    cache_hit: bool = False
    turns_ingested: int = 0
    """Conversation turns fed to the write path. The denominator for per-turn cost."""

    sessions_ingested: int = 0
    records_written: int = 0
    events_appended: int = 0
    costs: dict[LoopStage, StageCost] = Field(default_factory=dict)
    wall_ms: float | None = None
    store_bytes: int | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def total_tokens(self) -> int:
        return sum(cost.tokens.total for cost in self.costs.values())


class Environment(BaseModel):
    """Where the run happened. Not protocol, but the first thing anyone asks when two
    runs of the same protocol disagree."""

    model_config = ConfigDict(extra="forbid")

    harness_version: str = HARNESS_VERSION
    memspine_version: str = memspine.__version__
    python_version: str = Field(default_factory=lambda: sys.version.split()[0])
    platform: str = Field(default_factory=platform.platform)
    git_commit: str | None = None
    """Passed in by the caller; the schema never shells out to git."""

    @classmethod
    def capture(cls, git_commit: str | None = None) -> Environment:
        return cls(git_commit=git_commit)


class ScoreBreakdown(BaseModel):
    """Aggregate over a set of items. Every statistic is ``None`` when undefined."""

    model_config = ConfigDict(extra="forbid")

    n: int = 0
    n_judged: int = 0
    n_errors: int = 0
    judge_score_mean: float | None = None
    pass_rate: float | None = None
    """Fraction of judged items at or above ``judge.pass_threshold``."""

    exact_match: float | None = None
    f1: float | None = None
    tokens_total: int = 0
    """Loop-stage tokens actually recorded. A count, so ``0`` is a true count of zero
    recorded tokens; :attr:`tokens_mean` is ``None`` when nothing was measured."""

    tokens_mean: float | None = None
    latency_ms_mean: float | None = None
    latency_ms_p50: float | None = None
    latency_ms_p95: float | None = None
    retrieved_mean: float | None = None
    """Mean number of evidence items that reached the context."""


class MemoryTypeBreakdown(ScoreBreakdown):
    """Per-memory-type view.

    Attribution rule, stated because it is a choice and not a fact: an item belongs to
    memory type ``X`` when at least one retrieved item came from ``X``. Items therefore
    appear under several types and the ``n`` values do not sum to the item count.
    :attr:`evidence_share` and :attr:`top1_share` give the complementary, non-overlapping
    view over retrieved evidence.
    """

    memory_type: str = ""
    evidence_share: float | None = None
    """Fraction of all retrieved items across the run that came from this type."""

    top1_share: float | None = None
    """Fraction of items whose rank-1 evidence came from this type."""


class CostPerCycle(BaseModel):
    """The harness's operationalisation of cost per cycle for snapshot QA benchmarks.

    The framework defines cost per cycle over a live multi-session episode:
    read-stage cost per turn plus consolidation cost amortised over turns. LoCoMo and
    LongMemEval are not that shape — the history is frozen and the agent answers probes
    over it — so the harness reports the two halves separately *and* a combined
    per-question figure, and does not pretend the combined figure is the episode metric.

    Judge cost is excluded throughout.
    """

    model_config = ConfigDict(extra="forbid")

    questions: int = 0
    turns_ingested: int = 0
    cost_recorded: bool = False
    """Whether *any* per-stage cost reached this summary. When ``False`` every token
    ratio below is ``None``: the run was not instrumented, which is not the same as
    having been free, and the totals are counts of nothing rather than measurements."""

    query_tokens_total: int = 0
    build_tokens_total: int = 0
    query_tokens_per_question: float | None = None
    build_tokens_per_turn: float | None = None
    build_tokens_per_question: float | None = None
    tokens_per_question: float | None = None
    """Query plus amortised build cost, per question. The single-figure cost of a run."""

    query_latency_ms_per_question: float | None = None
    per_stage_tokens: dict[LoopStage, int] = Field(default_factory=dict)
    per_stage_tokens_per_question: dict[LoopStage, float] = Field(default_factory=dict)
    cached_prompt_tokens: int = 0
    """Prompt tokens served from a provider prefix cache (E2's headline claim, which has
    never been measured in this codebase — this is the field that would measure it)."""

    builds_reused: int = 0
    """Cached builds. Their cost is *not* counted as work done by this run."""


class RunSummary(BaseModel):
    """Run-level record: identity, protocol, environment, and every breakdown."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    run_id: str
    config: RunConfig
    protocol_digest: str
    build_digest: str
    environment: Environment = Field(default_factory=Environment)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    wall_seconds: float | None = None

    n_samples: int = 0
    n_items: int = 0
    n_judged: int = 0
    n_errors: int = 0

    overall: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    per_category: dict[str, ScoreBreakdown] = Field(default_factory=dict)
    per_memory_type: dict[str, MemoryTypeBreakdown] = Field(default_factory=dict)
    costs: dict[LoopStage, StageCost] = Field(default_factory=dict)
    judge_cost: StageCost = Field(default_factory=StageCost)
    cost_per_cycle: CostPerCycle = Field(default_factory=CostPerCycle)
    ablation_label: str = "reference"
    ablation_deviations: dict[str, bool] = Field(default_factory=dict)

    resolved_system_config: dict[str, Any] | None = None
    """The **effective** memspine configuration this run actually ran under, as the
    loader resolved it (template + overrides + env), dumped by the runner.

    ``config.system`` records what the harness *asked for* — a template name and an
    override map — which is a pointer to a mutable file. Two runs can share a protocol
    digest and still have run under different engine configuration if that template
    changed in between. This field is the only thing in the file that closes that gap,
    so a runner that leaves it ``None`` has recorded a request, not a protocol."""

    resolved_system_config_sha256: str | None = None
    """Digest over :attr:`resolved_system_config`, for cheap comparison between runs
    without diffing the whole config."""

    notes: str = ""


class RunResults(BaseModel):
    """A complete run: summary plus every item and build record.

    This is the file. It is the input to ``--score-only``, to the ablation analysis, and
    to any figure. It contains no derived table that cannot be recomputed from its items.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    summary: RunSummary
    items: list[ItemResult] = Field(default_factory=list)
    builds: list[BuildRecord] = Field(default_factory=list)

    def unjudged(self) -> list[ItemResult]:
        """Items awaiting a verdict from the judge model — the work list for a scoring
        pass. Items that errored during generation are excluded: they have no prediction
        to judge, so the scorer records a zero for them without spending a judge call."""
        return [item for item in self.items if item.judge is None and item.error is None]

    def judge_coverage(self) -> float | None:
        if not self.items:
            return None
        return sum(1 for item in self.items if item.judge is not None) / len(self.items)

    def refresh_summary(self) -> None:
        """Recompute every breakdown from the current items and builds.

        Called after a re-judge pass so the stored aggregates never drift from the items
        they claim to summarise.
        """
        refreshed = summarise(
            config=self.summary.config,
            items=self.items,
            builds=self.builds,
            run_id=self.summary.run_id,
            started_at=self.summary.started_at,
            finished_at=self.summary.finished_at,
            environment=self.summary.environment,
            notes=self.summary.notes,
        )
        # Carry forward what `summarise` cannot derive from items and builds.
        refreshed.wall_seconds = self.summary.wall_seconds
        refreshed.protocol_digest = self.summary.protocol_digest
        refreshed.resolved_system_config = self.summary.resolved_system_config
        refreshed.resolved_system_config_sha256 = self.summary.resolved_system_config_sha256
        self.summary = refreshed

    def save(self, path: Path | str) -> Path:
        """Recompute the aggregates, then write.

        ``refresh_summary`` is called here rather than trusted to the caller: a
        ``--score-only`` pass that forgot it would persist a headline number that
        contradicts the items in the same file, and nothing downstream could tell.
        Recomputation is pure and cheap, so the safe order is the only order.
        """
        self.refresh_summary()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: Path | str) -> RunResults:
        """Load a result file, refusing an incompatible schema major version."""
        source = Path(path)
        raw = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SchemaVersionError(f"{source} is not a result object")
        expected_major = SCHEMA_VERSION.split(".")[0]
        summary = raw.get("summary")
        versions = {str(raw.get("schema_version", "0.0.0"))}
        if isinstance(summary, dict):
            # Checked as well as the outer one: they are written together and a file
            # where they disagree was assembled by hand or by a tool with a stale
            # schema, and either way its fields cannot be trusted to mean what they say.
            versions.add(str(summary.get("schema_version", "0.0.0")))
        foreign = sorted(v for v in versions if v.split(".")[0] != expected_major)
        if foreign:
            raise SchemaVersionError(
                f"{source} has schema_version {foreign}; this harness reads "
                f"{SCHEMA_VERSION} (major versions must match)"
            )
        return cls.model_validate(raw)


def summarise(
    *,
    config: RunConfig,
    items: Sequence[ItemResult],
    builds: Sequence[BuildRecord] = (),
    run_id: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    environment: Environment | None = None,
    notes: str = "",
) -> RunSummary:
    """Build the run-level summary, including every breakdown, from raw records.

    Pure and deterministic: same records in, same summary out. No I/O, no clock read
    beyond ``wall_seconds`` when both timestamps are supplied.
    """
    threshold = config.judge.pass_threshold
    summary = RunSummary(
        run_id=run_id or config.run_id,
        config=config,
        protocol_digest=config.protocol_digest(),
        build_digest=config.build_digest(),
        environment=environment or Environment(),
        started_at=started_at,
        finished_at=finished_at,
        n_samples=len({item.sample_id for item in items} | {b.sample_id for b in builds}),
        n_items=len(items),
        n_judged=sum(1 for item in items if item.judge is not None),
        n_errors=sum(1 for item in items if item.error is not None),
        overall=_breakdown(items, threshold),
        per_category=_per_category(items, threshold),
        per_memory_type=_per_memory_type(items, threshold),
        costs=_sum_costs(item.costs for item in items),
        judge_cost=_sum_stage(item.judge.cost for item in items if item.judge is not None),
        cost_per_cycle=_cost_per_cycle(items, builds),
        ablation_label=config.ablation.label(),
        ablation_deviations=config.ablation.deviations(),
        notes=notes or config.notes,
    )
    if started_at is not None and finished_at is not None:
        summary.wall_seconds = (finished_at - started_at).total_seconds()
    build_costs = _sum_costs(build.costs for build in builds)
    for stage, cost in build_costs.items():
        summary.costs[stage] = summary.costs.get(stage, StageCost()) + cost
    return summary


def build_results(
    *,
    config: RunConfig,
    items: Sequence[ItemResult],
    builds: Sequence[BuildRecord] = (),
    run_id: str | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    environment: Environment | None = None,
    notes: str = "",
) -> RunResults:
    """Convenience: summarise and wrap into a saveable :class:`RunResults`."""
    summary = summarise(
        config=config,
        items=items,
        builds=builds,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        environment=environment,
        notes=notes,
    )
    return RunResults(summary=summary, items=list(items), builds=list(builds))


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp, so stored times are unambiguous."""
    return datetime.now(UTC)


def stage_costs_from_report(report: CostReport) -> dict[LoopStage, StageCost]:
    """Bridge a live :class:`~evals.harness.accounting.CostReport` into this schema.

    The bridge lives here, not in :mod:`evals.harness.accounting`, so that module keeps
    its guarantee of importing nothing — not memspine, not pydantic, not this file.
    The dependency runs one way: results knows about accounting, never the reverse.

    **Lossy, and deliberately so.** :class:`StageCost` forbids extra fields and has no
    slot for the exact-versus-estimated verdict, nor for the ingest/consolidation split,
    nor for the write/read side. Persist :meth:`CostReport.to_dict` alongside when any
    of those matter — ``StageCost.calls`` also documents *all* service invocations while
    the ledger counts LLM calls only. Reconciling the two cost vocabularies into one is
    an open decision (ADR), not something this function should quietly settle.
    """
    out: dict[LoopStage, StageCost] = {}
    for stage_name, payload in report.to_stage_costs().items():
        out[LoopStage(stage_name)] = StageCost.model_validate(payload)
    return out


# --- aggregation helpers -----------------------------------------------------


def _breakdown(items: Sequence[ItemResult], threshold: float) -> ScoreBreakdown:
    judged = [item.judge for item in items if item.judge is not None]
    scores = [judge.score for judge in judged]
    latencies = [item.latency_ms for item in items if item.latency_ms is not None]
    tokens = [item.total_tokens() for item in items]
    exact = [item.lexical.exact_match for item in items if item.lexical is not None]
    f1s = [item.lexical.f1 for item in items if item.lexical is not None]
    # A mean over items that recorded no stage cost at all is not "zero tokens", it is
    # "not measured". `tokens_total` stays an int because it is a count of what was
    # recorded; the *rate* is the field a reader mistakes for a measurement.
    measured = any(item.costs for item in items)
    return ScoreBreakdown(
        n=len(items),
        n_judged=len(judged),
        n_errors=sum(1 for item in items if item.error is not None),
        judge_score_mean=_mean(scores),
        pass_rate=_mean([1.0 if score >= threshold else 0.0 for score in scores]),
        exact_match=_mean([value for value in exact if value is not None]),
        f1=_mean([value for value in f1s if value is not None]),
        tokens_total=sum(tokens),
        tokens_mean=_mean([float(value) for value in tokens]) if measured else None,
        latency_ms_mean=_mean(latencies),
        latency_ms_p50=_percentile(latencies, 50),
        latency_ms_p95=_percentile(latencies, 95),
        retrieved_mean=_mean([float(len(item.retrieved)) for item in items]),
    )


def _per_category(items: Sequence[ItemResult], threshold: float) -> dict[str, ScoreBreakdown]:
    """Per-category statistics as a first-class output, not a later derivation.

    Items with no category land under ``"uncategorised"`` rather than vanishing.
    """
    buckets: dict[str, list[ItemResult]] = {}
    for item in items:
        buckets.setdefault(item.category or "uncategorised", []).append(item)
    return {name: _breakdown(bucket, threshold) for name, bucket in sorted(buckets.items())}


def _per_memory_type(
    items: Sequence[ItemResult], threshold: float
) -> dict[str, MemoryTypeBreakdown]:
    """Per-memory-type statistics. See :class:`MemoryTypeBreakdown` for the attribution
    rule and why the ``n`` values overlap."""
    buckets: dict[str, list[ItemResult]] = {}
    evidence_counts: dict[str, int] = {}
    top1_counts: dict[str, int] = {}
    total_evidence = 0
    for item in items:
        for memory_type in sorted(item.memory_types()):
            buckets.setdefault(memory_type, []).append(item)
        for retrieved in item.retrieved:
            evidence_counts[retrieved.memory_type] = (
                evidence_counts.get(retrieved.memory_type, 0) + 1
            )
            total_evidence += 1
        top1 = min(item.retrieved, key=lambda r: r.rank, default=None)
        if top1 is not None:
            top1_counts[top1.memory_type] = top1_counts.get(top1.memory_type, 0) + 1

    n_with_evidence = sum(1 for item in items if item.retrieved)
    out: dict[str, MemoryTypeBreakdown] = {}
    for memory_type, bucket in sorted(buckets.items()):
        base = _breakdown(bucket, threshold)
        out[memory_type] = MemoryTypeBreakdown(
            memory_type=memory_type,
            evidence_share=(
                evidence_counts.get(memory_type, 0) / total_evidence if total_evidence else None
            ),
            top1_share=(
                top1_counts.get(memory_type, 0) / n_with_evidence if n_with_evidence else None
            ),
            **base.model_dump(),
        )
    return out


def _sum_costs(
    per_record: Iterable[dict[LoopStage, StageCost]],
) -> dict[LoopStage, StageCost]:
    totals: dict[LoopStage, StageCost] = {}
    for costs in per_record:
        for stage, cost in costs.items():
            totals[stage] = totals.get(stage, StageCost()) + cost
    return totals


def _sum_stage(costs: Iterable[StageCost]) -> StageCost:
    total = StageCost()
    for cost in costs:
        total = total + cost
    return total


def _cost_per_cycle(items: Sequence[ItemResult], builds: Sequence[BuildRecord]) -> CostPerCycle:
    questions = len(items)
    turns = sum(build.turns_ingested for build in builds)
    query_tokens = sum(item.stage_tokens(QUERY_STAGES) for item in items)
    # Deposit/synthesise cost recorded against an *item* (a run that writes back what it
    # answered, or consolidates between questions) is build-side work and belongs in the
    # build total. Summing only `builds` here would leave those tokens visible in
    # `per_stage_tokens` and absent from `tokens_per_question` — one summary reporting
    # two different totals.
    build_tokens = sum(build.total_tokens() for build in builds)
    build_tokens += sum(item.stage_tokens(BUILD_STAGES) for item in items)
    measured = any(item.costs for item in items) or any(build.costs for build in builds)
    per_stage: dict[LoopStage, int] = {}
    for item in items:
        for stage, cost in item.costs.items():
            per_stage[stage] = per_stage.get(stage, 0) + cost.tokens.total
    for build in builds:
        for stage, cost in build.costs.items():
            per_stage[stage] = per_stage.get(stage, 0) + cost.tokens.total
    cached = sum(cost.tokens.cached for item in items for cost in item.costs.values())
    cached += sum(cost.tokens.cached for build in builds for cost in build.costs.values())
    latencies = [item.latency_ms for item in items if item.latency_ms is not None]
    return CostPerCycle(
        questions=questions,
        turns_ingested=turns,
        cost_recorded=measured,
        query_tokens_total=query_tokens,
        build_tokens_total=build_tokens,
        # Every token ratio is None when *no* stage cost was recorded anywhere: a run
        # whose cost accounting never ran did not cost zero, it was not measured, and
        # 0.0 in the frontier's cost column is the single most misleading value this
        # file could carry.
        query_tokens_per_question=_ratio(query_tokens, questions) if measured else None,
        build_tokens_per_turn=_ratio(build_tokens, turns) if measured else None,
        build_tokens_per_question=_ratio(build_tokens, questions) if measured else None,
        tokens_per_question=(_ratio(query_tokens + build_tokens, questions) if measured else None),
        query_latency_ms_per_question=_mean(latencies),
        per_stage_tokens=dict(sorted(per_stage.items())),
        per_stage_tokens_per_question={
            stage: value / questions for stage, value in sorted(per_stage.items()) if questions
        },
        cached_prompt_tokens=cached,
        builds_reused=sum(1 for build in builds if build.cache_hit),
    )


def _mean(values: Sequence[float]) -> float | None:
    """Mean, or ``None`` when there is nothing to average — never a fabricated zero."""
    if not values:
        return None
    return sum(values) / len(values)


def _ratio(numerator: float, denominator: float) -> float | None:
    if not denominator:
        return None
    return numerator / denominator
