"""LIB-03: deterministic grounding rule checks (W-17). Invented names only."""

from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path

import pytest
from labelled.loader import load_grounding

from memspine.derive.ground import (
    Cite,
    CitedSpan,
    Claim,
    WindowTurn,
    check_grounding,
    claim_from_envelope,
    find_normalised,
    normalise,
    turn_key,
    window_from_turns,
)
from memspine.schemas.durable.assertion import AssertionEnvelope
from memspine.schemas.durable.assessment import Assessment
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, Span, TurnRef
from memspine.schemas.durable.identity import AssertionKey, occurrence_id, write_id


def win(*rows: tuple[str, str, str]) -> dict[str, WindowTurn]:
    """Rows are (key, speaker, text)."""
    return {k: WindowTurn(key=k, speaker=s, text=t) for k, s, t in rows}


def claim(text, *keys, modality="asserted", binding=(), on=None, subject=None, spans=None):
    cites = [Cite(role="support", turn=k, span=(spans or {}).get(k)) for k in keys]
    cites += [Cite(role="binding", turn=k) for k in binding]
    return Claim(text=text, modality=modality, cites=tuple(cites), source_time=on, subject=subject)


def label(c, w):
    return check_grounding(c, w).label


# --- normalisation ------------------------------------------------------------------------------


def test_normalise_folds_case_space_unicode_accents_punctuation():
    assert normalise("  Ｃａｆé—Ünï  ") == "cafe uni"
    assert normalise("Straße") == "strasse"
    assert find_normalised("Wir sind  in KÖLN, ja.", "in köln") == (10, 17)
    assert find_normalised("abc", "xyz") is None
    assert find_normalised("abc", "  ") is None


# --- citations ----------------------------------------------------------------------------------


def test_missing_cited_turn_is_unsupported():
    w = win(("t1", "Lena", "I own a red bicycle."))
    r = check_grounding(claim("Lena owns a red bicycle.", "t9"), w)
    assert r.label == "unsupported"
    assert r.outcome("citation").status == "fail"
    assert r.outcome("citation").elements == ("t9",)


def test_no_support_or_binding_cites_is_unsupported_and_context_never_counts():
    w = win(("t1", "Lena", "I own a red bicycle."))
    c = Claim(text="Lena owns a bicycle.", cites=(Cite(role="context", turn="t1"),))
    assert check_grounding(c, w).label == "unsupported"
    assert check_grounding(Claim(text="x y", cites=()), w).label == "unsupported"


def test_plain_supported_case():
    w = win(("t1", "Lena", "I own a red bicycle and I ride it to work."))
    r = check_grounding(claim("Lena owns a red bicycle.", "t1"), w)
    assert r.label == "supported"
    assert r.undecided == ()
    assert r.spans and r.spans[0].turn == "t1" and "bicycle" in r.spans[0].text


# --- spans --------------------------------------------------------------------------------------


def test_span_exact_normalised_and_failures():
    text = "Ich wohne seit 2020 in Köln."
    w = win(("t1", "Jara", text))
    a = "Jara wohnt in Köln."
    exact = CitedSpan(start=text.index("in Köln"), end=len(text) - 1, quote="in Köln")
    r = check_grounding(claim(a, "t1", spans={"t1": exact}), w)
    assert r.outcome("span").status == "pass" and r.spans[0].how == "exact"

    loose = CitedSpan(start=0, end=3, quote="IN   KOLN")
    r = check_grounding(claim(a, "t1", spans={"t1": loose}), w)
    assert r.outcome("span").status == "pass"
    assert r.spans[0].how == "normalised" and r.spans[0].text == "in Köln"

    absent = CitedSpan(start=0, end=4, quote="in Bonn")
    r = check_grounding(claim(a, "t1", spans={"t1": absent}), w)
    assert r.outcome("span").status == "fail" and r.label == "unsupported"

    outside = CitedSpan(start=5, end=999)
    assert (
        check_grounding(claim(a, "t1", spans={"t1": outside}), w).outcome("span").status == "fail"
    )

    bare = CitedSpan(start=0, end=3)
    r = check_grounding(claim(a, "t1", spans={"t1": bare}), w)
    assert r.outcome("span").status == "pass" and r.spans[0].how == "bounds"


