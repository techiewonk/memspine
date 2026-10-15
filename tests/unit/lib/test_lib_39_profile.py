# ruff: noqa: E501, RUF059
"""LIB-39: profile card, guarded operations, drift, reflection plan, invalidation."""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime, timedelta

import pytest

from memspine.derive.histories import value_history
from memspine.derive.profile import (
    ChangedGroup,
    ProfileCard,
    ProfileConfig,
    ProfileOp,
    apply_ops,
    drift_ema,
    ema_update,
    empty_card,
    from_schema_op,
    invalidate,
    parse_op,
    record_use,
    reflection_plan,
)
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.assessment import Assessment, Support
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import Interval, TemporalRef, TimeAxes
from memspine.schemas.steps.shared import ProfileOp as WireOp

_n = itertools.count(1)


def dt(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def ev(
    text: str = "Ilse likes oat milk",
    day: str = "2026-01-10",
    *,
    attr: str = "drink",
    value: str | None = None,
    trust: float = 0.9,
    modality: str = "asserted",
    status: str = "active",
    src: str = "user_stated",
    binding: str = "bound",
    owner: str = "u1",
    tenant: str = "t1",
    end: str | None = None,
    restated: int = 0,
    oid: str | None = None,
) -> AssertionEnvelope:
    n = next(_n)
    event = None
    if end:
        event = Interval(
            end=TemporalRef(at=dt(end), precision="day", basis="explicit"),
        )
    time = TimeAxes(source_time=dt(day), recorded_at=dt(day), event=event)
    return AssertionEnvelope(
        write_id=f"w{n}",
        assertion_key=AssertionKey(src="Ilse", rel=attr, dst=value or text, kind=attr),
        occurrence_id=oid or f"a{n}",
        scope=OwnerScope(tenant=tenant, owner=owner),
        text=text,
        kind=attr,
        subject=Mention(surface="Ilse", mention_id="m_ilse"),
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
            source_trust=trust,
            binding=binding,  # type: ignore[arg-type]
            modality=modality,  # type: ignore[arg-type]
            time_precision=time.event_precision,
            support=Support(restated=restated),
        ),
        src_class=src,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
    )


def index(*es: AssertionEnvelope) -> dict[str, AssertionEnvelope]:
    return {e.occurrence_id: e for e in es}


def op(
    kind="add",
    section="preferences",
    key="drink",
    value="oat milk",
    day="2026-01-10",
    cites=(),
    **kw,
):
    return ProfileOp(
        kind=kind,
        section=section,
        key=key,
        observed_at=dt(day),
        value=value,
        cites=tuple(cites),
        **kw,
    )


CARD = empty_card("t1", "u1")


def one(card, ops, idx, **kw):
    return apply_ops(card, ops, idx, **kw)


def seeded(trust=0.9, day="2026-01-10", **kw):
    a = ev(trust=trust, day=day)
    card, rep = apply_ops(CARD, [op(day=day, cites=[a.occurrence_id], **kw)], index(a))
    assert rep.applied == 1
    return card, a


# ----------------------------------------------------------------- accept cases


def test_add_creates_cited_entry():
    a = ev(restated=2)
    card, rep = one(CARD, [op(cites=[a.occurrence_id])], index(a))
    e = card.get("preferences", "drink")
    assert e is not None and e.value == "oat milk" and e.status == "active"
    assert e.support_ids == (a.occurrence_id,)
    assert e.restated == 2 and e.first_confirmed == e.last_confirmed == dt("2026-01-10")
    assert e.trust == 0.9 and 0.0 < e.confidence <= 0.9
    assert e.attrs == ("drink",)
    assert rep.applied == 1 and rep.outcomes[0].reason == "created"


def test_input_card_not_mutated():
    card, a = seeded()
    before = card
    b = ev("Ilse likes oat milk", "2026-02-01")
    apply_ops(card, [op("keep", value=None, day="2026-02-01", cites=[b.occurrence_id])], index(b))
    assert card == before


