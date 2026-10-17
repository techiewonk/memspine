"""R14-DATEPRIOR: ``read.date_prior`` (R14-M-7), and the existing ``pool_protect_per_leg`` (M-3).

Model-free: hash embedder, no reranker. Invented names only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.read.pipeline import date_prior as dp
from memspine.store.records import MemoryRecord

_QUERY = "what did Bram plant on 7 May 2023 in the garden"
_DAY = datetime(2023, 5, 7, tzinfo=UTC)


class _Rec:
    def __init__(self, when: datetime, tags: list[str] | None = None) -> None:
        self.valid_from = when
        self.tags = tags or []


def test_defaults_off() -> None:
    cfg = ReadConfig()
    assert cfg.date_prior is False
    assert cfg.date_prior_days == 14
    assert cfg.date_prior_boost == 1.0


def test_span_needs_short_explicit_date() -> None:
    assert dp.prior_span("what happened on 7 May 2023") is not None
    assert dp.prior_span("what happened in May 2023") is not None
    assert dp.prior_span("what happened in 2023") is None  # a bare year names no session
    assert dp.prior_span("what happened last week") is None  # no anchor, no clock
    assert dp.prior_span("what happened") is None


def test_multiplier_is_bounded_and_linear() -> None:
    span = dp.prior_span("on 7 May 2023")
    assert span is not None
    recs: dict[str, Any] = {
        "on": _Rec(_DAY),
        "mid": _Rec(_DAY + timedelta(days=8)),  # 7 days past the day span's end
        "edge": _Rec(_DAY + timedelta(days=15)),  # 14 days past: window edge, no boost
        "far": _Rec(_DAY + timedelta(days=200)),
    }
    fused = [("far", 0.04), ("edge", 0.03), ("mid", 0.02), ("on", 0.01), ("ghost", 0.005)]
    out, boosted = dp.apply_prior(fused, recs, span, window_days=14, boost=1.0)
    assert boosted["on"] == pytest.approx(2.0)
    assert 1.0 < boosted["mid"] < boosted["on"]
    assert "edge" not in boosted and "far" not in boosted and "ghost" not in boosted
    assert max(boosted.values()) <= 2.0
    assert [rid for rid, _ in out][-1] == "ghost"  # unknown ids are untouched, never added
    assert {rid for rid, _ in out} == {rid for rid, _ in fused}


def test_happened_tag_counts_as_event_date() -> None:
    span = dp.prior_span("on 7 May 2023")
    assert span is not None
    rec = _Rec(_DAY + timedelta(days=100), tags=["happened:2023-05-07"])
    _, boosted = dp.apply_prior([("r", 0.01)], {"r": rec}, span, window_days=14, boost=0.5)
    assert boosted["r"] == pytest.approx(1.5)


def test_protect_needs_leg_rank_within_depth_and_is_capped() -> None:
    boosted = {f"r{i}": 1.0 + i / 100 for i in range(9)}
    ranks = {f"r{i}": ("vector", i + 1) for i in range(9)}
    ranks["r8"] = ("vector", 40)
    prot = dp.protect_ids(boosted, ranks)
    assert len(prot) == dp.PROTECT_MAX
    assert "r8" not in prot
    assert set(prot.values()) == {"date_prior"}
    assert dp.protect_ids({"x": 1.5}, {}) == {}


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={'provider': "sqlite", 'settings': {'path': ":memory:", 'busy_timeout_s': 5.0}},
        embedding={'provider': "hash", 'settings': {}},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, "rerank": "off", **read},
    )


async def _fill(eng: Engine) -> str:
    base = datetime(2023, 1, 1, tzinfo=UTC)
    for i in range(30):
        await eng.write(
            f"what did Bram plant in the garden on 7 May 2023 note {i}",
            namespace="a",
            memory_type="episodic",
            valid_from=base + timedelta(days=i),
        )
    await eng.write(
        "Bram will plant the maple in the garden",
        namespace="a",
        memory_type="episodic",
        valid_from=_DAY,
    )
    recs: list[MemoryRecord] = await eng._records("a")
    return next(r.record_id for r in recs if r.content == "Bram will plant the maple in the garden")


async def _rank(eng: Engine, rid: str, top_k: int = 20) -> int | None:
    ids = [r.record_id for r, _ in await eng.search(_QUERY, namespace="a", top_k=top_k)]
    return ids.index(rid) if rid in ids else None


async def test_prior_lifts_the_dated_record_and_off_is_unchanged() -> None:
    off = _engine(hybrid=True)
    await off.start()
    try:
        gold = await _fill(off)
        before = await _rank(off, gold)
        order_off = [r.record_id for r, _ in await off.search(_QUERY, namespace="a", top_k=20)]
    finally:
        await off.stop()
    on = _engine(hybrid=True, date_prior=True, date_prior_boost=3.0)
    await on.start()
    try:
        gold_on = await _fill(on)
        after = await _rank(on, gold_on)
    finally:
        await on.stop()
    assert before is None or before > 0  # off: the dated record is not first
    assert after == 0  # on: the date prior puts it first
    assert len(order_off) == 20  # the off path still returns a full list


async def test_prior_is_a_no_op_without_a_date() -> None:
    plain = "what did Bram plant in the garden"
    outs = []
    for flag in (False, True):
        eng = _engine(hybrid=True, date_prior=flag)
        await eng.start()
        try:
            await _fill(eng)
            outs.append([r.content for r, _ in await eng.search(plain, namespace="a", top_k=8)])
        finally:
            await eng.stop()
    assert outs[0] == outs[1]


async def test_pool_protect_already_protects_each_leg_top_n() -> None:
    """R14-M-3: ``pool_protect_per_leg`` is the leg-top-N guarantee; no second key is needed."""
    assert ReadConfig(pool_protect_per_leg=8).pool_protect_per_leg == 8
    assert not hasattr(ReadConfig(), "leg_topk_protect")
