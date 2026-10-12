# ruff: noqa: E501
"""LIB-06: exact token budget and complete evidence bundles (R-18 step 1, ER-24)."""

from __future__ import annotations

import random
import re

import pytest

from memspine.compose.budget import (
    Budget,
    BudgetExceededError,
    CallableCounter,
    EstimatingCounter,
    TokenCounter,
)
from memspine.compose.bundles import TurnText, build_bundles, pack
from memspine.schemas.durable import (
    AssertionEnvelope,
    AssertionKey,
    Assessment,
    Evidence,
    Interval,
    OwnerScope,
    TemporalRef,
    TimeAxes,
    TurnRef,
    occurrence_id,
    write_id,
)

SCOPE = OwnerScope(tenant="t", owner="o")


def exact_counter() -> CallableCounter:
    """A stand-in for a real tokenizer: words, punctuation and newlines are tokens."""
    return CallableCounter(lambda t: re.findall(r"\w+|[^\w\s]|\n", t), name="toy-words")


def wide_counter() -> CallableCounter:
    """Multilingual-ish stand-in: every non-ASCII character costs 3 tokens, ASCII words 1."""

    def fn(t: str) -> int:
        return sum(3 if ord(c) > 127 else 0 for c in t) + len(
            re.findall(r"[A-Za-z0-9_]+|[^\w\s]", t)
        )

    return CallableCounter(fn, name="toy-wide")


def tr(i: int) -> TurnRef:
    return TurnRef(source_id="src", source_version=1, seg_index=i)


def assertion(
    rid: str,
    text: str,
    support: tuple[int, ...] = (0,),
    binding: tuple[int, ...] = (),
    *,
    when: str | None = None,
    extra_context: bool = False,
) -> AssertionEnvelope:
    key = AssertionKey(src=rid, rel="r", dst="d", kind="state")
    ev = [Evidence(role="support", turn=tr(i), source_family="spk") for i in support]
    ev += [Evidence(role="binding", turn=tr(i), source_family="spk") for i in binding]
    if extra_context:
        ev.append(Evidence(role="context", source_family="spk"))
    time = TimeAxes()
    prec = "unknown"
    if when:
        ref = TemporalRef(
            text=when, precision="day", basis="explicit", at="2023-05-07T00:00:00+00:00"
        )
        time = TimeAxes(event=Interval(start=ref))
        prec = "day"
    return AssertionEnvelope(
        write_id=write_id(SCOPE, "c", 0, rid),
        assertion_key=key,
        occurrence_id=occurrence_id("e", [f"src#{support[0]}"], key),
        record_id=rid,
        scope=SCOPE,
        text=text,
        kind="state",
        speaker="Ann",
        evidence=ev,
        time=time,
        assessment=Assessment(
            source_trust=0.5, binding="bound", modality="asserted", time_precision=prec
        ),
        src_class="user_stated",
    )


def turns(n: int = 20, words: int = 6) -> dict[tuple[str, int, int], TurnText]:
    return {
        ("src", 1, i): TurnText(
            " ".join(f"w{i}x{j}" for j in range(words)), "Ann", f"2023-05-0{i % 9 + 1}"
        )
        for i in range(n)
    }


# --- counters and budget -----------------------------------------------------------------------------------


def test_counters_label_accounting():
    est = EstimatingCounter()
    assert est.accounting == "estimated" and est.count("abcdefgh") == 2 and est.count("") == 0
    ex = exact_counter()
    assert ex.accounting == "exact" and ex.name == "toy-words" and ex.count("a, b\nc") == 5
    assert isinstance(est, TokenCounter) and isinstance(ex, TokenCounter)
    assert CallableCounter(lambda t: 7, name="n").count("zzz") == 7
    with pytest.raises(ValueError):
        CallableCounter(lambda t: [], name="")


def test_budget_reserves_fits_consume_remaining():
    b = Budget(
        100, exact_counter(), system=10, output=20, history=5, shares={"raw": 0.5, "notes": 0.25}
    )
    assert b.available == 65 and b.remaining == 65
    assert b.block_cap("raw") == 32 and b.block_cap("notes") == 16 and b.block_cap("profile") == 0
    assert b.fits("a b c") and b.fits(65) and not b.fits(66)
    assert b.consume("a b c") == 3 and b.remaining == 62
    with pytest.raises(BudgetExceededError):
        b.consume(63)
    assert b.used == 3
    assert Budget(10, system=20).available == 0
    with pytest.raises(ValueError):
        Budget(10, shares={"raw": 0.7, "notes": 0.5})
    with pytest.raises(ValueError):
        Budget(0)