# --- numbers and units --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("turn", "assertion", "expected"),
    [
        ("Ik heb vier katten.", "Wout has 4 cats.", "pass"),
        ("Tengo veinte libros.", "Rosa has 20 books.", "pass"),
        ("Das Haus kostet 250.000 Euro.", "Karl's house costs 250,000 euro.", "pass"),
        ("I walked twelve kilometres today.", "Sunil walked 12 km.", "pass"),
        ("I walked twelve kilometres today.", "Sunil walked 21 km.", "fail"),
        ("I walked twelve kilometres today.", "Sunil walked twelve minutes.", "fail"),
        ("Ich bin dreißig Jahre alt.", "Mina is 30 years old.", "pass"),
        ("家賃は八万円です。", "Mina pays 80,000 yen.", "pass"),
        ("家賃は八万円です。", "Mina pays 90,000 yen.", "fail"),
    ],
)
def test_numbers_and_units(turn, assertion, expected):
    speaker = assertion.split()[0].removesuffix("'s")
    r = check_grounding(claim(assertion, "t1"), win(("t1", speaker, turn)))
    assert r.outcome("numbers").status == expected


def test_a_week_is_one_week_not_four_nights():
    w = win(("t1", "Wout", "We rented a cabin for four nights."))
    r = check_grounding(claim("Wout rented a cabin for a week.", "t1"), w)
    assert r.outcome("numbers").status == "fail"


# --- names, months, weekdays --------------------------------------------------------------------


def test_wrong_name_fails_and_exonym_variant_passes():
    w = win(("t1", "Jara", "Ik woon in Lissabon."))
    assert (
        check_grounding(claim("Jara lives in Lisbon.", "t1"), w).outcome("names").status == "pass"
    )
    assert (
        check_grounding(claim("Jara lives in Madrid.", "t1"), w).outcome("names").status == "fail"
    )


def test_name_in_a_script_the_source_does_not_use_is_undecided_not_failed():
    w = win(("t1", "Aiko", "来月、京都に引っ越す予定です。"))
    r = check_grounding(claim("Aiko moves to Kyoto next month.", "t1", modality="planned"), w)
    assert r.outcome("names").status == "undecided"
    assert r.label == "undecided"


def test_month_and_weekday_are_matched_across_languages():
    w = win(("t1", "Wout", "Am Dienstag im Oktober fahre ich nach Hamburg."))
    ok = check_grounding(claim("Wout travels to Hamburg on Tuesday in October.", "t1"), w)
    assert ok.outcome("dates").status == "pass"
    bad = check_grounding(claim("Wout travels to Hamburg on Friday in October.", "t1"), w)
    assert bad.outcome("dates").status == "fail"


# --- relative dates and binding to the source time ---------------------------------------------


@pytest.mark.parametrize(
    ("turn", "iso", "expected"),
    [
        ("The vote happened yesterday.", "2026-03-09", "pass"),
        ("The vote happened yesterday.", "2026-03-10", "fail"),
        ("Gestern war die Abstimmung.", "2026-03-09", "pass"),
        ("De stemming is morgen.", "2026-03-11", "pass"),
        ("Es la semana del mercado, ayer cerramos.", "2026-03-09", "pass"),
        ("We met last Friday.", "2026-03-06", "pass"),
        ("We meet next Monday.", "2026-03-16", "pass"),
        ("We meet next Monday.", "2026-03-09", "fail"),
    ],
)
def test_relative_dates_resolve_against_source_time(turn, iso, expected):
    anchor = date(2026, 3, 10)  # a Tuesday
    r = check_grounding(
        claim(f"Eda's event is on {iso}.", "t1", on=anchor), win(("t1", "Eda", turn))
    )
    assert r.outcome("binding").status == expected
    if expected == "fail":
        assert r.label == "unsupported"


