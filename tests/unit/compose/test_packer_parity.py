"""ADR-092 / DS-14: the live packers select the same units as ``read.tiers.fit_units``.

Two live packers fit units to a budget: ``compose.bundles.pack`` (fact bundles, R3-09b) and
``AssemblyPolicy.assemble`` (records that are their own evidence). ``fit_units`` is the pure
whole-unit model the CPU budget benchmarks run. They are kept separate on purpose; the parity
holds wherever their models coincide, and the intended differences are pinned here:

* ``pack`` counts the whole rendered text (block header, aliases, citations), so it stays exact
  for tokenizers that are not additive; ``fit_units`` sums per-unit costs. With an additive
  counter the two agree once the block header is taken off the budget.
* ``pack`` renders a turn cited by two bundles once; ``fit_units`` charges it to each unit.
* ``assemble`` without ``max_skips`` stops at the first overflow (``stop_at_first_overflow``);
  with ``max_skips`` it skips like ``fit_units`` until the skips run out.
* ADR-092: an oversize *first* record is truncated by ``assemble`` (marked in ``hits``), never
  by ``fit_units`` (ER-24: whole units only), which drops it.
"""

from __future__ import annotations

import re

import pytest

from memspine.compose.budget import Budget, CallableCounter
from memspine.compose.bundles import TurnText, build_bundles, pack
from memspine.compose.pack import AssemblyPolicy, estimate_tokens
from memspine.read.tiers import fit_units
from memspine.schemas.durable import (
    AssertionKey,
    Assessment,
    Evidence,
    OwnerScope,
    SemanticEnvelope,
    TimeAxes,
    TurnRef,
    occurrence_id,
    write_id,
)
from memspine.schemas.public.read import EvidenceBundle
from memspine.store.records import MemoryRecord

SCOPE = OwnerScope(tenant="t", owner="o")
BUDGETS = [3, 7, 12, 20, 33, 47, 60, 85, 120, 200, 400]


def word_counter() -> CallableCounter:
    """Additive over lines: alias brackets, separators and newlines cost nothing."""
    return CallableCounter(lambda t: re.findall(r"\w+", t), name="words")


def _assertion(rid: str, text: str, support: tuple[int, ...]) -> SemanticEnvelope:
    key = AssertionKey(src=rid, rel="r", dst="d", kind="state")
    return SemanticEnvelope(
        write_id=write_id(SCOPE, "c", 0, rid),
        assertion_key=key,
        occurrence_id=occurrence_id("e", [f"src#{support[0]}"], key),
        record_id=rid,
        scope=SCOPE,
        text=text,
        kind="state",
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id="src", source_version=1, seg_index=i),
                source_family="spk",
            )
            for i in support
        ],
        time=TimeAxes(),
        assessment=Assessment(
            source_trust=0.5, binding="bound", modality="asserted", time_precision="unknown"
        ),
        src_class="user_stated",
    )


def _fixture(lengths: list[tuple[int, int]]) -> tuple[list[EvidenceBundle], dict]:  # type: ignore[type-arg]
    """Bundles with distinct turns: ``(fact words, turn words)`` per bundle, best first."""
    envelopes, turns = [], {}
    for i, (fact_words, turn_words) in enumerate(lengths):
        envelopes.append(
            _assertion(f"f{i}", " ".join(f"a{i}x{j}" for j in range(fact_words)), (i,))
        )
        turns[("src", 1, i)] = TurnText(" ".join(f"t{i}x{j}" for j in range(turn_words)))
    return build_bundles(envelopes), turns


FIXTURES = [
    [(4, 6), (30, 40), (3, 5), (10, 2), (1, 1), (50, 70), (6, 6)],
    [(2, 2)] * 12,
    [(80, 90), (5, 5), (5, 5), (40, 1)],
]


def _costed(bundles: list[EvidenceBundle], turns: dict) -> tuple[list[EvidenceBundle], int]:  # type: ignore[type-arg]
    """Each bundle's own rendered cost for ``fit_units``, and the shared block-header cost."""
    counter = word_counter()
    one = [
        pack([b], Budget(10_000, counter), order=[b.record_id], turns=turns).tokens for b in bundles
    ]
    full = pack(bundles, Budget(10_000, counter), order=[b.record_id for b in bundles], turns=turns)
    # alone(b) = header + unit(b) and full = header + sum(units): the counter is additive
    header = (sum(one) - full.tokens) // (len(bundles) - 1)
    units = [c - header for c in one]
    assert full.tokens == header + sum(units)
    return [b.model_copy(update={"tokens": u}) for b, u in zip(bundles, units, strict=True)], header