def test_restate_confirms_and_counts_stability():
    card, a = seeded()
    b = ev("Ilse still likes oat milk", "2026-03-01")
    card2, rep = one(card, [op(day="2026-03-01", cites=[b.occurrence_id])], index(b))
    e = card2.get("preferences", "drink")
    assert e.restated == 1 and e.last_confirmed == dt("2026-03-01")
    assert e.first_confirmed == dt("2026-01-10")
    assert set(e.support_ids) == {a.occurrence_id, b.occurrence_id}
    assert rep.outcomes[0].reason == "confirmed"
    # same cite again counts nothing
    card3, _ = one(card2, [op(day="2026-03-02", cites=[b.occurrence_id])], index(b))
    assert card3.get("preferences", "drink").restated == 1


def test_update_higher_trust_replaces():
    card, a = seeded(trust=0.7)
    b = ev("Ilse likes soy milk", "2026-04-01", trust=0.9)
    card2, rep = one(
        card, [op("update", value="soy milk", day="2026-04-01", cites=[b.occurrence_id])], index(b)
    )
    e = card2.get("preferences", "drink")
    assert e.value == "soy milk" and e.support_ids == (b.occurrence_id,)
    assert e.first_confirmed == dt("2026-01-10") and rep.outcomes[0].reason == "replaced"


def test_keep_without_cites_is_noop_and_with_cites_confirms():
    card, a = seeded()
    c2, rep = one(card, [op("keep", value=None, day="2026-02-01")], {})
    assert c2 == card and rep.outcomes[0].status == "noop"
    b = ev(day="2026-02-01")
    c3, rep = one(
        card, [op("keep", value=None, day="2026-02-01", cites=[b.occurrence_id])], index(b)
    )
    assert c3.get("preferences", "drink").restated == 1 and rep.applied == 1


def test_remove_with_negation_assertion():
    card, a = seeded()
    n = ev("Ilse no longer drinks oat milk", "2026-05-01", modality="negated", trust=0.9)
    c2, rep = one(
        card, [op("remove", value=None, day="2026-05-01", cites=[n.occurrence_id])], index(n)
    )
    assert c2.get("preferences", "drink") is None and rep.outcomes[0].reason == "removed"


def test_remove_with_retire_end_date():
    card, a = seeded()
    r = ev("Ilse stopped oat milk", "2026-05-01", end="2026-04-30")
    c2, _ = one(
        card, [op("remove", value=None, day="2026-05-01", cites=[r.occurrence_id])], index(r)
    )
    assert c2.get("preferences", "drink") is None


# ----------------------------------------------------------------- reject cases


@pytest.mark.parametrize(
    ("make_op", "reason"),
    [
        (lambda a: op(cites=[]), "uncited"),
        (lambda a: op(cites=["ghost"]), "unknown_evidence"),
        (lambda a: op(section="hobbies", cites=[a.occurrence_id]), "bad_section"),
        (lambda a: op(key="  ", cites=[a.occurrence_id]), "bad_key"),
        (lambda a: op(value="  ", cites=[a.occurrence_id]), "empty_value"),
        (lambda a: op(value="x" * 500, cites=[a.occurrence_id]), "value_too_long"),
        (lambda a: op("update", cites=[a.occurrence_id]), "no_such_entry"),
        (lambda a: op("remove", value=None, cites=[a.occurrence_id]), "no_such_entry"),
        (lambda a: op("keep", value=None, cites=[a.occurrence_id]), "no_such_entry"),
        (lambda a: op(cites=[a.occurrence_id], stance="inferred"), "inferred_disabled"),
        (lambda a: op(cites=[a.occurrence_id], signal=1.5), "bad_signal"),
        (lambda a: op(cites=[a.occurrence_id], signal=float("nan")), "bad_signal"),
        (
            lambda a: ProfileOp(
                "add", "preferences", "drink", datetime(2026, 1, 1), "x", (a.occurrence_id,)
            ),
            "bad_time",
        ),
    ],
)
def test_guard_rejections(make_op, reason):
    a = ev()
    card, rep = one(CARD, [make_op(a)], index(a))
    assert card.entries == ()
    assert rep.rejected == 1 and rep.outcomes[0].reason == reason


def test_future_observation_rejected_when_now_given():
    a = ev()
    _, rep = one(
        CARD, [op(day="2026-06-01", cites=[a.occurrence_id])], index(a), now=dt("2026-05-01")
    )
    assert rep.outcomes[0].reason == "future"


@pytest.mark.parametrize("status", ["hidden", "superseded"])
def test_dead_evidence_rejected(status):
    a = ev(status=status)
    _, rep = one(CARD, [op(cites=[a.occurrence_id])], index(a))
    assert rep.outcomes[0].reason == "dead_evidence"


