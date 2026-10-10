"""A04 + A06 evidence table: fake model rows, no model call. Over-merge and double-count traps."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from memspine_evals.contracts import ReaderAnswer
from memspine_evals.count_verify import CountVerifyReader
from memspine_evals.evidence_table import (
    EvidenceTableBuilder,
    TableOut,
    build_table,
    cumulative_statements,
    needs_table,
    numbered_lines,
    render_table_answer,
)
from memspine_evals.slot_verify import SlotVerifyReader


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def row(i: int, line: int, item: str, span: str, **kw: Any) -> dict[str, Any]:
    return {
        "id": f"r{i}", "line": f"L{line}", "item": item, "actor": "Ana", "span": span,
        "predicate_match": True, "status": "done", "event_key": "", "same_as": "", **kw,
    }  # fmt: skip


def table(question: str, ctx: str, rows: list[dict[str, Any]], **kw: Any):
    kw.setdefault("subjects", ("Ana",))
    return build_table(question, numbered_lines(ctx), TableOut.model_validate({"rows": rows}), **kw)


TRIPS = (
    "[2023-03-01] Ana: I went to Lisbon for a long weekend.\n"
    "[2023-04-10] Ana: I went to Lisbon again, the food is great.\n"
    "[2023-04-10] Ana: Also a day trip to Sintra the same day.\n"
    "[2023-05-02] Ana: I am planning a trip to Rome next month.\n"
    "[2023-05-03] Ben: I went to Madrid last week.\n"
)
Q_TIMES = "How many times did Ana go on a trip?"


def test_numbered_lines_and_gate() -> None:
    lines = numbered_lines("* [2023-01-02] a: hi\n\n- [Note: skip]\nundated line\n")
    assert [(ln.n, str(ln.day), ln.text) for ln in lines] == [
        (1, "2023-01-02", "a: hi"), (2, "None", "undated line"),
    ]  # fmt: skip
    assert needs_table("How many trips did Ana take?")
    assert needs_table("Which cities has Ana visited?")
    assert not needs_table("Where did Ana go in March?")


def test_repeated_activity_on_different_days_is_two_events_not_merged() -> None:
    rows = [
        row(1, 1, "trip to Lisbon", "went to Lisbon"),
        row(2, 2, "trip to Lisbon", "went to Lisbon again"),
        row(3, 3, "day trip to Sintra", "day trip to Sintra"),
    ]
    t = table(Q_TIMES, TRIPS, rows)
    assert t.status == "resolved" and t.count == 3  # Lisbon twice (two dates) + Sintra
    assert [r.merged_into for r in t.rows] == ["", "", ""]


def test_same_event_merges_only_when_the_text_says_so() -> None:
    ctx = (
        "[2023-03-01] Ana: I went to Lisbon for a long weekend.\n"
        "[2023-03-20] Ana: As I mentioned, the same Lisbon trip was lovely.\n"
    )
    rows = [
        row(1, 1, "trip to Lisbon", "went to Lisbon"),
        row(2, 2, "trip to Lisbon", "the same Lisbon trip", same_as="r1"),
    ]
    t = table(Q_TIMES, ctx, rows)
    assert t.count == 1 and t.rows[1].merged_into == "r1"
    # the model claims same_as but the line carries no coreference: not merged
    rows[1] = row(2, 2, "trip to Lisbon", "the same Lisbon trip", same_as="r1")
    plain = ctx.replace("As I mentioned, the same Lisbon trip", "Another Lisbon trip")
    rows[1]["span"] = "Another Lisbon trip"
    assert table(Q_TIMES, plain, rows).count == 2


def test_same_day_different_items_are_not_overmerged_by_token_overlap() -> None:
    ctx = "[2023-06-01] Ana: Visited the city museum and then the city park in the afternoon.\n"
    rows = [
        row(1, 1, "visit city museum", "the city museum"),
        row(2, 1, "visit city park", "the city park"),
    ]
    assert table("How many places did Ana visit?", ctx, rows).count == 2


def test_things_merge_across_days_events_do_not() -> None:
    ctx = (
        "[2023-01-05] Ana: My dog Rex loves the park.\n"
        "[2023-02-09] Ana: Rex, my dog, chewed my shoe.\n"
        "[2023-02-09] Ana: I also adopted a cat named Mia.\n"
    )
    rows = [
        row(1, 1, "dog Rex", "My dog Rex"),
        row(2, 2, "dog Rex", "Rex, my dog"),
        row(3, 3, "cat Mia", "a cat named Mia"),
    ]
    assert table("How many pets does Ana have?", ctx, rows).count == 2


def test_planned_mentioned_other_actor_and_predicate_mismatch_are_not_counted() -> None:
    rows = [
        row(1, 1, "trip to Lisbon", "went to Lisbon"),
        row(2, 4, "trip to Rome", "planning a trip to Rome", status="planned"),
        row(3, 5, "trip to Madrid", "went to Madrid", actor="Ben"),
        row(4, 3, "day trip to Sintra", "day trip to Sintra", predicate_match=False),
        row(5, 2, "trip to Lisbon", "went to Lisbon again", status="mentioned"),
    ]
    t = table(Q_TIMES, TRIPS, rows)
    assert t.count == 1
    reasons = {r.id: r.reason for r in t.rows}
    assert reasons["r2"] == "planned_not_done" and reasons["r3"] == "actor_mismatch"
    assert reasons["r4"] == "predicate_mismatch" and reasons["r5"] == "status_mentioned"
    # a question about plans admits the planned row
    planned = table("How many trips did Ana plan?", TRIPS, rows)
    assert {r.id for r in planned.items} == {"r1", "r2"}


def test_rows_with_a_missing_line_or_span_are_dropped() -> None:
    rows = [
        row(1, 9, "trip", "went"),  # no such line
        row(2, 1, "trip to Lisbon", "flew to Paris"),  # span not on the line
        row(3, 1, "trip to Lisbon", "went to Lisbon"),
    ]
    t = table(Q_TIMES, TRIPS, rows)
    assert t.count == 1
    assert [r.reason for r in t.rows[:2]] == ["line_not_in_context", "span_not_on_line"]


def test_time_scope_from_the_question() -> None:
    rows = [
        row(1, 1, "trip to Lisbon", "went to Lisbon"),
        row(2, 2, "trip to Lisbon", "went to Lisbon again"),
    ]
    t = table("How many times did Ana go on a trip in April 2023?", TRIPS, rows)
    assert t.count == 1 and t.rows[0].reason == "outside_time_scope"


def test_cumulative_total_reconciles_to_a_range_or_unresolved() -> None:
    ctx = TRIPS + "[2023-06-01] Ana: That makes five trips so far this year.\n"
    rows = [
        row(1, 1, "trip to Lisbon", "went to Lisbon"),
        row(2, 2, "trip to Lisbon", "went to Lisbon again"),
    ]
    t = table(Q_TIMES, ctx, rows)
    assert t.status == "range" and (t.low, t.high) == (2, 5)
    assert "at least 2" in render_table_answer(t)
    # a total that agrees is resolved
    agree = table(Q_TIMES, ctx.replace("five trips", "two trips"), rows)
    assert agree.status == "resolved" and agree.count == 2
    # a total below the table is a conflict
    low = table(Q_TIMES, ctx.replace("five trips", "one trip"), rows)
    assert low.status == "unresolved"
    # two disagreeing undated totals are a conflict
    two = ctx.replace("[2023-06-01]", "[2023-06-01]") + "Ana: In total three trips.\n"
    assert cumulative_statements(Q_TIMES, numbered_lines(two))


def test_ordinal_lower_bound_makes_the_table_incomplete() -> None:
    ctx = TRIPS + "[2023-06-01] Ana: This was my fourth trip, so exciting.\n"
    rows = [row(1, 1, "trip to Lisbon", "went to Lisbon")]
    t = table(Q_TIMES, ctx, rows)
    assert t.status == "range" and t.low == 4


# ---- readers ---------------------------------------------------------------------------------


class Fake:
    reader_id = "fake"
    model = "m"
    makes_model_calls = True

    def __init__(self, text: str) -> None:
        self.text = text
        self.asked: list[str] = []

    def describe(self) -> dict[str, Any]:
        return {"reader_id": "fake"}

    async def answer(self, question: str, context: str, question_date: str | None = None):
        self.asked.append(question)
        return ReaderAnswer(
            text=self.text, prompt_tokens=10, completion_tokens=2, latency_ms=1.0, model_calls=1
        )


class Chat:
    def __init__(self, *replies: str) -> None:
        self.replies, self.calls = list(replies), []

    async def __call__(self, prompt: str, system: str | None = None) -> str:
        self.calls.append((prompt, system))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


GOOD = json.dumps(
    {"rows": [
        row(1, 1, "trip to Lisbon", "went to Lisbon"),
        row(2, 2, "trip to Lisbon", "went to Lisbon again"),
        row(3, 3, "day trip to Sintra", "day trip to Sintra"),
    ]}
)  # fmt: skip


def test_count_verify_uses_the_table_and_counts_rows_not_mentions() -> None:
    builder = EvidenceTableBuilder(Chat(GOOD))
    reader = CountVerifyReader(Fake("At least 5"), None, "two_call", table=builder)
    ans = run(reader.answer(Q_TIMES, TRIPS))
    assert ans.text.startswith("3: ") and "Sintra" in ans.text
    assert ans.model_calls == 2 and builder.calls == 1
    meta = ans.extra_meta["count_verify"]
    assert meta["mode"] == "table" and meta["table"]["n_items"] == 3
    assert meta["inner_answer"] == "At least 5"
    assert reader.reader_id.endswith("+count-table")


def test_count_verify_falls_back_when_the_table_is_unusable() -> None:
    ctx = TRIPS + "[2023-06-01] Ana: One trip so far, just that.\n"  # below the table's count
    reader = CountVerifyReader(Fake("About 3"), None, table=EvidenceTableBuilder(Chat(GOOD)))
    ans = run(reader.answer(Q_TIMES, ctx))
    assert ans.text == "About 3" and ans.extra_meta["count_verify"]["fallback"] == "unresolved"
    bad = CountVerifyReader(Fake("About 3"), None, table=EvidenceTableBuilder(Chat("garbage")))
    ans = run(bad.answer(Q_TIMES, TRIPS))
    assert ans.text == "About 3" and ans.extra_meta["count_verify"]["fallback"] == "empty"
    other = run(bad.answer("Where did Ana live?", TRIPS))
    assert other.text == "About 3" and "count_verify" not in other.extra_meta


def test_slot_verify_checks_a_list_against_the_table_and_shares_the_call() -> None:
    builder = EvidenceTableBuilder(Chat(GOOD))
    inner = Fake("Lisbon")
    reader = SlotVerifyReader(inner, table=builder)
    q = "Which places did Ana visit?"
    ans = run(reader.answer(q, TRIPS))
    meta = ans.extra_meta["slot_verify"]
    kinds = [d["kind"] for d in meta.get("defects", [])]
    assert "dropped_items" in kinds and "sintra" in meta["defects"][0]["detail"]
    assert meta["evidence_table"]["status"] == "resolved"
    assert builder.calls == 1
    # the same question again is a memo hit: no second extraction call
    run(reader.answer(q, TRIPS))
    assert builder.calls == 1
    # a complete answer is clean
    clean = run(SlotVerifyReader(Fake("Lisbon, Sintra"), table=builder).answer(q, TRIPS))
    assert clean.extra_meta["slot_verify"]["outcome"] == "clean"


def test_slot_verify_flags_a_count_that_disagrees_with_the_table() -> None:
    builder = EvidenceTableBuilder(Chat(GOOD))
    ans = run(SlotVerifyReader(Fake("5"), table=builder).answer(Q_TIMES, TRIPS))
    meta = ans.extra_meta["slot_verify"]
    assert meta["defects"][0]["kind"] == "count_table_mismatch"
    assert "says 5 but the memories support 3" in meta["defects"][0]["detail"]
    ok = run(SlotVerifyReader(Fake("Three trips"), table=builder).answer(Q_TIMES, TRIPS))
    assert ok.extra_meta["slot_verify"]["outcome"] == "clean"


def test_slot_verify_without_a_table_is_unchanged() -> None:
    ans = run(SlotVerifyReader(Fake("Lisbon")).answer("Which places did Ana visit?", TRIPS))
    assert "evidence_table" not in ans.extra_meta["slot_verify"]
    assert ans.model_calls == 1
