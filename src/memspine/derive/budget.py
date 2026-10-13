"""O4-01b: the background derivation budget (O-05).

A sleep cycle spends model calls and tokens. :class:`DerivationBudget` caps both per cycle per
namespace. It is a gate, not a trigger: the namespace job asks :meth:`DerivationBudget.exhausted`
before each model stage, and a spent budget defers the rest of the cycle. Deferred stages are
persisted and counted by the :class:`~memspine.derive.job.LeaseStore` (never dropped), and the next
cycle runs them first.

Granularity is one stage: a stage that has started always finishes, so a cycle may overshoot its cap
by at most one stage. Stages that make no model call (:data:`FREE_STAGES`) are never deferred.

Where the spend comes from is a seam. By default the budget reads the call and token counts the
stages already report in their stats (:func:`stage_usage`). A :class:`UsageFeed` can replace that,
so a later economy ledger (W2-12) feeds the same budget without ``derive`` importing it.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "COVERS_SEQ",
    "FREE_STAGES",
    "DerivationBudget",
    "Spend",
    "UsageFeed",
    "is_model_stage",
    "stage_usage",
]

#: Sleep stages that never call a model, so a spent budget does not defer them.
FREE_STAGES: frozenset[str] = frozenset(
    {
        "retention_expire",
        "check_watches",
        "session_lifecycle",
        "decay_sweep",
        "sleep_compute",
        "event_log_prune",
        "reconcile",
        "histories",
        "pattern_observations",
        "standing_answers",
        "compile_edges",
        "rule_edges",
        "project_facts",
        "predict_calibrate",
    }
)


#: O4-01b (O-06): while a derivation runs under ``offline.budget_v2``, the newest raw-turn seq per
#: namespace at the moment it started. The write door stamps it on every derived record as
#: ``covers_seq``. Unset (the default) means nothing is stamped.
COVERS_SEQ: ContextVar[Mapping[str, int] | None] = ContextVar("memspine_covers_seq", default=None)


def is_model_stage(stage: str) -> bool:
    """True when the stage may call a model (anything not listed in :data:`FREE_STAGES`)."""
    return stage not in FREE_STAGES


@dataclass(frozen=True)
class Spend:
    calls: int = 0
    tokens: int = 0


@runtime_checkable
class UsageFeed(Protocol):
    """A source of cumulative spend per namespace (for example the economy ledger)."""

    def spent(self, ns: str) -> Spend: ...


def _int(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def stage_usage(stats: Mapping[str, object]) -> Spend:
    """The model calls and tokens one stage reported: any numeric ``*llm_calls`` / ``calls`` key and
    ``tokens`` / ``tokens_used`` / ``prompt_tokens`` + ``completion_tokens``."""
    calls = sum(_int(v) for k, v in stats.items() if k == "calls" or k.endswith("llm_calls"))
    tokens = _int(stats.get("tokens")) + _int(stats.get("tokens_used"))
    tokens += _int(stats.get("prompt_tokens")) + _int(stats.get("completion_tokens"))
    return Spend(calls, tokens)


class DerivationBudget:
    """Per-cycle, per-namespace caps on model calls and tokens. ``None`` leaves a dimension open."""

    def __init__(
        self,
        max_calls: int | None = None,
        max_tokens: int | None = None,
        *,
        feed: UsageFeed | None = None,
    ) -> None:
        self.max_calls, self.max_tokens, self.feed = max_calls, max_tokens, feed
        self._spent: dict[str, Spend] = {}
        self._base: dict[str, Spend] = {}

    @property
    def limited(self) -> bool:
        return self.max_calls is not None or self.max_tokens is not None

    def begin(self, ns: str) -> None:
        """Start a cycle for ``ns``: spend resets to zero."""
        self._spent[ns] = Spend()
        if self.feed is not None:
            self._base[ns] = self.feed.spent(ns)

    def charge(self, ns: str, stats: Mapping[str, object]) -> Spend:
        """Add one finished stage's usage (from its stats or the feed's delta); return the total."""
        if self.feed is not None:
            now, base = self.feed.spent(ns), self._base.get(ns, Spend())
            total = Spend(max(0, now.calls - base.calls), max(0, now.tokens - base.tokens))
        else:
            used, prev = stage_usage(stats), self._spent.get(ns, Spend())
            total = Spend(prev.calls + used.calls, prev.tokens + used.tokens)
        self._spent[ns] = total
        return total

    def spent(self, ns: str) -> Spend:
        return self._spent.get(ns, Spend())

    def exhausted(self, ns: str) -> bool:
        s = self.spent(ns)
        return (self.max_calls is not None and s.calls >= self.max_calls) or (
            self.max_tokens is not None and s.tokens >= self.max_tokens
        )
