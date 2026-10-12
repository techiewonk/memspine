"""LIB-17: extraction coverage check and targeted re-extraction plan (ER-12 B, ER-15)."""

from __future__ import annotations

import pytest

from memspine.derive.coverage import (
    CoverageBudget,
    WindowLine,
    coverage,
    plan_reextraction,
)
from memspine.schemas.durable import (
    AssertionEnvelope,
    AssertionKey,
    Assessment,
    Evidence,
    Interval,
    OwnerScope,
    TemporalRef,
    TimeAxes,
    TurnRef,
    occurrence_id,
    write_id,
)

SCOPE = OwnerScope(tenant="t", owner="o")


def env(
    rid: str,
    text: str,
    cites: tuple[int, ...] = (0,),
    *,
    binding: tuple[int, ...] = (),
    when: str | None = None,
    at: str | None = None,
) -> AssertionEnvelope:
    key = AssertionKey(src=rid, rel="r", dst="d", kind="state")
    ev = [
        Evidence(
            role="support",
            turn=TurnRef(source_id="s", source_version=1, seg_index=i),
            source_family="spk",
        )
        for i in cites
    ]
    ev += [
        Evidence(
            role="binding",
            turn=TurnRef(source_id="s", source_version=1, seg_index=i),
            source_family="spk",
        )
        for i in binding
    ]
    time = TimeAxes()
    prec = "unknown"
    if at:
        ref = TemporalRef(text=when, precision="day", basis="explicit", at=at)
        time = TimeAxes(event=Interval(start=ref))
        prec = "day"
    return AssertionEnvelope(
        write_id=write_id(SCOPE, "c", 0, rid),
        assertion_key=key,
        occurrence_id=occurrence_id("e", [f"s#{cites[0]}"], key),
        record_id=rid,
        scope=SCOPE,
        text=text,
        kind="state",
        evidence=ev,
        time=time,
        assessment=Assessment(
            source_trust=0.5, binding="bound", modality="asserted", time_precision=prec
        ),
        src_class="user_stated",
    )


def lines(*texts: str, new: bool = True) -> list[WindowLine]:
    return [WindowLine(i, t, new) for i, t in enumerate(texts)]


def by_idx(report):
    return {ln.seg_index: ln for ln in report.lines}


# --- coverage ------------------------------------------------------------------------------------


def test_fully_covered_window():
    w = lines("I live in Lindenau with my dog Bruno", "ok", "I started at Quorvex on 5 May 2024")
    envs = [
        env("a", "Speaker lives in Lindenau; dog named Bruno", (0,)),
        env(
            "b",
            "Speaker started at Quorvex",
            (2,),
            when="5 May 2024",
            at="2024-05-05T00:00:00+00:00",
        ),
    ]
    r = coverage(w, envs)
    got = by_idx(r)
    assert (
        got[0].status == "covered" and got[2].status == "covered" and got[1].status == "not_salient"
    )
    assert r.ratio == 1.0 and r.salient_lines == 2 and r.omission_count == 0
    assert plan_reextraction(r).windows == ()


def test_missed_line_with_date_and_name():
    w = lines(
        "I live in Lindenau", "haha", "Zdenek Okafor moved to Brno on 14 March 2023", "thanks"
    )
    r = coverage(w, [env("a", "Speaker lives in Lindenau", (0,))])
    got = by_idx(r)
    assert got[2].status == "uncovered_salient"
    assert {"entity", "date"} <= set(got[2].signals)
    kinds = {(o.kind, o.text) for o in got[2].omissions}
    assert ("entity", "Zdenek Okafor") in kinds
    assert any(k == "date" and "March" in t for k, t in kinds)
    assert r.ratio == pytest.approx(0.5)
    plan = plan_reextraction(r)
    assert plan.target_indices == (2,) and plan.windows[0].purpose == "coverage"
    assert plan.windows[0].context_indices  # neighbours included for reference resolution


def test_binding_evidence_covers_and_uncited_does_not():
    w = lines("Kasimir lives in Gdansk")
    assert by_idx(coverage(w, []))[0].status == "uncovered_salient"
    b = coverage(w, [env("b", "Kasimir lives in Gdansk", (3,), binding=(0,))])
    assert by_idx(b)[0].status == "covered"
    other = coverage(w, [env("c", "Kasimir lives in Gdansk", (3,))])
    assert by_idx(other)[0].status == "uncovered_salient"


