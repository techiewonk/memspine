"""LIB-02: integration ladder, cardinality registry, event-time ordering, replay convergence."""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime

import pytest

from memspine.derive.integrate import (
    CardinalityRegistry,
    apply_decision,
    integrate,
    normalise_name,
    ordering_key,
    replay,
    summarise,
)
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.assessment import Assessment, Support
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import Interval, TemporalRef, TimeAxes

SCOPE = OwnerScope(tenant="t1", owner="u1")
_n = itertools.count(1)


def dt(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def _ref(day: str) -> TemporalRef:
    return TemporalRef(at=dt(day), precision="day", basis="explicit")


def make(
    value: str,
    day: str,
    *,
    attr: str = "residence",
    modality: str = "asserted",
    binding: str = "bound",
    trust: float = 0.9,
    family: str = "user",
    subject: str = "Noor",
    event_end: str | None = None,
    event_start: str | None = None,
    extras: dict | None = None,
    oid: str | None = None,
) -> AssertionEnvelope:
    n = next(_n)
    oid = oid or f"oc_{n}"
    ev = None
    if event_start or event_end:
        ev = Interval(
            start=_ref(event_start) if event_start else None,
            end=_ref(event_end) if event_end else None,
        )
    time = TimeAxes(event=ev, source_time=dt(day), recorded_at=dt(day))
    return AssertionEnvelope(
        write_id=f"w{n}",
        assertion_key=AssertionKey(src=subject, rel=attr, dst=value, kind=attr),
        occurrence_id=oid,
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
                source_family=family,
            )
        ],
        assessment=Assessment(
            source_trust=trust,
            binding=binding,
            modality=modality,
            time_precision=time.event_precision,
            support=Support(),
        ),
        src_class="user_stated",
        extras=extras or {},
    )


def registry(**declared: str) -> CardinalityRegistry:
    r = CardinalityRegistry()
    for k, v in declared.items():
        r.declare("t1", k, v)  # type: ignore[arg-type]
    return r


# ----------------------------------------------------------------- registry


def test_normalise_name():
    assert normalise_name("Favourite Hobbies") == "favourite_hobby"
    assert normalise_name("livesIn") == "lives_in"
    assert normalise_name("pets") == "pet"
    assert normalise_name("status") == "status"
    assert normalise_name(normalise_name("Home Addresses")) == normalise_name("Home Addresses")


def test_registry_declared_and_default():
    r = registry(residence="one", hobby="many")
    assert r.cardinality("t1", "Residence") == "one"
    assert r.cardinality("t1", "hobbies") == "many"
    assert r.cardinality("t1", "unknown_thing") == "many"  # Q7
    assert r.cardinality("other_ns", "residence") == "many"  # namespaces are separate
    assert r.is_declared("t1", "residence")
    assert not r.is_declared("t1", "unknown_thing")


def test_registry_model_proposed_cap_and_counts():
    r = CardinalityRegistry(cap=3)
    assert r.propose("t1", "Pet Names") == "pet_name"
    assert r.propose("t1", "pet name") == "pet_name"
    assert r.propose("t1", "a") == "a"
    assert r.propose("t1", "b") == "b"
    assert r.propose("t1", "c") == "other"  # over the cap of 3 distinct kinds
    assert r.propose("t1", "pet_name") == "pet_name"  # a known kind still resolves
    assert r.propose("t2", "c") == "c"  # the cap is per namespace
    counts = r.counts("t1")
    assert counts["pet_name"] == 3
    assert counts["other"] == 1
    rows = {k.name: k for k in r.entries("t1")}
    assert rows["pet_name"].provenance == "model-proposed"
    assert rows["pet_name"].cardinality == "many"
    r.declare("t1", "pet_name", "one")
    assert {k.name: k for k in r.entries("t1")}["pet_name"].provenance == "declared"
    assert r.proposed_count("t1") == 3  # a, b, other: declaring freed the slot


# ----------------------------------------------------------------- ladder rules


def test_first_value_appends():
    d = integrate(make("Utrecht", "2025-01-01"), [], registry(residence="one"))
    assert d.outcome == "append"


def test_supersede_closes_interval_at_new_start():
    old = make("Utrecht", "2025-01-01", oid="a")
    new = make("Rotterdam", "2025-03-01", oid="b")
    d = integrate(new, [old], registry(residence="one"))
    assert d.outcome == "supersede"
    assert d.affected == ("a",)
    assert [(c.record_id, c.end) for c in d.closures] == [("a", dt("2025-03-01"))]
    assert dict(d.status_changes) == {"a": "superseded"}


