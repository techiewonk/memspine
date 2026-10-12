"""LIB-20: near-duplicate merge plans and value histories."""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime

import pytest

from memspine.derive.histories import (
    DupThresholds,
    apply_merge,
    check_guard,
    find_near_duplicates,
    merge_group,
    scan_near_duplicates,
    shingle_similarity,
    value_history,
)
from memspine.derive.integrate import CardinalityRegistry
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.assessment import Assessment, Support
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import TimeAxes

_n = itertools.count(1)


def dt(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def make(
    text: str,
    day: str,
    *,
    value: str | None = None,
    attr: str = "pet",
    owner: str = "u1",
    subject: str = "Femke",
    family: str = "user",
    trust: float = 0.9,
    modality: str = "asserted",
    oid: str | None = None,
    support: Support | None = None,
) -> AssertionEnvelope:
    n = next(_n)
    time = TimeAxes(source_time=dt(day), recorded_at=dt(day))
    return AssertionEnvelope(
        write_id=f"w{n}",
        assertion_key=AssertionKey(src=subject, rel=attr, dst=value or text, kind=attr),
        occurrence_id=oid or f"oc_{n}",
        scope=OwnerScope(tenant="t1", owner=owner),
        text=text,
        kind=attr,
        subject=Mention(surface=subject, mention_id=f"m_{subject}"),
        attribute=attr,
        value=value,
        time=time,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id="s", source_version=1, seg_index=n),
                source_family=family,
            )
        ],
        assessment=Assessment(
            source_trust=trust,
            binding="bound",
            modality=modality,  # type: ignore[arg-type]
            time_precision=time.event_precision,
            support=support or Support(),
        ),
        src_class="user_stated",
    )


def residence(value: str, day: str, **kw) -> AssertionEnvelope:
    return make(f"Femke lives in {value}", day, value=value, attr="residence", **kw)


def reg() -> CardinalityRegistry:
    r = CardinalityRegistry()
    r.declare("t1", "residence", "one")
    return r


# ----------------------------------------------------------------- near-duplicates


def test_duplicates_with_typos_merge():
    a = make("Femke keeps two cats named Pim and Pam", "2026-01-01")
    b = make("Femke keeps two cats named Pim and Pamm", "2026-02-01")
    c = make("Femke keeps two cats named Pim and Pam.", "2026-03-01")
    groups = find_near_duplicates([c, b, a])
    assert len(groups) == 1
    assert [e.occurrence_id for e in groups[0].records] == [
        a.occurrence_id,
        b.occurrence_id,
        c.occurrence_id,
    ]
    assert groups[0].canonical is a


def test_different_numbers_never_merge():
    a = make("Femke keeps 3 cats at home", "2026-01-01")
    b = make("Femke keeps 4 cats at home", "2026-02-01")
    scan = scan_near_duplicates([a, b])
    assert scan.groups == ()
    assert [p.guard for p in scan.blocked] == ["numbers_differ"]
    # number words count too, and thousands separators do not
    three = make("Femke owns three bikes", "2026-01-01")
    four = make("Femke owns four bikes", "2026-01-02")
    assert check_guard(three, four) == "numbers_differ"
    p1 = make("Femke paid 1,000 euro", "2026-01-01")
    p2 = make("Femke paid 1000 euro", "2026-01-02")
    assert check_guard(p1, p2) == "pass"


def test_different_dates_negation_and_modality_never_merge():
    a = make("Femke moved on 3 March", "2026-01-01")
    assert check_guard(a, make("Femke moved on 3 April", "2026-01-02")) == "dates_differ"
    likes = make("Femke likes tea a lot", "2026-01-01")
    dislikes = make("Femke does not like tea a lot", "2026-01-02")
    assert check_guard(likes, dislikes) == "negation_differs"
    negated = make("Femke likes tea a lot", "2026-01-02", modality="negated")
    assert check_guard(likes, negated) == "modality_differs"
    moves_to = make("Femke moves to Delft", "2026-01-01")
    might = make("Femke might move to Delft", "2026-01-02")
    assert check_guard(moves_to, might) == "modality_differs"
    assert find_near_duplicates([likes, dislikes]) == []


