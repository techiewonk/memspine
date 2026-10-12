"""LIB-32: standing answers, changed-group invalidation, usefulness, standing instructions."""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime

import pytest

from memspine.derive.histories import value_history
from memspine.derive.integrate import CardinalityRegistry
from memspine.derive.standing import (
    GroupKey,
    HitEvent,
    QuestionKey,
    build,
    changed_groups,
    eviction_candidates,
    invalidate,
    live_instructions,
    mark_stale,
    propose_instruction,
    refresh,
    render_answer,
    select_for_context,
    select_instructions,
    usefulness,
)
from memspine.schemas.durable.assertion import AssertionEnvelope, Mention
from memspine.schemas.durable.assessment import Assessment
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
    attr: str = "residence",
    subject: str = "Ilse",
    owner: str = "u1",
    trust: float = 0.9,
    modality: str = "asserted",
    status: str = "active",
    src: str = "user_stated",
    oid: str | None = None,
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
                source_family="user",
            )
        ],
        assessment=Assessment(
            source_trust=trust,
            binding="bound",
            modality=modality,  # type: ignore[arg-type]
            time_precision=time.event_precision,
        ),
        status=status,  # type: ignore[arg-type]
        src_class=src,  # type: ignore[arg-type]
    )


def home(value: str, day: str, **kw) -> AssertionEnvelope:
    return make(f"Ilse lives in {value}", day, value=value, **kw)


def diet(value: str, day: str, **kw) -> AssertionEnvelope:
    return make(f"Ilse eats {value}", day, value=value, attr="diet", **kw)


def reg() -> CardinalityRegistry:
    r = CardinalityRegistry()
    r.declare("t1", "residence", "one")
    return r


def hist(*recs: AssertionEnvelope):
    return value_history(list(recs), reg())


def qk(subject="Ilse", attr="residence", view="current", as_of=None, owner="u1") -> QuestionKey:
    return QuestionKey("t1", owner, subject, attr, view, as_of)  # type: ignore[arg-type]


def g(subject="Ilse", attr="residence", owner="u1") -> GroupKey:
    return GroupKey.of("t1", owner, subject, attr)


# ------------------------------------------------------------------ build


def test_current_answer_has_value_support_and_group():
    a, b = home("Utrecht", "2025-01-01"), home("Delft", "2025-06-01")
    [ans] = build([qk()], hist(a, b), computed_at_seq=7)
    assert ans.values == ("Delft",)
    assert ans.support_ids == (b.occurrence_id,)
    assert ans.groups == (g(),)
    assert ans.computed_at_seq == 7 and ans.freshness == "fresh" and not ans.contested
    assert ans.trust == pytest.approx(0.9)


def test_key_normalisation_and_id_stability():
    assert qk("  ILSE ").answer_id == qk("ilse").answer_id
    assert qk(attr="Residence").group == g()
    assert qk(owner="u2").answer_id != qk().answer_id
    assert qk().answer_id != qk(view="history").answer_id


def test_as_of_validation():
    with pytest.raises(ValueError):
        QuestionKey("t1", "u1", "Ilse", "residence", "as_of", None)
    with pytest.raises(ValueError):
        QuestionKey("t1", "u1", "Ilse", "residence", "current", dt("2025-01-01"))


def test_as_of_and_history_views():
    a, b = home("Utrecht", "2025-01-01"), home("Delft", "2025-06-01")
    h = hist(a, b)
    [old] = build([qk(view="as_of", as_of=dt("2025-03-01"))], h)
    assert old.values == ("Utrecht",)
    [full] = build([qk(view="history")], h)
    assert full.values == ("Utrecht", "Delft")
    assert set(full.support_ids) == {a.occurrence_id, b.occurrence_id}


def test_many_valued_attribute_lists_all():
    h = hist(diet("vegetarian", "2025-01-01"), diet("no nuts", "2025-02-01"))
    [ans] = build([qk(attr="diet")], h)
    assert set(ans.values) == {"vegetarian", "no nuts"}
    assert not ans.contested


def test_unknown_question_gives_empty_answer_that_a_new_fact_invalidates():
    before = hist(home("Utrecht", "2025-01-01"))
    [ans] = build([qk(attr="diet")], before)
    assert ans.empty and ans.support_ids == ()
    after = hist(home("Utrecht", "2025-01-01"), diet("vegan", "2025-02-01"))
    assert invalidate([ans], changed_groups(before, after)) == [ans.id]


def test_tentative_never_supports_an_answer():
    h = hist(home("Utrecht", "2025-01-01"), home("Rome", "2025-07-01", modality="hypothetical"))
    [ans] = build([qk()], h)
    assert ans.values == ("Utrecht",)


