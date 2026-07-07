"""Assembly / ReadPolicy (M12 + E2): abstention, MMR diversity, cache-aware
placement.

E2 placement order (stability-sorted for provider prefix caching): persona →
skills (procedural) → semantic facts → [cache boundary] → retrieved episodic +
working. Volatile content never precedes stable content, so the provider
prefix cache stays warm across turns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord

__all__ = ["AssembledContext", "AssemblyPolicy"]

# E2 stability order: lower = more stable = earlier in the prompt prefix.
_PLACEMENT_RANK = {
    "persona": 0,  # source.channel == "persona" (pinned working memory)
    "procedural": 1,
    "semantic": 2,
    # ── cache boundary ──
    "shared": 3,
    "associative": 3,
    "reflective": 3,
    "episodic": 4,
    "working": 5,
}
_STABLE_RANKS = {0, 1, 2}


def _estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


def _token_jaccard(a: str, b: str) -> float:
    ta, tb = set(a.lower().split()), set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _rank(record: MemoryRecord) -> int:
    if record.source.channel == "persona":
        return _PLACEMENT_RANK["persona"]
    return _PLACEMENT_RANK.get(record.memory_type, 4)


class AssemblyOptions(PolicyOptions):
    theta_abstain: float = constants.THETA_ABSTAIN
    mmr_lambda: float = constants.MMR_LAMBDA
    cache_aware_placement: bool = True  # E2


@dataclass
class AssembledContext:
    """Ordered context bundle. ``boundary_index`` marks the E2 cache boundary:
    records before it are stable (cacheable prefix), after it volatile."""

    records: list[MemoryRecord] = field(default_factory=list)
    boundary_index: int = 0
    abstained: bool = False
    tokens_used: int = 0


class AssemblyPolicy(BindablePolicy):
    name: ClassVar[str] = "assembly"
    Options: ClassVar[type[PolicyOptions]] = AssemblyOptions

    def assemble(
        self,
        scored: list[tuple[MemoryRecord, float]],
        budget_tokens: int = 2048,
    ) -> AssembledContext:
        """Select by MMR under a token budget, then place by E2 stability order.

        Abstains (θ_abstain, M12) when the best candidate scores below the
        threshold — an honest "I don't know" beats confidently stale context.
        """
        options = self.options
        assert isinstance(options, AssemblyOptions)

        if not scored or max(score for _, score in scored) < options.theta_abstain:
            return AssembledContext(abstained=True)

        # Greedy MMR selection under the token budget.
        remaining = sorted(scored, key=lambda pair: pair[1], reverse=True)
        selected: list[tuple[MemoryRecord, float]] = []
        tokens_used = 0
        while remaining:
            best_index = -1
            best_value = float("-inf")
            for index, (candidate, score) in enumerate(remaining):
                redundancy = max(
                    (_token_jaccard(candidate.content, chosen.content) for chosen, _ in selected),
                    default=0.0,
                )
                value = options.mmr_lambda * score - (1.0 - options.mmr_lambda) * redundancy
                if value > best_value:
                    best_value, best_index = value, index
            candidate, score = remaining.pop(best_index)
            cost = _estimate_tokens(candidate.content)
            # Over budget: stop — unless nothing is selected yet, in which case
            # admit this one record so assembly never returns silently empty.
            if tokens_used + cost > budget_tokens and selected:
                break
            selected.append((candidate, score))
            tokens_used += cost
            if tokens_used >= budget_tokens:
                break

        # E2 placement: stability rank first; within a rank, score descending.
        if options.cache_aware_placement:
            selected.sort(key=lambda pair: (_rank(pair[0]), -pair[1]))
        records = [record for record, _ in selected]
        boundary = sum(1 for record in records if _rank(record) in _STABLE_RANKS)
        return AssembledContext(
            records=records,
            boundary_index=boundary,
            abstained=False,
            tokens_used=tokens_used,
        )