def test_different_values_never_merge_but_value_typos_do():
    assert (
        find_near_duplicates(
            [residence("Utrecht", "2026-01-01"), residence("Rotterdam", "2026-02-01")]
        )
        == []
    )
    assert (
        len(
            find_near_duplicates(
                [residence("Utrecht", "2026-01-01"), residence("Utrech", "2026-02-01")]
            )
        )
        == 1
    )


def test_different_owners_subjects_attributes_never_merge():
    a = make("Femke keeps two cats named Pim and Pam", "2026-01-01", owner="u1")
    b = make("Femke keeps two cats named Pim and Pam", "2026-02-01", owner="u2")
    assert find_near_duplicates([a, b]) == []
    c = make("Femke keeps two cats named Pim and Pam", "2026-02-01", subject="Jeroen")
    assert find_near_duplicates([a, c]) == []
    d = make("Femke keeps two cats named Pim and Pam", "2026-02-01", attr="household")
    assert find_near_duplicates([a, d]) == []


def test_ineligible_records_are_skipped_and_vectors_can_link():
    a = make("Femke adores her two cats", "2026-01-01")
    b = make("Femke is devoted to her two felines", "2026-02-01")
    assert find_near_duplicates([a, b]) == []
    vecs = {a.occurrence_id: [1.0, 0.0], b.occurrence_id: [0.99, 0.05]}
    assert len(find_near_duplicates([a, b], vec_sim=vecs, thresholds=DupThresholds(vec=0.95))) == 1
    hidden = b.model_copy(update={"status": "hidden"})
    assert find_near_duplicates([a, hidden], vec_sim=vecs) == []
    bad = {a.occurrence_id: [1.0], b.occurrence_id: [1.0, 0.0]}
    with pytest.raises(ValueError):
        find_near_duplicates([a, b], vec_sim=bad)


def test_threshold_controls_the_match():
    a = make("Femke keeps two cats named Pim and Pam", "2026-01-01")
    far = make("Femke keeps two cats called Pim and Pam both", "2026-02-01")
    assert shingle_similarity(a.text, far.text) < 0.95
    assert find_near_duplicates([a, far], thresholds=DupThresholds(text=0.95)) == []
    assert len(find_near_duplicates([a, far], thresholds=DupThresholds(text=0.7))) == 1


# ----------------------------------------------------------------- merge plan


def test_merge_plan_canonical_support_lineage_and_trust():
    a = make("Femke keeps two cats named Pim and Pam", "2026-01-01", family="user", trust=0.9)
    b = make("Femke keeps two cats named Pim and Pam", "2026-02-01", family="user", trust=0.6)
    c = make("Femke keeps two cats named Pim and Pam", "2026-03-01", family="partner", trust=0.7)
    (g,) = find_near_duplicates([c, a, b])
    plan = merge_group(g)
    assert plan.canonical_id == a.occurrence_id
    assert plan.member_ids == (b.occurrence_id, c.occurrence_id)
    assert (plan.support.restated, plan.support.independent) == (1, 2)  # S4-3
    assert (plan.restated_delta, plan.independent_delta, plan.reinforcement) == (1, 1, 2)
    assert plan.trust == 0.9  # canonical is the original: unchanged
    assert plan.derived_trust == 0.6  # cap for anything derived from the group
    assert {(e.child, e.parent, e.role) for e in plan.edges} == {
        (a.occurrence_id, b.occurrence_id, "merge_member"),
        (a.occurrence_id, c.occurrence_id, "merge_member"),
    }
    out = {e.occurrence_id: e for e in apply_merge(plan, [a, b, c])}
    assert out[a.occurrence_id].assessment.support.restated == 1
    assert out[a.occurrence_id].status == "active"
    assert out[b.occurrence_id].status == "hidden"
    assert out[b.occurrence_id].verdict.op == "merge"


def test_merge_adds_existing_counts():
    a = make("Femke keeps two cats named Pim and Pam", "2026-01-01", support=Support(restated=2))
    b = make("Femke keeps two cats named Pim and Pam", "2026-02-01", support=Support(restated=1))
    plan = merge_group(find_near_duplicates([a, b])[0])
    assert plan.support.restated == 2 + 1 + 1


# ----------------------------------------------------------------- histories


