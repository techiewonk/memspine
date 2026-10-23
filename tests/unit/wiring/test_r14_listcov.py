"""R14-LISTCOV (R14-M-1, reader side of R14-M-11): list-depth trigger v2 and coverage select.

Model-free: pure-function tests on ``read/list_trigger.py``, and engine tests on the hash
embedder. Invented names only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.read.list_trigger import coverage_order, list_signal, profile_features
from memspine.read.pipeline import request as read_request
from memspine.read.pipeline.router import _coverage_reorder

FLAT = [0.80 - 0.004 * i for i in range(30)]
SHARP = [0.80, 0.55, 0.50, 0.45, 0.42, 0.40, 0.38, 0.36, 0.34, 0.33] + [0.3] * 20


def test_profile_features_flat_vs_sharp() -> None:
    flat, sharp = profile_features(FLAT), profile_features(SHARP)
    assert flat["r10"] > 0.9 > sharp["r10"] and flat["gap"] < sharp["gap"]
    assert profile_features([]) == {"r10": 0.0, "gap": 0.0, "log_n90": 0.0}


def test_listy_shape_with_flat_profile_fires() -> None:
    assert list_signal("What events has Ilse attended?", FLAT).fire


def test_single_fact_with_sharp_profile_does_not_fire() -> None:
    sig = list_signal("When did Bram move to the new flat in the city centre?", SHARP)
    assert not sig.fire and sig.score < 0.28


def test_profile_moves_the_decision() -> None:
    q = "What might Noor do after the festival ends this year"
    assert list_signal(q, FLAT).score > list_signal(q, SHARP).score


def test_signal_is_deterministic_and_model_free() -> None:
    q = "Which books did Tomasz read?"
    a, b = list_signal(q, FLAT), list_signal(q, FLAT)
    assert a == b and set(a.features) >= {"intent", "r10", "gap", "log_qlen"}


def test_coverage_order_penalises_same_session() -> None:
    items = list("abcdef")
    scores = [1.0, 0.95, 0.9, 0.85, 0.6, 0.55]
    groups = ["s1", "s1", "s1", "s2", "s3", "s3"]
    order = coverage_order(items, scores, groups, penalty=0.3)
    assert sorted(order) == list(range(6))
    assert order[0] == 0 and order.index(3) < order.index(2)


def test_coverage_order_identity_when_penalty_zero_or_tiny_pool() -> None:
    assert coverage_order([1, 2, 3], [3.0, 2.0, 1.0], ["a", "a", "a"], penalty=0.0) == [0, 1, 2]
    assert coverage_order([1, 2], [2.0, 1.0], ["a", "a"]) == [0, 1]


def test_coverage_order_defers_duplicates_to_the_tail() -> None:
    order = coverage_order(
        list("abcd"),
        [4.0, 3.0, 2.0, 1.0],
        ["s1", "s2", "s3", "s4"],
        penalty=0.1,
        keys=["x", "x", "y", "z"],
    )
    assert order == [0, 2, 3, 1]


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )


@asynccontextmanager
async def _started(**read: Any) -> AsyncIterator[Engine]:
    eng = _engine(**read)
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


def test_flags_default_off() -> None:
    read = ReadConfig()
    assert read.list_mode_trigger_v2 is False and read.coverage_select is False


async def test_trigger_v2_runs_on_hash_embedder_and_flag_off_keeps_regex() -> None:
    q = "What markets has Ilse visited?"
    async with _started(list_mode=True, list_mode_trigger_v2=True) as eng:
        for i in range(12):
            text = f"Ilse went to the market number {i}"
            await eng.write(text, namespace="a", memory_type="episodic")
        assert isinstance(await read_request._list_fires_v2(eng, "a", q), bool)
    async with _started(list_mode=True) as off:
        assert await off._list_fires(q) == off._list_mode_fires(q)


async def test_trigger_v2_falls_back_to_regex_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _started(list_mode=True, list_mode_trigger_v2=True) as eng:

        async def boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("down")

        monkeypatch.setattr(eng, "_vector_leg", boom)
        assert await read_request._list_fires_v2(eng, "a", "What activities does Bram do?") is True


async def test_coverage_reorder_keeps_members_and_scores() -> None:
    async with _started() as eng:
        for i, sess in enumerate(["s1", "s1", "s1", "s2", "s3"]):
            await eng.write(f"Noor note {i}", namespace="a", memory_type="episodic", group_id=sess)
        records = sorted(await eng._records("a"), key=lambda r: r.content)
    scored = [(r, 1.0 - 0.05 * i) for i, r in enumerate(records)]
    out = _coverage_reorder(scored, 0.3)
    assert {r.record_id: s for r, s in out} == {r.record_id: s for r, s in scored}
    assert len(out) == len(scored)
    assert [r.group_id for r, _ in out][:3] != ["s1", "s1", "s1"]


async def test_read_with_both_flags_on_runs() -> None:
    async with _started(list_mode=True, list_mode_trigger_v2=True, coverage_select=True) as eng:
        for i in range(8):
            await eng.write(f"Tomasz baked bread batch {i}", namespace="a", memory_type="episodic")
        res = await eng.read("What has Tomasz baked?", namespace="a", mode="retrieve")
        assert res is not None
