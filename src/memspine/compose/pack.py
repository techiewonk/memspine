"""Assembly / ReadPolicy (M12 + E2): abstention, MMR diversity, cache-aware
placement.

E2 placement order (stability-sorted for provider prefix caching): persona →
skills (procedural) → semantic facts → [cache boundary] → retrieved episodic +
working. Volatile content never precedes stable content, so the provider
prefix cache stays warm across turns.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import ClassVar, Literal

from memspine.compose.compression import CompressionPolicy
from memspine.config import constants
from memspine.core import trace_sink
from memspine.core.evidence import EvidenceSignal
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.observability import tap
from memspine.store.records import MemoryRecord

__all__ = [
    "AssembledContext",
    "AssemblyPolicy",
    "BudgetStatus",
    "HitManifest",
    "estimate_tokens",
    "truncate_to_fit",
    "truncated_copy",
]

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


#: ADR-092: ``within`` = the context fits its budget; ``oversize`` = it does not: the first
#: record did not fit even truncated (it was left out), or a documented exception (E5
#: protected blocks, a stable prefix larger than the budget) left the context over it.
BudgetStatus = Literal["within", "oversize"]


@dataclass(frozen=True)
class HitManifest:
    """ADR-092 (DS-13 / DS-15): what one packed hit depends on and what of it was shown.

    ``needed_turns`` are the eligible source turns the hit rests on (a mined fact's live
    episodic parents; none for a record that is its own evidence), ``included_turns`` the ones
    the context shows with it, ``dropped_turns`` the rest. ``truncated`` marks an oversize
    first record cut to the budget; ``original_tokens`` is its cost before the cut, counted
    with the packer's own counter.

    A hit is ``partial`` (the name of docs/ENRICHMENT_SCHEMA_CONTRACTS.md section 6) when a
    needed turn was cut or its own text was truncated, else ``complete``.
    """

    record_id: str
    needed_turns: tuple[str, ...] = ()
    included_turns: tuple[str, ...] = ()
    dropped_turns: tuple[str, ...] = ()
    truncated: bool = False
    original_tokens: int | None = None

    @property
    def status(self) -> Literal["complete", "partial"]:
        return "partial" if self.dropped_turns or self.truncated else "complete"

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "status": self.status,
            "needed_turns": list(self.needed_turns),
            "included_turns": list(self.included_turns),
            "dropped_turns": list(self.dropped_turns),
            "truncated": self.truncated,
            "original_tokens": self.original_tokens,
        }


@dataclass
class AssembledContext:
    """Ordered context bundle. ``boundary_index`` marks the E2 cache boundary:
    records before it are stable (cacheable prefix), after it volatile.

    ADR-092: ``hits`` maps a packed record id to its :class:`HitManifest` (bundled facts, and
    any truncated record); ``budget_status`` says whether the context fits its budget.
    Truncated excerpts exist only here, as copies of their record under the same
    ``record_id``: they are never persisted, so an id-based erasure or consent check over
    ``records`` covers them as it covers the record itself."""

    records: list[MemoryRecord] = field(default_factory=list)
    boundary_index: int = 0
    abstained: bool = False
    tokens_used: int = 0
    #: W3 (``read.evidence_signal``): how strong the read's evidence is; None when off.
    evidence: EvidenceSignal | None = None
    hits: dict[str, HitManifest] = field(default_factory=dict)
    budget_status: BudgetStatus = "within"

    @property
    def partial_hits(self) -> list[str]:
        """Ids of the packed hits marked ``partial`` (a needed turn cut, or truncated)."""
        return [rid for rid, hit in self.hits.items() if hit.status == "partial"]

    def carry_over(self, records: Iterable[MemoryRecord]) -> dict[str, HitManifest]:
        """The manifests of those of ``records`` this context has one for."""
        return {r.record_id: self.hits[r.record_id] for r in records if r.record_id in self.hits}


# --- ADR-092 / DS-15: the hard cap -----------------------------------------------------------

#: Sentence terminators of common scripts: Latin, ellipsis, CJK full-width, Devanagari danda,
#: Arabic question mark. Closing quotes and brackets right after one stay with it.
_TERMINATORS = ".!?\u2026\u3002\uff01\uff1f\u0964\u0965\u061f"
#: Full-width terminators end a sentence without a following space (CJK uses none).
_FULLWIDTH = "\u3002\uff01\uff1f"
_SENTENCE_END = re.compile(
    rf"[{_TERMINATORS}]+[\"'\u201d\u2019\u300d\u300f)\]]*(?=\s|$)|[{_FULLWIDTH}]+"
)
#: Wrappers the engine puts around a record (E1 instruction flag, B6 untrusted note) stay
#: whole: a cut never lands inside one, and an untrusted note keeps its closing marker.
_UNTRUSTED_HEAD = re.compile(r"\[UNTRUSTED NOTE, trust [^\]]*\] ")
_UNTRUSTED_TAIL = re.compile(r" \[END UNTRUSTED NOTE [0-9a-f]+\]$")


def _split_wrappers(text: str) -> tuple[str, str, str]:
    head, body, tail = "", text, ""
    flag = constants.INSTRUCTION_FLAG_MARKER + "\n"
    while True:
        opened = _UNTRUSTED_HEAD.match(body)
        closed = _UNTRUSTED_TAIL.search(body) if opened is not None else None
        if opened is not None and closed is not None and closed.start() >= opened.end():
            head += body[: opened.end()]
            tail = body[closed.start() :] + tail
            body = body[opened.end() : closed.start()]
            continue
        if body.startswith(flag):
            head += flag
            body = body[len(flag) :]
            continue
        return head, body, tail


def _best_cut(cuts: list[int], fits: Callable[[int], bool]) -> int | None:
    """The largest of the ascending ``cuts`` that ``fits``: a binary search (a count grows with
    the prefix), then a downward walk in case the counter is not monotone."""
    lo, hi, found = 0, len(cuts) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if fits(cuts[mid]):
            found, lo = mid, mid + 1
        else:
            hi = mid - 1
    for index in range(found, -1, -1):
        if fits(cuts[index]):
            return cuts[index]
    return None


def truncate_to_fit(text: str, budget: int, count: Callable[[str], int]) -> str | None:
    """ADR-092 (DS-15): the longest prefix of ``text`` that ``count`` puts at or under ``budget``.

    The cut lands at a sentence boundary; when no sentence fits, at a whitespace boundary
    (works for any script that separates words). None when neither fits: the caller reports
    ``oversize``. Engine wrappers stay whole. ``text`` itself when it already fits."""
    if budget <= 0:
        return None
    if count(text) <= budget:
        return text
    head, body, tail = _split_wrappers(text)

    def fits(cut: int) -> bool:
        return count(head + body[:cut].rstrip() + tail) <= budget

    sentence = sorted({m.end() for m in _SENTENCE_END.finditer(body) if m.end() < len(body)})
    cut = _best_cut(sentence, fits) if sentence else None
    if cut is None:
        spaces = [i for i in range(1, len(body)) if body[i].isspace() and not body[i - 1].isspace()]
        cut = _best_cut(spaces, fits) if spaces else None
    if cut is None:
        return None
    prefix = body[:cut].rstrip()
    return head + prefix + tail if prefix else None


def truncated_copy(
    record: MemoryRecord, budget: int, count: Callable[[str], int]
) -> tuple[MemoryRecord, HitManifest] | None:
    """The record cut to ``budget`` (same ``record_id``, never persisted) with its manifest;
    None when it cannot fit even truncated."""
    original = count(record.content)
    excerpt = truncate_to_fit(record.content, budget, count)
    if excerpt is None:
        return None
    manifest = HitManifest(record.record_id, truncated=True, original_tokens=original)
    return record.model_copy(update={"content": excerpt}), manifest


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

    def abstains(
        self,
        scored: list[tuple[MemoryRecord, float]],
        raw_scores: Mapping[str, float] | None = None,
    ) -> bool:
        """M12: True when no evidence scores at least ``theta_abstain``.

        The pinned persona is not evidence: it is context for every query, so
        it never keeps an off-topic query from abstaining.

        I30: with ``raw_scores`` (record id -> the reranker's raw score, before min-max) the
        test reads those for every record that has one, so it can fire under rerank.
        """
        if raw_scores:
            raw = [
                raw_scores[r.record_id]
                for r, _ in scored
                if not _is_persona(r) and r.record_id in raw_scores
            ]
            if raw:
                return max(raw) < self._opts().theta_abstain
        evidence = [score for record, score in scored if not _is_persona(record)]
        return not evidence or max(evidence) < self._opts().theta_abstain

    def apply_floor(
        self,
        scored: list[tuple[MemoryRecord, float]],
        raw_scores: Mapping[str, float] | None = None,
    ) -> list[tuple[MemoryRecord, float]]:
        """H4: drop evidence below ``relative_floor x`` the best evidence score.

        The best is taken over non-persona records, and the persona is never dropped.
        I30: with ``raw_scores`` the floor is judged on the raw reranker scores of the
        records that have one (assumes non-negative scores, e.g. probabilities); records
        without a raw score are kept.
        """
        floor = self._opts().relative_floor
        if raw_scores and floor > 0.0:
            have = [
                raw_scores[r.record_id]
                for r, _ in scored
                if not _is_persona(r) and r.record_id in raw_scores
            ]
            if have and max(have) > 0.0:
                cut = floor * max(have)
                return [
                    (r, s)
                    for r, s in scored
                    if _is_persona(r)
                    or r.record_id not in raw_scores
                    or raw_scores[r.record_id] >= cut
                ]
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
        apply_floor: bool = True,
        raw_scores: Mapping[str, float] | None = None,
        cost: Callable[[str], int] | None = None,
        max_skips: int | None = None,
    ) -> AssembledContext:
        """Select by MMR under a token budget, then place by E2 stability order.

        Abstains (θ_abstain, M12) when the best candidate other than the pinned
        persona scores below the threshold — an honest "I don't know" beats
        confidently stale context.

        With an E5-enabled ``compression`` policy (D-51, config opt-in), MMR
        orders the full candidate set and the compression fallbacks
        (drop-lowest → llmlingua → provider seam) fit it to the budget —
        persona / instruction-flagged / disputed blocks are never touched.

        R3-09a (``read.exact_budget``): ``cost`` counts a record's tokens (default
        :func:`estimate_tokens`); with ``max_skips`` set, a record that does not fit is skipped and
        later, smaller ones are still tried, until ``max_skips`` skips close the budget (the old
        behaviour is to stop at the first overflow).

        ADR-092 (DS-15): without the E5 fit stage the budget is a hard cap, counted with
        ``cost``. A first record larger than the budget is cut at a sentence (else whitespace)
        boundary and marked truncated in ``hits``; one that cannot fit even truncated is left
        out and the result says ``budget_status="oversize"``.
        """
        count = cost or estimate_tokens
        skips = 0
        options = self.options
        assert isinstance(options, AssemblyOptions)
        fit_stage = compression is not None and compression.assembly_enabled()
        tracing = trace_sink.FORENSICS.get() is not None
        hits: dict[str, HitManifest] = {}
        oversize = False

        def opening(pair: tuple[MemoryRecord, float]) -> tuple[MemoryRecord, float] | None:
            """The first record, cut to the budget when it is larger (ADR-092)."""
            nonlocal oversize
            cut = truncated_copy(pair[0], budget_tokens, count)
            if cut is None:
                oversize = True
                if tracing:
                    trace_sink.cut(
                        "oversize", pair[0].record_id, cost=count(pair[0].content),
                        budget=budget_tokens,
                    )  # fmt: skip
                return None
            hits[pair[0].record_id] = cut[1]
            return cut[0], pair[1]

        if self.abstains(scored, raw_scores):
            if tracing:
                for record, score in scored:
                    if not _is_persona(record):
                        trace_sink.cut(
                            "theta_abstain", record.record_id, score=round(score, 4),
                            theta=options.theta_abstain,
                        )  # fmt: skip
            # No evidence: abstain, but the pinned persona stays (it is the stable
            # prefix of every turn, not an answer to this query), under the same cap.
            personas: list[MemoryRecord] = []
            used = 0
            for record, score in scored:
                if not _is_persona(record):
                    continue
                need = count(record.content)
                if used + need <= budget_tokens:
                    personas.append(record)
                    used += need
                elif not personas and (cut := opening((record, score))) is not None:
                    personas.append(cut[0])
                    used += count(cut[0].content)
            return AssembledContext(
                records=personas,
                boundary_index=len(personas) if options.cache_aware_placement else 0,
                abstained=True,
                tokens_used=used,
                hits=hits,
                budget_status="oversize" if oversize else "within",
            )
        before_floor = [r.record_id for r, _ in scored] if tracing else []
        if raw_scores:  # I30: the floor reads the raw reranker scores, min-max or not
            scored = self.apply_floor(scored, raw_scores)
        elif apply_floor:  # model.rerank.floor="skip" turns the H4 floor off for reranked reads
            scored = self.apply_floor(scored)
        if tracing:
            kept_ids = {r.record_id for r, _ in scored}
            reason = "rerank_floor_raw" if raw_scores else "relative_floor"
            for rid in before_floor:
                if rid not in kept_ids:
                    trace_sink.cut(reason, rid, floor=options.relative_floor)

        # Greedy MMR selection under the token budget. Token sets are computed
        # once per record — jaccard over pre-split sets, not raw strings.
        token_sets = {id(record): set(record.content.lower().split()) for record, _ in scored}

        def _jaccard(a: MemoryRecord, b: MemoryRecord) -> float:
            # a truncated opening record is a new copy: its words are taken on first use
            ta, tb = token_sets.get(id(a)), token_sets.get(id(b))
            if ta is None:
                ta = token_sets[id(a)] = set(a.content.lower().split())
            if tb is None:
                tb = token_sets[id(b)] = set(b.content.lower().split())
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
                    if tracing:
                        trace_sink.cut("jaccard_dedupe", pair[0].record_id, slot="latest")
                    continue
                need = count(pair[0].content)
                if tokens_used + need > budget_tokens:
                    if selected:
                        break
                    if not fit_stage:  # ADR-092: the first record, cut to the budget
                        remaining.remove(pair)
                        opened = opening(pair)
                        if opened is not None:
                            selected.append(opened)
                            tokens_used += count(opened[0].content)
                        break
                remaining.remove(pair)
                selected.append(pair)
                tokens_used += need
        while remaining and not oversize:
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
                if tracing:
                    trace_sink.cut("jaccard_dedupe", candidate.record_id)
                continue
            need = count(candidate.content)
            # E5 on: admit everything in MMR order — the compression stage
            # fits the selection to the budget afterwards (D-51). Otherwise,
            # over budget: stop — unless nothing is selected yet, in which
            # case this record opens the context, truncated to the budget (ADR-092).
            if not fit_stage and tokens_used + need > budget_tokens and selected:
                if tracing:
                    trace_sink.cut(
                        "budget", candidate.record_id, cost=need, used=tokens_used,
                        budget=budget_tokens,
                    )  # fmt: skip
                if max_skips is not None:
                    # R3-09a: skip this one, try smaller later ones, early break after max_skips.
                    skips += 1
                    tap.event(
                        "compose.pack.dropped",
                        ids=[candidate.record_id],
                        reason="over_budget",
                        cost=need,
                        used=tokens_used,
                    )
                    if skips < max_skips:
                        continue
                    tap.event(
                        "compose.pack.dropped",
                        ids=[r.record_id for r, _ in remaining],
                        reason="early_break",
                    )
                    for rest, _ in remaining:
                        trace_sink.cut(
                            "budget", rest.record_id, used=tokens_used, budget=budget_tokens,
                            note="not reached: the budget closed before it",
                        )  # fmt: skip
                break
            if not fit_stage and tokens_used + need > budget_tokens:
                # ADR-092: nothing selected yet, so this record opens the context, cut to
                # the budget; when not even a cut fits, the context is empty and oversize.
                opened = opening((candidate, score))
                if opened is None:
                    break
                candidate = opened[0]
                need = count(candidate.content)
            selected.append((candidate, score))
            tokens_used += need
            if not fit_stage and tokens_used >= budget_tokens:
                if tracing:
                    for rest, _ in remaining:
                        trace_sink.cut(
                            "budget", rest.record_id, used=tokens_used, budget=budget_tokens,
                            note="not reached: the budget filled before it",
                        )  # fmt: skip
                break

        if fit_stage:
            assert compression is not None
            before_fit = [r.record_id for r, _ in selected] if tracing else []
            selected = compression.fit_assembly(selected, budget_tokens, count)
            if tracing:
                kept_fit = {r.record_id for r, _ in selected}
                for rid in before_fit:
                    if rid not in kept_fit:
                        trace_sink.cut("compression_fit", rid, budget=budget_tokens)
            tokens_used = sum(count(record.content) for record, _ in selected)

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
        kept = {record.record_id for record in records}
        return AssembledContext(
            records=records,
            boundary_index=boundary,
            abstained=False,
            tokens_used=tokens_used,
            hits={rid: hit for rid, hit in hits.items() if rid in kept},
            # E5 protected blocks may stay over the budget (documented): oversize too.
            budget_status="oversize" if oversize or tokens_used > budget_tokens else "within",
        )