def moves():
    return [
        residence("Utrecht", "2026-01-01"),
        residence("Rotterdam", "2026-03-01"),
        residence("Leiden", "2026-06-01"),
    ]


def test_three_step_move_any_arrival_order():
    base = value_history(moves(), reg()).line()
    for perm in itertools.permutations(moves()):
        h = value_history(list(perm), reg()).line()
        assert [(s.value, s.effective_from, s.effective_to) for s in h.spans] == [
            (s.value, s.effective_from, s.effective_to) for s in base.spans
        ]
    assert [s.value for s in base.spans] == ["Utrecht", "Rotterdam", "Leiden"]
    assert base.spans[0].effective_to == dt("2026-03-01")
    assert base.spans[1].effective_to == dt("2026-06-01")
    assert base.spans[2].effective_to is None
    assert [s.value for s in base.current()] == ["Leiden"]
    assert [s.value for s in base.value_at(dt("2026-02-01"))] == ["Utrecht"]
    assert [s.value for s in base.value_at(dt("2026-03-01"))] == ["Rotterdam"]
    assert base.value_at(dt("2025-01-01")) == ()
    ch = base.changes_between(dt("2026-01-15"), dt("2026-06-01"))
    assert [(c.kind, c.value) for c in ch] == [
        ("end", "Utrecht"),
        ("start", "Rotterdam"),
        ("end", "Rotterdam"),
        ("start", "Leiden"),
    ]


def test_shuffled_random_orders_agree():
    rng = random.Random(7)
    items = [*moves(), residence("Leiden", "2026-07-01", family="partner")]
    ref = value_history(items, reg())
    for _ in range(20):
        shuffled = items[:]
        rng.shuffle(shuffled)
        got = value_history(shuffled, reg()).line()
        assert [s.record_id for s in got.spans] == [s.record_id for s in ref.line().spans]
        assert [s.value for s in got.spans] == ["Utrecht", "Rotterdam", "Leiden"]


def test_retirement_closes_the_interval():
    items = [
        residence("Utrecht", "2026-01-01"),
        make(
            "Femke no longer lives in Utrecht",
            "2026-04-01",
            value="Utrecht",
            attr="residence",
            modality="negated",
        ),
    ]
    h = value_history(items, reg()).line()
    assert len(h.spans) == 1
    assert h.spans[0].effective_to == dt("2026-04-01")
    assert h.current() == ()
    assert h.value_at(dt("2026-02-01"))[0].value == "Utrecht"
    assert h.value_at(dt("2026-05-01")) == ()


def test_contested_and_tentative_are_kept_separately():
    items = [
        residence("Utrecht", "2026-01-01", trust=0.9),
        residence("Delft", "2026-02-01", trust=0.5),
        residence("Zwolle", "2026-03-01", modality="hypothetical"),
    ]
    h = value_history(items, reg()).line()
    assert "Delft" in [s.value for s in h.contested]
    assert all(s.value != "Zwolle" for s in h.spans)
    assert [t.value for t in h.tentative] == ["Zwolle"]
    assert "Delft" not in [s.value for s in h.current()]
    assert "Delft" in [s.value for s in h.open_contests()]


def test_merged_duplicate_source_dates_attach_and_aba_returns():
    items = [
        residence("Utrecht", "2026-01-01"),
        residence("Utrecht", "2026-01-10", family="partner"),
        residence("Rotterdam", "2026-03-01"),
        residence("Utrecht", "2026-05-01"),
    ]
    h = value_history(items, reg()).line()
    assert [s.value for s in h.spans] == ["Utrecht", "Rotterdam", "Utrecht"]
    assert h.spans[0].source_dates == (dt("2026-01-01"), dt("2026-01-10"))
    assert h.spans[2].effective_to is None


def test_multiple_keys_and_determinism():
    pet = make("Femke keeps cats", "2026-01-01", value="cats", attr="pet")
    items = [*moves(), pet]
    a = value_history(items, reg())
    b = value_history(list(reversed(items)), reg())
    assert a.keys() == b.keys()
    assert len(a.keys()) == 2
    assert a == value_history(items, reg())
    with pytest.raises(ValueError):
        a.current()
    assert a.current(a.keys()[0]) is not None
