"""LIB-37: reconcile backlog, staged entity resolution and contradiction sweep (pure, unwired)."""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime, timedelta

import pytest
from labelled.loader import Assertion, ContradictionItem, load_contradictions

from memspine.derive.integrate import CardinalityRegistry, record_id
from memspine.derive.reconcile import (
    PendingMerge,
    QueueItem,
    ReconcilePlan,
    ReconcileQueue,
    apply_plan,
    merge_aliases,
    sweep,
)
from memspine.derive.resolve import Candidate
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.assessment import Assessment
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.temporal import Interval, TemporalRef, TimeAxes

_n = itertools.count(1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def dt(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def make(
    value: str,
    day: str,
    *,
    attr: str = "employer",
    subject: str = "Tomasz",
    entity: str | None = None,
    owner: str = "u1",
    trust: float = 0.9,
    family: str = "user",
    modality: str = "asserted",
    binding: str = "bound",
    session: str = "s1",
    key_src: str | None = None,
    event_end: datetime | None = None,
) -> AssertionEnvelope:
    n = next(_n)
    event = (
        Interval(end=TemporalRef(at=event_end, precision="day", basis="explicit"))
        if event_end
        else None
    )
    time = TimeAxes(event=event, source_time=dt(day), recorded_at=dt(day))
    return AssertionEnvelope(
        write_id=f"w{n}",
        assertion_key=AssertionKey(
            src=key_src or entity or subject, rel=attr, dst=value, kind=attr
        ),
        occurrence_id=f"oc{n}",
        scope=OwnerScope(tenant="t1", owner=owner),
        text=f"{subject} {attr} {value}",
        kind=attr,
        subject=Mention(surface=subject, mention_id=f"m{n}", entity_id=entity),
        attribute=attr,
        value=value,
        time=time,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id=session, source_version=1, seg_index=n),
                source_family=family,
            )
        ],
        assessment=Assessment(
            source_trust=trust,
            binding=binding,  # type: ignore[arg-type]
            modality=modality,  # type: ignore[arg-type]
            time_precision=time.event_precision,
        ),
        src_class="user_stated",
    )


def reg(attr: str = "employer", card: str = "one", ns: str = "t1") -> CardinalityRegistry:
    r = CardinalityRegistry()
    r.declare(ns, attr, card)  # type: ignore[arg-type]
    return r


def status_of(records: list[AssertionEnvelope]) -> dict[str, str]:
    return {r.value: r.status for r in records}  # type: ignore[misc]


# --------------------------------------------------------------------------- contradiction sweep


def test_cross_session_contest_detected():
    a = make("Harbour Logistics", "2025-01-10", session="s1")
    b = make("Veldkamp Bakery", "2025-03-02", session="s2", trust=0.3, family="web")
    rep = sweep([a, b], reg())
    assert not rep.is_noop
    assert {c.record_id for c in rep.plan.status_changes} == {record_id(a), record_id(b)}
    assert all(c.new == "contested" for c in rep.plan.status_changes)
    (flag,) = rep.plan.contest_flags
    assert set(flag.newly) == {record_id(a), record_id(b)}
    assert rep.open_contests == (flag.group,)
    # the contested group is queued for a later revisit
    assert [i.kind for i in rep.queue.items] == ["contested_group"]


def test_cross_session_supersede_closes_older_value():
    a = make("Harbour Logistics", "2025-01-10", session="s1")
    b = make("Veldkamp Bakery", "2025-03-02", session="s2")
    rep = sweep([a, b], reg())
    after = apply_plan(rep.plan, [a, b])
    st = {r.value: r for r in after}
    assert st["Harbour Logistics"].status == "superseded"
    assert st["Harbour Logistics"].time.event.end.at == dt("2025-03-02")  # type: ignore[union-attr]
    assert st["Veldkamp Bakery"].status == "active"
    assert not rep.plan.contest_flags


def test_many_valued_attribute_is_left_alone():
    a = make("chess", "2025-01-10", attr="hobby")
    b = make("rowing", "2025-02-10", attr="hobby")
    rep = sweep([a, b], reg("hobby", "many"))
    assert rep.is_noop


