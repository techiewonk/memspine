"""Cost accounting for the evals harness — the (accuracy, tokens, latency) triplet.

This module is the machinery behind the framework's cost-per-cycle (CPC) metric and
behind the cost-versus-capability frontier. It answers one question per run: *what did
each governance layer cost, in tokens and in seconds, on which side of the loop?*

Design notes
------------
**Outside the wheel (D-35/D-19).** This lives in ``evals/``, never in ``src/memspine``.
It imports nothing from memspine and nothing outside the standard library, so the
arithmetic is testable in isolation and the module can be vendored into a results
pipeline unchanged.

**Three partitions of the same spans.** Every timed span is classified three ways, and
each classification is a *partition* — the buckets sum to the same total:

* by :class:`Stage` — the five loop operators R/C/G/D/K.
* by :class:`Side` — write-side versus read-side. MAGMA reports only query-time tokens
  and never its construction cost; we report both, and the write side is where memspine
  is expensive.
* by :class:`Phase` — ``ingest`` / ``consolidation`` / ``query``. This is the partition
  the amortised cost-per-cycle figure is built from.

**Amortised cost per cycle.** One loop cycle is one turn's ingest cost, plus that turn's
amortised share of the consolidation passes, plus one query's read cost::

    tokens_per_cycle = ingest_tokens/turns + consolidation_tokens/turns + query_tokens/queries

Ratios with a zero denominator are ``None`` (undefined), never ``0.0`` — a reader must
never mistake "not measured" for "free". The one exception is a 0/0 ratio, which is 0.0.

**Exact versus estimated tokens.** Every LLM call is recorded as exact or estimated and
the report carries a ``token_measurement`` verdict (``exact`` / ``estimated`` / ``mixed``
/ ``none``). Nothing here fabricates a number: when usage is unavailable the estimate is
labelled as one. See the module note on the LLM port below.

**The LLM port exposes no usage.** ``memspine.services.llm.base.LLMService.chat`` returns
a bare ``str``. There is therefore no exact-usage path for memspine's *internal* calls
(entity extraction, conflict resolution, consolidation summaries) without a hook in
``src/``; :class:`CountingLLM` wraps the port and estimates, flagging every such call.
Generation (the G stage) is by design outside memspine — ``assemble()`` returns a context
and the harness generates — so the harness owns that call and can record its provider
usage exactly via :meth:`TokenUsage.from_usage_mapping`. In the lean benchmark profile,
which targets no LLM call on the write path, the accounting is exact end to end.
"""

from __future__ import annotations

import functools
import inspect
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, cast

__all__ = [
    "ACCOUNTING_SCHEMA_VERSION",
    "CHARS_PER_TOKEN",
    "CostBucket",
    "CostLedger",
    "CostReport",
    "CountingLLM",
    "CycleCost",
    "LLMPort",
    "LatencySummary",
    "Phase",
    "Side",
    "SpanRecord",
    "Stage",
    "StageSpan",
    "TokenUsage",
    "estimate_tokens",
    "frontier_row",
    "percentile",
]

type JSONValue = str | int | float | bool | list[JSONValue] | dict[str, JSONValue] | None

# Bumped when the shape emitted by CostReport.to_dict changes incompatibly, so a
# results file always says which reader it needs.
ACCOUNTING_SCHEMA_VERSION = 1

# Fallback token estimator (see module docstring): the standard ~4-characters-per-token
# approximation for English BPE vocabularies. Used ONLY when the provider reports no
# usage, and every call estimated this way is flagged in the report.
CHARS_PER_TOKEN = 4.0

_SECONDS_PRECISION = 6
_RATIO_PRECISION = 4


class Stage(StrEnum):
    """The five loop operators (framework §4.1): R/C/G/D/K."""

    RETRIEVE = "retrieve"
    COMPOSE = "compose"
    GENERATE = "generate"
    DEPOSIT = "deposit"
    SYNTHESISE = "synthesise"


class Side(StrEnum):
    """Which half of the loop a span belongs to.

    ``WRITE`` is construction cost (ingest + consolidation), ``READ`` is query cost.
    Reporting only ``READ`` is the omission this module exists to avoid.
    """

    WRITE = "write"
    READ = "read"


class Phase(StrEnum):
    """Run phase — the partition the amortised cost-per-cycle figure divides by."""

    INGEST = "ingest"
    CONSOLIDATION = "consolidation"
    QUERY = "query"


#: Default side per stage, **outside consolidation**. R/C/G are read-side, D/K are
#: write-side. Inside a consolidation scope every stage defaults to ``WRITE``: a
#: retrieve issued during a sleep cycle is construction cost, and billing it to the
#: read side inflates the query-leg figure that gets compared against peer systems
#: while deflating the layer we are trying to price. Callers still override per span.
DEFAULT_SIDE: Mapping[Stage, Side] = {
    Stage.RETRIEVE: Side.READ,
    Stage.COMPOSE: Side.READ,
    Stage.GENERATE: Side.READ,
    Stage.DEPOSIT: Side.WRITE,
    Stage.SYNTHESISE: Side.WRITE,
}


