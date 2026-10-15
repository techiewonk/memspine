"""LIB-10: family fusion, protected pool, calibrated floors (R-11, R-12, R-13)."""

from __future__ import annotations

import random

import pytest

from memspine.read.fusion import (
    DEFAULT_POOL_SIZE,
    FusionConfig,
    Hit,
    Leg,
    calibrate,
    fuse,
    percentile,
    to_scored_items,
)


def leg(name: str, family: str, ids: list[str], scores: list[float] | None = None) -> Leg:
    sc = scores or [None] * len(ids)
    return Leg(name, family, [Hit(i, s) for i, s in zip(ids, sc, strict=True)])


def plain_rrf(lists: list[list[str]], k: int = 60) -> list[str]:
    fused: dict[str, float] = {}
    for ids in lists:
        for rank, rid in enumerate(ids, start=1):
            fused[rid] = fused.get(rid, 0.0) + 1.0 / (k + rank)
    return [r for r, _ in sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))]


def ids_of(res) -> list[str]:
    return [c.record_id for c in res.candidates]


def test_one_leg_per_family_equals_plain_rrf():
    a = ["r1", "r2", "r3", "r4"]
    b = ["r3", "r1", "r5", "r6"]
    c = ["r7", "r3", "r2", "r8"]
    legs = [leg("dense", "dense", a), leg("bm", "lexical", b), leg("tmp", "temporal", c)]
    res = fuse(legs, FusionConfig(protect_per_family=0))
    assert ids_of(res) == plain_rrf([a, b, c])


def test_rrf_mode_votes_per_leg():
    a = ["x", "y"]
    legs = [leg("l1", "lexical", a), leg("l2", "lexical", a)]
    fam = fuse(legs, FusionConfig(protect_per_family=0))
    plain = fuse(legs, FusionConfig(mode="rrf", protect_per_family=0))
    assert plain.candidates[0].fused_score == pytest.approx(2 * 1 / 61 / (2 / 61))
    assert fam.candidates[0].fused_score == pytest.approx(1.0)
    assert plain.trace.families == ("l1", "l2")


def test_score_normalised_into_unit_interval():
    legs = [leg("a", "dense", ["x"]), leg("b", "lexical", ["x"])]
    res = fuse(legs)
    assert res.candidates[0].fused_score == pytest.approx(1.0)
    raw = fuse(legs, FusionConfig(normalise=False))
    assert raw.candidates[0].fused_score == pytest.approx(2 / 61)


def test_within_family_takes_best_rank_and_keeps_all_provenance():
    legs = [
        leg("bm25", "lexical", ["a", "b", "c"], [9.0, 8.0, 7.0]),
        leg("title", "lexical", ["c", "a"], [5.0, 4.0]),
        leg("vec", "dense", ["c"], [0.9]),
    ]
    res = fuse(legs, FusionConfig(protect_per_family=0, normalise=False))
    c = next(x for x in res.candidates if x.record_id == "c")
    assert c.legs == ("bm25", "title", "vec")
    assert c.families == ("dense", "lexical")
    assert [(p.leg, p.rank) for p in c.provenance] == [("bm25", 3), ("title", 1), ("vec", 1)]
    # lexical family rank for c is min(3, 1) = 1; dense 1
    assert c.fused_score == pytest.approx(2 / 61)
    assert (c.best_leg, c.best_rank) == ("title", 1)


def test_duplicate_record_within_leg_keeps_best_rank():
    res = fuse(
        [Leg("l", "lexical", [Hit("a"), Hit("b"), Hit("a")])], FusionConfig(protect_per_family=0)
    )
    assert ids_of(res) == ["a", "b"]
    assert res.candidates[0].provenance[0].rank == 1


def test_explicit_ranks_are_honoured():
    res = fuse(
        [Leg("l", "lexical", [Hit("a", rank=5), Hit("b", rank=2)])],
        FusionConfig(protect_per_family=0),
    )
    assert ids_of(res) == ["b", "a"]


def test_protected_pool_keeps_family_best_against_crowding():
    lexical = [f"lex{i}" for i in range(40)]
    legs = [
        leg("bm", "lexical", lexical),
        leg("vec", "dense", lexical),  # same ids, so lexical-ish records dominate
        leg("tmp", "temporal", ["t1"]),
        leg("ent", "entity", ["e1"]),
    ]
    res = fuse(legs, FusionConfig(pool_size=5, protect_per_family=1))
    got = ids_of(res)
    assert len(got) == 5
    assert "t1" in got and "e1" in got
    protected = {c.record_id for c in res.candidates if c.protected}
    assert {"t1", "e1", "lex0"} <= protected
    assert res.trace.evicted  # lower lexical records were cut


def test_pool_cap_default_and_eviction_never_protected():
    assert DEFAULT_POOL_SIZE == 64
    ids = [f"r{i}" for i in range(200)]
    res = fuse([leg("a", "dense", ids)])
    assert len(res.candidates) == 64
    assert res.trace.fused_total == 200
    assert len(res.trace.evicted) == 136


def test_protected_overflow_trims_by_family_coverage():
    legs = [leg(f"l{i}", f"fam{i}", [f"f{i}a", f"f{i}b", f"f{i}c"]) for i in range(4)]
    res = fuse(legs, FusionConfig(pool_size=4, protect_per_family=3))
    # four families, 12 protected, room for 4: every family's best survives
    assert sorted(ids_of(res)) == ["f0a", "f1a", "f2a", "f3a"]
    assert len(res.trace.protected_trimmed) == 8