def test_attribute_families_catch_cross_key_contradiction():
    a = make("Utrecht", "2025-01-10", attr="lives_in", subject="Noor")
    b = make("Ghent", "2025-06-10", attr="moved_to", subject="Noor")
    r = reg("residence")
    plain = sweep([a, b], r)
    assert plain.is_noop  # different attribute keys: nothing to compare
    fam = sweep([a, b], r, attribute_families={"lives_in": "residence", "moved_to": "residence"})
    after = {x.value: x.status for x in apply_plan(fam.plan, [a, b])}
    assert after == {"Utrecht": "superseded", "Ghent": "active"}


def test_unbound_and_late_arrival_queue_items():
    a = make("Harbour Logistics", "2025-01-10")
    u = make("Veldkamp Bakery", "2025-03-02", binding="unbound")
    rep = sweep([a, u], reg(), now=T0)
    assert rep.waiting_binding == (record_id(u),)
    assert [i.kind for i in rep.queue.items] == ["unbound"]
    assert rep.plan.status_changes[0].new == "tentative"
    # once bound, the item leaves the queue; a late-arrival marker is revisited and dropped
    bound = u.model_copy(
        update={"assessment": u.assessment.model_copy(update={"binding": "bound"})}
    )
    q = rep.queue.enqueue(QueueItem("late_arrival", record_id(a), "t1", "u1", 30))
    rep2 = sweep([a, bound], reg(), queue=q, now=T0)
    assert all(i.kind != "unbound" and i.kind != "late_arrival" for i in rep2.queue.items)


def test_idempotent_second_sweep_is_noop():
    recs = [
        make("A", "2025-01-01", session="s1"),
        make("B", "2025-02-01", session="s2", trust=0.3, family="web"),
        make("C", "2025-03-01", session="s3"),
        make("X", "2025-01-05", attr="city"),
        make("Y", "2025-02-05", attr="city"),
    ]
    r = CardinalityRegistry()
    r.declare("t1", "employer", "one")
    r.declare("t1", "city", "one")
    first = sweep(recs, r)
    assert not first.is_noop
    after = apply_plan(first.plan, recs)
    second = sweep(after, r, queue=first.queue)
    assert second.is_noop
    assert second.plan == ReconcilePlan()
    assert sorted(after, key=record_id) == sorted(apply_plan(second.plan, after), key=record_id)


def test_deterministic_under_input_order():
    recs = [
        make(v, d, session=f"s{i}")
        for i, (v, d) in enumerate(
            [("A", "2025-01-01"), ("B", "2025-02-01"), ("C", "2025-03-01"), ("D", "2025-04-01")]
        )
    ]
    recs.append(make("Z", "2025-02-15", trust=0.2, family="tool"))
    base = sweep(recs, reg())
    for seed in range(6):
        shuffled = recs[:]
        random.Random(seed).shuffle(shuffled)
        assert sweep(shuffled, reg()).plan == base.plan


def test_owners_are_swept_separately():
    a = make("Harbour Logistics", "2025-01-10", owner="u1")
    b = make("Veldkamp Bakery", "2025-03-02", owner="u2")
    assert sweep([a, b], reg()).is_noop


# --------------------------------------------------------------------------- entity resolution

SCOPE = OwnerScope(tenant="t1", owner="u1")
ROT = "Colleague at the Rotterdam office who handles the invoices."


def ilse_world(extra_evidence: tuple[str, ...] = ()) -> tuple[PendingMerge, list[Candidate]]:
    cand = Candidate("ent_ilse", "Ilse", SCOPE, "person", ROT)
    p = PendingMerge(
        pending_id="pm1",
        scope=SCOPE,
        provisional_id="prov_ilse",
        name="Ilse",
        type="person",
        evidence=("Joined for the audit last week.", *extra_evidence),
        candidates=("ent_ilse",),
    )
    return p, [cand]


def test_pending_merge_stays_pending_without_enough_evidence():
    p, ents = ilse_world()
    rep = sweep([], reg(), pending=[p], entities=ents, now=T0)
    assert rep.plan.merges == () and rep.plan.splits == ()
    (u,) = rep.unresolved
    assert u.reason == "homonym_unresolved" and u.attempts == 1 and not u.parked
    assert rep.queue.get("pending_merge", "t1", "u1", "pm1").attempts == 1  # type: ignore[union-attr]


