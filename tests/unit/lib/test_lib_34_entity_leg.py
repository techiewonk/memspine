"""LIB-34: entity leg with anti-hub damping."""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime, timedelta

import pytest

from memspine.read.entity_leg import (
    DampingConfig,
    EntityIndex,
    EntityMention,
    damping_factor,
    entity_leg,
    entity_leg_explained,
)
from memspine.read.fusion import FusionConfig, fuse


def M(rid, role="mention", date=None):
    return EntityMention(rid, role, date)


def ids(leg):
    return [h.record_id for h in leg.hits]


def test_empty_index_and_empty_query():
    leg = entity_leg(["Ilse"], EntityIndex(), k=5)
    assert leg.hits == ()
    assert (leg.name, leg.family) == ("entity", "structured")
    assert entity_leg([], EntityIndex({"Ilse": [M("r1")]}), k=5).hits == ()
    res = entity_leg_explained(["Nobody"], EntityIndex({"Ilse": [M("r1")]}), k=5)
    assert res.unknown_entities == ("Nobody",) and res.leg.hits == ()


def test_hub_is_damped_against_rare_entity():
    idx = EntityIndex(
        {
            "Ilse": [M(f"r{i}", "subject") for i in range(50)],
            "Bram": [M("r1", "subject"), M("rB", "subject")],
        }
    )
    for mode in ("idf", "inv_sqrt", "mem0"):
        cfg = DampingConfig(scoring="sum", mode=mode, mem0_c=0.05)
        a = entity_leg(["Ilse"], idx, k=3, damping=cfg).hits[0].score
        b = entity_leg(["Bram"], idx, k=3, damping=cfg).hits[0].score
        assert b > a, mode
    idf = DampingConfig(mode="idf")
    assert damping_factor(1, 100, idf) > damping_factor(10, 100, idf)
    assert damping_factor(10, 100, idf) > damping_factor(100, 100, idf) > 0
    assert damping_factor(4, 100, DampingConfig(mode="inv_sqrt")) == pytest.approx(0.5)
    assert damping_factor(1, 100, DampingConfig(mode="mem0")) == 1.0
    assert damping_factor(11, 100, DampingConfig(mode="mem0")) == pytest.approx(1 / 1.1)


def test_coverage_bonus_rewards_multi_entity_records():
    idx = EntityIndex(
        {
            "Noor": [M("both", "subject"), M("onlyN", "subject")],
            "Aiko": [M("both", "subject"), M("onlyA", "subject")],
        },
        n_records=20,
    )
    sm = DampingConfig(scoring="sum", mode="idf")
    leg = entity_leg(["Noor", "Aiko"], idx, k=5, damping=sm)
    assert ids(leg)[0] == "both"
    flat_cfg = DampingConfig(scoring="sum", mode="idf", coverage_bonus=0.0)
    flat = entity_leg(["Noor", "Aiko"], idx, k=5, damping=flat_cfg)
    s = {h.record_id: h.score for h in flat.hits}
    assert s["both"] == pytest.approx(2 * s["onlyN"])
    s2 = {h.record_id: h.score for h in leg.hits}
    assert s2["both"] > s["both"]


def test_self_entity_damped_unless_only_entity():
    idx = EntityIndex(
        {
            "me": [M(f"r{i}", "subject") for i in range(10)],
            "Tomasz": [M("r0", "subject"), M("rT", "subject")],
        },
        n_records=10,
        self_entity="me",
    )
    cfg0 = DampingConfig(scoring="sum", mode="idf", coverage_bonus=0.0)
    leg = entity_leg(["me", "Tomasz"], idx, k=20, damping=cfg0)
    s = {h.record_id: h.score for h in leg.hits}
    assert s["rT"] > s["r1"]
    sm = DampingConfig(scoring="sum", mode="idf")
    idf = damping_factor(10, 10, sm)
    alone = entity_leg(["me"], idx, k=3, damping=sm).hits[0].score
    assert alone == pytest.approx(idf)
    res = entity_leg_explained(["me", "Tomasz"], idx, k=3, damping=sm)
    ev = {e.entity_id: e for e in res.provenance["r0"]}
    assert ev["me"].damping == pytest.approx(idf * 0.05)
    # default (max) mode damps the self entity too, and not when alone
    res = entity_leg_explained(["me", "Tomasz"], idx, k=3)
    ev = {e.entity_id: e for e in res.provenance["r0"]}
    assert ev["me"].damping == pytest.approx(damping_factor(10, 10) * 0.05)
    res = entity_leg_explained(["me"], idx, k=3)
    assert res.provenance["r0"][0].damping == pytest.approx(damping_factor(10, 10))


def test_roles_and_duplicate_mentions_count_once():
    idx = EntityIndex(
        {"Ilse": [M("a", "mention"), M("a", "subject"), M("a", "subject"), M("b", "object")]},
        n_records=10,
    )
    res = entity_leg_explained(["Ilse"], idx, k=5)
    assert ids(res.leg) == ["a", "b"]
    assert res.provenance["a"][0].role == "subject"
    assert res.provenance["b"][0].role == "object"
    assert res.provenance["a"][0].entity_id == "Ilse"