def test_contested_values_make_a_contested_answer():
    a = home("Utrecht", "2025-01-01", trust=0.9)
    b = home(
        "Delft", "2025-02-01", trust=0.5
    )  # lower trust at cardinality one: contests, both kept
    [ans] = build([qk()], hist(a, b))
    assert ans.contested
    assert set(ans.values) == {"Delft", "Utrecht"}
    assert set(ans.support_ids) == {a.occurrence_id, b.occurrence_id}
    assert "contested" in render_answer(ans)


def test_dedup_order_and_determinism():
    h = hist(home("Utrecht", "2025-01-01"), diet("vegan", "2025-02-01"))
    keys = [qk(), qk(attr="diet"), qk(), qk(view="history")]
    first = build(keys, h, computed_at_seq=3)
    assert len(first) == 3
    shuffled = keys[:]
    for seed in range(5):
        random.Random(seed).shuffle(shuffled)
        assert build(shuffled, h, computed_at_seq=3) == first
    assert [a.id for a in first] == sorted(a.id for a in first)


def test_history_input_order_does_not_matter():
    recs = [home("Utrecht", "2025-01-01"), home("Delft", "2025-06-01"), diet("vegan", "2025-02-01")]
    keys = [qk(), qk(attr="diet"), qk(view="history")]
    base = build(keys, hist(*recs))
    for seed in range(6):
        r = recs[:]
        random.Random(seed).shuffle(r)
        got = build(keys, hist(*r))
        assert [(a.id, a.values, a.fingerprint) for a in got] == [
            (a.id, a.values, a.fingerprint) for a in base
        ]


# ------------------------------------------------------------------ invalidation


def test_invalidation_precision():
    u, v = home("Utrecht", "2025-01-01"), diet("vegan", "2025-02-01")
    d = home("Delft", "2025-06-01")
    answers = build([qk(), qk(attr="diet"), qk("Bram", "residence")], hist(u, v))
    ch = changed_groups(hist(u, v), hist(u, d, v))
    assert ch == {g()}
    assert invalidate(answers, ch) == [qk().answer_id]


def test_unrelated_changes_do_not_invalidate():
    u = home("Utrecht", "2025-01-01")
    answers = build([qk()], hist(u))
    extra = [
        make("Bram lives in Oslo", "2025-03-01", value="Oslo", subject="Bram"),
        diet("vegan", "2025-02-01"),
        home("Haarlem", "2025-03-01", owner="u2"),
    ]
    assert invalidate(answers, changed_groups(hist(u), hist(u, *extra))) == []
    assert invalidate(answers, []) == []


def test_tentative_only_change_does_not_invalidate():
    u = home("Utrecht", "2025-01-01")
    answers = build([qk()], hist(u))
    rome = home("Rome", "2025-07-01", modality="hypothetical")
    assert changed_groups(hist(u), hist(u, rome)) == frozenset()
    assert invalidate(answers, changed_groups(hist(u), hist(u, rome))) == []


def test_owner_isolation_in_group_keys():
    answers = build([qk()], hist(home("Utrecht", "2025-01-01")))
    assert invalidate(answers, [g(owner="u2")]) == []
    assert invalidate(answers, [g(owner="u1")]) == [answers[0].id]


def test_stale_and_refresh_keep_unchanged_identical():
    u, v, d = (
        home("Utrecht", "2025-01-01"),
        diet("vegan", "2025-02-01"),
        home("Delft", "2025-06-01"),
    )
    h1, h2 = hist(u, v), hist(u, d, v)
    answers = build([qk(), qk(attr="diet")], h1, computed_at_seq=1)
    stale = invalidate(answers, changed_groups(h1, h2))
    marked = mark_stale(answers, stale)
    by_id = {a.id: a for a in marked}
    assert by_id[qk().answer_id].freshness == "stale"
    assert by_id[qk(attr="diet").answer_id].freshness == "fresh"
    out = {a.id: a for a in refresh(marked, stale, h2, computed_at_seq=2)}
    assert out[qk().answer_id].values == ("Delft",)
    assert out[qk().answer_id].freshness == "fresh" and out[qk().answer_id].computed_at_seq == 2
    assert out[qk(attr="diet").answer_id] is {a.id: a for a in answers}[qk(attr="diet").answer_id]


def test_refresh_matches_full_rebuild():
    u, d = home("Utrecht", "2025-01-01"), home("Delft", "2025-06-01")
    h1, h2 = hist(u), hist(u, d)
    answers = build([qk()], h1, computed_at_seq=1)
    stale = invalidate(answers, changed_groups(h1, h2))
    assert refresh(answers, stale, h2, computed_at_seq=2) == build([qk()], h2, computed_at_seq=2)


def test_new_contest_changes_group():
    a = home("Utrecht", "2025-01-01")
    b = home("Delft", "2025-03-01", trust=0.5)
    assert changed_groups(hist(a), hist(a, b)) == {g()}