def test_pending_merge_resolved_with_more_context_and_rekeyed():
    p, ents = ilse_world()
    first = sweep([], reg(), pending=[p], entities=ents, now=T0)
    assert first.unresolved
    p2 = PendingMerge(
        **{**p.__dict__, "evidence": (*p.evidence, "She sits at the Rotterdam office on Fridays.")}
    )
    emp_old = make("Harbour Logistics", "2025-01-10", subject="Ilse", entity="ent_ilse")
    emp_new = make(
        "Veldkamp Bakery", "2025-05-10", subject="Ilse", entity="prov_ilse", key_src="prov_ilse"
    )
    rep = sweep([emp_old, emp_new], reg(), pending=[p2], entities=ents, queue=first.queue, now=T0)
    (m,) = rep.plan.merges
    assert (m.from_id, m.into_id) == ("prov_ilse", "ent_ilse")
    (k,) = rep.plan.rekeys
    assert k.record_id == record_id(emp_new)
    assert k.new_key.src == "ent_ilse" and k.roles == ("subject",)
    assert k.old_digest in k.key_aliases and k.new_digest not in k.key_aliases
    # once the two groups are one, the older employer is superseded by the newer
    after = {r.value: r for r in apply_plan(rep.plan, [emp_old, emp_new])}
    assert after["Veldkamp Bakery"].subject.entity_id == "ent_ilse"  # type: ignore[union-attr]
    assert after["Harbour Logistics"].status == "superseded"
    assert after["Veldkamp Bakery"].key_aliases == [k.old_digest]
    # the acknowledged item is gone and never re-applied
    assert rep.queue.get("pending_merge", "t1", "u1", "pm1") is None
    again = sweep(list(after.values()), reg(), pending=[p2], entities=ents, queue=rep.queue)
    # (the status change was already applied above, so nothing is left to do)
    assert again.is_noop


def test_pending_resolved_as_different_gives_split():
    p, ents = ilse_world()
    p = PendingMerge(
        **{**p.__dict__, "evidence": ("My sister who lives in Gent and teaches violin.",)}
    )
    rep = sweep([], reg(), pending=[p], entities=ents)
    (s,) = rep.plan.splits
    assert s.entity_id == "prov_ilse" and s.against == ("ent_ilse",)
    assert rep.plan.merges == () and rep.plan.rekeys == ()
    assert rep.queue.get("pending_merge", "t1", "u1", "pm1") is None


def test_no_cross_owner_merge():
    other = OwnerScope(tenant="t1", owner="u2")
    cand = Candidate("ent_ilse_u2", "Ilse", other, "person", ROT)
    p = PendingMerge(
        "pm1", SCOPE, "prov_ilse", "Ilse", "person",
        evidence=(ROT,), candidates=("ent_ilse_u2",),
    )  # fmt: skip
    rep = sweep([], reg(), pending=[p], entities=[cand])
    assert rep.plan.merges == () and rep.plan.splits == ()
    assert rep.unresolved[0].reason == "no_candidate_in_scope"
    # same entity id string in another tenant: also ignored
    cand2 = Candidate("ent_ilse_u2", "Ilse", OwnerScope(tenant="t2", owner="u1"), "person", ROT)
    assert sweep([], reg(), pending=[p], entities=[cand2]).plan.merges == ()


def test_records_of_other_owners_are_not_rekeyed():
    p, ents = ilse_world(("She sits at the Rotterdam office.",))
    mine = make("Veldkamp Bakery", "2025-05-10", subject="Ilse", entity="prov_ilse")
    theirs = make("Harbour Logistics", "2025-01-10", subject="Ilse", entity="prov_ilse", owner="u2")
    rep = sweep([mine, theirs], reg(), pending=[p], entities=ents)
    assert [k.record_id for k in rep.plan.rekeys] == [record_id(mine)]


def test_ambiguous_same_stays_pending():
    c1 = Candidate("e1", "Ilse", SCOPE, "person", ROT)
    c2 = Candidate("e2", "Ilse", SCOPE, "person", "Works at the Rotterdam office, new in sales.")
    p = PendingMerge(
        "pm1", SCOPE, "prov", "Ilse", "person",
        evidence=("At the Rotterdam office.",), candidates=("e1", "e2"),
    )  # fmt: skip
    rep = sweep([], reg(), pending=[p], entities=[c1, c2])
    assert rep.plan.merges == ()
    assert rep.unresolved[0].reason == "ambiguous_same"
    assert rep.unresolved[0].candidates == ("e1", "e2")