def test_recency():
    now = datetime(2026, 10, 1, tzinfo=UTC)
    idx = EntityIndex(
        {
            "Aiko": [
                M("new", "subject", now - timedelta(days=1)),
                M("old", "subject", datetime(2020, 1, 1)),  # naive treated as UTC
                M("undated", "subject"),
            ]
        },
        n_records=5,
    )
    cfg = DampingConfig(scoring="sum", recency_half_life_days=30.0, now=now)
    s = {h.record_id: h.score for h in entity_leg(["Aiko"], idx, k=5, damping=cfg).hits}
    assert s["undated"] > s["new"] > s["old"] > 0  # undated is neutral (1.0)
    off = {h.record_id: h.score for h in entity_leg(["Aiko"], idx, k=5).hits}
    assert len(set(off.values())) == 1  # recency off by default


def test_ties_deterministic_and_order_independent():
    ms = [M(f"r{i}", "subject") for i in range(6)]
    idx = EntityIndex({"Bram": ms, "Noor": list(reversed(ms))}, n_records=30)
    base = entity_leg(["Bram", "Noor"], idx, k=4)
    assert ids(base) == ["r0", "r1", "r2", "r3"]
    assert [h.rank for h in base.hits] == [1, 2, 3, 4]
    for perm in itertools.permutations(["Bram", "Noor", "Bram"]):
        assert entity_leg(list(perm), idx, k=4) == base
    shuffled = ms[:]
    random.Random(3).shuffle(shuffled)
    idx2 = EntityIndex({"Noor": shuffled, "Bram": shuffled}, n_records=30)
    assert entity_leg(["Noor", "Bram"], idx2, k=4) == base


def test_scores_positive_and_k_cut():
    idx = EntityIndex({"me": [M(f"r{i}") for i in range(5)]}, n_records=5, self_entity="me")
    leg = entity_leg(["me", "ghost"], idx, k=2)  # two query entities: self is damped but scored
    assert len(leg.hits) == 2
    assert all(h.score is not None and h.score > 0 for h in leg.hits)
    with pytest.raises(ValueError):
        entity_leg(["me"], idx, k=0)


def test_config_validation():
    for bad in (
        {"self_factor": 0.0},
        {"coverage_bonus": -1.0},
        {"recency_half_life_days": 0.0},
        {"recency_floor": 2.0},
        {"mem0_c": -1.0},
        {"cap": 0.0},
        {"boost": -1.0},
        {"scoring": "avg"},
        {"role_weights": {"subject": 1.0, "object": 0.0, "mention": 0.1}},
    ):
        with pytest.raises(ValueError):
            DampingConfig(**bad)


def test_leg_feeds_fusion():
    idx = EntityIndex({"Ilse": [M("r1", "subject"), M("r2")]}, n_records=4)
    leg = entity_leg(["Ilse"], idx, k=5)
    out = fuse([leg], FusionConfig())
    assert {c.record_id for c in out.candidates} == {"r1", "r2"}


def test_default_r08_mode_is_max_not_sum_and_has_no_coverage_bonus():
    idx = EntityIndex(
        {
            "Noor": [M("both", "subject"), M("onlyN", "subject")],
            "Aiko": [M("both", "subject"), M("onlyA", "subject")],
        },
        n_records=20,
    )
    assert DampingConfig().scoring == "max" and DampingConfig().mode == "mem0"
    res = entity_leg_explained(["Noor", "Aiko"], idx, k=5)
    s = {h.record_id: h.score for h in res.leg.hits}
    expected = 0.5 / 1.001  # degree 2: mem0 curve 1/(1+0.001), boost 0.5, subject weight 1.0
    assert s["both"] == pytest.approx(expected)
    assert s["both"] == pytest.approx(s["onlyN"])  # max, so a second entity adds nothing
    assert len(res.provenance["both"]) == 2


def test_default_r08_mode_cap_respected():
    idx = EntityIndex({"Noor": [M("r1", "subject")], "Aiko": [M("r1", "object")]}, n_records=5)
    hi = DampingConfig(boost=2.0, cap=0.8)  # uncapped boost would be 2.0
    s = entity_leg(["Noor", "Aiko"], idx, k=3, damping=hi).hits[0].score
    assert s == pytest.approx(0.8)
    low = DampingConfig(boost=0.5, cap=10.0)
    s = entity_leg(["Noor"], idx, k=3, damping=low).hits[0].score
    assert s == pytest.approx(0.5)
    assert all(h.score <= 0.5 for h in entity_leg(["Noor", "Aiko"], idx, k=3).hits)


def test_default_r08_mode_hub_damped_with_mem0_curve():
    idx = EntityIndex(
        {"Ilse": [M(f"r{i}", "subject") for i in range(101)], "Bram": [M("rB", "subject")]},
    )
    cfg = DampingConfig()
    hub = entity_leg(["Ilse"], idx, k=1, damping=cfg).hits[0].score
    rare = entity_leg(["Bram"], idx, k=1, damping=cfg).hits[0].score
    assert rare == pytest.approx(0.5)
    assert hub == pytest.approx(0.5 / (1 + 0.001 * 100**2))