def _phase_of(side: Side, in_consolidation: bool) -> Phase:
    if in_consolidation:
        return Phase.CONSOLIDATION
    return Phase.INGEST if side is Side.WRITE else Phase.QUERY


def _round(value: float, digits: int = _SECONDS_PRECISION) -> float:
    return round(value, digits)


def _round_opt(value: float | None, digits: int = _RATIO_PRECISION) -> float | None:
    return None if value is None else round(value, digits)


def _ratio(numerator: float, denominator: int) -> float | None:
    """``None`` when the ratio is undefined (nonzero cost over zero units)."""
    if denominator > 0:
        return numerator / denominator
    return 0.0 if numerator == 0 else None


def _sum_opt(values: Sequence[float | None]) -> float | None:
    total = 0.0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def estimate_tokens(text: str) -> int:
    """Character-based token estimate. Never used when real usage is available."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Prompt/completion tokens for one or more LLM calls.

    ``cached_tokens`` is the subset of ``prompt_tokens`` served from a provider prefix
    cache — the E2 cache-aware-assembly benefit, which is invisible in a raw token total.
    ``estimated_calls`` counts how many of ``calls`` were estimated rather than measured.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    calls: int = 0
    estimated_calls: int = 0

    def __post_init__(self) -> None:
        for name in (
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
            "calls",
            "estimated_calls",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"TokenUsage.{name} must be >= 0")
        if self.cached_tokens > self.prompt_tokens:
            raise ValueError("TokenUsage.cached_tokens cannot exceed prompt_tokens")
        if self.estimated_calls > self.calls:
            raise ValueError("TokenUsage.estimated_calls cannot exceed calls")

    @property
    def total_tokens(self) -> int:
        """Prompt + completion — the figure comparable with published per-query costs."""
        return self.prompt_tokens + self.completion_tokens

    @property
    def billable_tokens(self) -> int:
        """Total minus prefix-cache hits."""
        return self.total_tokens - self.cached_tokens

    @property
    def measurement(self) -> str:
        """``exact`` / ``estimated`` / ``mixed`` / ``none``."""
        if self.calls == 0:
            return "none"
        if self.estimated_calls == 0:
            return "exact"
        if self.estimated_calls == self.calls:
            return "estimated"
        return "mixed"

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            calls=self.calls + other.calls,
            estimated_calls=self.estimated_calls + other.estimated_calls,
        )

    @classmethod
    def from_usage_mapping(cls, usage: Mapping[str, Any], *, calls: int = 1) -> TokenUsage:
        """Read exact usage from a provider response's ``usage`` block.

        Accepts the OpenAI/litellm-normalised shape (``prompt_tokens`` /
        ``completion_tokens``, cache hits nested under ``prompt_tokens_details``) and the
        Anthropic shape (``input_tokens`` / ``output_tokens``, where cache-read and
        cache-creation tokens are counted *outside* ``input_tokens`` and so are added
        back in to keep ``cached_tokens`` a subset of ``prompt_tokens``).

        Pure mapping access — no vendor SDK is imported, and nothing is monkeypatched.
        """
        if "prompt_tokens" in usage or "completion_tokens" in usage:
            prompt = _non_negative_int(usage.get("prompt_tokens"))
            completion = _non_negative_int(usage.get("completion_tokens"))
            details = usage.get("prompt_tokens_details")
            cached = (
                _non_negative_int(details.get("cached_tokens"))
                if isinstance(details, Mapping)
                else 0
            )
            cached = min(cached, prompt)
        elif "input_tokens" in usage or "output_tokens" in usage:
            cache_read = _non_negative_int(usage.get("cache_read_input_tokens"))
            cache_created = _non_negative_int(usage.get("cache_creation_input_tokens"))
            prompt = _non_negative_int(usage.get("input_tokens")) + cache_read + cache_created
            completion = _non_negative_int(usage.get("output_tokens"))
            cached = cache_read
        else:
            raise ValueError(
                "unrecognised usage mapping: expected prompt_tokens/completion_tokens "
                f"or input_tokens/output_tokens, got keys {sorted(usage)!r}"
            )
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            cached_tokens=cached,
            calls=calls,
            estimated_calls=0,
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "billable_tokens": self.billable_tokens,
            "llm_calls": self.calls,
            "estimated_llm_calls": self.estimated_calls,
            "token_measurement": self.measurement,
        }


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return int(value) if value > 0 else 0


@dataclass(frozen=True, slots=True)
class SpanRecord:
    """One completed, stage-attributed operation.

    ``wall_seconds`` is inclusive of nested children; ``self_seconds`` excludes them and
    is the figure that sums without double counting. Under concurrency a parent may
    finish before overlapping children, so ``self_seconds`` is clamped at zero.
    """

    stage: Stage
    side: Side
    phase: Phase
    label: str
    wall_seconds: float
    self_seconds: float
    llm_seconds: float
    usage: TokenUsage
    turn_index: int | None = None
    query_index: int | None = None
    metadata: dict[str, JSONValue] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JSONValue]:
        payload: dict[str, JSONValue] = {
            "stage": self.stage.value,
            "side": self.side.value,
            "phase": self.phase.value,
            "label": self.label,
            "wall_seconds": _round(self.wall_seconds),
            "self_seconds": _round(self.self_seconds),
            "llm_seconds": _round(self.llm_seconds),
            "turn_index": self.turn_index,
            "query_index": self.query_index,
            **self.usage.to_dict(),
        }
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(slots=True)
class CostBucket:
    """Accumulated cost for one slice of a run (a stage, a side, a phase, or the total)."""

    usage: TokenUsage = field(default_factory=TokenUsage)
    self_seconds: float = 0.0
    wall_seconds: float = 0.0
    llm_seconds: float = 0.0
    spans: int = 0

    def add_span(self, record: SpanRecord) -> None:
        self.usage += record.usage
        self.self_seconds += record.self_seconds
        self.wall_seconds += record.wall_seconds
        self.llm_seconds += record.llm_seconds
        self.spans += 1

    def add_usage(self, usage: TokenUsage, *, llm_seconds: float = 0.0) -> None:
        self.usage += usage
        self.llm_seconds += llm_seconds

    def __add__(self, other: CostBucket) -> CostBucket:
        return CostBucket(
            usage=self.usage + other.usage,
            self_seconds=self.self_seconds + other.self_seconds,
            wall_seconds=self.wall_seconds + other.wall_seconds,
            llm_seconds=self.llm_seconds + other.llm_seconds,
            spans=self.spans + other.spans,
        )

    @property
    def total_tokens(self) -> int:
        return self.usage.total_tokens

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            **self.usage.to_dict(),
            "self_seconds": _round(self.self_seconds),
            "wall_seconds": _round(self.wall_seconds),
            "llm_seconds": _round(self.llm_seconds),
            "spans": self.spans,
        }


@dataclass(frozen=True, slots=True)
class LatencySummary:
    """Nearest-rank latency quantiles over per-turn or per-query wall times."""

    count: int
    total_seconds: float
    mean_seconds: float | None
    p50_seconds: float | None
    p95_seconds: float | None
    max_seconds: float | None

    @classmethod
    def from_samples(cls, samples: Sequence[float]) -> LatencySummary:
        if not samples:
            return cls(0, 0.0, None, None, None, None)
        ordered = sorted(samples)
        total = math.fsum(ordered)
        return cls(
            count=len(ordered),
            total_seconds=total,
            mean_seconds=total / len(ordered),
            p50_seconds=_percentile(ordered, 50),
            p95_seconds=_percentile(ordered, 95),
            max_seconds=ordered[-1],
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "count": self.count,
            "total_seconds": _round(self.total_seconds),
            "mean_seconds": _round_opt(self.mean_seconds, _SECONDS_PRECISION),
            "p50_seconds": _round_opt(self.p50_seconds, _SECONDS_PRECISION),
            "p95_seconds": _round_opt(self.p95_seconds, _SECONDS_PRECISION),
            "max_seconds": _round_opt(self.max_seconds, _SECONDS_PRECISION),
        }


def _percentile(ordered: Sequence[float], percentile: int) -> float:
    """Nearest-rank over an already-sorted sequence."""
    rank = math.ceil(percentile / 100 * len(ordered))
    return ordered[min(len(ordered) - 1, max(0, rank - 1))]


def percentile(values: Sequence[float], rank: int) -> float | None:
    """Nearest-rank percentile over an unsorted sequence; ``None`` when empty.

    **The one percentile definition in the harness.** ``evals.harness.results`` imports
    this rather than carrying its own, because a live ledger and the persisted summary
    reporting two different p95 values for the same latencies is the kind of
    discrepancy that gets noticed in review and never in development.

    Nearest-rank, so the answer is always a measurement that actually happened. A
    linearly interpolated p95 over five samples returns a latency no request ever had.
    """
    if not values:
        return None
    return _percentile(sorted(values), rank)


@dataclass(frozen=True, slots=True)
class CycleCost:
    """The amortised cost-per-cycle figure — the framework's CPC metric.

    A ratio is ``None`` when it is undefined (cost recorded against zero units), so an
    unmeasured leg can never read as a free one.
    """

    turns: int
    queries: int
    consolidation_passes: int
    ingest_tokens_per_turn: float | None
    consolidation_tokens_per_turn: float | None
    consolidation_tokens_per_pass: float | None
    query_tokens_per_query: float | None
    tokens_per_cycle: float | None
    """The headline figure — and ``None``, not a lower bound, whenever
    :attr:`unattributed_tokens` is non-zero. Tokens recorded outside every phase belong
    to no leg of the cycle, so a per-cycle figure computed without them would report a
    run that spent 5000 unattributed tokens as costing 0.0 per cycle. Same discipline as
    the 0/0 ratio: refuse the number rather than publish a misleading one."""

    ingest_seconds_per_turn: float | None
    consolidation_seconds_per_turn: float | None
    query_seconds_per_query: float | None
    seconds_per_cycle: float | None
    unattributed_tokens: int = 0
    """Tokens recorded with no phase. Non-zero means the instrumentation has a hole."""

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "turns": self.turns,
            "queries": self.queries,
            "consolidation_passes": self.consolidation_passes,
            "unattributed_tokens": self.unattributed_tokens,
            "ingest_tokens_per_turn": _round_opt(self.ingest_tokens_per_turn),
            "consolidation_tokens_per_turn": _round_opt(self.consolidation_tokens_per_turn),
            "consolidation_tokens_per_pass": _round_opt(self.consolidation_tokens_per_pass),
            "query_tokens_per_query": _round_opt(self.query_tokens_per_query),
            "tokens_per_cycle": _round_opt(self.tokens_per_cycle),
            # Seconds keep microsecond resolution: a sub-millisecond retrieve is a real
            # measurement, and rounding it at token precision would report it as free.
            "ingest_seconds_per_turn": _round_opt(self.ingest_seconds_per_turn, _SECONDS_PRECISION),
            "consolidation_seconds_per_turn": _round_opt(
                self.consolidation_seconds_per_turn, _SECONDS_PRECISION
            ),
            "query_seconds_per_query": _round_opt(self.query_seconds_per_query, _SECONDS_PRECISION),
            "seconds_per_cycle": _round_opt(self.seconds_per_cycle, _SECONDS_PRECISION),
        }


@dataclass(frozen=True, slots=True)
class CostReport:
    """An immutable snapshot of a ledger — the cost half of the (accuracy, tokens,
    latency) triplet. Accuracy is deliberately not computed here; it is the scorer's."""

    turns: int
    queries: int
    consolidation_passes: int
    by_stage: dict[Stage, CostBucket]
    by_side: dict[Side, CostBucket]
    by_phase: dict[Phase, CostBucket]
    unattributed: CostBucket
    turn_seconds: tuple[float, ...] = ()
    query_seconds: tuple[float, ...] = ()
    models: tuple[str, ...] = ()
    spans: tuple[SpanRecord, ...] = ()

    @property
    def total(self) -> CostBucket:
        """Sum over the stage partition plus anything that arrived without a stage."""
        total = CostBucket()
        for bucket in self.by_stage.values():
            total = total + bucket
        return total + self.unattributed

    @property
    def turn_latency(self) -> LatencySummary:
        return LatencySummary.from_samples(self.turn_seconds)

    @property
    def query_latency(self) -> LatencySummary:
        return LatencySummary.from_samples(self.query_seconds)

    @property
    def per_cycle(self) -> CycleCost:
        ingest = self.by_phase.get(Phase.INGEST, CostBucket())
        consolidation = self.by_phase.get(Phase.CONSOLIDATION, CostBucket())
        query = self.by_phase.get(Phase.QUERY, CostBucket())

        ingest_tokens = _ratio(ingest.total_tokens, self.turns)
        consolidation_tokens = _ratio(consolidation.total_tokens, self.turns)
        query_tokens = _ratio(query.total_tokens, self.queries)
        ingest_seconds = _ratio(ingest.self_seconds, self.turns)
        consolidation_seconds = _ratio(consolidation.self_seconds, self.turns)
        query_seconds = _ratio(query.self_seconds, self.queries)

        leaked = self.unattributed.total_tokens
        return CycleCost(
            turns=self.turns,
            queries=self.queries,
            consolidation_passes=self.consolidation_passes,
            ingest_tokens_per_turn=ingest_tokens,
            consolidation_tokens_per_turn=consolidation_tokens,
            consolidation_tokens_per_pass=_ratio(
                consolidation.total_tokens, self.consolidation_passes
            ),
            query_tokens_per_query=query_tokens,
            tokens_per_cycle=(
                None if leaked else _sum_opt((ingest_tokens, consolidation_tokens, query_tokens))
            ),
            ingest_seconds_per_turn=ingest_seconds,
            consolidation_seconds_per_turn=consolidation_seconds,
            query_seconds_per_query=query_seconds,
            seconds_per_cycle=_sum_opt((ingest_seconds, consolidation_seconds, query_seconds)),
            unattributed_tokens=leaked,
        )

    @classmethod
    def merge(cls, reports: Iterable[CostReport]) -> CostReport:
        """Combine per-sample reports into one run-level report.

        Buckets and counters add; latency samples concatenate; ratios are recomputed from
        the merged numerator and denominator rather than averaged (averaging ratios over
        unequal sample sizes is wrong and is the usual way this figure gets fudged).
        """
        by_stage: dict[Stage, CostBucket] = {}
        by_side: dict[Side, CostBucket] = {}
        by_phase: dict[Phase, CostBucket] = {}
        unattributed = CostBucket()
        turns = queries = passes = 0
        turn_seconds: list[float] = []
        query_seconds: list[float] = []
        models: set[str] = set()
        spans: list[SpanRecord] = []

        for report in reports:
            turns += report.turns
            queries += report.queries
            passes += report.consolidation_passes
            for stage, bucket in report.by_stage.items():
                by_stage[stage] = by_stage.get(stage, CostBucket()) + bucket
            for side, bucket in report.by_side.items():
                by_side[side] = by_side.get(side, CostBucket()) + bucket
            for phase, bucket in report.by_phase.items():
                by_phase[phase] = by_phase.get(phase, CostBucket()) + bucket
            unattributed = unattributed + report.unattributed
            turn_seconds.extend(report.turn_seconds)
            query_seconds.extend(report.query_seconds)
            models.update(report.models)
            spans.extend(report.spans)

        return cls(
            turns=turns,
            queries=queries,
            consolidation_passes=passes,
            by_stage=by_stage,
            by_side=by_side,
            by_phase=by_phase,
            unattributed=unattributed,
            turn_seconds=tuple(turn_seconds),
            query_seconds=tuple(query_seconds),
            models=tuple(sorted(models)),
            spans=tuple(spans),
        )

    def to_stage_costs(self) -> dict[str, dict[str, JSONValue]]:
        """Project onto the persisted ``StageCost`` shape in :mod:`evals.harness.results`,
        keyed by stage — each value validates as a ``StageCost``.

        **Lossy, deliberately.** That schema forbids extra fields and has no slot for
        whether a token count was measured or estimated, nor for the ingest-versus-
        consolidation split, so both are dropped here; use :meth:`to_dict` when either
        matters. ``calls`` is LLM calls only, whereas ``StageCost.calls`` documents all
        service invocations. Seconds become milliseconds.

        Live accounting (this module) and the persisted schema (``results.py``) were
        built independently to the same brief and overlap; which one owns the arithmetic
        is a decision for an ADR, not for this bridge.
        """
        return {
            stage.value: {
                "tokens": {
                    "prompt": bucket.usage.prompt_tokens,
                    "completion": bucket.usage.completion_tokens,
                    "cached": bucket.usage.cached_tokens,
                },
                "latency_ms": _round(bucket.self_seconds * 1000.0, 3),
                "calls": bucket.usage.calls,
            }
            for stage, bucket in self.by_stage.items()
        }

    def to_dict(
        self, *, include_spans: bool = False, include_samples: bool = False
    ) -> dict[str, JSONValue]:
        """JSON-serialisable form for the result schema.

        Span-level and raw-sample detail are opt-in: a 300-turn LoCoMo sample produces
        thousands of spans, and the aggregates above are what a results table reads.
        """
        payload: dict[str, JSONValue] = {
            "schema_version": ACCOUNTING_SCHEMA_VERSION,
            "counters": {
                "turns": self.turns,
                "queries": self.queries,
                "consolidation_passes": self.consolidation_passes,
            },
            "models": list(self.models),
            "totals": self.total.to_dict(),
            "by_stage": {stage.value: bucket.to_dict() for stage, bucket in self.by_stage.items()},
            "by_side": {side.value: bucket.to_dict() for side, bucket in self.by_side.items()},
            "by_phase": {phase.value: bucket.to_dict() for phase, bucket in self.by_phase.items()},
            "unattributed": self.unattributed.to_dict(),
            "latency": {
                "turn": self.turn_latency.to_dict(),
                "query": self.query_latency.to_dict(),
            },
            "per_cycle": self.per_cycle.to_dict(),
        }
        if include_samples:
            payload["samples"] = {
                "turn_seconds": [_round(value) for value in self.turn_seconds],
                "query_seconds": [_round(value) for value in self.query_seconds],
            }
        if include_spans:
            payload["spans"] = [span.to_dict() for span in self.spans]
        return payload