# ------------------------------------------------------------------ select_for_context


def test_select_exact_then_related_within_budget():
    h = hist(
        home("Utrecht", "2025-01-01"),
        diet("vegan", "2025-02-01"),
        make("Bram lives in Oslo", "2025-03-01", value="Oslo", subject="Bram"),
    )
    answers = build([qk(), qk(attr="diet"), qk("Bram")], h)
    sel = select_for_context(answers, qk(), 100)
    assert [a.key.attribute for a in sel.answers] == ["residence", "diet"]
    assert sel.cost <= 100
    tight = select_for_context(answers, qk(), 7)
    assert [a.key.attribute for a in tight.answers] == ["residence"]
    assert tight.dropped_over_budget
    assert select_for_context(answers, qk(), 0).answers == ()


def test_stale_exact_is_withheld_for_live_fallback():
    h = hist(home("Utrecht", "2025-01-01"), diet("vegan", "2025-02-01"))
    answers = mark_stale(build([qk(), qk(attr="diet")], h), [qk().answer_id])
    sel = select_for_context(answers, qk(), 100)
    assert sel.skipped_stale == (qk().answer_id,)
    assert all(a.id != qk().answer_id for a in sel.answers)
    assert [a.key.attribute for a in sel.answers] == ["diet"]


def test_no_cross_owner_and_no_empty_answers():
    h = hist(home("Utrecht", "2025-01-01"), home("Haarlem", "2025-01-01", owner="u2"))
    answers = build([qk(), qk(owner="u2"), qk(attr="diet")], h)
    sel = select_for_context(answers, qk(), 100)
    assert [a.key.owner for a in sel.answers] == ["u1"]
    assert all(not a.empty for a in sel.answers)


def test_contested_answer_is_selected_and_rendered_as_contested():
    h = hist(home("Utrecht", "2025-01-01", trust=0.9), home("Delft", "2025-02-01", trust=0.5))
    answers = build([qk()], h)
    sel = select_for_context(answers, qk(), 100)
    assert sel.answers and sel.answers[0].contested
    assert "contested" in render_answer(sel.answers[0])


def test_select_is_deterministic_under_input_order():
    h = hist(home("Utrecht", "2025-01-01"), diet("vegan", "2025-02-01"))
    answers = build([qk(), qk(attr="diet"), qk(view="history")], h)
    base = select_for_context(answers, qk(), 100)
    for seed in range(5):
        r = answers[:]
        random.Random(seed).shuffle(r)
        assert select_for_context(r, qk(), 100) == base


# ------------------------------------------------------------------ usefulness and eviction


def _three():
    h = hist(
        home("Utrecht", "2025-01-01"),
        diet("vegan", "2025-02-01"),
        make("Ilse likes tea", "2025-02-02", value="tea", attr="drink"),
    )
    return build([qk(), qk(attr="diet"), qk(attr="drink")], h, computed_at_seq=10)


def test_usefulness_counts_hits_misses_last_used():
    ans = _three()
    r, d, _ = (
        next(a for a in ans if a.key.attribute == x).id for x in ("residence", "diet", "drink")
    )
    log = [
        HitEvent(r, 11, "hit"),
        HitEvent(r, 15, "hit"),
        HitEvent(r, 12, "miss"),
        HitEvent(d, 13, "miss"),
        HitEvent("sa_unknown", 14, "hit"),
    ]
    st = {s.answer_id: s for s in usefulness(ans, log)}
    assert len(st) == 3
    assert (st[r].hits, st[r].misses, st[r].last_used_seq) == (2, 1, 15)
    assert (st[d].hits, st[d].misses, st[d].last_used_seq) == (0, 1, None)
    assert st[r].hit_rate == pytest.approx(2 / 3)
    other = next(i for i in st if i not in (r, d))
    assert st[other].samples == 0 and st[other].hit_rate == 0.0


def test_eviction_by_usefulness():
    ans = _three()
    ids = {a.key.attribute: a.id for a in ans}
    log = (
        [HitEvent(ids["residence"], 95, "hit")] * 3
        + [HitEvent(ids["diet"], 50, "miss")] * 6
        + [HitEvent(ids["diet"], 51, "hit")]
    )
    st = usefulness(ans, log)
    out = eviction_candidates(ans, st, now_seq=100, idle_after=30, min_samples=5, min_hit_rate=0.2)
    # residence is hot; diet has a poor hit rate; drink was never used and is idle since seq 10
    assert ids["residence"] not in out
    assert set(out) == {ids["diet"], ids["drink"]}
    assert eviction_candidates(ans, st, now_seq=100, idle_after=1000, min_samples=100) == []


