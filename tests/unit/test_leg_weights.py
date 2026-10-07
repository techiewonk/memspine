"""N16 (plan v3.2): per-leg RRF weights and the recency-only leg."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.temporal_query import LegHit
from memspine.services.lexical.base import rrf_fuse


def _hits(*ids: str) -> list[LegHit]:
    return [LegHit(i, 1.0) for i in ids]


def test_rrf_weights_shift_the_fusion() -> None:
    vec, lex = _hits("a", "b"), _hits("b", "a")
    plain = rrf_fuse(vec, lex)
    assert [r for r, _ in plain] == ["a", "b"]  # tie broken by the vector leg
    lexical_heavy = rrf_fuse(vec, lex, weights=[1.0, 3.0])
    assert [r for r, _ in lexical_heavy] == ["b", "a"]
    assert rrf_fuse(vec, lex, weights=None) == plain


def test_defaults_off() -> None:
    assert ReadConfig().leg_weights == {}
    assert ReadConfig().recency_leg is False


async def _top(**read: Any) -> str:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )
    await eng.start()
    try:
        t0 = datetime(2023, 1, 1, tzinfo=UTC)
        for day, text in enumerate(["rated the thriller 5 stars", "rated a comedy 2 stars"]):
            await eng.write(
                text,
                namespace="a",
                memory_type="episodic",
                valid_from=t0 + timedelta(days=30 * day),
            )
        hits = await eng.search("rated the thriller", namespace="a", top_k=2)
        return hits[0][0].content
    finally:
        await eng.stop()


async def test_recency_leg_with_a_heavy_extra_weight_prefers_the_newest() -> None:
    assert await _top() == "rated the thriller 5 stars"
    newest = await _top(recency_leg=True, leg_weights={"vector": 0.1, "extra": 5.0})
    assert newest == "rated a comedy 2 stars"