def test_relative_date_without_anchor_is_unsupported():
    r = check_grounding(
        claim("Eda signed on 2026-03-06.", "t1"), win(("t1", "Eda", "I signed last Friday."))
    )
    assert r.label == "unsupported" and r.outcome("binding").status == "fail"


# --- polarity and modality ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("turn", "assertion", "expected"),
    [
        ("I do not own a car.", "Pim owns a car.", "unsupported"),
        ("Ich habe keine Geschwister.", "Pim has siblings.", "unsupported"),
        ("Ik heb geen auto.", "Pim has a car.", "unsupported"),
        ("No tengo coche.", "Pim has a car.", "unsupported"),
        ("私は車を持っていません。", "Pim has a car.", "unsupported"),
    ],
)
def test_flipped_polarity_is_unsupported(turn, assertion, expected):
    r = check_grounding(claim(assertion, "t1"), win(("t1", "Pim", turn)))
    assert r.label == expected
    assert r.outcome("polarity").status == "fail"


def test_negated_claim_matches_negated_support_and_negation_in_a_condition_is_ignored():
    w = win(("t1", "Pim", "I don't own a car any more."))
    assert (
        check_grounding(claim("Pim does not own a car.", "t1", modality="negated"), w).label
        == "supported"
    )
    w2 = win(("t1", "Pim", "Wenn es nicht regnet, gehe ich gern ins Museum."))
    r = check_grounding(claim("Pim goes to the museum if it does not rain.", "t1"), w2)
    assert r.outcome("polarity").status != "fail"


@pytest.mark.parametrize(
    "turn",
    [
        "If I won the lottery I would buy a boat.",
        "Maybe I will buy a boat.",
        "Misschien koop ik een boot.",
        "Vielleicht kaufe ich ein Boot.",
        "Quizás compre un barco.",
        "Do you think I should buy a boat?",
    ],
)
def test_non_asserted_cues_contradict_an_asserted_claim(turn):
    r = check_grounding(claim("Pim bought a boat.", "t1"), win(("t1", "Pim", turn)))
    assert r.label == "unsupported" and r.outcome("modality").status == "fail"


def test_hearsay_alone_is_unsupported_but_with_an_extra_item_it_is_partly():
    w = win(("t1", "Pim", "My cousin says the bakery on Pine Road opens at nine."))
    assert label(claim("The bakery on Pine Road opens at nine.", "t1"), w) == "unsupported"
    assert (
        label(claim("The bakery on Pine Road opens at nine.", "t1", modality="reported"), w)
        == "supported"
    )


def test_modality_cue_on_only_part_of_the_claim_gives_partly():
    w = win(
        (
            "t1",
            "Pim",
            "We are leaving for Tallinn in May, and I would love to see Riga if there is time.",
        )
    )
    r = check_grounding(
        claim("Pim will visit Tallinn and Riga in May.", "t1", modality="planned"), w
    )
    assert r.label == "partly"


# --- speaker and binding ------------------------------------------------------------------------


def test_pronoun_needs_cited_binding_evidence():
    w = win(
        ("t1", "Eda", "My colleague Sander plays the cello."),
        ("t2", "Eda", "He practises every evening."),
    )
    a = "Sander practises every evening."
    assert label(claim(a, "t2"), w) == "unsupported"  # pronoun, nothing cited to bind it
    r = check_grounding(claim(a, "t2", binding=["t1"]), w)
    assert r.label == "supported" and r.outcome("binding").status == "pass"


def test_binding_turn_with_two_people_is_undecided():
    w = win(
        ("t1", "Eda", "My brother Sander and my aunt Lotte came over."),
        ("t2", "Eda", "She baked a cake."),
    )
    r = check_grounding(claim("Lotte baked a cake.", "t2", binding=["t1"]), w)
    assert r.label == "undecided" and r.outcome("binding").status == "undecided"


def test_binding_turn_that_does_not_name_the_subject_fails():
    w = win(
        ("t1", "Eda", "My colleague Sander plays the cello."), ("t2", "Eda", "He practises daily.")
    )
    assert label(claim("Lotte practises daily.", "t2", binding=["t1"]), w) == "unsupported"