def test_eviction_respects_size_cap_lowest_value_first():
    ans = _three()
    ids = {a.key.attribute: a.id for a in ans}
    log = [HitEvent(ids["residence"], 99, "hit")] * 4 + [HitEvent(ids["diet"], 98, "hit")]
    st = usefulness(ans, log)
    out = eviction_candidates(ans, st, now_seq=100, idle_after=1000, max_answers=2)
    assert out == [ids["drink"]]
    out2 = eviction_candidates(ans, st, now_seq=100, idle_after=1000, max_answers=1)
    assert out2 == [ids["drink"], ids["diet"]]


def test_eviction_deterministic():
    ans = _three()
    base = eviction_candidates(ans, usefulness(ans, []), now_seq=500, idle_after=10)
    for seed in range(5):
        r = ans[:]
        random.Random(seed).shuffle(r)
        assert eviction_candidates(r, usefulness(r, []), now_seq=500, idle_after=10) == base
    assert sorted(base) == sorted(a.id for a in ans)


# ------------------------------------------------------------------ standing instructions


def rule(text="Always answer in Dutch", **kw) -> AssertionEnvelope:
    return make(text, "2025-01-01", attr="instruction", subject="Ilse", **kw)


def test_instruction_from_firm_user_statement():
    inst = propose_instruction(rule(oid="oc_r"), created_seq=4, priority=80)
    assert inst is not None
    assert inst.scope == "global" and inst.priority == 80 and inst.source_ids == ("oc_r",)
    assert inst.id == propose_instruction(rule(oid="oc_other"), created_seq=9, priority=10).id


@pytest.mark.parametrize(
    "kw",
    [
        {"status": "tentative"},
        {"status": "contested"},
        {"status": "superseded"},
        {"status": "hidden"},
        {"modality": "hypothetical"},
        {"modality": "planned"},
        {"modality": "negated"},
        {"src": "assistant_proposed"},
        {"src": "derived"},
        {"src": "tool"},
        {"trust": 0.5},
    ],
)
def test_instruction_never_from_weak_source(kw):
    assert propose_instruction(rule(**kw), created_seq=1) is None


def test_user_confirmed_is_accepted():
    assert propose_instruction(rule(src="user_confirmed"), created_seq=1) is not None


def test_instruction_scope_rules():
    with pytest.raises(ValueError):
        propose_instruction(rule(), created_seq=1, scope="topic")
    with pytest.raises(ValueError):
        propose_instruction(rule(), created_seq=1, scope="global", scope_ref="x")
    with pytest.raises(ValueError):
        propose_instruction(rule(), created_seq=1, priority=101)


def test_live_instructions_drop_erased_sources():
    i1 = propose_instruction(rule(oid="oc_1"), created_seq=1)
    i2 = propose_instruction(rule("Keep replies short", oid="oc_2"), created_seq=2)
    assert live_instructions([i1, i2], {"oc_2"}) == [i2]
    assert live_instructions([i1, i2], set()) == []


def test_select_instructions_priority_scope_budget_and_owner():
    gl = propose_instruction(rule("Always answer in Dutch", oid="a"), created_seq=1, priority=50)
    hi = propose_instruction(rule("Never use bullet lists", oid="b"), created_seq=2, priority=90)
    tp = propose_instruction(
        rule("Cite the source file", oid="c"),
        created_seq=3,
        priority=50,
        scope="topic",
        scope_ref="Code Review",
    )
    ss = propose_instruction(
        rule("Be brief today", oid="d"), created_seq=4, priority=50, scope="session", scope_ref="s1"
    )
    dup = propose_instruction(rule("always answer in dutch!", oid="e"), created_seq=5, priority=50)
    foreign = propose_instruction(rule("Reply in French", oid="f", owner="u2"), created_seq=6)
    allx = [gl, hi, tp, ss, dup, foreign]
    out = select_instructions(
        allx, tenant="t1", owner="u1", session="s1", topics=["code review"], budget=100
    )
    assert [i.text for i in out] == [
        "Never use bullet lists",
        "Be brief today",
        "Cite the source file",
        "always answer in dutch!",
    ]
    assert foreign not in out
    assert len({i.text.lower().rstrip("!") for i in out}) == len(out)
    assert not any(
        i.scope == "session"
        for i in select_instructions(allx, tenant="t1", owner="u1", session="s2", budget=100)
    )
    assert not any(
        i.scope == "topic" for i in select_instructions(allx, tenant="t1", owner="u1", budget=100)
    )
    small = select_instructions(
        allx, tenant="t1", owner="u1", session="s1", topics=["code review"], budget=4
    )
    assert sum(len(i.text.split()) for i in small) <= 4
    assert select_instructions(allx, tenant="t1", owner="u1", budget=0) == []
    shuffled = allx[:]
    random.Random(3).shuffle(shuffled)
    assert (
        select_instructions(
            shuffled, tenant="t1", owner="u1", session="s1", topics=["code review"], budget=100
        )
        == out
    )