# --- bundles -----------------------------------------------------------------------------------------------


def test_build_bundles_carries_all_support_binding_and_anchors():
    a = assertion(
        "r1",
        "Ann moved to Rome.",
        support=(0, 1),
        binding=(2,),
        when="7 May 2023",
        extra_context=True,
    )
    [b] = build_bundles([a])
    assert [e.turn.seg_index for e in b.support] == [0, 1]
    assert [e.turn.seg_index for e in b.binding] == [2]
    assert [r.text for r in b.time_anchors] == ["7 May 2023"]
    assert all(e.role != "context" for e in (*b.support, *b.binding))


def test_build_bundles_order_dedup_and_missing_record_id():
    a, b = assertion("r1", "A."), assertion("r2", "B.")
    assert [x.record_id for x in build_bundles([b, a, b])] == ["r2", "r1"]
    no_id = a.model_copy(update={"record_id": None})
    with pytest.raises(ValueError, match="record_id"):
        build_bundles([no_id])


# --- packing -----------------------------------------------------------------------------------------------


def test_pack_renders_whole_bundle_with_headers_and_counts_them():
    [b] = build_bundles(
        [assertion("r1", "Ann moved to Rome.", support=(0,), binding=(1,), when="7 May 2023")]
    )
    c = exact_counter()
    r = pack([b], Budget(500, c), order=["r1"], turns=turns(), preamble="Answer from memory.")
    assert r.accounting == "exact" and r.tokenizer == "toy-words"
    assert r.text.startswith(
        "Answer from memory.\n\n## raw\n[1] Ann moved to Rome. (Ann, 7 May 2023) [2][3]"
    )
    assert "[2] support | Ann: w0x0" in r.text and "[3] binding | Ann: w1x0" in r.text
    assert r.tokens == c.count(r.text) <= r.available
    [blk] = r.blocks
    assert blk.kind == "raw" and blk.accounting == "exact" and blk.tokenizer == "toy-words"
    assert blk.tokens == c.count(
        "## raw\n" + "\n".join(f"{ln.alias} {ln.text}" for ln in blk.lines)
    )
    assert blk.cap is not None and blk.tokens <= blk.cap
    assert [ln.record_id for ln in blk.lines] == ["r1", "r1", "r1"]
    assert r.dropped == [] and r.bundle_tokens["r1"] == r.tokens - c.count("Answer from memory.")


def test_estimated_accounting_is_labelled_not_exact():
    [b] = build_bundles([assertion("r1", "Ann moved to Rome.")])
    r = pack([b], Budget(500), order=["r1"], turns=turns())
    assert r.accounting == "estimated" and r.blocks[0].accounting == "estimated"


def test_oversize_bundle_is_skipped_and_smaller_later_bundles_still_pack():
    big = assertion("big", "Long. " * 60, support=(0, 1, 2, 3))
    small1, small2 = (
        assertion("s1", "Short one.", support=(4,)),
        assertion("s2", "Short two.", support=(5,)),
    )
    bundles = build_bundles([big, small1, small2])
    r = pack(bundles, Budget(80, exact_counter()), order=["big", "s1", "s2"], turns=turns())
    assert [b.record_id for b in r.included] == ["s1", "s2"]
    assert [(d.record_id, d.reason) for d in r.dropped] == [("big", "over_budget")]
    assert r.dropped[0].tokens_needed and r.dropped[0].tokens_needed > 0
    # the old packer broke at the first overflow; nothing of the big bundle leaks in
    assert "Long." not in r.text and "w0x0" not in r.text


def test_early_break_after_n_skips():
    bs = build_bundles(
        [assertion(f"b{i}", "Long. " * 80, support=(i,)) for i in range(4)]
        + [assertion("tiny", "Hi.", support=(9,))]
    )
    order = [b.record_id for b in bs]
    full = pack(bs, Budget(60, exact_counter()), order=order, turns=turns())
    assert [b.record_id for b in full.included] == ["tiny"]
    cut = pack(
        bs, Budget(60, exact_counter()), order=order, turns=turns(), early_break_after_n_skips=2
    )
    assert cut.included == []
    assert [(d.record_id, d.reason) for d in cut.dropped] == [
        ("b0", "over_budget"),
        ("b1", "over_budget"),
        ("b2", "early_break"),
        ("b3", "early_break"),
        ("tiny", "early_break"),
    ]