def test_other_names_resolve_an_unqualified_nickname():
    cand = Candidate("e_av", "Abraham Visser", SCOPE, "person", "Plumber in Utrecht.")
    p = PendingMerge("pm1", SCOPE, "prov_b", "Bram", "person", evidence=("Plumber in Utrecht.",),
                     candidates=("e_av",))  # fmt: skip
    assert sweep([], reg(), pending=[p], entities=[cand]).plan.merges == ()
    p2 = PendingMerge(**{**p.__dict__, "other_names": ("Bram Visser",)})
    (m,) = sweep([], reg(), pending=[p2], entities=[cand]).plan.merges
    assert m.into_id == "e_av"


def test_pending_attempts_park_after_cap():
    p, ents = ilse_world()
    q = ReconcileQueue()
    for i in range(3):
        rep = sweep([], reg(), pending=[p], entities=ents, queue=q, now=T0 + timedelta(minutes=i),
                    max_attempts=3)  # fmt: skip
        q = rep.queue
    assert rep.unresolved[0].parked
    rep = sweep([], reg(), pending=[p], entities=ents, queue=q, max_attempts=3)
    assert rep.unresolved[0].reason == "parked" and rep.queue.parked(3)


# --------------------------------------------------------------------------- aliases (A-B-A)


def test_alias_aba_rules():
    assert merge_aliases([], "A", "B") == ("A",)
    assert merge_aliases(["A"], "B", "A") == ("B",)  # returning to A drops A, keeps B
    assert merge_aliases(["A", "B"], "B", "C") == ("A", "B")  # no repeats
    assert merge_aliases(["C"], "A", "C") == ("A",)  # never lists the current key


def test_rekey_keeps_existing_aliases():
    p, ents = ilse_world(("She sits at the Rotterdam office.",))
    e = make(
        "Veldkamp Bakery", "2025-05-10", subject="Ilse", entity="prov_ilse", key_src="prov_ilse"
    )
    e = e.model_copy(update={"key_aliases": ["ak_old"]})
    (k,) = sweep([e], reg(), pending=[p], entities=ents).plan.rekeys
    assert k.key_aliases == ("ak_old", k.old_digest)


# --------------------------------------------------------------------------- queue


def item(kind="pending_merge", ref="r", prio=40, first=None, **kw):
    return QueueItem(kind, ref, "t1", "u1", prio, first_seen=first, **kw)  # type: ignore[arg-type]


def test_queue_ordering_and_dedupe():
    q = ReconcileQueue()
    q = q.enqueue(item("unbound", "u", 10, T0)).enqueue(
        item("pending_merge", "b", 40, T0 + timedelta(hours=1))
    )
    q = q.enqueue(item("pending_merge", "a", 40, T0))
    assert [i.ref for i in q.ordered()] == ["a", "b", "u"]
    q = q.attempted("pending_merge", "t1", "u1", "a", T0).attempted(
        "pending_merge", "t1", "u1", "a", T0
    )
    q2 = q.enqueue(item("pending_merge", "a", 50, T0 + timedelta(days=1)))
    got = q2.get("pending_merge", "t1", "u1", "a")
    assert got and got.attempts == 2 and got.priority == 50 and got.first_seen == T0
    assert len(q2) == 3
    assert [i.ref for i in q2.ready(2)] == ["b", "u"] and [i.ref for i in q2.parked(2)] == ["a"]


def test_queue_acknowledge_blocks_requeue_and_drop_does_not():
    q = ReconcileQueue().enqueue(item(ref="a")).enqueue(item("contested_group", "g", 20))
    q = q.acknowledge("pending_merge", "t1", "u1", "a").drop("contested_group", "t1", "u1", "g")
    assert len(q) == 0
    assert len(q.enqueue(item(ref="a"))) == 0
    assert len(q.enqueue(item("contested_group", "g", 20))) == 1


def test_queue_caps_fire_early_pass():
    q = (
        ReconcileQueue()
        .enqueue(item(ref="a", first=T0))
        .enqueue(item(ref="b", first=T0 + timedelta(minutes=30)))
    )
    now = T0 + timedelta(minutes=45)
    assert q.oldest_age(now) == 45 * 60
    assert not q.over_cap(now, max_items=2, max_age_seconds=3600)
    assert q.over_cap(now, max_items=1)
    assert q.over_cap(now, max_age_seconds=40 * 60)
    assert not ReconcileQueue().over_cap(now, max_items=0, max_age_seconds=0)