def test_statement_about_a_neighbour_is_not_the_speaker_s():
    w = win(("t1", "Eda", "Our neighbour Tomas keeps goats."))
    r = check_grounding(claim("Eda keeps goats.", "t1"), w)
    assert r.label == "unsupported" and r.outcome("subject").status == "fail"


def test_first_person_statement_by_a_different_speaker_is_rejected():
    w = win(
        ("t1", "Eda", "My sister Ruut lives in Tartu."),
        ("t2", "Ruut", "I study chemistry in Tartu."),
    )
    # the claim is about Eda but the matching first-person clause is Ruut's
    r = check_grounding(claim("Eda studies chemistry in Tartu.", "t2"), w)
    assert r.label == "unsupported"


# --- content ------------------------------------------------------------------------------------


def test_coordinated_extra_item_gives_partly():
    w = win(("t1", "Eda", "I grow tomatoes at the allotment."))
    r = check_grounding(claim("Eda grows tomatoes and beans at the allotment.", "t1"), w)
    assert r.label == "partly" and r.outcome("content").status == "fail"
    assert "content:beans" in r.failing


def test_unmatched_paraphrase_and_other_language_are_undecided_never_guessed():
    w = win(("t1", "Eda", "I enjoy hiking in the hills."))
    assert label(claim("Eda likes trekking in the hills.", "t1"), w) == "undecided"
    w2 = win(("t1", "Eda", "Ich arbeite als Hebamme."))
    assert label(claim("Eda is a midwife.", "t1"), w2) == "undecided"


def test_value_subject_and_object_fields_are_probed():
    w = win(("t1", "Eda", "I drive a Saab."))
    ok = Claim(text="Eda drives a car.", value="Saab", cites=(Cite(role="support", turn="t1"),))
    bad = Claim(text="Eda drives a car.", value="Volvo", cites=(Cite(role="support", turn="t1"),))
    assert check_grounding(ok, w).label in ("supported", "undecided")
    assert check_grounding(bad, w).outcome("content").status != "pass"


# --- result projections and envelope adapter ----------------------------------------------------


def test_to_checks_and_to_grounding_projection():
    w = win(("t1", "Wout", "I cycled sixty kilometres on Saturday."))
    good = check_grounding(claim("Wout cycled 60 km on Saturday.", "t1"), w)
    assert good.label == "supported" and good.to_grounding() == "grounded"
    assert good.to_checks().numbers is True and good.to_checks().span is None
    bad = check_grounding(claim("Wout cycled 80 km on Saturday.", "t1"), w)
    assert bad.to_checks().numbers is False
    assert bad.label == "partly" and bad.to_grounding() == "uncertain"
    assert check_grounding(claim("Wout cycled 60 km.", "gone"), w).to_grounding() == "rejected"


def test_envelope_adapter_roundtrip():
    ref = TurnRef(source_id="src1", source_version=1, seg_index=0)
    scope = OwnerScope(tenant="t", owner="o")
    key = AssertionKey(src="ilse", rel="lives_in", dst="utrecht", kind="state")
    env = AssertionEnvelope(
        write_id=write_id(scope, "c", 0, "x"),
        assertion_key=key,
        occurrence_id=occurrence_id("e", ["s#0"], key),
        scope=scope,
        text="Ilse lives in Utrecht.",
        kind="state",
        evidence=[
            Evidence(
                role="support",
                turn=ref,
                source_family="ilse",
                span=Span(start=2, end=18, quote="live in Utrecht"),
            )
        ],
        assessment=Assessment(source_trust=0.8, binding="bound", modality="asserted"),
        src_class="user_stated",
    )
    w = window_from_turns([(ref, "I live in Utrecht with my cat.", "Ilse")])
    c = claim_from_envelope(env)
    assert c.cites[0].turn == turn_key(ref) and c.modality == "asserted"
    r = check_grounding(c, w)
    assert r.label == "supported"
    assert r.outcome("span").status == "pass"
    gone = check_grounding(c, {})
    assert gone.label == "unsupported"