def test_foreign_scope_rejected():
    a = ev(owner="u2")
    b = ev(tenant="t9")
    _, rep = one(
        CARD, [op(cites=[a.occurrence_id]), op(key="k2", cites=[b.occurrence_id])], index(a, b)
    )
    assert [o.reason for o in rep.outcomes] == ["foreign_scope", "foreign_scope"]


def test_negated_evidence_cannot_support_a_value():
    n = ev(modality="negated")
    _, rep = one(CARD, [op(cites=[n.occurrence_id])], index(n))
    assert rep.outcomes[0].reason == "negated_evidence"


def test_stale_op_rejected():
    card, a = seeded(day="2026-03-01")
    b = ev(day="2026-01-01")
    c2, rep = one(
        card, [op("update", value="soy", day="2026-01-01", cites=[b.occurrence_id])], index(b)
    )
    assert c2 == card and rep.outcomes[0].reason == "stale"


def test_remove_without_retire_evidence_rejected():
    card, a = seeded()
    b = ev("Ilse likes oat milk a lot", "2026-05-01")
    c2, rep = one(
        card, [op("remove", value=None, day="2026-05-01", cites=[b.occurrence_id])], index(b)
    )
    assert c2 == card and rep.outcomes[0].reason == "no_retire_evidence"


def test_remove_with_negation_of_other_attribute_rejected():
    card, a = seeded()
    n = ev("Ilse does not own a car", "2026-05-01", modality="negated", attr="car")
    _, rep = one(
        card, [op("remove", value=None, day="2026-05-01", cites=[n.occurrence_id])], index(n)
    )
    assert rep.outcomes[0].reason == "mismatched_evidence"


def test_remove_with_dead_negation_rejected():
    card, a = seeded()
    n = ev(modality="negated", status="superseded")
    _, rep = one(
        card, [op("remove", value=None, day="2026-05-01", cites=[n.occurrence_id])], index(n)
    )
    assert rep.outcomes[0].reason == "dead_evidence"


# ----------------------------------------------------------------- system fields and parsing


def test_raw_mapping_with_system_field_rejected_whole():
    a = ev()
    raw = {
        "op": "add", "section": "preferences", "key": "drink", "value": "oat milk",
        "observed_at": "2026-01-10T00:00:00+00:00", "cites": [a.occurrence_id], "trust": 1.0,
    }  # fmt: skip
    card, rep = one(CARD, [raw], index(a))
    assert card.entries == () and rep.outcomes[0].reason == "system_field:trust"
    for field in (
        "support",
        "restated",
        "status",
        "first_confirmed",
        "confidence",
        "needs_recheck",
    ):
        r = parse_op({**raw, field: 1})
        assert isinstance(r, str) and r.startswith("system_field")


def test_clean_mapping_is_applied():
    a = ev()
    raw = {
        "op": "add", "section": "preferences", "key": "drink", "value": "oat milk",
        "observed_at": "2026-01-10T00:00:00+00:00", "cites": [a.occurrence_id],
    }  # fmt: skip
    card, rep = one(CARD, [raw], index(a))
    assert rep.applied == 1 and card.get("preferences", "drink") is not None


def test_parse_op_malformed_never_raises():
    for bad in ({}, {"op": "zap"}, {"op": "add", "section": 1, "key": "k"},
                {"op": "add", "section": "identity", "key": "k", "observed_at": "nope"},
                {"op": "add", "section": "identity", "key": "k", "observed_at": "2026-01-01T00:00:00+00:00",
                 "cites": "abc"}):  # fmt: skip
        assert isinstance(parse_op(bad), str)


def test_wire_op_mapping():
    a = ev()
    wire = WireOp(op="add", entry_type="attribute", key="drink", value="oat milk",
                  observed_at="2026-01-10T00:00:00+00:00", stance="stated", cites=[a.occurrence_id])  # fmt: skip
    assert from_schema_op(wire).section == "preferences"
    card, rep = one(CARD, [wire], index(a))
    assert rep.applied == 1
    gone = WireOp(op="none", entry_type="identity", key="k", value="", observed_at="2026-01-10T00:00:00+00:00", stance="stated")  # fmt: skip
    assert from_schema_op(gone).kind == "keep"


# ----------------------------------------------------------------- S5-2 tentative handling


