"""I76 duration solver and I79 prompt variants: synthetic lines and fake readers, no model."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from memspine_evals.contracts import ReaderAnswer
from memspine_evals.duration_solve import (
    DurationSolveReader,
    check_answer,
    solve_duration,
)
from memspine_evals.readers import QA_PROMPTS

CTX_MONTHS = (
    "[2023-07-11] Andrew: Hey! meet Toby, my puppy. He is a bundle of joy.\n"
    "[2023-07-11] Audrey: Toby looks so adorable!\n"
    "[2023-08-04] Andrew: Gotta take Toby out for a small hike.\n"
    "[2023-10-19] Andrew: I named him Buddy because he is my buddy.\n"
    "[2023-10-19] Audrey: Nice! Buddy seems happy."
)


class Fake:
    reader_id = "fake"
    model = "m"
    makes_model_calls = True

    def __init__(self, text: str) -> None:
        self.text, self.seen = text, []

    def describe(self) -> dict[str, Any]:
        return {"reader_id": "fake"}

    async def answer(self, question: str, context: str, question_date: str | None = None):
        self.seen.append(context)
        return ReaderAnswer(
            text=self.text, prompt_tokens=1, completion_tokens=1, latency_ms=1.0, model_calls=1
        )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_months_between_two_named_events() -> None:
    q = "How many months passed between Andrew adopting Toby and Buddy?"
    sol, meta = solve_duration(q, CTX_MONTHS)
    assert sol is not None and meta["decision"] == "solved"
    assert (sol.unit, sol.n, sol.days) == ("month", 3, 100)  # 11 Jul -> 19 Oct
    assert sol.first.day.isoformat() == "2023-07-11" and sol.second.day.isoformat() == "2023-10-19"
    assert {e["basis"] for e in meta["endpoints"]} == {"first_mention"}


def test_days_with_a_relative_phrase_endpoint() -> None:
    ctx = (
        "[2023-08-11] Maria: I got a puppy two weeks ago! Her name is Coco.\n"
        "[2023-08-25] Maria: Her name is Shadow, a new puppy."
    )
    sol, meta = solve_duration("How many days passed between Coco and Shadow?", ctx)
    assert sol is not None
    # "two weeks ago" -> 2023-07-28 (approximate); Shadow's line is 2023-08-25: 28 days
    assert sol.days == 28 and sol.unit == "day" and sol.approx
    assert sol.text().startswith("About 28 days")


def test_years_between_absolute_dates() -> None:
    ctx = (
        "[2023-01-05] Sam: I started college on 3 September 2019, so excited.\n"
        "[2023-01-06] Sam: I graduated from college on 1 June 2023? no, planning"
    )
    sol, _ = solve_duration("How many years between Sam's college start and graduation?", ctx)
    assert sol is None  # clause without a capitalised event name: fail closed


def test_years_for_named_events() -> None:
    ctx = (
        "[2023-03-01] Ana: We adopted Rex, a lovely dog.\n"
        "[2026-03-03] Ana: Tara joined our home last week, we adopted her.\n"
    )
    sol, meta = solve_duration("How many years passed between Ana adopting Rex and Tara?", ctx)
    # Tara's line says "last week": 2026-02-24 -> 2026-02-24 minus 2023-03-01 = 3 years
    assert sol is not None and sol.unit == "year" and sol.n == 3


def test_ambiguous_event_fails_closed() -> None:
    ctx = (
        "[2023-07-11] Andrew: meet Toby, my puppy. We adopted him today.\n"
        "[2023-09-30] Andrew: We adopted Toby after a long search, finally official.\n"
        "[2023-10-19] Andrew: I named him Buddy and adopted him."
    )  # two adoption lines for Toby more than a week apart
    sol, meta = solve_duration(
        "How many months passed between Andrew adopting Toby and Buddy?", ctx
    )
    assert sol is None and meta["decision"] == "abstain:event1_event_lines_disagree"


def test_missing_second_event_and_unnamed_events_fail_closed() -> None:
    sol, meta = solve_duration(
        "How many months passed between Andrew adopting Toby and Zorro?", CTX_MONTHS
    )
    assert sol is None and meta["decision"] == "abstain:event2_event_not_found"
    sol, meta = solve_duration(
        "How many months lapsed between his first and second doctor's appointment?", CTX_MONTHS
    )
    assert sol is None and meta["decision"].startswith("abstain:event")


def test_not_a_duration_question_leaves_no_record() -> None:
    assert solve_duration("When did Andrew adopt Toby?", CTX_MONTHS) == (None, {})
    assert solve_duration("Where was John between August 11 and August 15?", CTX_MONTHS) == (
        None,
        {},
    )


def test_elapsed_since_with_as_of_month() -> None:
    sol, meta = solve_duration(
        "How long has it been since Rae adopted Toby, as of November 2023?",
        "[2023-07-11] Rae: I adopted Toby today, so happy.",
    )
    assert sol is not None and sol.unit == "month" and sol.n == 4 and sol.approx


@pytest.mark.parametrize(
    ("answer", "verdict"),
    [
        ("3 months", "agrees"),
        ("About three months.", "agrees"),
        ("4 months", "disagrees"),
        ("Not mentioned.", "refusal"),
        ("It is a long time", "unparsed"),
        ("0 months", "disagrees"),
    ],
)
def test_reader_answer_check(answer: str, verdict: str) -> None:
    sol, _ = solve_duration(
        "How many months passed between Andrew adopting Toby and Buddy?", CTX_MONTHS
    )
    assert sol is not None and check_answer(sol, answer)[0] == verdict


def test_rewrite_mode_replaces_a_wrong_number_and_records_the_decision() -> None:
    inner = Fake("4 months")
    reader = DurationSolveReader(inner, "rewrite")
    q = "How many months passed between Andrew adopting Toby and Buddy?"
    out = run(reader.answer(q, CTX_MONTHS))
    assert out.text.startswith("About 3 months") and "2023-07-11" in out.text
    meta = out.extra_meta["duration_solve"]
    assert meta["decision"] == "rewritten" and meta["original"] == "4 months"
    assert inner.seen == [CTX_MONTHS]  # context untouched, one reader call
    assert reader.rewritten == 1 and out.model_calls == 1


def test_rewrite_mode_keeps_an_agreeing_answer_and_abstains_quietly() -> None:
    q = "How many months passed between Andrew adopting Toby and Buddy?"
    ok = run(DurationSolveReader(Fake("3 months"), "rewrite").answer(q, CTX_MONTHS))
    assert ok.text == "3 months" and ok.extra_meta["duration_solve"]["decision"] == "agrees"
    q2 = "How many months passed between Andrew adopting Toby and Zorro?"
    ab = run(DurationSolveReader(Fake("5 months"), "rewrite").answer(q2, CTX_MONTHS))
    assert ab.text == "5 months" and ab.extra_meta["duration_solve"]["decision"].startswith(
        "abstain"
    )
    plain = run(
        DurationSolveReader(Fake("Tuesday"), "rewrite").answer("When did Toby come?", CTX_MONTHS)
    )
    assert plain.text == "Tuesday" and "duration_solve" not in plain.extra_meta


def test_rewrite_replaces_a_refusal() -> None:
    q = "How many months passed between Andrew adopting Toby and Buddy?"
    out = run(DurationSolveReader(Fake("Not mentioned."), "rewrite").answer(q, CTX_MONTHS))
    assert out.text.startswith("About 3 months")


def test_hint_mode_prepends_one_line_and_keeps_the_reader_answer() -> None:
    inner = Fake("3 months, I think")
    reader = DurationSolveReader(inner, "hint")
    q = "How many months passed between Andrew adopting Toby and Buddy?"
    out = run(reader.answer(q, CTX_MONTHS))
    assert out.text == "3 months, I think"
    assert inner.seen[0].endswith(CTX_MONTHS) and inner.seen[0].startswith("Computed by calendar")
    assert "100 days" in inner.seen[0]
    assert out.extra_meta["duration_solve"]["decision"] == "hinted"


def test_bad_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        DurationSolveReader(Fake("x"), "guess")


def test_default_grounded_prompts_are_clean_and_legacy_is_byte_identical() -> None:
    """2026-10-11: ``grounded`` / ``grounded_detail`` carried a LoCoMo gold answer as an example."""
    for name in ("grounded", "grounded_detail", "grounded_nodate", "grounded_detail_nodate"):
        text = QA_PROMPTS[name]
        assert "9 June 2023" not in text and "2023-05-20" not in text
        assert "{context}" in text and "{question}" in text
    assert QA_PROMPTS["grounded"] == QA_PROMPTS["grounded_nodate"]
    assert QA_PROMPTS["grounded_detail"] == QA_PROMPTS["grounded_detail_nodate"]
    assert "9 June 2023" in QA_PROMPTS["grounded_legacy"]
    assert "2023-05-20" in QA_PROMPTS["grounded_legacy"]
    assert "9 June 2023" in QA_PROMPTS["grounded_detail_legacy"]
    # the clean prompt differs from the legacy one in exactly the two example dates
    swapped = QA_PROMPTS["grounded_legacy"].replace(
        '"last Saturday [= 2023-05-20]"', '"last Saturday [= <resolved date>]"'
    )
    swapped = swapped.replace(
        '(for example "the week before 9 June 2023" or "2022")',
        '(for example "the week before <the line\'s date>", or only the year when a year is asked)',
    )
    assert swapped == QA_PROMPTS["grounded"] != QA_PROMPTS["grounded_legacy"]
