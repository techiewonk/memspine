"""E05 milestone extraction + deterministic computation: fake model outputs, no model call."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import pytest
from memspine_evals.contracts import ReaderAnswer
from memspine_evals.milestones import (
    MilestoneOut,
    MilestoneReader,
    compute_from_milestones,
    elapsed_in_unit,
    is_milestone_question,
    numbered_dated_lines,
)

PROJECT = (
    "[2023-03-02] Sam: We just kicked off the website redesign project today.\n"
    "[2023-03-20] Lee: How is the team doing?\n"
    "[2023-05-18] Sam: We launched the website redesign last week, finally done!\n"
    "[2023-06-01] Lee: Congrats on the launch."
)


def ms(**kw: Any) -> dict[str, Any]:
    return {"actor": "Sam", "event_key": "", "status": "done", "span": "", **kw}


def out(**kw: Any) -> MilestoneOut:
    return MilestoneOut.model_validate({"shape": "between", "same_actor": True, **kw})


def solve(question: str, context: str, o: MilestoneOut, qd: str | None = None, **kw: Any):
    return compute_from_milestones(question, context, o, qd, **kw)


# ---- the question shapes -----------------------------------------------------------------------


def test_how_long_did_it_take_start_and_end_of_one_event() -> None:
    o = out(
        unit="week",
        start="m1",
        end="m2",
        milestones=[
            ms(id="m1", line="L1", event="kick off redesign", event_key="redesign_start"),
            ms(id="m2", line="L3", event="launch redesign", event_key="redesign_end",
               span="last week"),
        ],
    )  # fmt: skip
    sol, meta = solve("How long did the website redesign take?", PROJECT, o)
    assert sol is not None and meta["decision"] == "solved"
    # 2023-05-18 is a Thursday; "last week" -> the calendar week 8-14 May (midpoint 11 May)
    assert sol.first.day == date(2023, 3, 2)
    assert sol.second.basis == "span" and sol.second.approx
    assert (sol.unit, sol.n, sol.days) == ("month", 2, 70)  # unit picked by size; 2 Mar -> 11 May
    assert sol.text().startswith("About ")


def test_after_how_many_weeks_between_two_events() -> None:
    ctx = (
        "[2023-01-09] Ana: I signed the lease today.\n"
        "[2023-02-20] Ana: I finally moved into the apartment on 2023-02-13.\n"
    )
    o = out(
        unit="week",
        start="a",
        end="b",
        milestones=[
            ms(id="a", line="L1", actor="Ana", event="signed lease", event_key="lease"),
            ms(id="b", line="L2", actor="Ana", event="moved in", event_key="move",
               span="2023-02-13"),
        ],
    )  # fmt: skip
    sol, meta = solve("After how many weeks did Ana move in after signing the lease?", ctx, o)
    assert sol is not None
    assert (sol.unit, sol.n, sol.days) == ("week", 5, 35)  # 9 Jan -> 13 Feb = 35 days exactly
    assert not sol.approx
    assert meta["endpoints"][1]["basis"] == "span"
    assert sol.text().startswith("5 weeks (from 2023-01-09 to 2023-02-13")


def test_first_and_second_ordinals() -> None:
    ctx = (
        "[2023-04-01] Bo: Went to the dentist today for a checkup.\n"
        "[2023-07-15] Bo: Back at the dentist today, new filling.\n"
        "[2023-10-30] Bo: Third dentist visit today.\n"
    )
    ms_ = [
        ms(id="v1", line="L1", actor="Bo", event="dentist visit", event_key="dentist"),
        ms(id="v2", line="L2", actor="Bo", event="dentist visit", event_key="dentist"),
        ms(id="v3", line="L3", actor="Bo", event="dentist visit", event_key="dentist"),
    ]
    q = "How many months passed between Bo's first and second dentist visit?"
    sol, _ = solve(
        q, ctx, out(start="v1", end="v2", start_ordinal=1, end_ordinal=2, milestones=ms_)
    )
    assert sol is not None and (sol.unit, sol.n) == ("month", 3)  # 1 Apr -> 15 Jul
    # the model picked the wrong occurrence for "second": fail closed
    bad, meta = solve(
        q, ctx, out(start="v1", end="v3", start_ordinal=1, end_ordinal=2, milestones=ms_)
    )
    assert bad is None and meta["decision"] == "abstain:ordinal_mismatch"
    # an ordinal the question never says is an invention
    inv, meta = solve(
        "How many months passed between Bo's dentist visits?",
        ctx,
        out(start="v1", end="v2", start_ordinal=1, end_ordinal=2, milestones=ms_),
    )
    assert inv is None and meta["decision"] == "abstain:ordinal_not_in_question"
    # "last" counts from the end
    last, _ = solve(
        "How many months between Bo's second and last dentist visit?",
        ctx,
        out(start="v2", end="v3", start_ordinal=2, end_ordinal=-1, milestones=ms_),
    )
    assert last is not None and (last.unit, last.n) == ("month", 3)  # 15 Jul -> 30 Oct


def test_since_uses_the_question_date_and_until_a_planned_event() -> None:
    ctx = "[2023-05-04] Mia: I started my new job at the bank today.\n"
    o = out(
        shape="since",
        unit="month",
        start="j",
        milestones=[ms(id="j", line="L1", actor="Mia", event="started job")],
    )
    sol, meta = solve("How many months since Mia started her job?", ctx, o, "2023-11-10")
    assert sol is not None and (sol.unit, sol.n, sol.days) == ("month", 6, 190)
    assert meta["endpoints"][1]["basis"] == "question"
    none, meta = solve("How many months since Mia started her job?", ctx, o, None)
    assert none is None and meta["decision"] == "abstain:no_question_date"

    plan = "[2023-05-04] Mia: The conference is scheduled for 2023-06-20, I will go.\n"
    u = out(shape="until", unit="week", end="c", milestones=[
        ms(id="c", line="L1", actor="Mia", event="conference", status="planned", span="2023-06-20")
    ])  # fmt: skip
    sol, _ = solve(
        "How many weeks until the conference Mia plans to attend?", plan, u, "2023-05-09"
    )
    assert sol is not None and (sol.unit, sol.n) == ("week", 6)  # 9 May -> 20 Jun = 42 days
    # the same planned endpoint is refused when the question is about what happened
    no, meta = solve("How many weeks after the conference did Mia sign?", plan, u, "2023-05-09")
    assert no is None and meta["decision"] == "abstain:endpoint_planned_not_done"


def test_relative_expression_resolved_by_code_against_the_line_date() -> None:
    ctx = (
        "[2023-08-11] Maria: I got my puppy two weeks ago!\n"
        "[2023-08-25] Maria: Her name is Shadow now.\n"
    )
    o = out(
        unit="day",
        start="p",
        end="n",
        milestones=[
            ms(id="p", line="L1", actor="Maria", event="got puppy", span="two weeks ago"),
            ms(id="n", line="L2", actor="Maria", event="named puppy"),
        ],
    )
    sol, meta = solve("How many days between Maria getting the puppy and naming her?", ctx, o)
    assert sol is not None
    assert meta["endpoints"][0]["date"] == "2023-07-28" and meta["endpoints"][0]["basis"] == "span"
    assert sol.days == 28


# ---- fail closed -------------------------------------------------------------------------------


def _base(**over: Any) -> MilestoneOut:
    spec = {
        "unit": "month",
        "start": "m1",
        "end": "m2",
        "milestones": [
            ms(id="m1", line="L1", event="kick off", event_key="start"),
            ms(id="m2", line="L3", event="launch", event_key="end", span="last week"),
        ],
    }
    spec.update(over)
    return out(**spec)


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"shape": "other"}, "shape_other"),
        ({"end": "zz"}, "endpoint_missing"),
        ({"end": ""}, "endpoint_missing"),
        ({"end": "m1"}, "same_milestone"),
        (
            {"milestones": [ms(id="m1", line="L9", event="x"), ms(id="m2", line="L3", event="y")]},
            "endpoint_line_not_in_context",
        ),
        (
            {"milestones": [ms(id="m1", line="L1", status="cancelled"), ms(id="m2", line="L3")]},
            "endpoint_status_cancelled",
        ),
        (
            {"milestones": [ms(id="m1", line="L1", status="unknown"), ms(id="m2", line="L3")]},
            "endpoint_status_unknown",
        ),
        (
            {"milestones": [ms(id="m1", line="L1", span="in 2019"), ms(id="m2", line="L3")]},
            "endpoint_span_not_on_line",
        ),
        (
            {"milestones": [ms(id="m1", line="L1"), ms(id="m1", line="L3")]},
            "duplicate_milestone_ids",
        ),
        (
            {"milestones": [ms(id="m1", line="L1"), ms(id="m2", line="L3", actor="Lee")]},
            "actor_mismatch",
        ),
    ],
)
def test_fail_closed_reasons(patch: dict[str, Any], reason: str) -> None:
    sol, meta = solve("How long did the redesign take in months?", PROJECT, _base(**patch))
    assert sol is None and meta["decision"].startswith(f"abstain:{reason}"), meta


def test_actor_mismatch_only_matters_when_the_question_needs_one_actor() -> None:
    o = _base(
        same_actor=False,
        milestones=[ms(id="m1", line="L1"), ms(id="m2", line="L3", actor="Lee", span="last week")],
    )
    sol, _ = solve("How many months between the kickoff and the launch?", PROJECT, o)
    assert sol is not None


def test_conflicting_reports_of_one_event() -> None:
    ctx = (
        "[2023-03-02] Sam: We kicked off the project on 2023-03-01.\n"
        "[2023-03-20] Sam: Project kicked off on 2023-03-15 actually.\n"
        "[2023-05-18] Sam: Launched on 2023-05-17.\n"
    )
    o = out(
        unit="week",
        start="a",
        end="c",
        milestones=[
            ms(id="a", line="L1", event_key="kick", span="2023-03-01"),
            ms(id="b", line="L2", event_key="kick", span="2023-03-15"),
            ms(id="c", line="L3", event_key="launch", span="2023-05-17"),
        ],
    )
    sol, meta = solve("After how many weeks was the launch after the kickoff?", ctx, o)
    assert sol is None and meta["decision"] == "abstain:conflicting_reports"
    # a planned report of the same event (a reschedule) does not conflict with the done one
    o2 = out(
        unit="week", start="a", end="c",
        milestones=[
            ms(id="a", line="L1", event_key="kick", span="2023-03-01"),
            ms(id="b", line="L2", event_key="kick", status="planned", span="2023-03-15"),
            ms(id="c", line="L3", event_key="launch", span="2023-05-17"),
        ],
    )  # fmt: skip
    sol, _ = solve("After how many weeks was the launch after the kickoff?", ctx, o2)
    assert sol is not None and sol.n == 11  # 1 Mar -> 17 May = 77 days


def test_vague_line_date_is_approximate_never_exact() -> None:
    ctx = "[2023-03-02] Sam: I just started.\n[2023-05-02] Sam: Done now.\n"
    o = out(
        unit="month",
        start="a",
        end="b",
        milestones=[
            ms(id="a", line="L1", span="just"),
            ms(id="b", line="L2"),
        ],
    )
    sol, _ = solve("How long did it take Sam to finish?", ctx, o)
    assert sol is not None and sol.approx


def test_end_before_start_is_ordered_for_between_and_refused_for_since() -> None:
    o = _base(start="m2", end="m1")
    sol, meta = solve("How many months passed between the launch and the kickoff?", PROJECT, o)
    assert sol is not None and meta.get("swapped")
    ctx = "[2023-12-01] Mia: I will start the job on 2023-12-20.\n"
    s = out(shape="since", start="j", milestones=[
        ms(id="j", line="L1", actor="Mia", status="planned", span="2023-12-20")
    ])  # fmt: skip
    none, meta = solve("How many days since Mia's planned start?", ctx, s, "2023-12-05")
    assert none is None and meta["decision"] == "abstain:event_after_question_date"


# ---- elapsed-time convention -------------------------------------------------------------------


def test_elapsed_conventions() -> None:
    _, n, days = elapsed_in_unit(date(2024, 1, 31), date(2024, 3, 1), "month")
    assert days == 30 and n == 1  # Jan 31 -> Feb 29 (clamped) is 1 whole month, +1 day
    _, n, days = elapsed_in_unit(date(2024, 2, 28), date(2024, 3, 1), "day")
    assert (n, days) == (2, 2)  # leap year
    _, n, days = elapsed_in_unit(date(2023, 2, 28), date(2023, 3, 1), "day")
    assert (n, days) == (1, 1)
    assert elapsed_in_unit(date(2023, 5, 1), date(2023, 5, 1), "day", inclusive=True)[1] == 1
    assert elapsed_in_unit(date(2023, 5, 1), date(2023, 5, 8), "week")[1] == 1
    assert elapsed_in_unit(date(2023, 5, 1), date(2023, 5, 8), "week", inclusive=True)[0] > 1.0
    # nearest vs floor: 10 weeks and 4 days
    a, b = date(2023, 1, 1), date(2023, 3, 16)
    assert elapsed_in_unit(a, b, "week", rounding="nearest")[1] == 11
    assert elapsed_in_unit(a, b, "week", rounding="floor")[1] == 10
    assert elapsed_in_unit(date(2020, 6, 15), date(2023, 6, 14), "year", rounding="floor")[1] == 2
    assert elapsed_in_unit(date(2020, 6, 15), date(2023, 6, 15), "year")[1] == 3
    with pytest.raises(ValueError):
        elapsed_in_unit(a, b, "decade")


# ---- gate, wrapper -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "q",
    [
        "How long did it take Sam to finish the project?",
        "After how many weeks did Ana move in after signing the lease?",
        "How many months passed between the first and the second visit?",
        "How many months since Mia started her job?",
        "How many days before the trip did she book the flight?",
        "How long has Tom been at the firm?",
    ],
)
def test_gate_accepts_duration_shapes(q: str) -> None:
    assert is_milestone_question(q)


@pytest.mark.parametrize(
    "q",
    [
        "When did Sam start the project?",
        "How many weeks a year does she travel?",
        "How many books did Lee read?",
        "What is Mia's job?",
    ],
)
def test_gate_rejects_other_questions(q: str) -> None:
    assert not is_milestone_question(q)


class Fake:
    reader_id = "fake"
    model = "m"
    makes_model_calls = True

    def __init__(self, text: str, extra: dict[str, Any] | None = None) -> None:
        self.text, self.extra = text, extra or {}

    def describe(self) -> dict[str, Any]:
        return {"reader_id": "fake"}

    async def answer(self, question: str, context: str, question_date: str | None = None):
        return ReaderAnswer(
            text=self.text, prompt_tokens=10, completion_tokens=2, latency_ms=1.0, model_calls=1,
            extra_meta=self.extra,
        )  # fmt: skip


class Chat:
    def __init__(self, *replies: str) -> None:
        self.replies, self.calls = list(replies), []

    async def __call__(self, prompt: str, system: str | None = None) -> str:
        self.calls.append((prompt, system))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


GOOD = json.dumps(
    {
        "shape": "between", "unit": "week", "start": "a", "end": "b", "same_actor": True,
        "milestones": [
            {"id": "a", "line": "L1", "actor": "Ana", "event": "signed", "event_key": "lease",
             "status": "done", "span": ""},
            {"id": "b", "line": "L2", "actor": "Ana", "event": "moved", "event_key": "move",
             "status": "done", "span": "2023-02-13"},
        ],
    }
)  # fmt: skip
CTX = (
    "[2023-01-09] Ana: I signed the lease today.\n"
    "[2023-02-20] Ana: I finally moved into the apartment on 2023-02-13.\n"
)
Q = "After how many weeks did Ana move in after signing the lease?"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_wrapper_rewrites_a_wrong_number_with_one_extra_call() -> None:
    chat = Chat(GOOD)
    reader = MilestoneReader(Fake("About 8 weeks"), chat)
    ans = run(reader.answer(Q, CTX))
    assert ans.text.startswith("5 weeks")
    assert len(chat.calls) == 1 and ans.model_calls == 2
    meta = ans.extra_meta["milestones"]
    assert meta["decision"] == "rewritten" and meta["original"] == "About 8 weeks"
    assert meta["reader_check"] == "disagrees" and meta["extracted"]["start"] == "a"
    assert "never" in chat.calls[0][1] and "L2 [2023-02-20]" in chat.calls[0][0]


def test_wrapper_keeps_an_agreeing_answer_and_replaces_a_refusal() -> None:
    agree = run(MilestoneReader(Fake("5 weeks"), Chat(GOOD)).answer(Q, CTX))
    assert agree.text == "5 weeks" and agree.extra_meta["milestones"]["decision"] == "agrees"
    refuse = run(MilestoneReader(Fake("I don't know."), Chat(GOOD)).answer(Q, CTX))
    assert refuse.text.startswith("5 weeks")


def test_wrapper_makes_no_call_for_other_questions_or_when_i76_settled() -> None:
    chat = Chat(GOOD)
    reader = MilestoneReader(Fake("Paris"), chat)
    ans = run(reader.answer("Where did Ana move?", CTX))
    assert ans.text == "Paris" and not chat.calls and "milestones" not in ans.extra_meta
    settled = Fake("8 weeks", {"duration_solve": {"decision": "agrees"}})
    ans = run(MilestoneReader(settled, chat).answer(Q, CTX))
    assert not chat.calls and ans.text == "8 weeks"
    assert ans.extra_meta["milestones"]["decision"] == "skipped:i76_agrees"
    # I76 abstained: the milestone step runs
    abst = Fake("8 weeks", {"duration_solve": {"decision": "abstain:event1_event_not_found"}})
    ans = run(MilestoneReader(abst, chat).answer(Q, CTX))
    assert len(chat.calls) == 1 and ans.text.startswith("5 weeks")


def test_wrapper_fails_closed_on_garbage_and_on_unsupported_output() -> None:
    reader = MilestoneReader(Fake("8 weeks"), Chat("not json at all"))
    ans = run(reader.answer(Q, CTX))
    assert ans.text == "8 weeks"
    assert ans.extra_meta["milestones"]["decision"] == "abstain:extract_failed"
    assert reader.failures == 1
    bad = json.loads(GOOD)
    bad["milestones"][1]["span"] = "2024-02-13"  # not on the line
    ans = run(MilestoneReader(Fake("8 weeks"), Chat(json.dumps(bad))).answer(Q, CTX))
    assert ans.text == "8 weeks"
    assert ans.extra_meta["milestones"]["decision"] == "abstain:endpoint_span_not_on_line"


def test_wrapper_repairs_a_fenced_reply_and_retries_once() -> None:
    fenced = "Sure!\n```json\n" + GOOD + "\n```"
    ans = run(MilestoneReader(Fake("8 weeks"), Chat(fenced)).answer(Q, CTX))
    assert ans.text.startswith("5 weeks")
    chat = Chat("{broken", GOOD)  # first reply invalid, the retry is valid
    ans = run(MilestoneReader(Fake("8 weeks"), chat).answer(Q, CTX))
    assert len(chat.calls) == 2 and ans.text.startswith("5 weeks")


def test_bad_rounding_rejected_and_describe() -> None:
    with pytest.raises(ValueError):
        MilestoneReader(Fake("x"), Chat(GOOD), rounding="ceil")
    r = MilestoneReader(Fake("x"), Chat(GOOD), inclusive=True, rounding="floor")
    assert r.describe()["milestones"] == "v1/inclusive/floor"


def test_lines_are_capped_and_stripped_of_annotations() -> None:
    ctx = "\n".join(f"[2023-01-{(i % 27) + 1:02d}] Sam: line {i} [=2023-01-01]" for i in range(120))
    lines = numbered_dated_lines(ctx)
    assert len(lines) == 80 and "[=" not in lines[0][1]