@pytest.mark.parametrize(
    "kw",
    [
        {"status": "tentative"},
        {"status": "contested"},
        {"modality": "hypothetical"},
        {"modality": "planned"},
        {"modality": "reported"},
        {"binding": "partly"},
        {"src": "assistant_proposed"},
    ],
)
def test_tentative_evidence_creates_only_tentative_entry(kw):
    a = ev(**kw)
    card, rep = one(CARD, [op(cites=[a.occurrence_id])], index(a))
    e = card.get("preferences", "drink")
    assert e.status == "tentative" and rep.outcomes[0].reason == "created_tentative"
    assert e.confidence < e.trust


def test_tentative_evidence_cannot_change_active_entry():
    card, a = seeded()
    t = ev("Ilse might switch to soy", "2026-02-01", modality="hypothetical")
    c2, rep = one(
        card, [op("update", value="soy", day="2026-02-01", cites=[t.occurrence_id])], index(t)
    )
    assert c2 == card and rep.outcomes[0].reason == "tentative_cannot_update"
    # the same value from tentative evidence is not a confirmation either
    c3, rep = one(
        card, [op("keep", value=None, day="2026-02-01", cites=[t.occurrence_id])], index(t)
    )
    assert c3 == card
    c4, rep = one(card, [op(day="2026-02-01", cites=[t.occurrence_id])], index(t))
    assert c4 == card and rep.outcomes[0].status == "noop"


def test_mixed_cites_use_only_firm_support():
    t = ev(modality="hypothetical")
    f = ev(day="2026-01-11")
    card, _ = one(CARD, [op(cites=[t.occurrence_id, f.occurrence_id])], index(t, f))
    e = card.get("preferences", "drink")
    assert e.status == "active" and e.support_ids == (f.occurrence_id,)


def test_firm_evidence_promotes_tentative_entry():
    t = ev(modality="hypothetical")
    card, _ = one(CARD, [op(cites=[t.occurrence_id])], index(t))
    f = ev(day="2026-02-01")
    c2, rep = one(card, [op(day="2026-02-01", cites=[f.occurrence_id])], index(f))
    e = c2.get("preferences", "drink")
    assert e.status == "active" and e.support_ids == (f.occurrence_id,)
    assert e.first_confirmed == dt("2026-01-10") and rep.outcomes[0].reason == "confirmed"
    # a tentative entry can also be moved by tentative evidence
    t2 = ev(modality="planned", day="2026-02-01")
    c3, rep = one(
        card, [op("update", value="soy", day="2026-02-01", cites=[t2.occurrence_id])], index(t2)
    )
    assert c3.get("preferences", "drink").value == "soy"
    assert c3.get("preferences", "drink").status == "tentative"


def test_tentative_never_active_without_firm_even_in_batch():
    t1 = ev(modality="reported")
    t2 = ev(modality="reported", day="2026-01-11")
    card, _ = one(
        CARD,
        [op(cites=[t1.occurrence_id]), op(day="2026-01-11", cites=[t2.occurrence_id])],
        index(t1, t2),
    )
    assert card.get("preferences", "drink").status == "tentative"


# ----------------------------------------------------------------- POL-2, contest instead of overwrite


def test_lower_trust_conflict_becomes_contest_note():
    card, a = seeded(trust=0.9)
    b = ev("Ilse likes soy milk", "2026-04-01", trust=0.5)
    c2, rep = one(
        card, [op("update", value="soy milk", day="2026-04-01", cites=[b.occurrence_id])], index(b)
    )
    e = c2.get("preferences", "drink")
    assert e.value == "oat milk" and e.status == "contested"
    assert rep.contested == 1 and rep.applied == 0
    assert len(e.contests) == 1 and e.contests[0].value == "soy milk"
    assert e.contests[0].assertion_ids == (b.occurrence_id,) and e.support_ids == (a.occurrence_id,)


def test_contest_resolved_by_higher_trust_and_deduped():
    card, a = seeded(trust=0.8)
    b = ev(trust=0.5, day="2026-04-01")
    c2, _ = one(
        card, [op("update", value="soy", day="2026-04-01", cites=[b.occurrence_id])], index(b)
    )
    b2 = ev(trust=0.4, day="2026-04-02")
    c3, _ = one(
        c2, [op("update", value="soy", day="2026-04-02", cites=[b2.occurrence_id])], index(b2)
    )
    assert len(c3.get("preferences", "drink").contests) == 1
    hi = ev(trust=0.95, day="2026-05-01")
    c4, rep = one(
        c3, [op("update", value="almond", day="2026-05-01", cites=[hi.occurrence_id])], index(hi)
    )
    e = c4.get("preferences", "drink")
    assert e.value == "almond" and e.status == "active" and e.contests == ()