def test_equal_trust_supersedes_lower_trust_contests():
    reg = registry(residence="one")
    cur = make("Utrecht", "2025-01-01", oid="a")
    assert integrate(make("X", "2025-02-01", trust=0.9), [cur], reg).outcome == "supersede"
    d = integrate(make("X", "2025-02-01", trust=0.3, family="web"), [cur], reg)
    assert d.outcome == "contest"
    assert d.incoming_status == "contested"
    assert dict(d.status_changes) == {"a": "contested"}


def test_higher_trust_supersedes_lower():
    cur = make("Emacs", "2025-01-01", trust=0.3, family="tool", oid="a")
    d = integrate(make("Vim", "2025-02-01"), [cur], registry(residence="one"))
    assert d.outcome == "supersede"


def test_tentative_modalities_and_binding():
    reg = registry(residence="one")
    cur = make("Utrecht", "2025-01-01", oid="a")
    for m in ("hypothetical", "planned", "reported", "question"):
        d = integrate(make("Ghent", "2025-02-01", modality=m), [cur], reg)
        assert d.outcome == "tentative"
        assert d.incoming_status == "tentative"
        assert not d.creates_value
    for b in ("partly", "unbound"):
        assert integrate(make("Ghent", "2025-02-01", binding=b), [cur], reg).outcome == "tentative"
    # a hypothetical negation never retires
    d = integrate(make("none", "2025-02-01", modality="hypothetical"), [cur], reg)
    assert d.outcome == "tentative"


def test_negation_retires_or_contests():
    reg = registry(residence="one")
    cur = make("Utrecht", "2025-01-01", oid="a")
    d = integrate(make("none", "2025-05-01", modality="negated"), [cur], reg)
    assert d.outcome == "retire"
    assert d.affected == ("a",)
    assert not d.creates_value
    assert d.closures[0].end == dt("2025-05-01")
    low = make("none", "2025-05-01", modality="negated", trust=0.3, family="web")
    assert integrate(low, [cur], reg).outcome == "contest"
    # a negation naming a different value is not a retraction
    other = make("Rotterdam", "2025-05-01", modality="negated")
    assert integrate(other, [cur], reg).outcome == "tentative"
    # a negation naming the current value retires it
    same = make("Utrecht", "2025-05-01", modality="negated")
    assert integrate(same, [cur], reg).outcome == "retire"


def test_cardinality_many_coexists_and_undeclared_coexists():
    cur = make("cycling", "2025-01-01", attr="hobby", oid="a")
    new = make("chess", "2025-02-01", attr="hobby")
    assert integrate(new, [cur], registry(hobby="many")).outcome == "coexist"
    d = integrate(new, [cur], registry())  # undeclared
    assert d.outcome == "coexist"
    assert not d.declared


def test_undeclared_with_transition_mark_contests_never_supersedes():
    cur = make("cycling", "2025-01-01", attr="hobby", oid="a")
    new = make("chess", "2025-02-01", attr="hobby", extras={"transition": True})
    d = integrate(new, [cur], registry())
    assert d.outcome == "contest"
    assert d.reason == "undeclared_transition"


def test_closed_past_interval_coexists():
    cur = make("Delft", "2025-01-10", oid="a")
    past = make("Leiden", "2025-02-10", event_end="2024-12-31")
    d = integrate(past, [cur], registry(residence="one"))
    assert d.outcome == "coexist"
    assert d.reason == "past_interval"
    assert d.closures == ()


# ----------------------------------------------------------------- S4-3 restated vs support


def test_same_value_same_family_is_restated_not_corroboration():
    cur = make("Utrecht", "2025-01-01", oid="a")
    d = integrate(make("Utrecht", "2025-02-01"), [cur], registry(residence="one"))
    assert d.outcome == "merge"
    assert d.merge_into == "a"
    assert (d.restated_delta, d.independent_delta) == (1, 0)
    assert d.incoming_status == "hidden"


def test_same_value_other_family_is_support():
    cur = make("Utrecht", "2025-01-01", oid="a")
    d = integrate(make("Utrecht", "2025-02-01", family="spouse"), [cur], registry(residence="one"))
    assert d.outcome == "merge"
    assert (d.restated_delta, d.independent_delta) == (0, 1)


def test_merge_in_many_attribute():
    cur = make("cycling", "2025-01-01", attr="hobby", oid="a")
    d = integrate(make("Cycling", "2025-02-01", attr="hobby"), [cur], registry(hobby="many"))
    assert d.outcome == "merge"


# ----------------------------------------------------------------- event-time ordering


def test_ordering_key_is_event_then_source_then_recorded():
    a = make("x", "2025-03-01", event_start="2025-01-01")
    b = make("y", "2025-02-01")  # no event time: falls back to source time
    assert ordering_key(a) < ordering_key(b)


