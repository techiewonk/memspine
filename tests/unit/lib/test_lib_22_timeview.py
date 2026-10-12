"""LIB-22: time view, two-axis as-of reads, three-valued handling, temporal-leg score."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from memspine.derive import temporal as lib01
from memspine.derive.integrate import CardinalityRegistry, apply_decision, integrate, replay
from memspine.read.timeview import (
    TimeView,
    TimeViewInput,
    apply,
    temporal_score,
)
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.assessment import Assessment, Support
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import Interval, TemporalRef, TemporalRelation, TimeAxes

SCOPE = OwnerScope(tenant="t1", owner="u1")
_n = itertools.count(1)


def dt(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def ref(day: str, precision: str = "day", *, approximate: bool = False) -> TemporalRef:
    return TemporalRef(
        at=dt(day),
        precision=precision,  # type: ignore[arg-type]
        approximate=approximate,
        basis="explicit",
    )


def make(
    value: str,
    *,
    start: TemporalRef | None = None,
    end: TemporalRef | None = None,
    event: Interval | None = None,
    learned: str | None = "2022-01-01",
    subject: str = "Ilse",
    attr: str = "residence",
    oid: str | None = None,
    status: str = "active",
) -> AssertionEnvelope:
    n = next(_n)
    if event is None and (start or end):
        event = Interval(start=start, end=end)
    time = TimeAxes(
        event=event,
        source_time=dt(learned) if learned else None,
        recorded_at=dt(learned) if learned else None,
    )
    return AssertionEnvelope(
        write_id=f"w{n}",
        assertion_key=AssertionKey(src=subject, rel=attr, dst=value, kind=attr),
        occurrence_id=oid or f"oc_{value}_{n}",
        scope=SCOPE,
        text=f"{subject} {attr} {value}",
        kind=attr,
        subject=Mention(surface=subject, mention_id=f"m{subject}"),
        attribute=attr,
        value=value,
        time=time,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id="s", source_version=1, seg_index=n),
                source_family="user",
            )
        ],
        assessment=Assessment(
            source_trust=0.9,
            binding="bound",
            modality="asserted",
            time_precision=time.event_precision,
            support=Support(),
        ),
        src_class="user_stated",
        status=status,  # type: ignore[arg-type]
    )


def registry() -> CardinalityRegistry:
    r = CardinalityRegistry()
    r.declare("t1", "residence", "one")
    return r


def replayer(recs):
    """The exact LIB-02 re-derivation the engine would inject."""
    return replay(recs, registry()).records


def store(*incoming: AssertionEnvelope) -> list[AssertionEnvelope]:
    """Integrate in the given arrival order, like the write path would."""
    reg = registry()
    cur: list[AssertionEnvelope] = []
    for a in incoming:
        cur = apply_decision(integrate(a, cur, reg), a, cur)
    return cur


def ilse() -> list[AssertionEnvelope]:
    groningen = make(
        "Groningen",
        start=ref("2015-01-01", "year"),
        end=ref("2019-12-31"),
        learned="2022-06-10",
        oid="oc_gron",
    )
    utrecht = make("Utrecht", start=ref("2020-01-01"), learned="2021-02-01", oid="oc_utr")
    berlin = make("Berlin", start=ref("2022-05-10"), learned="2022-06-10", oid="oc_ber")
    return store(utrecht, groningen, berlin)


def by_value(res):
    return {h.assertion.value: h for h in res.hits}


# ----------------------------------------------------------------- the two axes


def test_stored_state_fixture():
    recs = {e.value: e for e in ilse()}
    assert recs["Utrecht"].status == "superseded"
    assert recs["Utrecht"].time.event.end is not None
    assert recs["Berlin"].status == "active"


def test_true_at_march_2022_uses_event_time():
    march = TemporalRef(at=dt("2022-03-01"), precision="month", basis="explicit")
    res = apply(TimeView.at_valid(march), ilse(), replayer)
    assert [h.assertion.value for h in res.hits] == ["Utrecht"]
    assert not res.hits[0].uncertain_time and res.hits[0].valid is True
    assert len(res.out_of_window) == 2  # Groningen ended, Berlin not yet started


def test_known_at_first_april_ignores_late_records_and_reopens_utrecht():
    res = apply(TimeView.at_known(dt("2022-04-01")), ilse(), replayer)
    assert [h.assertion.value for h in res.hits] == ["Utrecht"]
    h = res.hits[0]
    assert h.assertion.status == "active" and h.assertion.time.event.end is None
    assert [(v.attribute, v.value) for v in res.live] == [("residence", "Utrecht")]
    # Groningen is long past in event time but out because it was only learned in June
    assert len(res.out_of_window) == 2


def test_known_after_everything_matches_current():
    res = apply(TimeView.at_known(dt("2023-01-01")), ilse(), replayer)
    assert [v.value for v in res.live] == ["Berlin"]
    assert res.out_of_window == ()


def test_true_at_and_known_at_differ_on_the_same_date():
    when = dt("2022-06-01")
    valid_only = apply(TimeView.at_valid(when), ilse(), replayer)
    both = apply(TimeView.both(when, dt("2022-04-01")), ilse(), replayer)
    assert [h.assertion.value for h in valid_only.hits] == ["Berlin"]
    # as known on 1 April, Utrecht was still the live value in June
    assert [h.assertion.value for h in both.hits] == ["Utrecht"]
    assert [v.value for v in both.live] == ["Utrecht"]


def test_both_known_late_enough_sees_the_correction():
    res = apply(TimeView.both(dt("2022-06-01"), dt("2022-07-01")), ilse(), replayer)
    assert [h.assertion.value for h in res.hits] == ["Berlin"]


def test_current_live_values_and_change_history():
    res = apply(TimeView.current(), ilse(), replayer)
    assert [(v.subject, v.attribute, v.value) for v in res.live] == [
        ("ilse", "residence", "Berlin")
    ]
    assert [h.assertion.value for h in res.hits] == ["Berlin"]
    assert len(res.out_of_window) == 2
    assert [e.value for e in res.history[0].entries] == ["Groningen", "Utrecht", "Berlin"]


def test_history_order_independent_of_arrival():
    recs = ilse()
    for perm in itertools.permutations(recs):
        res = apply(TimeView.history(), list(perm), replayer)
        vals = [e.value for e in res.history[0].entries]
        assert vals == ["Groningen", "Utrecht", "Berlin"]
        assert [h.assertion.value for h in res.hits] == vals
    assert res.history[0].entries[1].end is not None  # Utrecht closed by Berlin


def test_a_b_a_is_kept_as_three_entries():
    recs = store(
        make("Utrecht", start=ref("2018-01-01"), learned="2022-01-01", oid="a1"),
        make("Berlin", start=ref("2020-01-01"), learned="2022-01-02", oid="b1"),
        make("Utrecht", start=ref("2022-01-01"), learned="2022-01-03", oid="a2"),
    )
    res = apply(TimeView.history(), recs, replayer)
    assert [e.value for e in res.history[0].entries] == ["Utrecht", "Berlin", "Utrecht"]


# ----------------------------------------------------------------- three-valued time


def test_approximate_date_straddling_boundary_is_undecidable_not_certain():
    around = make(
        "Oslo",
        start=ref("2021-01-01", "year", approximate=True),
        end=ref("2021-01-01", "year", approximate=True),
    )
    exact = make("Rome", start=ref("2021-01-01", "year"), end=ref("2021-01-01", "year"), oid="rome")
    res = apply(TimeView.at_valid(dt("2022-01-10")), [around, exact])
    hits = by_value(res)
    assert hits["Oslo"].window == "undecidable" and hits["Oslo"].uncertain_time
    assert hits["Oslo"].valid is None
    assert "Rome" not in hits and len(res.out_of_window) == 1
    assert res.live == ()


def test_coarse_precision_covering_period_is_certain():
    year = make(
        "Oslo", event=Interval(start=ref("2022-01-01", "year"), end=ref("2022-01-01", "year"))
    )
    march = TemporalRef(at=dt("2022-03-01"), precision="month", basis="explicit")
    res = apply(TimeView.at_valid(march), [year])
    assert res.hits[0].window == "in" and res.hits[0].score == 1.0


def test_relation_without_date_and_undated_are_flagged_not_dropped():
    rel = TemporalRef(
        basis="relation", relation=TemporalRelation(relation="after", target_assertion_id="x1")
    )
    after_rel = make("Delft", event=Interval(start=rel))
    undated = make("Breda", learned="2022-01-01")
    res = apply(TimeView.at_valid(dt("2022-03-15")), [after_rel, undated])
    assert {h.assertion.value for h in res.hits} == {"Delft", "Breda"}
    assert all(h.uncertain_time and h.window == "undecidable" for h in res.hits)
    assert all(h.score is None for h in res.hits)
    assert res.out_of_window == ()


def test_range_mode_and_unstamped_known_at():
    a = make("Utrecht", start=ref("2020-01-01"), end=ref("2020-12-31"), oid="ra")
    b = make("Berlin", start=ref("2022-05-10"), oid="rb")
    res = apply(TimeView.between(dt("2020-06-01"), dt("2020-07-01")), [a, b])
    assert [h.assertion.value for h in res.hits] == ["Utrecht"]
    unstamped = make("Zwolle", learned=None, oid="rz")
    res2 = apply(TimeView.at_known(dt("2022-01-01")), [unstamped, a])
    assert {h.assertion.value: h.window for h in res2.hits} == {
        "Zwolle": "undecidable",
        "Utrecht": "in",
    }
    assert res2.live == ()  # Utrecht is closed; the undecidable one is never reported as live


def test_hidden_records_are_out_in_every_mode():
    h = make("Ghost", start=ref("2020-01-01"), status="hidden", oid="rh")
    for view in (TimeView.current(), TimeView.history(), TimeView.at_known(dt("2030-01-01"))):
        res = apply(view, [h])
        assert res.hits == () and res.out_of_window == ("rh",)


# ----------------------------------------------------------------- score


def test_temporal_score_values():
    target = TimeView.at_valid(dt("2022-03-15"))
    inside = make("A", start=ref("2022-03-01"), end=ref("2022-03-31"))
    outside = make("B", start=ref("2019-01-01"), end=ref("2019-12-31"))
    approx = make(
        "C",
        start=ref("2021-01-01", "year", approximate=True),
        end=ref("2021-01-01", "year", approximate=True),
    )
    none = make("D")
    assert temporal_score(inside, target) == 1.0
    assert temporal_score(outside, target) == 0.0
    assert temporal_score(approx, TimeView.at_valid(dt("2022-01-10"))) == 0.5
    assert temporal_score(none, target) is None
    assert temporal_score(inside, TimeView.current()) is None
    assert temporal_score(inside, TimeView.at_known(dt("2022-03-15"))) is None


def test_temporal_score_range_coverage_is_fractional():
    a = make("A", start=ref("2022-03-01"), end=ref("2022-03-10"))
    s = temporal_score(a, TimeView.between(dt("2022-03-01"), dt("2022-03-21")))
    assert s is not None and 0.0 < s < 1.0
    assert s == pytest.approx(10 / 20)


# ----------------------------------------------------------------- determinism and request


def test_deterministic_and_input_order_independent():
    recs = ilse()
    views = [
        TimeView.current(),
        TimeView.history(),
        TimeView.at_valid(dt("2022-03-01")),
        TimeView.at_known(dt("2022-04-01")),
        TimeView.both(dt("2022-06-01"), dt("2022-04-01")),
    ]
    for v in views:
        base = apply(v, recs, replayer)
        assert apply(v, recs, replayer) == base
        for perm in itertools.permutations(recs):
            assert apply(v, list(perm), replayer) == base


def test_inputs_are_not_mutated():
    recs = ilse()
    snap = [e.model_dump() for e in recs]
    apply(TimeView.both(dt("2022-06-01"), dt("2022-04-01")), recs, replayer)
    assert [e.model_dump() for e in recs] == snap


def test_from_contract():
    f = TimeView.from_contract
    assert f(TimeViewInput()).mode == "current"
    assert f(TimeViewInput(scope="as_of")).mode == "current"  # no date: fall back
    assert f(TimeViewInput(scope="history")).mode == "history"
    assert f(TimeViewInput(scope="as_of", as_of_valid=dt("2022-03-01"))).mode == "true_at"
    assert f(TimeViewInput(as_of_known=dt("2022-03-01"))).mode == "known_at"
    both = f(TimeViewInput(as_of_valid=dt("2022-03-01"), as_of_known=dt("2022-04-01")))
    assert both.mode == "both"
    assert f(TimeViewInput(scope="range", range_start=dt("2022-01-01"))).mode == "range"


def test_request_validation():
    with pytest.raises(ValueError):
        TimeView(mode="true_at")
    with pytest.raises(ValueError):
        TimeView(mode="current", known=dt("2022-01-01"))
    with pytest.raises(ValueError):
        TimeView.between(dt("2022-02-01"), dt("2022-01-01"))
    with pytest.raises(ValueError):
        TimeView.between(None, None)


# ----------------------------------------------------------------- default re-derivation, parity


def test_default_rederivation_without_injected_replay_agrees_on_the_fixture():
    for view in (
        TimeView.at_known(dt("2022-04-01")),
        TimeView.at_known(dt("2023-01-01")),
        TimeView.both(dt("2022-06-01"), dt("2022-04-01")),
        TimeView.both(dt("2022-06-01"), dt("2022-07-01")),
    ):
        injected = apply(view, ilse(), replayer)
        default = apply(view, ilse())
        assert [(h.record_id, h.window) for h in default.hits] == [
            (h.record_id, h.window) for h in injected.hits
        ]
        assert default.live == injected.live
        assert default.out_of_window == injected.out_of_window


def test_local_overlap_matches_derive_temporal():
    precisions = ["instant", "minute", "hour", "day", "week", "month", "season", "year", "decade"]
    days = ["2019-12-31", "2020-01-01", "2021-07-15", "2022-03-10", "2022-12-01"]
    refs = [ref(d, p, approximate=a) for d in days for p in precisions for a in (False, True)]
    from memspine.read import timeview as tv

    for a, b in itertools.product(refs[::3], refs[1::4]):
        assert tv._overlaps(a, b) == lib01.overlaps(a, b)
    for r in refs[::5]:
        for t in (dt("2020-06-01"), dt("2022-03-15"), dt("2018-01-01")):
            assert tv._overlaps(r, t) == lib01.overlaps(r, t)
    rel = TemporalRef(
        at=dt("2022-01-01"),
        precision="day",
        basis="relation",
        anchor=dt("2021-12-01"),
        relation=TemporalRelation(relation="after", target_assertion_id="x"),
    )
    for t in (dt("2021-06-01"), dt("2023-06-01")):
        assert tv._overlaps(rel, t) == lib01.overlaps(rel, t)