def test_contests_are_capped():
    card, a = seeded(trust=0.9)
    ops, idx = [], {}
    for i in range(6):
        b = ev(trust=0.1, day=f"2026-02-{i + 1:02d}")
        idx[b.occurrence_id] = b
        ops.append(op("update", value=f"v{i}", day=f"2026-02-{i + 1:02d}", cites=[b.occurrence_id]))
    c2, _ = one(card, ops, idx)
    assert len(c2.get("preferences", "drink").contests) == 3


def test_trust_is_min_over_support_and_never_exceeds_parents():
    a, b = ev(trust=0.9), ev(trust=0.6, day="2026-01-11")
    card, _ = one(CARD, [op(cites=[a.occurrence_id, b.occurrence_id])], index(a, b))
    assert card.get("preferences", "drink").trust == 0.6
    assert card.get("preferences", "drink").confidence <= 0.6


def test_low_trust_removal_is_contest_not_removal():
    card, a = seeded(trust=0.9)
    n = ev(modality="negated", trust=0.4, day="2026-05-01")
    c2, rep = one(
        card, [op("remove", value=None, day="2026-05-01", cites=[n.occurrence_id])], index(n)
    )
    e = c2.get("preferences", "drink")
    assert e is not None and e.status == "contested" and e.contests[0].value is None
    assert rep.outcomes[0].status == "contested"


def test_inferred_never_overwrites_stated_but_stated_beats_inferred():
    cfg = ProfileConfig(allow_inferred=True)
    a = ev()
    card, _ = one(CARD, [op(cites=[a.occurrence_id])], index(a), config=cfg)
    b = ev(day="2026-02-01", trust=0.99)
    c2, rep = one(
        card,
        [op("update", value="x", day="2026-02-01", cites=[b.occurrence_id], stance="inferred")],
        index(b),
        config=cfg,
    )
    assert rep.outcomes[0].reason == "inferred_over_stated" and c2 == card
    # inferred entry, lower-trust stated op still wins
    weak = ev(trust=0.9, day="2026-01-01")
    ci, _ = one(
        CARD,
        [op(day="2026-01-01", cites=[weak.occurrence_id], stance="inferred")],
        index(weak),
        config=cfg,
    )
    s = ev(trust=0.3, day="2026-03-01")
    c3, rep = one(
        ci,
        [op("update", value="soy", day="2026-03-01", cites=[s.occurrence_id])],
        index(s),
        config=cfg,
    )
    assert (
        c3.get("preferences", "drink").value == "soy"
        and c3.get("preferences", "drink").stance == "stated"
    )


# ----------------------------------------------------------------- size bound (kappa_p)


def many(n, **kw):
    es, ops = [], []
    for i in range(n):
        a = ev(f"fact {i}", "2026-01-10", attr=f"k{i}", **kw)
        es.append(a)
        ops.append(op(key=f"k{i}", value=f"v{i}", cites=[a.occurrence_id]))
    return es, ops


def test_kappa_p_bound_holds():
    es, ops = many(10)
    cfg = ProfileConfig(kappa_p=4)
    card, rep = one(CARD, ops, index(*es), config=cfg)
    assert len(card.entries) == 4 and card.within(cfg) and len(rep.evicted) == 6


def test_eviction_prefers_unstable_and_unused():
    es, ops = many(4)
    cfg = ProfileConfig(kappa_p=3)
    card, _ = one(CARD, ops[:3], index(*es), config=cfg)
    # restate k0 and use k1; k2 stays plain
    r = ev(day="2026-02-01", attr="k0")
    card, _ = one(
        card,
        [op(key="k0", value="v0", day="2026-02-01", cites=[r.occurrence_id])],
        index(r),
        config=cfg,
    )
    card = record_use(card, [("preferences", "k1"), ("preferences", "k1")])
    card, rep = one(card, [ops[3]], index(*es), config=cfg, usefulness={("preferences", "k3"): 5})
    keys = {e.key for e in card.entries}
    assert rep.evicted == (("preferences", "k2"),) and keys == {"k0", "k1", "k3"}


