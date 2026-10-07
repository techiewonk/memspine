"""Assembly / ReadPolicy (M12 + E2): abstention, MMR diversity, cache-aware
placement.

E2 placement order (stability-sorted for provider prefix caching): persona →
skills (procedural) → semantic facts → [cache boundary] → retrieved episodic +
working. Volatile content never precedes stable content, so the provider
prefix cache stays warm across turns.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import ClassVar

from memspine.config import constants
from memspine.core.evidence import EvidenceSignal
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.policies.compression import CompressionPolicy
from memspine.core.records import MemoryRecord

__all__ = ["AssembledContext", "AssemblyPolicy", "estimate_tokens"]

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


def ranked(pairs: Iterable[tuple[MemoryRecord, float]]) -> list[tuple[MemoryRecord, float]]:
    """Score descending with a CONTENT-based tie-break (event time, then content
    fingerprint), so equal scores order the same way in every run. Record ids are
    random and set iteration is hash-randomised, which made ties nondeterministic."""
    return sorted(
        pairs,
        key=lambda pair: (-pair[1], pair[0].valid_from, pair[0].content_fingerprint),
    )


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 1


def _rank(record: MemoryRecord) -> int:
    if record.source.channel == "persona":
        return _PLACEMENT_RANK["persona"]
    return _PLACEMENT_RANK.get(record.memory_type, 4)


class AssemblyOptions(PolicyOptions):
    theta_abstain: float = constants.THETA_ABSTAIN
    mmr_lambda: float = constants.MMR_LAMBDA
    cache_aware_placement: bool = True  # E2
    #: H4: drop candidates scoring below ``relative_floor x best score`` before
    #: MMR fills the budget (precision over recall: distractors cost more than a
    #: missing marginal record). 0.0 = off, byte-identical.
    relative_floor: float = 0.0
    #: H19 (Mem++): guarantee up to this many of the most recent (by event time)
    #: candidates a place in the selection, so "latest" evidence is never crowded
    #: out by older, higher-scoring matches. 0 = off.
    latest_slots: int = 0
    #: H23 (Mnemon): drop a candidate whose word set overlaps an already selected
    #: one at or above this Jaccard ratio (near-duplicates waste budget). 1.0 = off.
    dedupe_jaccard: float = 1.0


@dataclass
class AssembledContext:
    """Ordered context bundle. ``boundary_index`` marks the E2 cache boundary:
    records before it are stable (cacheable prefix), after it volatile."""

    records: list[MemoryRecord] = field(default_factory=list)
    boundary_index: int = 0
    abstained: bool = False
    tokens_used: int = 0
    #: W3 (``read.evidence_signal``): how strong the read's evidence is; None when off.
    evidence: EvidenceSignal | None = None


def _is_persona(record: MemoryRecord) -> bool:
    return record.source.channel == "persona"


def _words(record: MemoryRecord) -> set[str]:
    return set(record.content.lower().split())


def _jaccard_sets(ta: set[str], tb: set[str]) -> float:
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class AssemblyPolicy(BindablePolicy):
    name: ClassVar[str] = "assembly"
    Options: ClassVar[type[PolicyOptions]] = AssemblyOptions

    def _opts(self) -> AssemblyOptions:
        options = self.options
        assert isinstance(options, AssemblyOptions)
        return options

    def abstains(self, scored: list[tuple[MemoryRecord, float]]) -> bool:
        """M12: True when no evidence scores at least ``theta_abstain``.

        The pinned persona is not evidence: it is context for every query, so
        it never keeps an off-topic query from abstaining.
        """
        evidence = [score for record, score in scored if not _is_persona(record)]
        return not evidence or max(evidence) < self._opts().theta_abstain

    def apply_floor(
        self, scored: list[tuple[MemoryRecord, float]]
    ) -> list[tuple[MemoryRecord, float]]:
        """H4: drop evidence below ``relative_floor x`` the best evidence score.

        The best is taken over non-persona records, and the persona is never dropped.
        """
        floor = self._opts().relative_floor
        evidence = [score for record, score in scored if not _is_persona(record)]
        if floor <= 0.0 or not evidence:
            return scored
        best = max(evidence)
        return [(r, s) for r, s in scored if _is_persona(r) or s >= floor * best]

    def is_near_duplicate(self, candidate: MemoryRecord, chosen: list[MemoryRecord]) -> bool:
        """H23: True when ``candidate`` overlaps a chosen record at ``dedupe_jaccard`` or more."""
        threshold = self._opts().dedupe_jaccard
        if threshold >= 1.0:
            return False
        words = _words(candidate)
        return any(_jaccard_sets(words, _words(other)) >= threshold for other in chosen)

    def assemble(
        self,
        scored: list[tuple[MemoryRecord, float]],
        budget_tokens: int = 2048,
        compression: CompressionPolicy | None = None,
    ) -> AssembledContext:
        """Select by MMR under a token budget, then place by E2 stability order.

        Abstains (θ_abstain, M12) when the best candidate other than the pinned
        persona scores below the threshold — an honest "I don't know" beats
        confidently stale context.

        With an E5-enabled ``compression`` policy (D-51, config opt-in), MMR
        orders the full candidate set and the compression fallbacks
        (drop-lowest → llmlingua → provider seam) fit it to the budget —
        persona / instruction-flagged / disputed blocks are never touched.
        """
        options = self.options
        assert isinstance(options, AssemblyOptions)
        fit_stage = compression is not None and compression.assembly_enabled()

        if self.abstains(scored):
            # No evidence: abstain, but the pinned persona stays (it is the stable
            # prefix of every turn, not an answer to this query).
            personas = [record for record, _ in scored if _is_persona(record)]
            return AssembledContext(
                records=personas,
                boundary_index=len(personas) if options.cache_aware_placement else 0,
                abstained=True,
                tokens_used=sum(estimate_tokens(record.content) for record in personas),
            )
        scored = self.apply_floor(scored)

        # Greedy MMR selection under the token budget. Token sets are computed
        # once per record — jaccard over pre-split sets, not raw strings.
        token_sets = {id(record): set(record.content.lower().split()) for record, _ in scored}

        def _jaccard(a: MemoryRecord, b: MemoryRecord) -> float:
            ta, tb = token_sets[id(a)], token_sets[id(b)]
            if not ta or not tb:
                return 0.0
            return len(ta & tb) / len(ta | tb)

        remaining = ranked(scored)
        selected: list[tuple[MemoryRecord, float]] = []
        tokens_used = 0
        if options.latest_slots > 0:
            newest = sorted(
                remaining,
                key=lambda pair: (pair[0].valid_from, pair[0].content_fingerprint),
                reverse=True,
            )
            for pair in newest[: options.latest_slots]:
                if options.dedupe_jaccard < 1.0 and any(
                    _jaccard(pair[0], chosen) >= options.dedupe_jaccard for chosen, _ in selected
                ):
                    remaining.remove(pair)  # H23 applies to the guaranteed slots too
                    continue
                cost = estimate_tokens(pair[0].content)
                if selected and tokens_used + cost > budget_tokens:
                    break
                remaining.remove(pair)
                selected.append(pair)
                tokens_used += cost
        while remaining:
            best_index = -1
            best_value = float("-inf")
            for index, (candidate, score) in enumerate(remaining):
                redundancy = max(
                    (_jaccard(candidate, chosen) for chosen, _ in selected),
                    default=0.0,
                )
                value = options.mmr_lambda * score - (1.0 - options.mmr_lambda) * redundancy
                if value > best_value:
                    best_value, best_index = value, index
            candidate, score = remaining.pop(best_index)
            if options.dedupe_jaccard < 1.0 and any(
                _jaccard(candidate, chosen) >= options.dedupe_jaccard for chosen, _ in selected
            ):
                continue
            cost = estimate_tokens(candidate.content)
            # E5 on: admit everything in MMR order — the compression stage
            # fits the selection to the budget afterwards (D-51). Otherwise,
            # over budget: stop — unless nothing is selected yet, in which
            # case admit this one record so assembly never returns empty.
            if not fit_stage and tokens_used + cost > budget_tokens and selected:
                break
            selected.append((candidate, score))
            tokens_used += cost
            if not fit_stage and tokens_used >= budget_tokens:
                break

        if fit_stage:
            assert compression is not None
            selected = compression.fit_assembly(selected, budget_tokens, estimate_tokens)
            tokens_used = sum(estimate_tokens(record.content) for record, _ in selected)

        # E2 placement: stability rank first; within a rank, score descending.
        # The stable-prefix promise only holds when placement actually sorted —
        # with placement off, boundary_index is 0 (no cacheable prefix claimed).
        if options.cache_aware_placement:
            selected.sort(
                key=lambda pair: (
                    _rank(pair[0]),
                    -pair[1],
                    pair[0].valid_from,
                    pair[0].content_fingerprint,
                )
            )
            boundary = sum(1 for record, _ in selected if _rank(record) in _STABLE_RANKS)
        else:
            boundary = 0
        records = [record for record, _ in selected]
        return AssembledContext(
            records=records,
            boundary_index=boundary,
            abstained=False,
            tokens_used=tokens_used,
        )