# Nesting state. These are module-level (not per-ledger) because creating a ContextVar
# per instance leaks; a span carries its owning ledger so a foreign ledger's span is
# never credited to this one.
_CURRENT_SPAN: ContextVar[StageSpan | None] = ContextVar("memspine_evals_span", default=None)
_TURN_INDEX: ContextVar[int | None] = ContextVar("memspine_evals_turn", default=None)
_QUERY_INDEX: ContextVar[int | None] = ContextVar("memspine_evals_query", default=None)
_IN_CONSOLIDATION: ContextVar[bool] = ContextVar("memspine_evals_consolidating", default=False)


class StageSpan:
    """The live handle yielded by :meth:`CostLedger.stage`."""

    __slots__ = (
        "_child_seconds",
        "_ledger",
        "_llm_seconds",
        "_metadata",
        "_parent",
        "_usage",
        "in_consolidation",
        "label",
        "query_index",
        "side",
        "stage",
        "turn_index",
    )

    def __init__(
        self,
        *,
        ledger: CostLedger,
        stage: Stage,
        side: Side,
        label: str,
        parent: StageSpan | None,
        turn_index: int | None,
        query_index: int | None,
        in_consolidation: bool,
        metadata: dict[str, JSONValue],
    ) -> None:
        self._ledger = ledger
        self._parent = parent
        self._usage = TokenUsage()
        self._llm_seconds = 0.0
        self._child_seconds = 0.0
        self._metadata = metadata
        self.stage = stage
        self.side = side
        self.label = label
        self.turn_index = turn_index
        self.query_index = query_index
        self.in_consolidation = in_consolidation

    @property
    def ledger(self) -> CostLedger:
        return self._ledger

    @property
    def usage(self) -> TokenUsage:
        """Usage recorded directly on this span (children keep their own)."""
        return self._usage

    def record_llm(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        seconds: float = 0.0,
        estimated: bool = False,
        model: str | None = None,
    ) -> None:
        """Record one LLM call against *this* span.

        Prefer this over :meth:`CostLedger.record_llm` when the span handle is in hand:
        attribution is explicit rather than resolved from the ambient context, so a call
        made after a nested span has opened still lands where it belongs.
        """
        self.record_usage(
            TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                calls=1,
                estimated_calls=1 if estimated else 0,
            ),
            seconds=seconds,
            model=model,
        )

    def record_usage(
        self, usage: TokenUsage, *, seconds: float = 0.0, model: str | None = None
    ) -> None:
        if model:
            self._ledger.note_model(model)
        self._usage += usage
        self._llm_seconds += seconds

    def annotate(self, **metadata: JSONValue) -> None:
        """Attach JSON-serialisable detail (top_k, hit counts, profile flags)."""
        self._metadata.update(metadata)

    def _finish(self, wall_seconds: float) -> SpanRecord:
        if self._parent is not None:
            self._parent._child_seconds += wall_seconds
        return SpanRecord(
            stage=self.stage,
            side=self.side,
            phase=_phase_of(self.side, self.in_consolidation),
            label=self.label,
            wall_seconds=wall_seconds,
            self_seconds=max(0.0, wall_seconds - self._child_seconds),
            llm_seconds=self._llm_seconds,
            usage=self._usage,
            turn_index=self.turn_index,
            query_index=self.query_index,
            metadata=dict(self._metadata),
        )


