"""I75a/I75b/I78: leg-protected pool, perspective as a multiplier only, pool-cut logging."""

from __future__ import annotations

from typing import Any

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.engine import _leg_rank_map, _protected_ids, search_forensics

_POOL = 5
_GOLD = "zyzzyva"
_QUERY = f"garden travel garden travel {_GOLD}"


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )


async def _state(**read: Any) -> tuple[bool, int, dict[str, Any]]:
    """(gold in pool, gold fused rank, forensics) for a store whose only BM25-only hit is the gold."""
    eng = _engine(**read)
    await eng.start()
    try:
        for i in range(40):
            await eng.write(
                f"garden travel plans note {i} garden travel garden travel",
                namespace="a",
                memory_type="episodic",
            )
        await eng.write(
            f"a {_GOLD} appeared once between many other unrelated words today",
            namespace="a",
            memory_type="episodic",
        )
        gold_id = next(r.record_id for r in await eng._records("a") if _GOLD in r.content)
        with search_forensics() as fx:
            await eng.search(_QUERY, namespace="a", top_k=_POOL)
        fx["gold_id"] = gold_id
        in_pool = any(rid == gold_id for rid, _ in fx["pool"])
        cut = [c for c in fx.get("cuts", []) if c["reason"] == "pool_cut" and c["id"] == gold_id]
        return in_pool, (cut[0]["fused_rank"] if cut else 0), fx
    finally:
        await eng.stop()


def test_helpers_pick_top_n_per_leg_and_best_rank() -> None:
    class H:
        def __init__(self, rid: str) -> None:
            self.record_id = rid

    legs = [("vector", [H("a"), H("b"), H("c")]), ("lexical", [H("z"), H("a")])]
    assert _protected_ids(legs, 0) == {}
    assert _protected_ids(legs, 2) == {"a": "vector", "b": "vector", "z": "lexical"}
    assert _leg_rank_map(legs)["a"] == ("vector", 1)
    assert _leg_rank_map(legs)["z"] == ("lexical", 1)


async def test_bm25_only_gold_survives_with_protect() -> None:
    off_in, off_rank, _ = await _state()
    assert not off_in  # control: outside the pool without protection
    assert off_rank > _POOL  # and the cut says where it stood
    on_in, _, fx = await _state(pool_protect_per_leg=1)
    assert on_in
    assert fx["protected"][fx["gold_id"]] == "lexical"


async def test_pool_stays_bounded() -> None:
    n = 3
    _, _, fx = await _state(pool_protect_per_leg=n)
    legs = 2 + len(fx["extra_legs"])
    assert len(fx["pool"]) <= _POOL + legs * n
    assert len(fx["protected"]) <= legs * n


async def test_default_is_off() -> None:
    _, _, fx = await _state()
    assert "protected" not in fx
    assert len(fx["pool"]) <= _POOL


async def test_pool_cuts_are_logged_with_ranks() -> None:
    _, _, fx = await _state()
    gold = [c for c in fx["cuts"] if c["reason"] == "pool_cut" and c["id"] == fx["gold_id"]]
    assert gold
    assert gold[0]["fused_rank"] > _POOL
    assert gold[0]["best_leg"] == "lexical"
    assert gold[0]["best_leg_rank"] == 1
    assert gold[0]["pool"] == _POOL


async def test_perspective_multiplier_only_adds_no_leg() -> None:
    names: dict[bool, list[str]] = {}
    for leg in (True, False):
        eng = Engine(
            template="core",
            dotenv_path=None,
            storage={"path": ":memory:"},
            embedding={"provider": "hash"},
            memories={"episodic": {"enabled": True, "policies": {"perspective": "heuristic"}}},
            read={
                "record_access": False,
                "perspective_mode": "subject_weight",
                "perspective_leg": leg,
            },
        )
        await eng.start()
        try:
            for turn in ("Caroline: I like garden work", "Melanie: I like travel plans"):
                await eng.write(turn, namespace="a", memory_type="episodic")
            with search_forensics() as fx:
                await eng.search("What does Caroline like?", namespace="a", top_k=5)
            names[leg] = [n for n, _ in fx["extra_legs"]]
            assert "perspective" in fx  # the multiplier ran in both modes
        finally:
            await eng.stop()
    assert "perspective" not in names[False]
    assert "perspective" in names[True]


def test_defaults() -> None:
    assert ReadConfig().perspective_leg is True
    assert ReadConfig().pool_protect_per_leg == 0