def test_non_new_lines_are_context_only():
    w = [WindowLine(0, "Yesterday Ingrid called Oskar", is_new=False), WindowLine(1, "ok")]
    r = coverage(w, [])
    assert [ln.seg_index for ln in r.lines] == [1] and r.ratio == 1.0


def test_omission_in_covered_line():
    w = lines("Mira bought 250 shares of Zentrix and visited Tallinn")
    r = coverage(w, [env("a", "Mira bought shares of Zentrix", (0,))])
    ln = by_idx(r)[0]
    assert ln.status == "covered"
    om = {(o.kind, o.text) for o in ln.omissions}
    assert ("number", "250") in om and ("entity", "Tallinn") in om
    assert ("entity", "Zentrix") not in om and ("entity", "Mira") not in om
    plan = plan_reextraction(r)
    assert plan.target_indices == (0,)
    assert plan_reextraction(r, include_omissions=False).windows == ()


def test_number_formats_and_resolved_date_reflected():
    w = lines("Pieter paid 1,500 euros on 3 June")
    e = env("a", "Pieter paid 1500 euros", (0,), when="3 June", at="2024-06-03T00:00:00+00:00")
    assert by_idx(coverage(w, [e]))[0].omissions == ()


def test_duplicate_indices_rejected():
    with pytest.raises(ValueError):
        coverage([WindowLine(0, "a"), WindowLine(0, "b")], [])


# --- multilingual signals ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "signal"),
    [
        ("I live in a small flat near the river", "first_person"),
        ("Ich wohne seit Jahren in einer kleinen Wohnung", "first_person"),
        ("Ik woon al jaren in een klein appartement", "first_person"),
        ("Vivo en una casa pequeña cerca del río", "first_person"),
        ("Me gusta mucho el café con leche", "first_person"),
        ("Ich fahre am 12. Mai nach Wien", "date"),
        ("Ze komt volgende week terug", "date"),
        ("Gestern habe ich lange geschlafen", "date"),
        ("Nos vemos el lunes por la tarde", "date"),
        ("Hace 3 años fuimos a la costa", "date"),
        ("Das kostet 49 Euro im Monat", "number"),
        ("Ik heb Wilhelmina ontmoet in Utrecht", "entity"),
        ("Hablé con Itziar en Bilbao", "entity"),
        ("彼はタナカさんに会った", "entity"),
        ("我住在上海", "first_person"),
        ("Shopping list: eggs, flour, butter, and honey", "list"),
        ("- buy tickets", "list"),
        ("Привет, это Святослав из Киева", "entity"),
    ],
)
def test_signals_multilingual(text, signal):
    ln = by_idx(coverage(lines(text), []))[0]
    assert signal in ln.signals, ln


@pytest.mark.parametrize(
    "text",
    [
        "ok",
        "Ok.",
        "haha",
        "Haha, ok!",
        "danke",
        "Danke!",
        "Bitte, gern geschehen",
        "gracias",
        "Hola, vale",
        "bedankt",
        "Thanks!! :)",
        "I'm fine, thanks",
        "Hallo",
        "lol",
        "Danke für den Kaffee",
        "Ja, genau",
        "",
        "...",
    ],
)
def test_no_false_salience_on_filler(text):
    r = coverage([WindowLine(0, text)], [])
    ln = by_idx(r)[0]
    assert ln.status == "not_salient" and ln.score < 1.5, ln
    assert plan_reextraction(r).windows == ()


def test_german_nouns_not_entities_without_cue():
    ln = by_idx(coverage(lines("Wir haben einen Hund und eine Katze"), []))[0]
    assert "entity" not in ln.signals
    ln = by_idx(coverage(lines("Wir fahren nach Hamburg"), []))[0]
    assert "entity" in ln.signals


def test_sentence_initial_singleton_is_not_entity():
    assert "entity" not in by_idx(coverage(lines("Berlin is big"), []))[0].signals
    assert "entity" in by_idx(coverage(lines("Anna Kowalska is big"), []))[0].signals
    assert (
        by_idx(coverage(lines("We met Ludwig van Beethoven"), []))[0].spans[0].text
        == "Ludwig van Beethoven"
    )


# --- re-extraction plan --------------------------------------------------------------------------


