"""I19: no user-B record is ever returned to user A, under every retrieval path (offline)."""

from __future__ import annotations

import pytest
from memspine_evals.leakage import CONFIGS, LeakReport, ProbeResult, probe, run_all


@pytest.mark.parametrize("name", sorted(CONFIGS))
async def test_no_cross_user_leak(name: str) -> None:
    res = await probe(name, CONFIGS[name])
    assert not res.errors, res.errors
    assert res.leaked_records == 0 and not res.leaked_canaries, f"P0 LEAK under {name}: {res}"
    assert res.own_records_returned > 0, f"vacuous probe under {name}: user A got nothing back"


async def test_report_aggregates_and_flags_a_leak() -> None:
    report = await run_all({"baseline": {}, "list_mode": CONFIGS["list_mode"]})
    assert not report.leaked and not report.vacuous
    assert "| baseline |" in report.markdown()
    bad = LeakReport([ProbeResult("x", records_returned=4, leaked_records=1)])
    assert bad.leaked and bad.to_dict()["severity"] == "P0" and bad.results[0].leak_rate == 0.25


async def test_probe_exercises_the_list_bridge_session_and_rerank_paths() -> None:
    """Guards against a vacuous pass: the legs the probe claims to cover really ran."""
    from memspine.engine import search_forensics
    from memspine_evals import leakage as lk

    lk._register_reranker()
    eng = lk._make_engine(CONFIGS["rerank_bridge_list"])
    await eng.start()
    try:
        await lk._seed(eng, lk.NS_A, "zqa")
        await lk._seed(eng, lk.NS_B, "zqb")
        with search_forensics() as fx:
            await eng.read("What hobbies does Sam enjoy?", namespace=lk.NS_A, mode="replay")
        legs = {name for name, _ in fx.get("extra_legs", [])}
        assert {"session", "speaker_vote", "bridge"} <= legs
        assert fx.get("reranker") == "leak_probe_const"
    finally:
        await eng.stop()


async def test_probe_detects_a_planted_leak() -> None:
    """The probe is not blind: an engine whose search ignores the namespace is caught."""
    from memspine import Engine

    class Leaky:
        def __init__(self, inner: Engine) -> None:
            self._inner = inner

        def __getattr__(self, item: str):
            return getattr(self._inner, item)

        async def search(self, query: str, namespace: str = "default", **kw):
            other = "user-b" if namespace == "user-a" else "user-a"
            return await self._inner.search(query, namespace=other, **kw)

    def factory(read):
        from memspine_evals.leakage import _make_engine

        return Leaky(_make_engine(read))

    res = await probe("planted", {}, engine_factory=factory, modes=())
    assert res.leaked_records > 0