def test_sweep_does_not_mutate_inputs():
    recs = [make("A", "2025-01-01"), make("B", "2025-02-01", trust=0.2, family="web")]
    before = [r.model_dump() for r in recs]
    q = ReconcileQueue()
    sweep(recs, reg(), queue=q)
    assert [r.model_dump() for r in recs] == before and len(q) == 0


# --------------------------------------------------------------------------- X-05 scoring

TRUST = {"default": 0.9, "low": 0.3}
SCORES: dict[str, float] = {}


def _lab(item: ContradictionItem, a: Assertion, n: int, session: str) -> AssertionEnvelope:
    time = TimeAxes(source_time=dt(a.source_time), recorded_at=dt(a.source_time))
    return AssertionEnvelope(
        write_id=f"w-{item.id}-{a.id}",
        assertion_key=AssertionKey(
            src=a.subject, rel=a.predicate, dst=a.value, kind=item.attribute
        ),
        occurrence_id=f"{item.id}-{a.id}",
        scope=OwnerScope(tenant="lab", owner="lab"),
        text=a.text,
        kind=item.attribute,
        subject=Mention(surface=a.subject, mention_id=f"m-{a.subject}-{a.id}"),
        attribute=item.attribute,
        value=a.value,
        time=time,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id=session, source_version=1, seg_index=n),
                source_family=a.source_family,
            )
        ],
        assessment=Assessment(
            source_trust=TRUST[a.source_trust],
            binding=a.binding,  # type: ignore[arg-type]
            modality=a.modality,  # type: ignore[arg-type]
            time_precision=time.event_precision,
        ),
        src_class="user_stated",
    )


def _sweep_item(it: ContradictionItem) -> tuple[ReconcilePlan, list[AssertionEnvelope]]:
    r = CardinalityRegistry()
    r.declare("lab", it.attribute, it.cardinality)  # type: ignore[arg-type]
    recs = [_lab(it, a, n, f"session-{n}") for n, a in enumerate(it.existing)]
    recs.append(
        _lab(it, it.incoming, 99, "session-late")
    )  # deposited in a later session, never integrated
    rep = sweep(recs, r)
    return rep.plan, apply_plan(rep.plan, recs)


def test_contest_and_conflict_recall_on_labelled_contradictions():
    data = load_contradictions()
    contest = [i for i in data.items if i.expected == "contest"]
    conflict = [i for i in data.items if i.expected in ("supersede", "contest", "retire")]
    benign = [i for i in data.items if i.expected in ("coexist", "tentative")]
    assert len(data) == 44 and contest and conflict and benign

    hit_contest = 0
    hit_conflict = 0
    missed: list[str] = []
    for it in conflict:
        plan, after = _sweep_item(it)
        if it.expected == "contest":
            ok = bool(plan.contest_flags)
            hit_contest += ok
        elif it.expected == "supersede":
            ok = any(r.status == "superseded" for r in after)
        else:  # retire: the named value's interval is closed
            ok = bool(plan.closures) or any(r.status == "superseded" for r in after)
        hit_conflict += ok
        if not ok:
            missed.append(it.id)
    false_flags = [
        i.id for i in benign if _sweep_item(i)[0].contest_flags
    ]  # coexist/tentative must not be flagged as contests
    SCORES["contest_recall"] = hit_contest / len(contest)
    SCORES["conflict_recall"] = hit_conflict / len(conflict)
    print(
        f"contest recall {hit_contest}/{len(contest)} = {SCORES['contest_recall']:.2f}; "
        f"conflict recall {hit_conflict}/{len(conflict)} = {SCORES['conflict_recall']:.2f}; "
        f"missed {missed}; false contest flags on benign items {false_flags}"
    )
    assert SCORES["contest_recall"] == pytest.approx(1.0)
    assert SCORES["conflict_recall"] >= 0.9
    assert not false_flags


def test_labelled_sweep_is_idempotent():
    for it in load_contradictions().items:
        r = CardinalityRegistry()
        r.declare("lab", it.attribute, it.cardinality)  # type: ignore[arg-type]
        recs = [_lab(it, a, n, f"s{n}") for n, a in enumerate(it.existing)]
        recs.append(_lab(it, it.incoming, 99, "s-late"))
        rep = sweep(recs, r)
        after = apply_plan(rep.plan, recs)
        assert sweep(after, r, queue=rep.queue).is_noop, it.id