def test_deterministic_and_pure():
    w = win(("t1", "Eda", "I grow tomatoes."))
    c = claim("Eda grows tomatoes and beans.", "t1")
    assert check_grounding(c, w) == check_grounding(c, w)
    assert check_grounding(c, list(w.values())) == check_grounding(c, w)


# --- scoring against the X-05 grounding set -----------------------------------------------------

LABELS = ("supported", "partly", "unsupported")


def _cites_for(it, spans: bool):
    """A faithful extractor cites the support turns (and binding turns). For items labelled
    unsupported nothing supports them, so the extractor cites every turn of the snippet."""
    by_id = {t.turn_id: t for t in it.turns}
    if it.support_turns:
        cites = [Cite(role="support", turn=k) for k in it.support_turns]
        if spans and it.span is not None:
            cites[0] = Cite(
                role="support",
                turn=it.span.turn_id,
                span=CitedSpan(start=it.span.start, end=it.span.end, quote=it.span.text),
            )
        cites += [Cite(role="binding", turn=k) for k in it.binding_turns]
    else:
        cites = [Cite(role="support", turn=k) for k in by_id]
    return cites


def _run(it, spans: bool = False):
    w = {t.turn_id: WindowTurn(key=t.turn_id, speaker=t.speaker, text=t.text) for t in it.turns}
    on = date.fromisoformat(it.source_time) if it.source_time else None
    c = Claim(
        text=it.assertion,
        modality=it.claimed_modality,
        source_time=on,
        cites=tuple(_cites_for(it, spans)),
    )
    return check_grounding(c, w)


def score():
    items = load_grounding().items
    results = [(it, _run(it)) for it in items]
    conf = Counter((it.label, r.label) for it, r in results)
    table = {}
    for lab in LABELS:
        tp = conf[(lab, lab)]
        predicted = sum(conf[(g, lab)] for g in LABELS)
        gold = sum(conf[(lab, p)] for p in (*LABELS, "undecided"))
        table[lab] = {
            "precision": tp / predicted if predicted else None,
            "recall": tp / gold if gold else None,
            "predicted": predicted,
            "gold": gold,
        }
    undecided = sum(1 for _, r in results if r.label == "undecided")
    wrong = [(it, r) for it, r in results if r.label not in (it.label, "undecided")]
    return {
        "n": len(results),
        "table": table,
        "undecided_rate": undecided / len(results),
        "wrong": wrong,
        "conf": conf,
    }


def test_scorer_on_the_x05_grounding_set(capsys):
    s = score()
    with capsys.disabled():
        print(
            f"\nLIB-03 grounding.v1 scorer (n={s['n']}, undecided rate {s['undecided_rate']:.3f})"
        )
        for lab, row in s["table"].items():
            p = "n/a" if row["precision"] is None else f"{row['precision']:.3f}"
            r = "n/a" if row["recall"] is None else f"{row['recall']:.3f}"
            print(
                f"  {lab:12s} precision {p}  recall {r}  (predicted {row['predicted']}, gold {row['gold']})"
            )
        for it, r in s["wrong"]:
            print(
                f"  MISCLASSIFIED {it.id}: gold {it.label}/{it.failure}, got {r.label}: {r.reason[:100]}"
            )
    assert s["n"] == 62
    unsup = s["table"]["unsupported"]
    assert unsup["predicted"] > 0
    assert unsup["precision"] >= 0.9
    assert 0.0 <= s["undecided_rate"] < 1.0


def test_given_gold_spans_are_located_exactly_and_not_misread():
    items = load_grounding().items
    seen = 0
    for it in items:
        if it.span is None:
            continue
        r = _run(it, spans=True)
        seen += 1
        assert r.outcome("span").status == "pass", it.id
        assert r.spans[0].how == "exact" and r.spans[0].text == it.span.text, it.id
    assert seen == 41


def test_labelled_file_is_the_expected_version():
    head = json.loads(
        Path(load_grounding.__globals__["HERE"], "grounding.v1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert head["schema"] == "grounding/1"