def test_floors_drop_unprotected_below_floor_only():
    legs = [leg("bm", "lexical", ["a", "b", "c", "d"], [9.0, 8.0, 3.0, 2.0])]
    floors = calibrate({"bm": [1.0, 2.0, 3.0, 4.0, 5.0]}, 50)
    assert floors.floor("bm") == pytest.approx(3.0)
    res = fuse(legs, FusionConfig(protect_per_family=1), floors)
    assert ids_of(res) == ["a", "b", "c"]  # d below floor, c equal to floor is kept
    assert res.trace.floored == (("bm", "d"),)


def test_floor_never_drops_protected_or_unscored():
    legs = [leg("bm", "lexical", ["a", "b"], [0.1, 0.1]), Leg("v", "dense", [Hit("z")])]
    floors = calibrate({"bm": [5.0], "v": [5.0]})
    res = fuse(legs, FusionConfig(protect_per_family=1), floors)
    assert set(ids_of(res)) == {"a", "z"}  # a protected, z unscored, b dropped
    assert res.trace.floored == (("bm", "b"),)


def test_calibrate_percentiles_and_edges():
    assert percentile([1.0, 2.0, 3.0, 4.0], 0) == 1.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == pytest.approx(2.5)
    f = calibrate({"a": [1.0, 2.0, 3.0], "b": [], "c": [float("nan"), 4.0]}, {"a": 100.0})
    assert f.by_leg == {"a": 3.0, "c": pytest.approx(4.0)}
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        percentile([1.0], 101)


def test_empty_inputs_and_config_validation():
    res = fuse([])
    assert res.candidates == () and res.trace.fused_total == 0
    assert ids_of(fuse([leg("a", "dense", [])])) == []
    with pytest.raises(ValueError):
        FusionConfig(k=0)
    with pytest.raises(ValueError):
        FusionConfig(pool_size=0)
    with pytest.raises(ValueError):
        fuse([Leg("a", "dense", [Hit("x", rank=0)])])


def test_to_scored_items_are_scored():
    items = to_scored_items(fuse([leg("a", "dense", ["x", "y"])]))
    assert [i.record_id for i in items] == ["x", "y"]
    assert all(i.scored and i.score is not None for i in items)


# --- properties (seeded random; no hypothesis in the venv) -----------------------------------


def random_legs(rng: random.Random) -> list[Leg]:
    universe = [f"r{i}" for i in range(30)]
    fams = ["lexical", "dense", "temporal", "entity", "graph", "episode"]
    legs = []
    for i in range(rng.randint(1, 8)):
        ids = rng.sample(universe, rng.randint(0, 15))
        scores = [round(rng.random(), 3) for _ in ids]
        legs.append(leg(f"leg{i}", rng.choice(fams), ids, scores))
    return legs


def snapshot(res):
    return [(c.record_id, c.fused_score, c.legs, c.families, c.protected) for c in res.candidates]


@pytest.mark.parametrize("seed", range(40))
def test_permuting_legs_gives_identical_output(seed):
    rng = random.Random(seed)
    legs = random_legs(rng)
    floors = calibrate({lg.name: [0.2, 0.4, 0.6] for lg in legs}, 30)
    cfg = FusionConfig(pool_size=rng.choice([3, 8, 64]), protect_per_family=rng.choice([0, 1, 3]))
    base = fuse(legs, cfg, floors)
    for _ in range(3):
        shuffled = legs[:]
        rng.shuffle(shuffled)
        assert fuse(shuffled, cfg, floors) == base


@pytest.mark.parametrize("seed", range(40))
def test_duplicate_same_family_leg_changes_no_ranks(seed):
    rng = random.Random(1000 + seed)
    legs = random_legs(rng)
    cfg = FusionConfig(pool_size=rng.choice([5, 64]), protect_per_family=rng.choice([0, 2]))
    base = fuse(legs, cfg)
    src = rng.choice(legs)
    dup = Leg(src.name + "_dup", src.family, src.hits)
    after = fuse([*legs, dup], cfg)
    assert ids_of(after) == ids_of(base)
    assert [c.fused_score for c in after.candidates] == pytest.approx(
        [c.fused_score for c in base.candidates]
    )


@pytest.mark.parametrize("seed", range(40))
def test_protected_hits_always_present(seed):
    rng = random.Random(2000 + seed)
    legs = random_legs(rng)
    n = rng.choice([1, 2, 3])
    fams = {lg.family for lg in legs if lg.hits}
    cfg = FusionConfig(pool_size=max(1, n * len(fams)), protect_per_family=n)
    floors = calibrate({lg.name: [0.99] for lg in legs}, 100)  # floors drop nearly everything
    res = fuse(legs, cfg, floors)
    got = set(ids_of(res))
    for fam in fams:
        best: dict[str, int] = {}
        for lg in legs:
            if lg.family != fam:
                continue
            for pos, h in enumerate(lg.hits, start=1):
                best[h.record_id] = min(best.get(h.record_id, pos), pos)
        top = sorted(best.items(), key=lambda kv: (kv[1], kv[0]))[:n]
        for rid, _ in top:
            assert rid in got
    assert len(res.candidates) <= cfg.pool_size
