"""LIB-44: explicit feedback events, assertion drafts, correction latency, observer views."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from memspine.derive.feedback import (
    Actor,
    ExposureRecord,
    FeedbackError,
    FeedbackEvent,
    ObservableRecord,
    check_feedback,
    correction_latency,
    observer_view,
    to_assertions,
)
from memspine.derive.integrate import CardinalityRegistry, integrate
from memspine.schemas.durable import (
    AssertionEnvelope,
    AssertionKey,
    Assessment,
    Evidence,
    OwnerScope,
    Support,
    TimeAxes,
    TurnRef,
)
from memspine.trust.horizon import Grant

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
TURN = TurnRef(source_id="s1", source_version=1, seg_index=4)


def target(
    value: str = "Bram", trust: float = 0.6, owner: str = "Ilse", rid: str | None = None
) -> AssertionEnvelope:
    return AssertionEnvelope(
        write_id="wr_x",
        assertion_key=AssertionKey(src="Ilse", rel="lives_with", kind="state"),
        occurrence_id="oc_x",
        record_id=rid,
        scope=OwnerScope(tenant="t1", owner=owner),
        text=f"Ilse lives with {value}",
        kind="state",
        attribute="lives_with",
        value=value,
        time=TimeAxes(source_time=NOW),
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id="s0", source_version=1, seg_index=1),
                source_family="assistant:bot",
            )
        ],
        assessment=Assessment(
            source_trust=trust,
            support=Support(independent=0),
            binding="bound",
            modality="asserted",
        ),
        src_class="assistant_proposed",
    )


def event(kind: str = "correction", **kw) -> FeedbackEvent:
    base = dict(
        event_id="fb1",
        kind=kind,
        channel="user_message",
        target_ids=("r1",),
        actor=Actor(principal="Ilse", kind="user"),
        owner="Ilse",
        at=NOW,
        turn=10,
        source=TURN,
    )
    if kind == "correction":
        base["new_value"] = "Noor"
    if kind == "rating":
        base.update(rating=2, source=None)
    base.update(kw)
    return FeedbackEvent(**base)


# ---- explicit only


@pytest.mark.parametrize(
    "channel", ["silence", "engagement", "dwell", "regeneration", "classifier", "implicit"]
)
def test_inferred_channels_rejected(channel):
    with pytest.raises(ValidationError, match="inferred"):
        event(channel=channel)


def test_unknown_channel_rejected():
    with pytest.raises(ValidationError, match="unknown feedback channel"):
        event(channel="telepathy")


@pytest.mark.parametrize("channel", ["user_message", "ui_control", "operator_edit", "api"])
def test_explicit_channels_accepted(channel):
    assert event(channel=channel).channel == channel


def test_shape_rules():
    with pytest.raises(ValidationError):
        event(new_value=None)
    with pytest.raises(ValidationError):
        event("rejection", new_value="x")
    with pytest.raises(ValidationError):
        event(target_ids=("a", "b"))
    with pytest.raises(ValidationError):
        event("confirmation", source=None)
    with pytest.raises(ValidationError):
        event("rating", rating=None)
    with pytest.raises(ValidationError):
        event("confirmation", rating=3)
    with pytest.raises(ValidationError):
        event("rejection", target_ids=("a", "a"))
    with pytest.raises(ValidationError):
        event(at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        event(target_ids=())
    with pytest.raises(ValidationError):
        event(not_a_field=1)


# ---- permissions


def test_owner_may_give_feedback():
    check_feedback(event())


def test_other_user_refused():
    with pytest.raises(FeedbackError, match="not the memory owner"):
        check_feedback(event(actor=Actor(principal="Tomasz", kind="user")))


def test_agent_self_confirm_refused():
    ev = event("confirmation", actor=Actor(principal="agent-1", kind="agent"))
    with pytest.raises(FeedbackError, match="agent cannot"):
        to_assertions(ev, {"r1": target()})


def test_agent_claiming_owner_principal_still_refused():
    with pytest.raises(FeedbackError):
        check_feedback(event("confirmation", actor=Actor(principal="Ilse", kind="agent")))


def test_operator_must_be_registered_and_cannot_confirm():
    op = Actor(principal="Aiko", kind="operator")
    with pytest.raises(FeedbackError, match="not a registered operator"):
        check_feedback(event(actor=op, channel="operator_edit"))
    check_feedback(event(actor=op, channel="operator_edit"), operators=["Aiko"])
    with pytest.raises(FeedbackError, match="cannot confirm"):
        check_feedback(event("confirmation", actor=op), operators=["Aiko"])


def test_owner_mismatch_refused():
    with pytest.raises(FeedbackError):
        check_feedback(event(), owner="Bram")


# ---- assertions


def test_correction_assertion():
    (a,) = to_assertions(event(), {"r1": target()})
    assert a.record_id is None and a.status == "active"
    assert a.value == "Noor"
    assert a.assessment.modality == "asserted" and a.assessment.binding == "bound"
    assert a.src_class == "user_stated"
    assert a.assertion_key == target().assertion_key
    assert a.provenance.principal == "Ilse"
    assert a.evidence[0].source_family == "person:Ilse"
    assert a.assessment.support.independent == 1
    assert a.assessment.source_trust >= 0.9
    assert a.time.source_time == NOW
    assert a.extras["feedback"]["target"] == "r1"


def _registry() -> CardinalityRegistry:
    r = CardinalityRegistry()
    r.declare("t1", "lives_with", "one")
    return r


def test_correction_supersedes_even_a_high_trust_target():
    old = target(trust=0.95, rid="r1")
    (a,) = to_assertions(event(), {"r1": old})
    assert a.assessment.source_trust >= 0.95
    d = integrate(a, [old], _registry())
    assert d.outcome == "supersede"


def test_rejection_is_negation_and_retires_through_integration():
    old = target("Bram", rid="r1")
    (a,) = to_assertions(event("rejection"), {"r1": old})
    assert a.assessment.modality == "negated" and a.assessment.binding == "bound"
    assert a.value == "Bram" and a.src_class == "user_stated" and a.text.startswith("not:")
    d = integrate(a, [old], _registry())
    assert d.outcome != "contest"
    assert "r1" in d.affected


def test_confirmation_is_user_confirmed():
    (a,) = to_assertions(event("confirmation"), {"r1": target("Bram")})
    assert a.src_class == "user_confirmed"
    assert a.assessment.modality == "asserted" and a.value == "Bram"
    assert a.assessment.support.families == ["person:Ilse"]


def test_operator_edit_provenance():
    ev = event(actor=Actor(principal="Aiko", kind="operator"), channel="operator_edit")
    (a,) = to_assertions(ev, {"r1": target()}, operators=["Aiko"])
    assert a.src_class == "external"
    assert a.evidence[0].source_family == "operator:Aiko"
    assert a.provenance.principal == "Aiko"


def test_rating_makes_no_assertion():
    assert to_assertions(event("rating"), {}) == []


def test_unknown_and_foreign_targets_refused():
    with pytest.raises(FeedbackError, match="unknown target"):
        to_assertions(event(), {})
    with pytest.raises(FeedbackError, match="belongs to"):
        to_assertions(event(), {"r1": target(owner="Bram")})


def test_multi_target_rejection():
    ev = event("rejection", target_ids=("r1", "r2"))
    out = to_assertions(ev, {"r1": target("Bram"), "r2": target("Tomasz")})
    assert [a.value for a in out] == ["Bram", "Tomasz"]
    assert len({a.write_id for a in out}) == 2


def test_assertions_deterministic():
    a = to_assertions(event(), {"r1": target()})
    b = to_assertions(event(), {"r1": target()})
    assert [x.model_dump() for x in a] == [x.model_dump() for x in b]


def test_quote_becomes_span():
    (a,) = to_assertions(event(quote="no, it is Noor"), {"r1": target()})
    assert a.evidence[0].span is not None and a.evidence[0].span.quote == "no, it is Noor"


# ---- correction latency


def test_correction_latency_stats():
    events = [
        event(event_id="a", turn=10),
        event("rejection", event_id="b", target_ids=("r2",), turn=7),
        event("confirmation", event_id="c", turn=9),
        event(event_id="d", target_ids=("r9",), turn=5),
        event(event_id="e", turn=None),
    ]
    hist = [
        ExposureRecord(record_id="r1", turn=4),
        ExposureRecord(record_id="r1", turn=8),
        ExposureRecord(record_id="r2", turn=6),
    ]
    s = correction_latency(events, hist)
    assert (s.total, s.measured, s.unmatched, s.unturned) == (4, 2, 1, 1)
    assert s.per_event == (("a", 6), ("b", 1))
    assert s.mean == 3.5 and s.median == 3.5
    assert (s.minimum, s.maximum, s.p90) == (1, 6, 6.0)


def test_exposure_after_correction_ignored():
    s = correction_latency([event(turn=3)], [ExposureRecord(record_id="r1", turn=5)])
    assert s.measured == 0 and s.unmatched == 1 and s.mean is None


def test_latency_empty():
    s = correction_latency([], [])
    assert s.total == 0 and s.measured == 0 and s.median is None


def test_latency_order_independent():
    evs = [event(event_id=f"e{i}", turn=10 + i) for i in range(4)]
    hist = [ExposureRecord(record_id="r1", turn=2)]
    assert correction_latency(evs, hist) == correction_latency(reversed(evs), hist)


# ---- observer views


def rec(rid, owner, scope="mem", trust=0.8, text="t"):
    return ObservableRecord(record_id=rid, owner=owner, scope=scope, trust=trust, text=text)


def test_own_records_full_trust():
    (v,) = observer_view("A", [rec("r1", "A", trust=0.7)], [])
    assert v.included and v.trust == 0.7 and v.read_only


def test_granted_record_attenuated():
    g = Grant("B", "A", "mem", "help", horizon=0, kappa=0.5)
    (v,) = observer_view("A", [rec("r1", "B", trust=0.8, text="hi")], [g])
    assert v.included and v.trust == pytest.approx(0.4) and v.text == "hi"
    assert v.via == ("B", "A") and v.hops == 1


def test_chain_attenuation_multiplies():
    g1 = Grant("B", "C", "mem", "help", horizon=1, kappa=0.5)
    g2 = Grant("C", "A", "mem", "help", horizon=0, kappa=0.5)
    (v,) = observer_view("A", [rec("r1", "B", trust=0.8)], [g1, g2])
    assert v.included and v.trust == pytest.approx(0.2) and v.hops == 2


def test_best_chain_chosen():
    direct = Grant("B", "A", "mem", "help", horizon=0, kappa=0.3)
    g1 = Grant("B", "C", "mem", "help", horizon=1, kappa=0.9)
    g2 = Grant("C", "A", "mem", "help", horizon=0, kappa=0.9)
    (v,) = observer_view("A", [rec("r1", "B", trust=1.0)], [direct, g1, g2])
    assert v.trust == pytest.approx(0.81)


def test_exclusion_reasons_without_leak():
    grants = [
        Grant("B", "A", "mem", "help", 0, 0.5),
        Grant("D", "C", "mem", "help", 0, 0.5),
    ]
    recs = [
        rec("r1", "B", scope="other", text="secret"),
        rec("r2", "D", text="secret"),
        rec("r3", "Z", text="secret"),
    ]
    views = {v.record_id: v for v in observer_view("A", recs, grants)}
    assert views["r1"].reason == "scope_not_granted"
    assert views["r2"].reason == "no_grant"
    assert views["r3"].reason == "no_grant"
    assert all(not v.included and v.text is None and v.trust == 0.0 for v in views.values())


def test_horizon_exceeded():
    g1 = Grant("B", "C", "mem", "help", horizon=0, kappa=0.9)
    g2 = Grant("C", "A", "mem", "help", horizon=0, kappa=0.9)
    (v,) = observer_view("A", [rec("r1", "B")], [g1, g2])
    assert not v.included and v.reason == "horizon_exceeded"


def test_purpose_filter_and_zero_trust():
    g = Grant("B", "A", "mem", "help", 0, 1.0)
    (v,) = observer_view("A", [rec("r1", "B")], [g], purpose="other")
    assert not v.included
    (z,) = observer_view("A", [rec("r2", "B", trust=0.0)], [g])
    assert z.reason == "zero_trust" and not z.included


def test_grant_direction_matters():
    g = Grant("A", "B", "mem", "help", 0, 1.0)
    (v,) = observer_view("A", [rec("r1", "B")], [g])
    assert not v.included


def test_cycles_terminate():
    gs = [
        Grant("B", "C", "mem", "p", 3, 0.9),
        Grant("C", "B", "mem", "p", 3, 0.9),
        Grant("C", "A", "mem", "p", 3, 0.9),
    ]
    (v,) = observer_view("A", [rec("r1", "B")], gs)
    assert v.included and v.hops == 2


def test_view_items_are_immutable():
    (v,) = observer_view("A", [rec("r1", "A")], [])
    with pytest.raises(ValidationError):
        v.trust = 1.0


def test_view_deterministic_and_sorted():
    g = [Grant("B", "A", "mem", "p", 0, 0.5), Grant("B", "A", "mem", "q", 0, 0.7)]
    recs = [rec("r2", "B"), rec("r1", "B"), rec("r3", "A")]
    a = observer_view("A", recs, g)
    b = observer_view("A", list(reversed(recs)), list(reversed(g)))
    assert a == b
    assert [v.record_id for v in a] == ["r1", "r2", "r3"]
    assert a[0].trust == pytest.approx(0.56)