def test_tentative_evicted_before_active_and_constraints_protected():
    cfg = ProfileConfig(kappa_p=2)
    t = ev(modality="hypothetical", attr="t")
    f = ev(attr="f", trust=0.5)
    c = ev(attr="c", trust=0.5)
    card, rep = one(
        CARD,
        [
            op(key="t", value="v", cites=[t.occurrence_id]),
            op(key="f", value="v", cites=[f.occurrence_id]),
            op(
                section="constraints", key="c", value="always metric units", cites=[c.occurrence_id]
            ),
        ],
        index(t, f, c),
        config=cfg,
    )
    assert rep.evicted == (("preferences", "t"),)
    assert {e.key for e in card.entries} == {"f", "c"}


def test_char_bound_and_section_cap():
    es, ops = many(6)
    cfg = ProfileConfig(max_chars=3 * len("k0v0"), section_caps=(("preferences", 5),))
    card, _ = one(CARD, ops, index(*es), config=cfg)
    assert card.chars <= cfg.max_chars and len(card.entries) == 3
    cfg2 = ProfileConfig(section_caps=(("preferences", 2),))
    card2, _ = one(CARD, ops, index(*es), config=cfg2)
    assert len(card2.section("preferences")) == 2 and card2.within(cfg2)


def test_config_validation():
    for bad in (
        dict(kappa_p=0),
        dict(alpha=0.0),
        dict(alpha=1.5),
        dict(section_caps=(("nope", 1),)),
    ):
        with pytest.raises(ValueError):
            ProfileConfig(**bad)


# ----------------------------------------------------------------- EMA drift


def test_ema_formula():
    assert ema_update(None, 0.8, 0.3) == 0.8
    assert ema_update(0.5, 1.0, 0.3) == pytest.approx(0.3 * 1.0 + 0.7 * 0.5)
    assert ema_update(0.5, 1.0, 1.0) == 1.0
    with pytest.raises(ValueError):
        ema_update(0.5, 1.0, 0.0)


def test_drift_ema_orders_by_date_not_input_order():
    obs = [(dt("2026-03-01"), 1.0), (dt("2026-01-01"), 0.0), (dt("2026-02-01"), 0.5)]
    expect = ema_update(ema_update(0.0, 0.5, 0.4), 1.0, 0.4)
    assert drift_ema(obs, 0.4) == pytest.approx(expect)
    assert drift_ema([], 0.4) is None and drift_ema([], 0.4, prior=0.2) == 0.2


def test_signals_drift_through_ops_with_configured_alpha():
    cfg = ProfileConfig(alpha=0.5)
    es = [ev(day=f"2026-0{m}-01") for m in (1, 2, 3)]
    ops = [
        op(day=f"2026-0{m}-01", cites=[e.occurrence_id], signal=s)
        for m, e, s in zip((1, 2, 3), es, (0.2, 0.8, 0.4), strict=True)
    ]
    card, _ = one(CARD, list(reversed(ops)), index(*es), config=cfg)
    d = card.get("preferences", "drink").drift
    assert d.n == 3 and d.last_at == dt("2026-03-01")
    assert d.value == pytest.approx(
        drift_ema([(dt("2026-01-01"), 0.2), (dt("2026-02-01"), 0.8), (dt("2026-03-01"), 0.4)], 0.5)
    )


def test_rejected_op_does_not_drift():
    card, a = seeded(signal=0.2)
    b = ev(trust=0.1, day="2026-04-01")
    c2, _ = one(
        card,
        [op("update", value="x", day="2026-04-01", cites=[b.occurrence_id], signal=1.0)],
        index(b),
    )
    assert c2.get("preferences", "drink").drift.value == 0.2


# ----------------------------------------------------------------- reflection plan