def test_inv_order_swap_older_arriving_later_becomes_history():
    reg = registry(residence="one")
    newer = make("Rotterdam", "2025-06-01", oid="new")
    older = make("Utrecht", "2025-01-01", oid="old")
    d = integrate(older, [newer], reg)
    assert d.outcome == "supersede"
    assert d.reason == "late_arrival_older"
    assert d.incoming_status == "superseded"
    assert d.affected == ("new",)
    assert [(c.record_id, c.end) for c in d.closures] == [("old", dt("2025-06-01"))]
    assert d.status_changes == ()  # the newer value is untouched
    st = summarise(apply_decision(d, older, [newer]))
    assert [v for _, v in st.live] == ["rotterdam"]
    assert len(st.history) == 1
    assert st.history[0][1] == "utrecht"


def test_inv_order_swap_both_orders_give_same_state():
    reg = registry(residence="one")
    a = make("Utrecht", "2025-01-01")
    b = make("Rotterdam", "2025-06-01")
    assert replay([a, b], reg) == replay([b, a], reg)
    assert len(replay([b, a], reg).live) == 1


def test_late_middle_value_recloses_predecessor():
    reg = registry(residence="one")
    a, b, c = make("A", "2025-01-01"), make("B", "2025-02-01"), make("C", "2025-03-01")
    final = replay([a, c, b], reg)
    assert final == replay([a, b, c], reg)
    assert [h[1] for h in final.history] == ["a", "b"]


def test_late_duplicate_keeps_state_consistent_with_in_order():
    reg = registry(residence="one")
    seq = [make("U", "2025-01-01"), make("R", "2025-02-01"), make("U", "2025-03-01")]
    assert [v for _, v in replay(seq, reg).live] == ["u"]
    assert replay(seq, reg) == replay(list(reversed(seq)), reg)
    assert replay(seq, reg) == replay([seq[0], seq[2], seq[1]], reg)


# ----------------------------------------------------------------- replay convergence


def _scenario(kind: str) -> list[AssertionEnvelope]:
    if kind == "chain":
        return [make(v, f"2025-0{i}-01", oid=f"c{i}") for i, v in enumerate("ABCDE", 1)]
    if kind == "mixed_trust":
        return [
            make("A", "2025-01-01", oid="m1"),
            make("B", "2025-02-01", trust=0.3, family="web", oid="m2"),
            make("C", "2025-03-01", oid="m3"),
            make("C", "2025-04-01", family="spouse", oid="m4"),
            make("D", "2025-05-01", modality="planned", oid="m5"),
        ]
    if kind == "negation":
        return [
            make("A", "2025-01-01", oid="n1"),
            make("A", "2025-02-01", oid="n2"),
            make("none", "2025-03-01", modality="negated", oid="n3"),
            make("B", "2025-04-01", oid="n4"),
            make("Z", "2025-02-15", modality="hypothetical", oid="n5"),
        ]
    if kind == "past":
        return [
            make("A", "2025-01-01", oid="p1"),
            make("old", "2025-02-01", event_end="2024-06-01", oid="p2"),
            make("B", "2025-03-01", oid="p3"),
        ]
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["chain", "mixed_trust", "negation", "past"])
def test_replay_converges_for_every_permutation(kind):
    reg = registry(residence="one")
    items = _scenario(kind)
    reference = replay(items, reg)
    count = 0
    for perm in itertools.permutations(items):
        assert replay(perm, reg) == reference, [o.occurrence_id for o in perm]
        count += 1
    assert count >= 6


def test_replay_converges_random_larger_sets_seeded():
    rng = random.Random(20261013)
    reg = registry(residence="one", hobby="many")
    for trial in range(40):
        items = []
        for i in range(rng.randint(4, 9)):
            modality = rng.choice(["asserted"] * 5 + ["negated", "planned", "hypothetical"])
            e = make(
                "none" if modality == "negated" else rng.choice(["A", "B", "C", "D"]),
                f"2025-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                attr=rng.choice(["residence", "hobby"]),
                modality=modality,
                trust=rng.choice([0.9, 0.9, 0.3]),
                family=rng.choice(["user", "spouse", "web"]),
                oid=f"r{trial}_{i}",
            )
            items.append(e)
        reference = replay(items, reg)
        for _ in range(8):
            shuffled = items[:]
            rng.shuffle(shuffled)
            assert replay(shuffled, reg) == reference, trial


def test_group_isolation_by_owner_subject_attribute():
    reg = registry(residence="one")
    other = make("Utrecht", "2025-01-01", subject="Bram", oid="a")
    d = integrate(make("Rotterdam", "2025-02-01"), [other], reg)
    assert d.outcome == "append"