def test_shared_turn_rendered_once_cited_by_both_and_counted_once():
    a = assertion("r1", "Ann likes tea.", support=(0, 1))
    b = assertion("r2", "Ann drinks tea daily.", support=(1, 2))
    bs = build_bundles([a, b])
    c = exact_counter()
    r = pack(bs, Budget(500, c), order=["r1", "r2"], turns=turns())
    assert r.text.count("w1x0") == 1  # shared turn 1 rendered once
    lines = {ln.record_id: ln for blk in r.blocks for ln in blk.lines if ln.modality}
    assert lines["r1"].text.endswith("[2][3]")
    # r2 is alias [4]; it cites the shared alias [3] and its own new turn [5]
    assert lines["r2"].alias == "[4]" and lines["r2"].text.endswith("[3][5]")
    alone = pack([bs[1]], Budget(500, c), order=["r2"], turns=turns())
    assert r.bundle_tokens["r2"] < alone.tokens  # the shared turn is not paid for twice


def test_missing_turn_text_drops_whole_bundle_never_partial():
    bs = build_bundles(
        [
            assertion("r1", "Fact.", support=(0,), binding=(99,)),
            assertion("r2", "Other.", support=(1,)),
        ]
    )
    r = pack(bs, Budget(500, exact_counter()), order=["r1", "r2"], turns=turns())
    assert [b.record_id for b in r.included] == ["r2"]
    assert [(d.record_id, d.reason) for d in r.dropped] == [("r1", "missing_turn_text")]
    assert "Fact." not in r.text and "w0x0" not in r.text


def test_order_unnamed_bundles_follow_in_input_order_and_duplicates_dropped():
    bs = build_bundles([assertion(f"r{i}", f"Fact {i}.", support=(i,)) for i in range(3)])
    r = pack([*bs, bs[0]], Budget(500, exact_counter()), order=["r2"], turns=turns())
    assert [b.record_id for b in r.included] == ["r2", "r0", "r1"]
    assert [(d.record_id, d.reason) for d in r.dropped] == [("r0", "duplicate_record")]


def test_block_shares_cap_then_redistribute_unused_share():
    items = [assertion(f"n{i}", "Note text here.", support=(i,)) for i in range(6)]
    items.append(assertion("f0", "Fact zero.", support=(10,)))
    bs = build_bundles(items)
    kinds = {f"n{i}": "notes" for i in range(6)} | {"f0": "raw"}
    c = exact_counter()
    order = [f"n{i}" for i in range(6)] + ["f0"]
    shares = {"raw": 0.5, "notes": 0.5}
    # raw is nearly empty, so notes may spill into raw's unused share (pass 2)
    spill = pack(bs, Budget(160, c, shares=shares), order=order, turns=turns(), kinds=kinds)
    notes = next(b for b in spill.blocks if b.kind == "notes")
    n_in = len(notes.lines)
    assert notes.tokens > Budget(160, c, shares=shares).block_cap(
        "notes"
    )  # redistributed beyond share
    assert spill.tokens <= 160 and n_in > 0
    # without redistribution (notes alone with no spare), fewer notes would fit: compare with a
    # budget where raw also needs its share
    hog = build_bundles([assertion("f1", "Raw fact. " * 12, support=(11,))])
    tight = pack(
        [*bs, *hog],
        Budget(160, c, shares=shares),
        order=["f1", *order],
        turns=turns(),
        kinds=kinds | {"f1": "raw"},
    )
    assert len([b for b in tight.included if b.record_id.startswith("n")]) < len(
        [b for b in spill.included if b.record_id.startswith("n")]
    )
    assert tight.tokens <= 160


def test_share_guarantee_protects_lower_ranked_block():
    big_raw = [assertion(f"a{i}", "Raw fact. " * 8, support=(i,)) for i in range(6)]
    note = assertion("n0", "Note.", support=(10,))
    bs = build_bundles([*big_raw, note])
    kinds = {f"a{i}": "raw" for i in range(6)} | {"n0": "notes"}
    order = [f"a{i}" for i in range(6)] + ["n0"]
    r = pack(
        bs,
        Budget(120, exact_counter(), shares={"raw": 0.6, "notes": 0.4}),
        order=order,
        turns=turns(),
        kinds=kinds,
    )
    assert "n0" in [b.record_id for b in r.included]  # raw could not starve notes
    noshare = pack(bs, Budget(120, exact_counter()), order=order, turns=turns(), kinds=kinds)
    assert noshare.tokens <= 120