def test_reflection_plan_reasons_and_order():
    cfg = ProfileConfig(stale_after_days=100, min_support=2)
    a = ev(attr="a")
    t = ev(attr="t", modality="hypothetical")
    c = ev(attr="c", day="2026-06-01")
    card, _ = one(
        CARD,
        [
            op(key="a", value="va", cites=[a.occurrence_id]),
            op(key="t", value="vt", cites=[t.occurrence_id]),
            op(key="c", value="vc", day="2026-06-01", cites=[c.occurrence_id]),
        ],
        index(a, t, c),
        config=cfg,
    )
    # contest c
    lo = ev(attr="c", trust=0.1, day="2026-06-02")
    card, _ = one(
        card,
        [op("update", key="c", value="other", day="2026-06-02", cites=[lo.occurrence_id])],
        index(lo),
        config=cfg,
    )
    plan = reflection_plan(card, None, now=dt("2026-06-10"), config=cfg)
    by = {i.key: i.reasons for i in plan.items}
    assert "stale" in by["a"] and "low_support" in by["a"]
    assert "tentative" in by["t"]
    assert "contested" in by["c"]
    assert plan.items[0].key == "c" or "contested" in plan.items[0].reasons
    # fresh, well-supported entry is not listed
    ok1, ok2 = ev(attr="z"), ev(attr="z", day="2026-06-05")
    card2, _ = one(
        CARD,
        [op(key="z", value="vz", day="2026-06-05", cites=[ok1.occurrence_id, ok2.occurrence_id])],
        index(ok1, ok2),
        config=cfg,
    )
    assert reflection_plan(card2, None, now=dt("2026-06-10"), config=cfg).items == ()


def test_reflection_plan_uses_value_histories():
    cfg = ProfileConfig()
    old = AssertionEnvelope  # noqa: F841
    a = ev("Ilse lives in Delft", "2026-01-10", attr="residence", value="Delft")
    card, _ = one(
        CARD, [op(section="identity", key="home", value="Delft", cites=[a.occurrence_id])], index(a)
    )
    b = ev("Ilse lives in Utrecht", "2026-05-01", attr="residence", value="Utrecht")
    from memspine.derive.integrate import CardinalityRegistry

    reg = CardinalityRegistry()
    reg.declare("t1", "residence", "one")
    hist = value_history([a, b], reg)
    plan = reflection_plan(card, hist, now=dt("2026-05-02"), config=cfg)
    assert plan.keys() == (("identity", "home"),)
    assert "history_conflict" in plan.items[0].reasons
    # agreeing history lists nothing
    assert (
        reflection_plan(card, value_history([a], reg), now=dt("2026-05-02"), config=cfg).items == ()
    )
    # a history for another owner is ignored
    other = ev("Bram lives in Gouda", "2026-05-01", attr="residence", value="Gouda", owner="u2")
    assert (
        reflection_plan(card, value_history([other], reg), now=dt("2026-05-02"), config=cfg).items
        == ()
    )


def test_reflection_plan_flags_dead_support():
    card, a = seeded()
    dead = ev(oid=a.occurrence_id, status="superseded")
    plan = reflection_plan(card, None, now=dt("2026-01-11"), evidence_index=index(dead))
    assert plan.items[0].reasons == ("dead_support",)
    plan2 = reflection_plan(card, None, now=dt("2026-01-11"), evidence_index={})
    assert plan2.items[0].reasons == ("dead_support",)
    assert reflection_plan(card, None, now=dt("2026-01-11"), evidence_index=index(a)).items == ()


# ----------------------------------------------------------------- invalidation


def test_invalidate_keeps_independent_support_and_expires_empty():
    a, b = ev(attr="x"), ev(attr="x", day="2026-01-11")
    c = ev(attr="y")
    card, _ = one(
        CARD,
        [
            op(key="x", value="vx", cites=[a.occurrence_id, b.occurrence_id]),
            op(key="y", value="vy", cites=[c.occurrence_id]),
        ],
        index(a, b, c),
    )
    card2, rep = invalidate(
        card, [ChangedGroup(assertion_ids=frozenset({a.occurrence_id, c.occurrence_id}))]
    )
    x = card2.get("preferences", "x")
    assert x.support_ids == (b.occurrence_id,) and x.needs_recheck and x.restated == 0
    assert card2.get("preferences", "y") is None
    assert rep.expired == (("preferences", "y"),) and rep.reduced == (("preferences", "x"),)
    plan = reflection_plan(card2, None, now=dt("2026-01-12"))
    assert plan.items[0].reasons == ("changed_group",)
    # untouched groups change nothing
    assert invalidate(card, [ChangedGroup(assertion_ids=frozenset({"nope"}))])[0] == card
    assert invalidate(card, [])[0] == card