@pytest.mark.parametrize("lengths", FIXTURES)
@pytest.mark.parametrize("stop", [False, True])
def test_bundle_packer_matches_fit_units(lengths: list[tuple[int, int]], stop: bool) -> None:
    bundles, turns = _fixture(lengths)
    costed, header = _costed(bundles, turns)
    order = [b.record_id for b in bundles]
    for budget in BUDGETS:
        packed = pack(
            bundles,
            Budget(budget, word_counter()),
            order=order,
            turns=turns,
            early_break_after_n_skips=1 if stop else None,
        )
        kept, _ = fit_units(costed, budget - header, stop_at_first_overflow=stop)
        assert [b.record_id for b in packed.included] == [b.record_id for b in kept], budget


def test_shared_turn_is_charged_once_by_pack_and_per_unit_by_fit_units() -> None:
    envelopes = [_assertion("f0", "a b", (0,)), _assertion("f1", "c d", (0,))]
    bundles = build_bundles(envelopes)
    turns = {("src", 1, 0): TurnText(" ".join(f"t{j}" for j in range(20)))}
    counter = word_counter()
    alone = pack(bundles[:1], Budget(10_000, counter), order=["f0"], turns=turns).tokens
    both = pack(bundles, Budget(10_000, counter), order=["f0", "f1"], turns=turns)
    # pack: the second bundle costs only its own line, the turn is cited by alias
    assert both.tokens < 2 * alone
    packed = pack(bundles, Budget(alone + 4, counter), order=["f0", "f1"], turns=turns)
    assert [b.record_id for b in packed.included] == ["f0", "f1"]
    costed = [b.model_copy(update={"tokens": alone}) for b in bundles]
    kept, _ = fit_units(costed, alone + 4)
    assert [b.record_id for b in kept] == ["f0"]  # the intended difference


# ---------------------------------------------------------------- records as their own units


def _records(sizes: list[int]) -> list[tuple[MemoryRecord, float]]:
    return [
        (
            MemoryRecord(
                namespace="a",
                memory_type="episodic",
                content=" ".join(f"r{i}w{j}" for j in range(n)),
            ),
            1.0 - i / 100,
        )
        for i, n in enumerate(sizes)
    ]


def _units(scored: list[tuple[MemoryRecord, float]]) -> list[EvidenceBundle]:
    bundles = build_bundles(
        _assertion(r.record_id, r.content, (i,)) for i, (r, _) in enumerate(scored)
    )
    return [
        b.model_copy(update={"tokens": estimate_tokens(r.content)})
        for b, (r, _) in zip(bundles, scored)
    ]


RECORD_FIXTURES = [[3, 40, 5, 9, 1, 60, 2], [2] * 15, [6, 6, 30, 1, 1]]


@pytest.mark.parametrize("sizes", RECORD_FIXTURES)
@pytest.mark.parametrize("max_skips", [None, 1_000])
def test_record_packer_matches_fit_units(sizes: list[int], max_skips: int | None) -> None:
    scored = _records(sizes)
    policy = AssemblyPolicy.bind(
        {"theta_abstain": 0.0, "mmr_lambda": 1.0, "cache_aware_placement": False}
    )
    units = _units(scored)
    for budget in BUDGETS:
        ctx = policy.assemble(scored, budget_tokens=budget, max_skips=max_skips)
        kept, dropped = fit_units(units, budget, stop_at_first_overflow=max_skips is None)
        first = units[0].record_id
        if units[0].tokens > budget:  # the intended difference (ADR-092), pinned below
            assert ctx.hits[first].truncated and first in {b.record_id for b in dropped}
            continue
        assert [r.record_id for r in ctx.records] == [b.record_id for b in kept], budget
        assert not ctx.hits  # the first record fits: no truncation


def test_oversize_first_record_is_truncated_by_assemble_dropped_by_fit_units() -> None:
    scored = _records([200, 3, 3])
    policy = AssemblyPolicy.bind(
        {"theta_abstain": 0.0, "mmr_lambda": 1.0, "cache_aware_placement": False}
    )
    first = scored[0][0].record_id
    ctx = policy.assemble(scored, budget_tokens=40)
    assert [r.record_id for r in ctx.records] == [first] and ctx.hits[first].truncated
    kept, dropped = fit_units(_units(scored), 40)
    assert first in {b.record_id for b in dropped}
    assert [b.record_id for b in kept] == [r.record_id for r, _ in scored[1:]]