class CostLedger:
    """Collects stage-attributed timings and token usage for one run.

    Not thread-safe by design: nesting is tracked with context variables, so concurrent
    ``asyncio`` tasks nest independently and correctly, but two OS threads sharing one
    ledger would race the accumulators. Benchmark runs are single-threaded per sample.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.perf_counter,
        record_spans: bool = True,
    ) -> None:
        self._clock = clock
        self._record_spans = record_spans
        self._spans: list[SpanRecord] = []
        self._by_stage: dict[Stage, CostBucket] = {}
        self._by_side: dict[Side, CostBucket] = {}
        self._by_phase: dict[Phase, CostBucket] = {}
        self._unattributed = CostBucket()
        self._turns = 0
        self._queries = 0
        self._consolidation_passes = 0
        self._turn_seconds: list[float] = []
        self._query_seconds: list[float] = []
        self._models: set[str] = set()

    # -- timing -----------------------------------------------------------------

    @contextmanager
    def stage(
        self,
        stage: Stage,
        label: str = "",
        *,
        side: Side | None = None,
        metadata: Mapping[str, JSONValue] | None = None,
    ) -> Iterator[StageSpan]:
        """Time an operation and attribute it to a loop stage.

        A span is consolidation work when it is opened inside
        :meth:`consolidation_pass` or when its stage is ``SYNTHESISE`` — K *is* the
        synthesise stage, so a consolidation pipeline outside an explicit pass scope is
        still priced as consolidation.
        """
        parent = _CURRENT_SPAN.get()
        consolidating = _IN_CONSOLIDATION.get() or stage is Stage.SYNTHESISE
        span = StageSpan(
            ledger=self,
            stage=stage,
            side=side
            if side is not None
            else (Side.WRITE if consolidating else DEFAULT_SIDE[stage]),
            label=label or stage.value,
            parent=parent if parent is not None and parent.ledger is self else None,
            turn_index=_TURN_INDEX.get(),
            query_index=_QUERY_INDEX.get(),
            in_consolidation=consolidating,
            metadata=dict(metadata) if metadata else {},
        )
        token = _CURRENT_SPAN.set(span)
        # A SYNTHESISE span makes its *children* consolidation work too. Without this,
        # a retrieve issued during a sleep cycle outside an explicit consolidation_pass
        # is billed to the read side, inflating the very query-leg figure that gets
        # compared against peer systems and deflating the consolidation cost we are
        # trying to price.
        consolidation_token = _IN_CONSOLIDATION.set(consolidating)
        started = self._clock()
        try:
            yield span
        finally:
            elapsed = max(0.0, self._clock() - started)
            _IN_CONSOLIDATION.reset(consolidation_token)
            _CURRENT_SPAN.reset(token)
            self._ingest(span._finish(elapsed))

    def measure[**P, R](
        self,
        stage: Stage,
        label: str = "",
        *,
        side: Side | None = None,
    ) -> Callable[[Callable[P, R]], Callable[P, R]]:
        """Decorator form of :meth:`stage`; works on sync and async callables."""

        def decorate(fn: Callable[P, R]) -> Callable[P, R]:
            span_label = label or getattr(fn, "__qualname__", "") or stage.value
            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                    with self.stage(stage, span_label, side=side):
                        return await cast(Awaitable[Any], fn(*args, **kwargs))

                return cast(Callable[P, R], async_wrapper)

            @functools.wraps(fn)
            def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                with self.stage(stage, span_label, side=side):
                    return fn(*args, **kwargs)

            return sync_wrapper

        return decorate

    # -- run structure ----------------------------------------------------------

    @contextmanager
    def turn(self, index: int | None = None) -> Iterator[int]:
        """One ingested conversation turn — the denominator of the write-side cost."""
        resolved = self._turns if index is None else index
        token = _TURN_INDEX.set(resolved)
        started = self._clock()
        try:
            yield resolved
        finally:
            self._turn_seconds.append(max(0.0, self._clock() - started))
            self._turns += 1
            _TURN_INDEX.reset(token)

    @contextmanager
    def query(self, index: int | None = None) -> Iterator[int]:
        """One evaluated query — the denominator of the read-side cost."""
        resolved = self._queries if index is None else index
        token = _QUERY_INDEX.set(resolved)
        started = self._clock()
        try:
            yield resolved
        finally:
            self._query_seconds.append(max(0.0, self._clock() - started))
            self._queries += 1
            _QUERY_INDEX.reset(token)

    @contextmanager
    def consolidation_pass(self) -> Iterator[None]:
        """One sleep cycle. Spans opened inside are priced as consolidation and
        amortised across the turns they cover."""
        token = _IN_CONSOLIDATION.set(True)
        try:
            yield
        finally:
            self._consolidation_passes += 1
            _IN_CONSOLIDATION.reset(token)

    def add_turns(self, count: int = 1) -> None:
        """Count turns ingested outside a :meth:`turn` scope (bulk ingest)."""
        self._turns += _require_non_negative(count, "count")

    def add_queries(self, count: int = 1) -> None:
        self._queries += _require_non_negative(count, "count")

    def add_consolidation_passes(self, count: int = 1) -> None:
        self._consolidation_passes += _require_non_negative(count, "count")

    # -- token accounting -------------------------------------------------------

    def record_llm(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
        seconds: float = 0.0,
        estimated: bool = False,
        model: str | None = None,
    ) -> None:
        """Record one LLM call against the innermost open span.

        A call made with no span open is kept in the ``unattributed`` bucket rather than
        dropped or raised — a benchmark run must not die over bookkeeping, and leakage
        has to be visible in the report instead of silently vanishing from the total.
        Use :meth:`StageSpan.record_llm` instead when the span handle is available.
        """
        self.record_usage(
            TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                calls=1,
                estimated_calls=1 if estimated else 0,
            ),
            seconds=seconds,
            model=model,
        )

    def record_usage(
        self, usage: TokenUsage, *, seconds: float = 0.0, model: str | None = None
    ) -> None:
        """Record pre-built usage (e.g. read straight off a provider response)."""
        self.note_model(model)
        span = _CURRENT_SPAN.get()
        if span is not None and span.ledger is self:
            span.record_usage(usage, seconds=seconds)
        else:
            self._unattributed.add_usage(usage, llm_seconds=seconds)

    def note_model(self, model: str | None) -> None:
        """Record which model produced a call, so the results row names its backbone."""
        if model:
            self._models.add(model)

    # -- reporting --------------------------------------------------------------

    @property
    def clock(self) -> Callable[[], float]:
        """The ledger's time source, so wrappers time against the same clock the spans
        do — and a fake clock in a test covers them both."""
        return self._clock

    @property
    def spans(self) -> tuple[SpanRecord, ...]:
        return tuple(self._spans)

    def report(self) -> CostReport:
        """A snapshot that does **not** move when the ledger keeps working.

        The buckets are copied, not aliased: ``CostBucket`` is mutable and ``_ingest``
        mutates it in place, so a shallow ``dict(...)`` would hand out live handles and
        a snapshot taken after sample 1 would silently report sample 7's totals. That is
        exactly the multi-sample path :meth:`CostReport.merge` exists for.
        """
        return CostReport(
            turns=self._turns,
            queries=self._queries,
            consolidation_passes=self._consolidation_passes,
            by_stage={stage: CostBucket() + b for stage, b in self._by_stage.items()},
            by_side={side: CostBucket() + b for side, b in self._by_side.items()},
            by_phase={phase: CostBucket() + b for phase, b in self._by_phase.items()},
            unattributed=CostBucket() + self._unattributed,
            turn_seconds=tuple(self._turn_seconds),
            query_seconds=tuple(self._query_seconds),
            models=tuple(sorted(self._models)),
            spans=tuple(self._spans),
        )

    def _ingest(self, record: SpanRecord) -> None:
        if self._record_spans:
            self._spans.append(record)
        self._by_stage.setdefault(record.stage, CostBucket()).add_span(record)
        self._by_side.setdefault(record.side, CostBucket()).add_span(record)
        self._by_phase.setdefault(record.phase, CostBucket()).add_span(record)


def _require_non_negative(value: int, name: str) -> int:
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


class LLMPort(Protocol):
    """Structural mirror of ``memspine.services.llm.base.LLMService``.

    Declared here rather than imported so this module stays free of memspine imports;
    protocols are structural, so a :class:`CountingLLM` still satisfies the real port.
    """

    @property
    def provider_id(self) -> str: ...

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str: ...


class CountingLLM:
    """Ledger-aware decorator over the LLM port.

    Wraps an ``LLMService`` and satisfies the same protocol, so it binds into
    ``LLMRouter`` wherever a provider does — no vendor SDK is patched and no call site in
    ``src/`` changes.

    **Accuracy caveat, stated plainly.** ``LLMService.chat`` returns a bare ``str``, so
    the provider's own usage block never reaches this wrapper. Without a ``usage_reader``
    every call is estimated from message and completion text and flagged
    ``estimated`` in the report. Supply ``usage_reader`` (a callable returning
    :class:`TokenUsage` for the call just made) when the wrapped provider can surface
    real usage; see the module docstring for the ``src/`` hook this needs.
    """

    def __init__(
        self,
        inner: LLMPort,
        ledger: CostLedger,
        *,
        model: str | None = None,
        token_counter: Callable[[str], int] = estimate_tokens,
        usage_reader: Callable[[LLMPort], TokenUsage | None] | None = None,
    ) -> None:
        self._inner = inner
        self._ledger = ledger
        self._model = model
        self._count = token_counter
        self._usage_reader = usage_reader

    @property
    def provider_id(self) -> str:
        return self._inner.provider_id

    @property
    def inner(self) -> LLMPort:
        return self._inner

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        """Delegate, and price the call whether or not it succeeds.

        A provider error is not free: the prompt was sent and the latency was spent, and
        a benchmark run that retries a failing call would otherwise report those retries
        as costing nothing. A failed call is recorded with the prompt tokens it actually
        sent and no completion, then the exception propagates unchanged.

        Timing reads the ledger's clock rather than :func:`time.perf_counter`, so a fake
        clock makes LLM latency deterministic like everything else here.
        """
        clock = self._ledger.clock
        started = clock()
        completion = ""
        try:
            completion = await self._inner.chat(messages, **options)
            return completion
        finally:
            elapsed = max(0.0, clock() - started)
            usage = self._usage_reader(self._inner) if self._usage_reader is not None else None
            if usage is None:
                usage = TokenUsage(
                    prompt_tokens=sum(
                        self._count(str(part.get("content", ""))) for part in messages
                    ),
                    completion_tokens=self._count(completion),
                    calls=1,
                    estimated_calls=1,
                )
            self._ledger.record_usage(
                usage, seconds=elapsed, model=self._model or self._inner.provider_id
            )


def frontier_row(
    *,
    config: str,
    report: CostReport,
    accuracy: float | None = None,
    extra: Mapping[str, JSONValue] | None = None,
) -> dict[str, JSONValue]:
    """One row of the cost-versus-capability frontier.

    ``accuracy`` is passed in by the scorer and is ``None`` until one has run — this
    module never derives, defaults or guesses it.
    """
    cycle = report.per_cycle
    query_latency = report.query_latency
    row: dict[str, JSONValue] = {
        "config": config,
        "accuracy": accuracy,
        "turns": report.turns,
        "queries": report.queries,
        "tokens_per_cycle": _round_opt(cycle.tokens_per_cycle),
        "ingest_tokens_per_turn": _round_opt(cycle.ingest_tokens_per_turn),
        "consolidation_tokens_per_turn": _round_opt(cycle.consolidation_tokens_per_turn),
        "query_tokens_per_query": _round_opt(cycle.query_tokens_per_query),
        "seconds_per_cycle": _round_opt(cycle.seconds_per_cycle, _SECONDS_PRECISION),
        "query_p50_seconds": _round_opt(query_latency.p50_seconds, _SECONDS_PRECISION),
        "query_p95_seconds": _round_opt(query_latency.p95_seconds, _SECONDS_PRECISION),
        "total_tokens": report.total.total_tokens,
        "token_measurement": report.total.usage.measurement,
        # Non-zero means `tokens_per_cycle` above is None: the frontier's x-axis cannot
        # be computed while cost is arriving outside every phase.
        "unattributed_tokens": report.unattributed.total_tokens,
    }
    if extra:
        row.update(extra)
    return row