def test_invalidate_recomputes_trust_from_remaining_and_flags_attributes():
    lo, hi = ev(trust=0.4, attr="x"), ev(trust=0.9, attr="x", day="2026-01-11")
    card, _ = one(
        CARD, [op(key="x", value="vx", cites=[lo.occurrence_id, hi.occurrence_id])], index(lo, hi)
    )
    assert card.get("preferences", "x").trust == 0.4
    c2, _ = invalidate(card, [ChangedGroup(assertion_ids=frozenset({lo.occurrence_id}))])
    assert c2.get("preferences", "x").trust == 0.9
    c3, rep = invalidate(card, [ChangedGroup(attributes=frozenset({"x"}))])
    assert rep.flagged == (("preferences", "x"),) and c3.get("preferences", "x").needs_recheck
    assert c3.get("preferences", "x").support == card.get("preferences", "x").support


def test_invalidate_drops_contest_notes_citing_changed_ids_and_clears_status():
    card, a = seeded(trust=0.9)
    b = ev(trust=0.2, day="2026-02-01")
    card, _ = one(
        card, [op("update", value="soy", day="2026-02-01", cites=[b.occurrence_id])], index(b)
    )
    c2, _ = invalidate(card, [ChangedGroup(assertion_ids=frozenset({b.occurrence_id}))])
    e = c2.get("preferences", "drink")
    assert e.contests == () and e.status == "active"
    assert c2.get("preferences", "drink").support_ids == (a.occurrence_id,)


def test_confirmation_clears_recheck_flag():
    a, b = ev(attr="x"), ev(attr="x", day="2026-01-11")
    card, _ = one(
        CARD, [op(key="x", value="vx", cites=[a.occurrence_id, b.occurrence_id])], index(a, b)
    )
    card, _ = invalidate(card, [ChangedGroup(assertion_ids=frozenset({a.occurrence_id}))])
    f = ev(attr="x", day="2026-02-01")
    card, _ = one(
        card, [op(key="x", value="vx", day="2026-02-01", cites=[f.occurrence_id])], index(f)
    )
    assert not card.get("preferences", "x").needs_recheck


# ----------------------------------------------------------------- determinism and order independence


def test_batch_is_order_independent():
    es, ops = [], []
    for i, (kind, val, tr) in enumerate(
        [("add", "oat", 0.9), ("update", "soy", 0.95), ("update", "almond", 0.5)]
    ):
        e = ev(day=f"2026-0{i + 1}-05", trust=tr)
        es.append(e)
        ops.append(op(kind, value=val, day=f"2026-0{i + 1}-05", cites=[e.occurrence_id]))
    base, rep0 = one(CARD, ops, index(*es))
    rng = random.Random(7)
    for _ in range(10):
        shuffled = ops[:]
        rng.shuffle(shuffled)
        card, rep = one(CARD, shuffled, index(*es))
        assert card == base
        assert sorted(o.reason for o in rep.outcomes) == sorted(o.reason for o in rep0.outcomes)
    e = base.get("preferences", "drink")
    assert e.value == "soy" and e.status == "contested" and e.contests[0].value == "almond"


def test_repeat_runs_identical_and_entries_sorted():
    es, ops = many(8)
    ops = [*ops[4:], *ops[:4]]
    c1, r1 = one(CARD, ops, index(*es))
    c2, r2 = one(CARD, ops, index(*es))
    assert c1 == c2 and r1 == r2
    assert [e.key for e in c1.entries] == sorted(e.key for e in c1.entries)
    secs = apply_ops(
        CARD,
        [op(section="constraints", key="b", value="x", cites=[es[0].occurrence_id]),
         op(section="identity", key="a", value="x", cites=[es[0].occurrence_id])],
        index(*es),
    )[0]  # fmt: skip
    assert [e.section for e in secs.entries] == ["identity", "constraints"]


def test_report_outcomes_keep_input_index():
    a = ev()
    _, rep = one(CARD, [op(cites=[]), op(key="ok", cites=[a.occurrence_id])], index(a))
    assert [o.index for o in rep.outcomes] == [0, 1]
    assert rep.reasons() == {"uncited": 1}


def test_card_type_is_frozen():
    card, _ = seeded()
    with pytest.raises(AttributeError):
        card.entries = ()  # type: ignore[misc]
    assert isinstance(card, ProfileCard)
    assert timedelta(0) == timedelta(0)