def test_reserves_shrink_available_and_empty_pack_is_valid():
    bs = build_bundles([assertion("r1", "Fact.")])
    r = pack(bs, Budget(40, exact_counter(), system=20, output=20), order=["r1"], turns=turns())
    assert r.available == 0 and r.included == [] and r.blocks == [] and r.text == ""
    assert [d.reason for d in r.dropped] == ["over_budget"]
    r2 = pack([], Budget(40, exact_counter()), order=[])
    assert r2.tokens == 0 and r2.dropped == []


def test_preamble_larger_than_budget_drops_everything():
    bs = build_bundles([assertion("r1", "Fact.")])
    r = pack(bs, Budget(5, exact_counter()), order=["r1"], turns=turns(), preamble="word " * 50)
    assert r.included == [] and [d.reason for d in r.dropped] == ["over_budget"]


# --- properties (seeded random; hypothesis is not a dependency) --------------------------------------------


def random_case(seed: int):
    rng = random.Random(seed)
    n = rng.randint(1, 14)
    items = []
    for i in range(n):
        k = rng.randint(1, 3)
        sup = tuple(rng.sample(range(20), k))
        bnd = tuple(rng.sample([x for x in range(20) if x not in sup], rng.randint(0, 2)))
        text = " ".join(
            rng.choice(["alpha", "béta", "日本語", "x", "longer-word", "7"])
            for _ in range(rng.randint(1, 40))
        )
        items.append(assertion(f"r{i}", text, sup, bnd, when=rng.choice([None, "7 May 2023"])))
    bs = build_bundles(items)
    kinds = {b.record_id: rng.choice(["raw", "notes", "profile", "public"]) for b in bs}
    order = [b.record_id for b in bs]
    rng.shuffle(order)
    shares = rng.choice(
        [{}, {"raw": 0.5, "notes": 0.3}, {"profile": 0.2, "raw": 0.2, "notes": 0.2, "public": 0.2}]
    )
    budget_total = rng.randint(30, 600)
    counter = rng.choice([exact_counter(), wide_counter()])
    return (
        bs,
        kinds,
        order,
        shares,
        budget_total,
        counter,
        rng.choice([None, 1, 3]),
        rng.randint(0, 10),
    )


@pytest.mark.parametrize("seed", range(60))
def test_property_never_exceeds_cap_no_partial_bundle_and_byte_identical(seed):
    bs, kinds, order, shares, total, counter, skips, reserve = random_case(seed)
    t = turns()

    def run():
        return pack(
            bs,
            Budget(total, counter, system=reserve, shares=shares),
            order=order,
            turns=t,
            kinds=kinds,
            early_break_after_n_skips=skips,
            preamble="Memory:",
        )

    r = run()
    assert r.accounting == "exact"
    assert counter.count(r.text) == r.tokens
    assert r.tokens <= r.available
    for blk in r.blocks:
        assert blk.cap is not None and blk.tokens <= blk.cap
        assert blk.accounting == "exact" and blk.tokenizer == counter.name

    included = {b.record_id for b in r.included}
    dropped = {d.record_id for d in r.dropped}
    assert included.isdisjoint(dropped) and included | dropped == {b.record_id for b in bs}
    heads = {ln.record_id for blk in r.blocks for ln in blk.lines}
    assert heads == included
    for b in r.included:  # whole bundle: assertion text and every support/binding turn present
        assert b.assertion.text.split(" ")[0] in r.text
        for e in (*b.support, *b.binding):
            tt = t[(e.turn.source_id, e.turn.source_version, e.turn.seg_index)]
            assert tt.text in r.text
    for b in bs:  # a dropped bundle contributes no line of its own
        if b.record_id in dropped:
            assert b.record_id not in heads

    r2 = run()
    assert r2.text == r.text and r2.tokens == r.tokens
    assert [b.record_id for b in r2.included] == [b.record_id for b in r.included]
    assert r2.dropped == r.dropped
    assert [blk.model_dump_json() for blk in r2.blocks] == [
        blk.model_dump_json() for blk in r.blocks
    ]
    assert r.text.encode("utf-8") == r2.text.encode("utf-8")


def test_input_order_of_equal_rank_inputs_is_deterministic():
    bs = build_bundles([assertion(f"r{i}", f"F{i}.", support=(i,)) for i in range(5)])
    a = pack(bs, Budget(500, exact_counter()), order=[], turns=turns())
    b = pack(bs, Budget(500, exact_counter()), order=[], turns=turns())
    assert a.text == b.text
