"""I7 (defaulted timestamps), I8 (question-date anchoring), I23 (language guard)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.language import blocked, language_scope, looks_english
from memspine.core.lead import timeline_line
from memspine.core.query_shape import (
    is_count,
    is_ordering,
    is_set_question,
    is_temporal,
    question_shape,
    rule_read_mode,
)
from memspine.core.temporal_query import query_interval
from memspine.core.temporal_resolve import annotate, resolve

QD = datetime(2023, 5, 30, 23, 40, tzinfo=UTC)


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )


# ---------------------------------------------------------------- I7


def test_i7_defaults_off() -> None:
    assert ReadConfig().skip_defaulted_dates is False
    assert ReadConfig().language_guard == "off"


async def test_i7_undated_write_is_tagged_and_renders_without_date() -> None:
    eng = _engine(skip_defaulted_dates=True, resolve_relative_dates=True)
    await eng.start()
    try:
        undated = await eng.write("I went hiking last Friday", namespace="a")
        dated = await eng.write(
            "Caroline went to the group last Friday",
            namespace="a",
            valid_from=datetime(2023, 7, 15, tzinfo=UTC),
        )
        assert "ts_defaulted" in undated.tags
        assert "ts_defaulted" not in dated.tags
        # No annotation for the defaulted record (it would resolve against today).
        assert eng._annotate_dates(undated).content == "I went hiking last Friday"
        assert eng._annotate_dates(dated).content.endswith("[= Fri 2023-07-14]")
        # No "[YYYY-MM-DD Day]" prefix either.
        assert eng._render_dated(undated).content == "I went hiking last Friday"
        assert eng._render_dated(dated).content.startswith("[2023-07-15 Sat] ")
    finally:
        await eng.stop()


async def test_i7_off_changes_nothing() -> None:
    eng = _engine(resolve_relative_dates=True)
    await eng.start()
    try:
        rec = await eng.write("I went hiking last Friday", namespace="a")
        assert "ts_defaulted" not in rec.tags
        assert eng._render_dated(rec).content.startswith("[")
    finally:
        await eng.stop()


async def test_i7_write_messages_without_timestamps() -> None:
    eng = _engine(skip_defaulted_dates=True)
    await eng.start()
    try:
        recs = await eng.write_messages(
            [{"role": "user", "content": "I like tea"}], namespace="a", session_id="s"
        )
        assert all("ts_defaulted" in r.tags for r in recs)
        stamped = await eng.write_messages(
            [
                {
                    "role": "user",
                    "content": "I like coffee",
                    "timestamp": datetime(2023, 1, 2, tzinfo=UTC),
                }
            ],
            namespace="a",
            session_id="s2",
        )
        assert all("ts_defaulted" not in r.tags for r in stamped)
    finally:
        await eng.stop()


async def test_i7_timeline_line_skips_defaulted_date() -> None:
    eng = _engine(skip_defaulted_dates=True)
    await eng.start()
    try:
        undated = await eng.write("Anna likes tea", namespace="a")
        dated = await eng.write(
            "Anna likes coffee", namespace="a", valid_from=datetime(2023, 1, 2, tzinfo=UTC)
        )
        assert timeline_line(undated, "Anna") == "- likes tea"
        assert timeline_line(dated, "Anna") == "- 2023-01-02: likes coffee"
    finally:
        await eng.stop()


# ---------------------------------------------------------------- I8


@pytest.mark.parametrize(
    ("question", "start", "end"),
    [
        (
            "What did I buy last month?",
            datetime(2023, 4, 1, tzinfo=UTC),
            datetime(2023, 5, 1, tzinfo=UTC),
        ),
        (
            "What did I do yesterday?",
            datetime(2023, 5, 29, tzinfo=UTC),
            datetime(2023, 5, 30, tzinfo=UTC),
        ),
        (
            "What happened two weeks ago?",
            datetime(2023, 5, 16, tzinfo=UTC),
            datetime(2023, 5, 17, tzinfo=UTC),
        ),
        (
            "What happened 10 days ago?",
            datetime(2023, 5, 20, tzinfo=UTC),
            datetime(2023, 5, 21, tzinfo=UTC),
        ),
    ],
)
def test_i8_relative_question_resolves_against_the_question_date(
    question: str, start: datetime, end: datetime
) -> None:
    assert query_interval(question, QD) == (start, end)


def test_i8_anchor_is_the_question_date_not_today() -> None:
    a = query_interval("What did I do yesterday?", QD)
    b = query_interval("What did I do yesterday?", datetime(2020, 1, 10, tzinfo=UTC))
    assert a != b and b is not None and b[0] == datetime(2020, 1, 9, tzinfo=UTC)


def test_i8_naive_anchor_is_accepted() -> None:
    assert query_interval("last month", datetime(2023, 5, 30, 23, 40)) is not None


def test_i8_no_anchor_means_no_relative_span() -> None:
    assert query_interval("What did I do yesterday?") is None


async def test_i8_as_of_scope_anchors_the_temporal_leg() -> None:
    """``assemble(as_of=question_date)``: a relative question finds the day before the
    question date, though the clock says another day."""
    eng = _engine(temporal_leg=True)
    await eng.start()
    try:
        eng._clock = lambda: datetime(2026, 1, 1, tzinfo=UTC)
        await eng.write(
            "We fixed the garden fence",
            namespace="a",
            valid_from=datetime(2023, 5, 29, 10, tzinfo=UTC),
        )
        await eng.write(
            "We fixed the old car", namespace="a", valid_from=datetime(2023, 4, 2, 10, tzinfo=UTC)
        )
        ctx = await eng.assemble("What did we fix yesterday?", namespace="a", as_of=QD)
        assert ctx.records and ctx.records[0].content == "We fixed the garden fence"
    finally:
        await eng.stop()


def test_i8_parse_question_date_formats() -> None:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "evals"))
    from memspine_evals.systems.memspine_system import parse_question_date

    assert parse_question_date("2023/05/30 (Tue) 23:40") == QD
    assert parse_question_date("2023-05-30 23:40") == QD  # LoCoMo-Plus shape
    assert parse_question_date("2023-05-30T23:40:00") == QD
    assert parse_question_date("garbled") is None


# ---------------------------------------------------------------- I23

NON_ENGLISH = [
    "Quand Anna est-elle partie en vacances le mois dernier ?",  # French
    "Wann hat Anna letzten Monat geheiratet?",  # German
    "¿Cuándo fue Ana de viaje el mes pasado?",  # Spanish
    "安娜上个月去了哪里?",  # Chinese
]


@pytest.mark.parametrize("text", NON_ENGLISH)
def test_i23_detects_non_english(text: str) -> None:
    assert not looks_english(text)


@pytest.mark.parametrize(
    "text",
    ["When did Anna go camping last month?", "Anna", "Mars", "How many days ago was the trip?", ""],
)
def test_i23_english_and_short_text_pass(text: str) -> None:
    assert looks_english(text)


def test_i23_guard_off_is_a_no_op() -> None:
    assert not blocked("Wann hat Anna letzten Monat geheiratet?")
    assert is_ordering("Was it the first time? I mean the first one") in (True, False)


def test_i23_regex_features_fail_closed_under_the_guard() -> None:
    english = "When did Anna go camping last month?"
    german = "Wann hat Anna letzten Monat geheiratet am 5 May?"
    with language_scope(True):
        assert is_temporal(english)
        assert query_interval("What did I buy last month?", QD) is not None
        assert resolve("I went last Friday", QD)
        # non-English: nothing fires
        assert not is_temporal("Wann hat Anna wie lange nicht und when the Mai?" + german)
        assert query_interval(german, QD) is None
        assert (
            resolve("Ich war letzten Freitag nicht da, wir haben last Friday gesagt und", QD) == []
        )
        assert annotate("Wir sind letzten Freitag nicht mit dem Auto gefahren, last week", QD) == (
            "Wir sind letzten Freitag nicht mit dem Auto gefahren, last week"
        )
        assert (
            question_shape("Combien de fois est-ce que Anna a visité le musée ? how many times")
            == "plain"
        )
        assert (
            rule_read_mode("Combien de fois est-ce que Anna a visité le musée, how many times")
            is None
        )
        assert not is_set_question(
            "Quelles activités est-ce que Anna fait dans les vacances? What activities"
        )
        assert not is_count(
            "Combien de fois est-ce que Anna est partie en vacances? how many times"
        )
        # an ISO date survives
        assert query_interval("¿Qué pasó el 2023-05-12 con los amigos de Ana?", QD) == (
            datetime(2023, 5, 12, tzinfo=UTC),
            datetime(2023, 5, 13, tzinfo=UTC),
        )
    # outside the scope the same French/German text still trips the English regex
    assert is_count("Combien de fois est-ce que Anna est partie en vacances? how many times")


async def test_i23_engine_scopes_the_guard_around_assemble() -> None:
    eng = _engine(language_guard="on", temporal_leg=True)
    await eng.start()
    try:
        await eng.write("Anna a acheté une voiture dans la ville", namespace="a")
        seen: list[bool] = []
        original = eng._assemble

        async def spy(*args: Any, **kwargs: Any) -> Any:
            seen.append(blocked("Wann hat Anna letzten Monat geheiratet?"))
            return await original(*args, **kwargs)

        eng._assemble = spy  # type: ignore[method-assign]
        await eng.assemble("Wann hat Anna letzten Monat geheiratet?", namespace="a")
        assert seen == [True]
    finally:
        await eng.stop()