def _long_window():
    return lines(
        "hi",
        "Dagny Halvorsen lives in Tromso",
        "ok",
        "ok",
        "ok",
        "ok",
        "ok",
        "I plan to move to Lisbon on 1 July 2025",
        "thanks",
        "Rutger Kok owes me 300 euros",
    )


def test_plan_groups_and_prioritises():
    r = coverage(_long_window(), [])
    plan = plan_reextraction(r, CoverageBudget(max_calls=5, max_tokens=10_000))
    assert plan.windows[0].priority >= plan.windows[-1].priority
    assert sorted(plan.target_indices) == [1, 7, 9]
    assert any(w.target_indices == (7, 9) for w in plan.windows)
    for w in plan.windows:
        assert set(w.target_indices) | set(w.context_indices) == set(w.line_indices)
        assert list(w.line_indices) == sorted(w.line_indices)
    assert plan.skipped == ()


def test_budget_calls_exhausted():
    r = coverage(_long_window(), [])
    plan = plan_reextraction(r, CoverageBudget(max_calls=1, max_tokens=10_000))
    assert plan.calls_planned == 1 and plan.calls_remaining == 0
    assert {s.reason for s in plan.skipped} == {"budget_calls"}
    assert set(plan.target_indices) | {s.seg_index for s in plan.skipped} == {1, 7, 9}
    spent = plan_reextraction(r, CoverageBudget(max_calls=2, calls_used=2))
    assert spent.windows == () and {s.reason for s in spent.skipped} == {"budget_calls"}


def test_budget_tokens_exhausted_and_drops_lowest_priority():
    r = coverage(_long_window(), [])
    full = plan_reextraction(r, CoverageBudget(max_calls=5, max_tokens=10_000))
    big = max(full.windows, key=lambda w: len(w.target_indices))
    tight = plan_reextraction(r, CoverageBudget(max_calls=5, max_tokens=big.est_tokens - 1))
    assert any(s.reason == "budget_tokens" for s in tight.skipped)
    assert tight.tokens_planned <= big.est_tokens - 1
    none = plan_reextraction(r, CoverageBudget(max_calls=5, max_tokens=10))
    assert none.windows == () and {s.reason for s in none.skipped} == {"budget_tokens"}
    used = plan_reextraction(r, CoverageBudget(max_calls=5, max_tokens=1000, tokens_used=1000))
    assert used.windows == ()


def test_never_reextracts_a_line_twice():
    r = coverage(_long_window(), [])
    first = plan_reextraction(r, CoverageBudget(max_calls=1, max_tokens=10_000))
    b2 = CoverageBudget(
        max_calls=3,
        max_tokens=10_000,
        calls_used=first.calls_planned,
        tokens_used=first.tokens_planned,
        already_reextracted=first.reextracted_after,
    )
    second = plan_reextraction(r, b2)
    assert not set(first.target_indices) & set(second.target_indices)
    assert "already_reextracted" in {s.reason for s in second.skipped}
    done = second.reextracted_after
    third = plan_reextraction(
        r, CoverageBudget(max_calls=9, max_tokens=99999, already_reextracted=done)
    )
    assert third.windows == () or not set(third.target_indices) & set(done)
    last = plan_reextraction(
        r, CoverageBudget(max_calls=9, max_tokens=99999, already_reextracted=frozenset({1, 7, 9}))
    )
    assert last.windows == () and {s.reason for s in last.skipped} == {"already_reextracted"}


def test_plan_is_idempotent_and_deterministic():
    r = coverage(_long_window(), [])
    b = CoverageBudget(max_calls=2, max_tokens=500)
    assert plan_reextraction(r, b) == plan_reextraction(r, b)
    assert coverage(_long_window(), []) == r


def test_max_targets_per_window_and_custom_counter():
    w = lines(*[f"Quillan met Odette Brandt in Zurich {i}" for i in range(5)])
    r = coverage(w, [])
    plan = plan_reextraction(
        r,
        CoverageBudget(max_calls=9, max_tokens=99999, max_targets_per_window=2),
        count_tokens=lambda t: 10,
    )
    assert max(len(x.target_indices) for x in plan.windows) == 2
    assert sorted(plan.target_indices) == [0, 1, 2, 3, 4]


def test_invalid_budget():
    with pytest.raises(ValueError):
        CoverageBudget(max_calls=-1)
    with pytest.raises(ValueError):
        CoverageBudget(max_targets_per_window=0)
