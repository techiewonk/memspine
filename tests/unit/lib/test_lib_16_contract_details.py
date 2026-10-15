# ruff: noqa: E501
"""LIB-16: details of the rule contract (time anchoring, language, personal flag, header). Invented names only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from memspine.read.contract_rules import TypedContract, build_contract, render_header
from memspine.schemas.steps.shared import QueryContract

NOW = datetime(2025, 5, 14, 10, 0, tzinfo=UTC)  # a Wednesday


def D(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def test_deterministic() -> None:
    for q in (
        "When did Ilse last visit Zorbia in 2023?",
        "Wie viele Bücher habe ich letzte Woche gelesen?",
        "Hoeveel keer ben ik naar Gent geweest?",
        "¿Cuánto tiempo vivió Marta en Utrecht?",
        "Blorp?",
    ):
        a = build_contract(q, now=NOW, lang_hint=None)
        b = build_contract(q, now=NOW, lang_hint=None)
        assert a == b
        assert a.model_dump_json() == b.model_dump_json()
        assert render_header(a) == render_header(b)


def test_empty_and_garbage_never_raise() -> None:
    for q in ("", "   ", "?", "12345", "\u0000\u0001", "x" * 5000):
        c = build_contract(q, now=NOW, lang_hint=None)
        assert c.answer_type == "unknown"
        assert c.shape == "unknown"
        assert c.personal is None
        assert "answer_type" in c.unknown
    c = build_contract(None, now=None, lang_hint=None)  # type: ignore[arg-type]
    assert c.answer_type == "unknown"


def test_relative_phrases_anchor_and_stay_unanchored_without_now() -> None:
    c = build_contract("What did Ilse do last week?", now=NOW, lang_hint=None)
    assert c.time_view.kind == "range"
    assert (c.time_view.start, c.time_view.end) == (D(2025, 5, 5), D(2025, 5, 12))
    assert c.time_view.anchored is True
    assert c.time_view.precision == "week"
    u = build_contract("What did Ilse do last week?", now=None, lang_hint=None)
    assert u.time_view.kind == "range"
    assert u.time_view.anchored is False
    assert (u.time_view.start, u.time_view.end) == (None, None)
    assert u.time_view.phrase == "last week"
    y = build_contract("What did Ilse do in 2021?", now=None, lang_hint=None)
    assert (y.time_view.start, y.time_view.end, y.time_view.anchored) == (
        D(2021, 1, 1),
        D(2022, 1, 1),
        True,
    )


@pytest.mark.parametrize(
    ("q", "start", "end"),
    [
        ("Was hat Bram vor 2 Tagen gekocht?", D(2025, 5, 12), D(2025, 5, 13)),
        ("Wat heeft Bram 3 weken geleden gekookt?", D(2025, 4, 21), D(2025, 4, 28)),
        ("¿Qué cocinó Bram hace 3 días?", D(2025, 5, 11), D(2025, 5, 12)),
        ("¿Qué cocinó Bram la semana pasada?", D(2025, 5, 5), D(2025, 5, 12)),
        ("What did Bram cook today?", D(2025, 5, 14), D(2025, 5, 15)),
        ("Was hat Bram letzten Monat gekocht?", D(2025, 4, 1), D(2025, 5, 1)),
        ("¿Qué hará Bram dentro de 2 semanas?", D(2025, 5, 26), D(2025, 6, 2)),
    ],
)
def test_relative_phrases_four_languages(q: str, start: datetime, end: datetime) -> None:
    c = build_contract(q, now=NOW, lang_hint=None)
    assert c.time_view.kind == "range", q
    assert (c.time_view.start, c.time_view.end) == (start, end), q


def test_date_without_year_is_never_completed() -> None:
    c = build_contract("What did Bram do on 3 March?", now=NOW, lang_hint=None)
    assert c.time_view.kind == "range"
    assert c.time_view.anchored is False
    assert (c.time_view.start, c.time_view.end) == (None, None)


def test_may_alone_is_a_modal_not_a_month() -> None:
    c = build_contract("Which flights may Ilse have booked?", now=NOW, lang_hint=None)
    assert c.time_view.kind == "unspecified"
    d = build_contract("What did Ilse do in May 2022?", now=NOW, lang_hint=None)
    assert (d.time_view.start, d.time_view.end) == (D(2022, 5, 1), D(2022, 6, 1))


def test_german_morgen_morning_vs_tomorrow() -> None:
    both = build_contract("Was habe ich heute Morgen gegessen?", now=NOW, lang_hint="de")
    assert (both.time_view.start, both.time_view.end) == (D(2025, 5, 14), D(2025, 5, 15))
    tom = build_contract("Was steht morgen an?", now=NOW, lang_hint="de")
    assert (tom.time_view.start, tom.time_view.end) == (D(2025, 5, 15), D(2025, 5, 16))


def test_directions() -> None:
    cases = {
        "What did Ilse do before 2020?": (None, D(2020, 1, 1)),
        "What did Ilse do until 2020?": (None, D(2021, 1, 1)),
        "What did Ilse do after 2020?": (D(2021, 1, 1), None),
        "What did Ilse do since 2020?": (D(2020, 1, 1), None),
        "Was hat Ilse bis 2020 gemacht?": (None, D(2021, 1, 1)),
        "Wat deed Ilse na 2020?": (D(2021, 1, 1), None),
        "¿Qué hizo Ilse antes de 2020?": (None, D(2020, 1, 1)),
        "¿Qué hizo Ilse desde 2020?": (D(2020, 1, 1), None),
    }
    for q, (lo, hi) in cases.items():
        c = build_contract(q, now=NOW, lang_hint=None)
        assert c.time_view.kind == "range", q
        assert (c.time_view.start, c.time_view.end) == (lo, hi), q


def test_two_unrelated_time_phrases_are_not_guessed() -> None:
    c = build_contract("Did Ilse move in 2019 and change jobs in 2022?", now=NOW, lang_hint=None)
    assert c.time_view.kind == "unspecified"
    assert "time_view" in c.unknown


def test_between_range_is_not_a_join() -> None:
    c = build_contract(
        "What did Ilse do between March 2020 and June 2021?", now=NOW, lang_hint=None
    )
    assert c.time_view.kind == "range"
    assert (c.time_view.start, c.time_view.end) == (D(2020, 3, 1), D(2021, 7, 1))
    assert c.shape != "join"


def test_history_with_period_keeps_both() -> None:
    c = build_contract("Show the history of my address since 2019", now=NOW, lang_hint=None)
    assert c.time_view.kind == "history"
    assert (c.time_view.start, c.time_view.end) == (D(2019, 1, 1), None)
    assert c.shape == "history"


def test_as_of_without_a_date_is_unanchored() -> None:
    c = build_contract("Where did I live back then, as of the move?", now=NOW, lang_hint="en")
    assert c.time_view.kind == "as_of"
    assert c.time_view.as_of is None
    assert c.time_view.anchored is False


def test_as_of_value_and_precision() -> None:
    c = build_contract("What was my address as of March 2021?", now=NOW, lang_hint=None)
    assert c.time_view.kind == "as_of"
    assert c.time_view.as_of == D(2021, 3, 1)
    assert c.time_view.precision == "month"
    d = build_contract("What was my address as of 2021-03-04?", now=NOW, lang_hint=None)
    assert d.time_view.as_of == D(2021, 3, 4)
    assert d.time_view.precision == "day"


def test_naive_now_is_utc_and_zone_is_kept() -> None:
    naive = build_contract(
        "What did I eat yesterday?", now=datetime(2025, 5, 14, 10, 0), lang_hint=None
    )
    assert (naive.time_view.start, naive.time_view.end) == (D(2025, 5, 13), D(2025, 5, 14))
    tz = timezone(timedelta(hours=2))
    c = build_contract(
        "What did I eat yesterday?", now=datetime(2025, 5, 14, 1, 0, tzinfo=tz), lang_hint=None
    )
    assert c.time_view.start == datetime(2025, 5, 13, tzinfo=tz)
    assert c.time_view.end == datetime(2025, 5, 14, tzinfo=tz)


def test_hint_decides_when_text_is_thin_and_unsupported_hint_blocks_rules() -> None:
    thin = "Is Ilse vegetarisch of veganistisch?"
    assert build_contract(thin, now=NOW, lang_hint="nl").answer_type == "entity"
    assert build_contract(thin, now=NOW, lang_hint="nl-NL").language == "nl"
    fr = build_contract("Quand Ilse a-t-elle déménagé ?", now=NOW, lang_hint="fr")
    assert fr.language == "fr"
    assert fr.answer_type == "unknown"
    assert "language" in fr.unknown
    fy = build_contract("Que faisait Ilse en 2019 ?", now=NOW, lang_hint="fr")
    assert (fy.time_view.start, fy.time_view.end) == (D(2019, 1, 1), D(2020, 1, 1))


def test_clear_text_overrides_a_wrong_hint() -> None:
    c = build_contract(
        "Wann hat Ilse bei Quorvex angefangen und wie lange blieb sie dort?",
        now=NOW,
        lang_hint="en",
    )
    assert c.language == "de"
    mild = build_contract("When did Ilse start?", now=NOW, lang_hint="de")
    assert mild.language == "de"


def test_personal_rules() -> None:
    assert build_contract("Where do I live?", now=NOW).personal is True
    assert build_contract("What is the tallest mountain in Freedonia?", now=NOW).personal is None
    assert build_contract("What is the capital of Freedonia?", now=NOW).personal is False
    assert build_contract("What did you recommend to me?", now=NOW).personal is True
    assert build_contract("Could you explain what a mortgage is?", now=NOW).personal is False
    assert build_contract("Where is Zorbia?", now=NOW).personal is None
    assert build_contract("What is the weather in Zorbia?", now=NOW).personal is None
    assert (
        build_contract("What is the capital of the country my sister lives in?", now=NOW).personal
        is True
    )


def test_confidence_orders_the_evidence() -> None:
    first = build_contract("Where do I live?", now=NOW).confidence["personal"]
    name = build_contract("When did Ilse move?", now=NOW).confidence["personal"]
    poss = build_contract("When was Ilse's wedding?", now=NOW).confidence["personal"]
    assert first > poss > name


def test_targets_and_attributes() -> None:
    c = build_contract(
        'When did Ilse and Anna Meier read "The Quiet Orrery" in Zorbia?', now=NOW, lang_hint=None
    )
    assert c.targets == ("Ilse", "Anna Meier", "Zorbia", "The Quiet Orrery")
    d = build_contract("What is Bram's salary and where does he live?", now=NOW, lang_hint=None)
    assert d.attributes == ("salary", "residence")
    e = build_contract("Wanneer is de verjaardag van Jan van der Berg?", now=NOW, lang_hint=None)
    assert "Jan van der Berg" in e.targets
    assert e.attributes == ("birthday",)
    f = build_contract("What did Ilse do on Monday in March?", now=NOW, lang_hint=None)
    assert f.targets == ("Ilse",)
    g = build_contract("Wie viele Bücher stehen im Regal?", now=NOW, lang_hint="de")
    assert g.targets == ()
    h = build_contract("Wie viele Bücher hat Ilse im Regal?", now=NOW, lang_hint="de")
    assert h.targets == ("Ilse",)


def test_no_name_from_a_sentence_initial_word() -> None:
    assert build_contract("Birthdays of my friends?", now=NOW, lang_hint="en").targets == ()
    assert build_contract("Ilse's birthday, when is it?", now=NOW, lang_hint="en").targets == (
        "Ilse",
    )


def test_unknown_is_named_and_confidence_is_zero() -> None:
    c = build_contract("Blorp?", now=NOW)
    assert set(c.unknown) >= {"answer_type", "shape", "personal", "language"}
    assert c.confidence["answer_type"] == 0.0
    assert c.confidence["personal"] == 0.0
    assert render_header(c) == ""
    ok = build_contract("When did Ilse move to Zorbia?", now=NOW)
    assert "answer_type" not in ok.unknown
    assert all(0.0 <= v <= 1.0 for v in ok.confidence.values())
    assert set(ok.confidence) >= {
        "answer_type",
        "shape",
        "time_view",
        "personal",
        "request_kind",
        "language",
    }


def test_how_old_stays_unknown_and_header_omits_it() -> None:
    c = build_contract("How old is Bram?", now=NOW)
    assert c.answer_type == "unknown"
    assert "type=" not in render_header(c)


def test_choice_question_is_not_yesno() -> None:
    assert build_contract("Is Bram taller or shorter than Ilse?", now=NOW).answer_type == "entity"
    assert build_contract("Is Bram taller than Ilse or not?", now=NOW).answer_type == "yesno"


def test_show_all_and_enumeration_kinds() -> None:
    assert build_contract("What do you know about Ilse?", now=NOW).request_kind == "show_all"
    assert build_contract("How many pets does Ilse have?", now=NOW).request_kind == "enumeration"
    assert build_contract("What is Ilse's job?", now=NOW).request_kind == "lookup"
    assert build_contract("What was Ilse's job as of 2020-01-01?", now=NOW).request_kind == "as_of"


def test_header_exact_strings() -> None:
    c = build_contract("When did Ilse last visit Zorbia in 2023?", now=NOW, lang_hint=None)
    assert render_header(c) == (
        "[contract] type=date; shape=latest; view=range:2023-01-01..2024-01-01; req=as_of; "
        "scope=personal; targets=Ilse|Zorbia; lang=en"
    )
    d = build_contract("Wo wohne ich jetzt?", now=NOW, lang_hint=None)
    assert (
        render_header(d)
        == "[contract] type=entity; shape=latest; view=current; scope=personal; attrs=residence; lang=de"
    )
    e = build_contract("What is the capital of Freedonia?", now=NOW, lang_hint=None)
    assert (
        render_header(e)
        == "[contract] type=entity; shape=single; scope=generic; targets=Freedonia; lang=en"
    )
    f = build_contract("What did Ilse do before 2020?", now=NOW, lang_hint=None)
    assert "view=range:..2020-01-01" in render_header(f)
    g = build_contract("What did Ilse do last week?", now=None, lang_hint="en")
    assert "view=range:unanchored" in render_header(g)
    h = build_contract("What was my address as of 2021-03-04?", now=NOW, lang_hint=None)
    assert "view=as_of:2021-03-04" in render_header(h)
    i = build_contract("Tell me everything you know about Ilse", now=NOW, lang_hint=None)
    assert "req=show_all" in render_header(i)


def test_header_is_compact_and_floor_aware() -> None:
    c = build_contract("Why did Bram leave Quorvex?", now=NOW, lang_hint=None)
    h = render_header(c)
    assert "\n" not in h
    assert len(h) < 160
    assert render_header(c, min_confidence=0.99) == ""
    amount = build_contract("Wie viel kostet Ilses Miete?", now=NOW, lang_hint="de")
    assert amount.answer_type == "free"
    assert "type=free" in render_header(amount)
    assert "type=free" not in render_header(amount, min_confidence=0.6)


def test_header_cleans_target_text() -> None:
    c = build_contract('Who is "Ann;Lee|Ray=x" in the story?', now=NOW, lang_hint="en")
    h = render_header(c)
    seg = h.split("targets=")[1].split("; ")[0]
    assert ";" not in seg and "=" not in seg
    assert h.count("targets=") == 1


def test_to_schema_maps_the_fields() -> None:
    c = build_contract("List all the pets that Ilse has owned", now=NOW)
    s = c.to_schema()
    assert isinstance(s, QueryContract)
    assert s.answer_type == "entity"
    assert s.cardinality == "many"
    assert s.subjects == ["Ilse"]
    assert s.relation == "pet"
    n = build_contract("How many times did I visit Lumea in 2023?", now=NOW).to_schema()
    assert (n.answer_type, n.cardinality, n.time_scope) == ("count", "count", "2023")
    d = build_contract("Why did Bram leave?", now=NOW).to_schema()
    assert d.answer_type == "description"
    assert build_contract("Blorp?", now=NOW).to_schema().answer_type == "unknown"


def test_typed_contract_is_frozen_and_round_trips() -> None:
    c = build_contract("When did Ilse move?", now=NOW)
    assert isinstance(c, TypedContract)
    with pytest.raises(ValueError, match="frozen"):
        c.answer_type = "count"  # type: ignore[misc]
    assert TypedContract.model_validate_json(c.model_dump_json()) == c
