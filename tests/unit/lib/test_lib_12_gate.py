from __future__ import annotations

from typing import Any

from memspine.read.gate import (
    GateConfig,
    GateContract,
    GateItem,
    ProbeObservation,
    StoreCalibration,
    assess,
)

_TOPS = (0.10, 0.12, 0.08, 0.15, 0.11, 0.13, 0.09, 0.14)


def _cal(scorer: str = "rr", tops: tuple[float, ...] = _TOPS) -> StoreCalibration:
    return StoreCalibration.from_observations(
        [ProbeObservation(query=f"q{i}", scorer=scorer, top_score=t) for i, t in enumerate(tops)]
    )


def _it(i: str, s: float | None, **kw: Any) -> GateItem:
    return GateItem(item_id=i, score=s, scorer="rr", **kw)


def _thr(cal: StoreCalibration, margin: float) -> float:
    st = cal.stats("rr")
    assert st is not None
    return st.threshold(margin)


def test_unanswerable_is_missing() -> None:
    d = assess([_it("a", 0.05), _it("b", 0.1)], _cal())
    assert d.state == "missing" and d.admitted_ids == ()
    assert d.assessments[0].strong_threshold is not None


def test_empty_candidates_missing() -> None:
    assert assess([], _cal()).state == "missing"


def test_strong_single_hit_answerable_with_thresholds() -> None:
    d = assess([_it("a", 0.9), _it("b", 0.05)], _cal())
    assert d.state == "answerable" and d.admitted_ids == ("a",)
    a = d.assessments[0]
    assert a.score == 0.9 and a.strong_threshold is not None and a.strong_threshold < 0.9
    assert d.config.margin_sd == 1.0


def test_threshold_is_store_relative() -> None:
    low = _cal(tops=(0.1, 0.12, 0.11, 0.1, 0.13))
    high = _cal(tops=(0.7, 0.72, 0.71, 0.7, 0.73))
    assert assess([_it("a", 0.5)], low).state == "answerable"
    assert assess([_it("a", 0.5)], high).state == "missing"


def test_conflicting_keeps_both() -> None:
    items = [
        _it("a", 0.9, key="city", value="Paris"),
        _it("b", 0.85, key="city", value="  berlin "),
        _it("c", 0.8, key="job", value="chef"),
    ]
    d = assess(items, _cal())
    assert d.state == "conflicting"
    assert d.conflicts[0].key == "city" and set(d.conflicts[0].item_ids) == {"a", "b"}
    assert {"a", "b"} <= set(d.admitted_ids)


def test_same_value_or_noncurrent_is_not_conflict() -> None:
    same = [_it("a", 0.9, key="k", value="X"), _it("b", 0.9, key="k", value="x")]
    assert assess(same, _cal()).state == "answerable"
    old = [_it("a", 0.9, key="k", value="X"), _it("b", 0.9, key="k", value="Y", current=False)]
    assert assess(old, _cal()).state == "answerable"


def test_weak_band_low_confidence() -> None:
    cal = _cal()
    mid = (_thr(cal, 0.0) + _thr(cal, 1.0)) / 2
    d = assess([_it("a", mid)], cal)
    assert d.state == "weak" and d.low_confidence and d.admitted_ids == ("a",)


def test_generic_question_with_weak_personal_hits_is_missing() -> None:
    cal = _cal()
    s = _thr(cal, 1.0) + 0.001  # strong for a normal query, below the strict bar
    items = [_it("p", s, personal=True)]
    assert assess(items, cal, contract=GateContract(personal=True)).state == "answerable"
    assert assess(items, cal, contract=GateContract(personal=None)).state == "answerable"
    d = assess(items, cal, contract=GateContract(personal=False))
    assert d.state == "missing" and d.dropped_ids == ("p",)
    strict = _thr(cal, 3.0) + 0.01
    d2 = assess([_it("p", strict, personal=True)], cal, contract=GateContract(personal=False))
    assert d2.state == "answerable"
    d3 = assess([_it("g", s, personal=False)], cal, contract=GateContract(personal=False))
    assert d3.state == "answerable"


def test_bypass_recorded() -> None:
    items = [_it("a", 0.01), _it("b", 0.02)]
    for kind in ("enumeration", "show_all", "as_of"):
        d = assess(items, _cal(), contract=GateContract(request_kind=kind))  # type: ignore[arg-type]
        assert d.state == "bypass" and d.bypass_reason == kind and d.admitted_ids == ("a", "b")
    d = assess(items, _cal(), contract=GateContract(named_reference=True))
    assert d.bypass_reason == "named_reference"
    d = assess(items, _cal(), contract=GateContract(time_view="as_of"))
    assert d.bypass_reason == "as_of"
    assert assess([], _cal(), contract=GateContract(request_kind="show_all")).state == "missing"


def test_unscored_never_zero() -> None:
    d = assess([_it("a", None)], _cal())
    assert d.state == "weak" and d.low_confidence and d.assessments[0].band == "unscored"
    d = assess([_it("a", None), _it("b", 0.9)], _cal())
    assert d.state == "answerable" and d.admitted_ids == ("b",)
    base = _cal()
    more = base.with_observations([ProbeObservation(scorer="rr", top_score=None)] * 10)
    st, st2 = base.stats("rr"), more.stats("rr")
    assert st and st2 and st2.n == st.n and st2.n_unscored == 10 and st2.p95 == st.p95
    nan = base.with_observations([ProbeObservation(scorer="rr", top_score=float("nan"))])
    nst = nan.stats("rr")
    assert nst is not None and st is not None and nst.n == st.n and nst.n_unscored == 1


def test_uncalibrated_scorer_fails_open() -> None:
    d = assess([_it("a", 0.0)], StoreCalibration())
    assert d.state == "weak" and d.assessments[0].band == "uncalibrated"


def test_incremental_refresh_and_window() -> None:
    cal = StoreCalibration.from_observations(
        [ProbeObservation(scorer="s", top_score=float(i)) for i in range(10)], max_samples=5
    )
    st = cal.stats("s")
    assert st is not None and st.samples == (5.0, 6.0, 7.0, 8.0, 9.0)
    cal2 = cal.with_observations([ProbeObservation(scorer="s", top_score=10.0)])
    st2 = cal2.stats("s")
    assert st2 is not None and st2.samples[-1] == 10.0
    assert st.samples[-1] == 9.0  # original is immutable


def test_scorers_calibrated_separately() -> None:
    cal = _cal("rr").with_observations(
        [ProbeObservation(scorer="fuse", top_score=0.02 + i * 0.001) for i in range(6)]
    )
    d = assess([GateItem(item_id="a", score=0.5, scorer="fuse"), _it("b", 0.9)], cal)
    assert {a.scorer for a in d.assessments} == {"fuse", "rr"}
    assert d.state == "answerable"


def test_custom_thresholds_and_determinism() -> None:
    cfg = GateConfig(margin_sd=100.0)
    d = assess([_it("a", 0.9)], _cal(), config=cfg)
    assert d.state != "answerable" and d.config.margin_sd == 100.0
    assert assess([_it("a", 0.9)], _cal()) == assess([_it("a", 0.9)], _cal())
