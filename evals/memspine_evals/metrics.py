"""The (accuracy, tokens, latency) triplet, per-stage cost, and CPC (A4-4).

Accuracy alone ranks a system that spends 200k tokens per question above one
that spends 4k and loses a point. ConvoMem's finding — full-context beats
memory systems below roughly 150 conversations — is only legible when the
three axes are reported together, so the harness reports them together or not
at all.

Cost per cycle (CPC) is MemSpine's own metric, defined in the framework's
Section 10.3 and accounted here per loop stage, so a number can be attributed
to $\\mathcal{R}$, $\\mathcal{C}$, $\\mathcal{G}$, $\\mathcal{D}$ or
$\\mathcal{K}$ rather than to "the system".
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Stage(str, Enum):
    """The five loop operators, used as cost and latency buckets."""

    RETRIEVE = "R"
    COMPOSE = "C"
    GENERATE = "G"
    DEPOSIT = "D"
    SYNTHESISE = "K"


@dataclass(frozen=True, slots=True)
class Price:
    """USD per 1k tokens. Zero for local models — which is a real number, not a
    missing one, and the manifest records which model it belonged to."""

    prompt: float = 0.0
    completion: float = 0.0


@dataclass(frozen=True, slots=True)
class CostModel:
    model_prices: Mapping[str, Price] = field(default_factory=dict)
    default: Price = field(default_factory=Price)

    def cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        price = self.model_prices.get(model, self.default)
        return (prompt_tokens / 1000) * price.prompt + (completion_tokens / 1000) * price.completion


@dataclass(slots=True)
class StageAccount:
    """Per-stage tokens, calls, latency and cost for one run."""

    tokens_prompt: int = 0
    tokens_completion: int = 0
    calls: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0

    def add(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        calls: int = 0,
        latency_ms: float = 0.0,
        cost_usd: float = 0.0,
    ) -> None:
        self.tokens_prompt += prompt_tokens
        self.tokens_completion += completion_tokens
        self.calls += calls
        self.latency_ms += latency_ms
        self.cost_usd += cost_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "tokens_prompt": self.tokens_prompt,
            "tokens_completion": self.tokens_completion,
            "calls": self.calls,
            "latency_ms": round(self.latency_ms, 3),
            "cost_usd": round(self.cost_usd, 6),
        }


class Ledger:
    """Per-stage accounting across a whole run.

    Follows `LOOP_METRIC_CONTRACT.md` §5: every cost event is assigned to one
    stage exactly once, per-stage contributions are reported alongside the
    total, and — the part that is easy to skip — **unknown telemetry prevents a
    complete total**. An explicit zero means known zero work; a stage whose cost
    the harness cannot observe is marked unknown, and ``complete`` goes false so
    no reader mistakes an unmeasured total for a measured one.
    """

    def __init__(self) -> None:
        self._stages: dict[Stage, StageAccount] = {stage: StageAccount() for stage in Stage}
        self._unknown: dict[Stage, str] = {}

    def add(self, stage: Stage, **kwargs: Any) -> None:
        self._stages[stage].add(**kwargs)

    def mark_unknown(self, stage: Stage, reason: str) -> None:
        """Record that this stage's cost is not observable in this run."""
        self._unknown.setdefault(stage, reason)

    def stage(self, stage: Stage) -> StageAccount:
        return self._stages[stage]

    @property
    def complete(self) -> bool:
        return not self._unknown

    @property
    def unknown_stages(self) -> dict[str, str]:
        return {stage.value: reason for stage, reason in self._unknown.items()}

    @property
    def total_cost_usd(self) -> float:
        return sum(account.cost_usd for account in self._stages.values())

    @property
    def total_model_calls(self) -> int:
        return sum(account.calls for account in self._stages.values())

    def cost_per_cycle(self, n_cycles: int) -> float:
        """CPC over **attempted** cycles, failed attempts included.

        Cycles that were scheduled but never ran (an aborted run) are excluded
        from the denominator and reported separately: they incurred no cost, and
        dividing by them would understate the price of the cycles that did run.
        """
        return self.total_cost_usd / n_cycles if n_cycles else 0.0

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            stage.value: account.to_dict() for stage, account in self._stages.items()
        }
        payload["complete"] = self.complete
        if self._unknown:
            payload["unknown"] = self.unknown_stages
        return payload


def percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def summarise_floats(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 4),
        "p50": round(percentile(values, 0.5), 4),
        "p95": round(percentile(values, 0.95), 4),
        "max": round(max(values), 4),
    }
