"""I56 count-verify, I57 date-repair, I58 judge conventions, I62 errata: fake readers only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from memspine_evals.contracts import ReaderAnswer
from memspine_evals.count_verify import (
    ENUMERATE_PROMPT,
    SINGLE_PROMPT,
    CountVerifyReader,
    ListedItem,
    dedupe_items,
    parse_listing,
    render_count_answer,
)
from memspine_evals.date_repair import DateRepairReader, repair_date_answer
from memspine_evals.judge import GuardedJudge, JudgeScale, Verdict
from memspine_evals.judge_conventions import check_conventions, edit_close
from memspine_evals.readers import ScriptedReader

CTX = (
    "[2023-05-01] Nate: I won my first tournament today\n"
    "[2023-05-20] Nate: I won another tournament, a regional one\n"
    "[2023-05-21] Nate: that regional tournament win was great\n"
    "[2023-07-15] Caroline: I went hiking last Friday [= 2023-07-14]\n"
    "[2023-07-15] Caroline: I started pottery yesterday"
)


class FakeReader:
    """Answers with a fixed text, counts its calls and the prompts it was shown."""

    reader_id = "fake"
    model = "fake-model"
    makes_model_calls = True

    def __init__(self, text: str, *, calls: int = 1, extra: dict[str, Any] | None = None) -> None:
        self.text = text
        self.n = 0
        self._calls = calls
        self._extra = extra or {}

    def describe(self) -> dict[str, Any]:
        return {"reader_id": self.reader_id}

    async def answer(self, question: str, context: str, question_date: str | None = None) -> ReaderAnswer:
        self.n += 1
        return ReaderAnswer(
            text=self.text,
            prompt_tokens=100,
            completion_tokens=20,
            latency_ms=5.0,
            model_calls=self._calls,
            extra_meta=self._extra,
        )


# -- I56 ------------------------------------------------------------------------------------


def test_prompts_format() -> None:
    for p in (ENUMERATE_PROMPT, SINGLE_PROMPT):
        out = p.format(context="c", question="q", question_date="d")
        assert "Question: q" in out and '"items"' in out


def test_parse_listing_variants() -> None:
    text = '```json\n{"items": [{"item": "Won tournament A", "date": "2023-05-01"}, "tournament B"]}\n```\nCount: 2'
    items, stated = parse_listing(text, CTX)  # type: ignore[misc]
    assert [i.item for i in items] == ["Won tournament A", "tournament B"]
    assert items[0].date == "2023-05-01" and stated == 2
    assert parse_listing("no json here") is None
    # a date the context never states is dropped
    items, _ = parse_listing('{"items": [{"item": "x", "date": "2020-01-01"}]}', CTX)  # type: ignore[misc]
    assert items[0].date == ""
    # think block and a bare list
    items, _ = parse_listing('<think>[1]</think>[{"name": "a"}]', "")  # type: ignore[misc]
    assert [i.item for i in items] == ["a"]


def test_dedupe_same_date_and_same_string() -> None:
    items = [
        ListedItem("won a regional tournament", "2023-05-20"),
        ListedItem("Regional tournament win", "2023-05-20"),  # same date, overlapping content
        ListedItem("Won the first tournament", "2023-05-01"),
        ListedItem("won the first tournaments", "2023-05-01"),  # plural, same date
    ]
    assert len(dedupe_items(items)) == 2


def test_dedupe_events_vs_things() -> None:
    same = [ListedItem("went camping", "2023-05-01"), ListedItem("Went camping.", "2023-06-01")]
    assert len(dedupe_items(same, events=True)) == 2  # "how many times": two events
    assert len(dedupe_items(same, events=False)) == 1  # things: one item named twice
    # different items on different dates are never merged
    assert len(dedupe_items([ListedItem("dog Oliver", "a"), ListedItem("cat Luna", "b")])) == 2
    # an undated repeat keeps the date of the dated entry
    out = dedupe_items([ListedItem("hike", ""), ListedItem("hike", "2023-05-01")])
    assert out == [ListedItem("hike", "2023-05-01")]


def test_render() -> None:
    assert render_count_answer([ListedItem("a", "2023-05-01"), ListedItem("b")]) == "2: a (2023-05-01); b"


async def test_two_call_counts_in_code_and_costs_one_extra_call() -> None:
    inner = FakeReader("Nate won at least five tournaments.")
    listing = json.dumps(
        {
            "items": [
                {"item": "first tournament", "date": "2023-05-01"},
                {"item": "regional tournament", "date": "2023-05-20"},
                {"item": "regional tournament win", "date": "2023-05-21"},
                {"item": "the regional tournament", "date": "2023-05-20"},
            ]
        }
    )
    enum = FakeReader(listing)
    reader = CountVerifyReader(inner, enum, "two_call")
    out = await reader.answer("How many tournaments has Nate won?", CTX)
    # 4 listed: the 4th merges with the 2nd (same date, containment); the 3rd (05-21) stays apart
    assert out.text.startswith("3:")
    assert out.model_calls == 2 and out.prompt_tokens == 200
    meta = out.extra_meta["count_verify"]
    assert meta["extra_calls"] == 1 and meta["n_listed"] == 4 and meta["n_counted"] == 3
    assert meta["inner_answer"] == "Nate won at least five tournaments."
    assert reader.seen == 1 and reader.replaced == 1 and reader.extra_calls == 1
    assert reader.reader_id == "fake+count-twocall"
    assert reader.describe()["count_verify"] == "two_call"


async def test_single_call_replaces_the_answer_and_records_model_count() -> None:
    inner = FakeReader("should not be called")
    enum = FakeReader('{"items": [{"item": "a", "date": ""}, {"item": "b", "date": ""}]}\nCount: 7')
    reader = CountVerifyReader(inner, enum, "single")
    out = await reader.answer("How many pets does she have?", CTX)
    assert out.text == "2: a; b"  # the code's count, not the model's 7
    assert inner.n == 0 and out.model_calls == 1
    assert out.extra_meta["count_verify"]["model_count"] == 7
    assert out.extra_meta["count_verify"]["extra_calls"] == 0


async def test_non_count_question_is_untouched() -> None:
    inner = FakeReader("Denver")
    enum = FakeReader("{}")
    out = await CountVerifyReader(inner, enum).answer("Where did she move?", CTX)
    assert out.text == "Denver" and enum.n == 0 and out.model_calls == 1
    assert "count_verify" not in out.extra_meta
    # an empty context is not enumerated either
    out = await CountVerifyReader(inner, enum).answer("How many pets?", "  ")
    assert enum.n == 0 and out.text == "Denver"


async def test_unparsed_or_empty_listing_falls_back() -> None:
    inner = FakeReader("about three")
    reader = CountVerifyReader(inner, FakeReader("I cannot list them."), "two_call")
    out = await reader.answer("How many hikes?", CTX)
    assert out.text == "about three" and out.extra_meta["count_verify"]["fallback"] == "unparsed"
    reader = CountVerifyReader(inner, FakeReader('{"items": []}'), "two_call")
    out = await reader.answer("How many hikes?", CTX)
    assert out.text == "about three" and out.extra_meta["count_verify"]["fallback"] == "empty_list"
    assert reader.fallbacks == 1
    with pytest.raises(ValueError):
        CountVerifyReader(inner, inner, "bogus")


async def test_scripted_reader_composes() -> None:
    inner = ScriptedReader({"How many hikes?": "maybe 3"})
    enum = ScriptedReader({"How many hikes?": '{"items": ["a", "b"]}'})
    out = await CountVerifyReader(inner, enum).answer("How many hikes?", CTX)
    assert out.text == "2: a; b"


# -- I57 ------------------------------------------------------------------------------------


def test_repair_relative_phrase_against_the_line_date() -> None:
    q = "When did Caroline go hiking?"
    fixed = repair_date_answer(q, "She went hiking last Friday.", CTX)
    assert fixed is not None
    text, meta = fixed
    assert "2023-07-14" in text and "originally" in text and "last Friday" in text
    assert meta["rule"] == "relative_phrase" and meta["anchor"] == "2023-07-15"


def test_repair_yesterday_and_bare_weekday() -> None:
    text, meta = repair_date_answer("When did Caroline start pottery?", "Yesterday.", CTX)  # type: ignore[misc]
    assert "2023-07-14" in text and meta["rule"] == "relative_phrase"
    ctx = "[2023-07-15] Caroline: we went hiking on Friday with the kids"
    text, meta = repair_date_answer("When did Caroline go hiking?", "On Friday.", ctx)  # type: ignore[misc]
    assert "2023-07-14" in text and meta["rule"] == "bare_weekday"


def test_repair_weekday_that_disagrees_with_its_date() -> None:
    # 10 July 2022 is a Sunday; the line is dated 2022-07-10, so the weekday names the event
    ctx = "[2022-07-10] Nate: I won my fourth tournament last Friday"
    text, meta = repair_date_answer("When did Nate win his fourth tournament?", "Friday, 2022-07-10", ctx)  # type: ignore[misc]
    assert "2022-07-08" in text and meta["rule"] == "weekday_vs_line_date"
    # a date that is no line's date wins; the weekday name is fixed
    text, meta = repair_date_answer("When was the party?", "Friday, 2022-07-10", "[2022-07-12] x: party")  # type: ignore[misc]
    assert text == "Sunday, 2022-07-10" and meta["rule"] == "weekday_fixed"


@pytest.mark.parametrize(
    ("question", "answer", "context"),
    [
        ("When did she hike?", "Friday, 2023-07-14", CTX),  # consistent: nothing to repair
        ("When did she hike?", "I do not know.", CTX),  # refusal
        ("What did she do?", "She went hiking last Friday.", CTX),  # not a date question
        ("How long ago did she hike?", "last Friday", CTX),  # duration
        ("When did she hike?", "last Friday", "no dated lines here"),  # no anchor
        ("When did she hike?", "The week before 9 June 2023", CTX),  # absolute date present
        ("When did she hike?", "last Friday or yesterday", CTX),  # two phrases: ambiguous
        # tie between two lines of different dates that both hold the phrase
        ("When did Bob hike?", "last Friday", "[2023-07-15] Bob: hiked last Friday\n[2023-07-22] Bob: hiked last Friday"),
    ],
)
def test_repair_fails_closed(question: str, answer: str, context: str) -> None:
    assert repair_date_answer(question, answer, context) is None


async def test_date_repair_reader_costs_no_call_and_keeps_original_in_meta() -> None:
    inner = FakeReader("She went hiking last Friday.")
    reader = DateRepairReader(inner)
    out = await reader.answer("When did Caroline go hiking?", CTX)
    assert "2023-07-14" in out.text and out.model_calls == 1 and inner.n == 1
    assert out.extra_meta["date_repair"]["original"] == "She went hiking last Friday."
    assert reader.repaired == 1 and reader.describe()["date_repair"] == "v1"
    same = await DateRepairReader(FakeReader("Denver")).answer("Where?", CTX)
    assert same.text == "Denver" and "date_repair" not in same.extra_meta


# -- I58 ------------------------------------------------------------------------------------


def test_edit_close() -> None:
    assert edit_close("xeonoblade", "xenoblade")  # insertion
    assert edit_close("chronicles", "chronicels")  # transposition
    assert not edit_close("maria", "mario")  # short substitution: two different people
    assert not edit_close("jon", "jan")
    assert edit_close("blueberry", "blueberrt")  # long substitution


@pytest.mark.parametrize(
    ("question", "gold", "answer", "rule"),
    [
        ("How many?", "Two", "2 hikes", "numeral"),
        ("How many?", "five", "She has 5 cats.", "numeral"),
        ("How many?", "4", "Joanna has been on four hikes.", "numeral"),
        ("How many times?", "twice", "2: a (2023-05-01); b (2023-06-02)", "numeral"),
        ("How long?", "four months", "It took her 4 months.", "numeral"),
        ("What game?", "Xenoblade Chronicles", "Xeonoblade Chronicles", "typo"),
        ("What does she do?", "Running, pottery", "She runs... running, painting, pottery and reading.", "list_superset"),
        ("Where?", "Paris, Rome", "Jon visited Paris, Rome and Berlin.", "list_superset"),
        ("Pets?", "cats, dogs", "She has two cats, three dogs and a horse.", "list_superset"),
    ],
)
def test_conventions_credit(question: str, gold: str, answer: str, rule: str) -> None:
    got = check_conventions(question, answer, gold)
    assert got is not None and got.rule == rule


@pytest.mark.parametrize(
    ("question", "gold", "answer"),
    [
        ("How many?", "Two", "at least 2"),  # an estimate
        ("How many?", "Two", "She has three, not two."),
        ("How many?", "Two", "About 3 weeks (adopted two weeks before)."),  # first number differs
        ("How many weeks?", "two weeks", "Approximately 3 weeks (adopted two weeks before)"),
        ("How many?", "2022", "In 2022."),  # a year gold is not a numeral gold
        ("Where?", "Paris, Rome", "Jon visited Paris."),  # a partial list stays wrong
        ("Where?", "Paris, Rome", "He visited Paris and Rome? No, never Rome."),
        ("Where?", "Paris, Rome", "Only Paris and Rome."),  # restriction word
        ("Who?", "Maria", "Mario"),  # a short substitution is another person
        ("Who?", "Maria", "Marie"),
        ("Who?", "Xenoblade Chronicles", "I do not know."),
        ("Who?", "Not mentioned in the conversation", "Not mentioned"),
        ("When?", "20 June, 2023", "June 20, 2023 and June 21, 2023"),  # date golds: not a list
        ("What?", "Poetry reading, conference", "A conference."),
        ("What?", "the beach", "She went to the lake."),
        # false positives found while reading the credits over 119 runs (regression cases)
        ("Pets?", "Two cats and a dog", "Melanie has a dog named Oliver and a cat named Bailey."),
        ("Setback?", "She got hurt and had to take a break from pottery.", "She got hurt, so she broke."),
        ("Which?", "blueberries and coconut milk", "The ingredients (in addition to blueberries, coconut milk) are walnuts."),
        ("Symbols?", "Rainbow flag, transgender symbol", "Her symbols: a necklace, a bowl. She mentioned the transgender community and a flag she saw at a very long march in the city centre last year."),
    ],
)
def test_conventions_do_not_credit(question: str, gold: str, answer: str) -> None:
    assert check_conventions(question, answer, gold) is None


class _Spec:
    judge_id, model, prompt_id, prompt_hash, makes_model_calls = "x", "m", "p", "h", True
    scale = JudgeScale.BINARY
    params: Any = ()


class _Inner:
    spec = _Spec()

    def __init__(self, score: float) -> None:
        self._score = score

    async def score(self, question: str, answer: str, gold: str | None) -> Verdict:
        return Verdict(score=self._score, scale=JudgeScale.BINARY, raw="x", model_calls=1)


async def test_guarded_judge_conventions_keep_score_and_add_second_column() -> None:
    judge = GuardedJudge(_Inner(0.0), conventions=True)
    v = await judge.score("What game?", "Xeonoblade Chronicles", "Xenoblade Chronicles")
    assert v.score == 0.0  # the LLM verdict is untouched
    assert v.meta["score_conventions"] == 1.0 and v.meta["convention"] == "typo"
    assert judge.spec.params["conventions"] == "conventions/v1"
    wrong = await judge.score("What game?", "Zelda", "Xenoblade Chronicles")
    assert wrong.score == 0.0 and wrong.meta["score_conventions"] == 0.0
    ok = await GuardedJudge(_Inner(1.0), conventions=True).score("q", "anything", "gold")
    assert ok.score == 1.0 and ok.meta["score_conventions"] == 1.0 and "convention" not in ok.meta


async def test_guarded_judge_without_flag_is_unchanged() -> None:
    judge = GuardedJudge(_Inner(0.0))
    v = await judge.score("What game?", "Xeonoblade Chronicles", "Xenoblade Chronicles")
    assert v.meta == {} and "conventions" not in judge.spec.params


# -- I62 ------------------------------------------------------------------------------------


def test_errata_new_classes_are_in_the_file_and_not_default_excluded() -> None:
    from memspine_evals.errata import DEFAULT_EXCLUDE_TAGS, load_errata

    path = Path(__file__).resolve().parents[1] / "analysis" / "locomo_errata.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    new = {(e["item"], e["qid"]): e for e in payload["entries"] if e.get("source") == "I62 review 2026-10-10"}
    assert set(new) == {
        ("conv-41", "2-68"),
        ("conv-42", "3-88"),
        ("conv-26", "0-184"),
        ("conv-26", "0-186"),
        ("conv-42", "3-244"),
    }
    assert {e["tag"] for e in new.values()} == {"premise_error", "bad_evidence_id", "cat5_answerable"}
    default = load_errata(path)
    assert not set(new) & set(default)  # defaults unchanged
    extended = load_errata(path, exclude_tags=(*DEFAULT_EXCLUDE_TAGS, "premise_error", "cat5_answerable"))
    assert ("conv-26", "0-184") in extended and ("conv-42", "3-88") not in extended


def test_with_post_steps_wiring() -> None:
    from types import SimpleNamespace

    from memspine_evals.experiments import with_post_steps

    base = FakeReader("x")
    built: list[str] = []

    def sibling(prompt: str) -> FakeReader:
        built.append(prompt)
        return FakeReader("{}")

    off = SimpleNamespace(count_verify="", date_repair=False)
    assert with_post_steps(base, off, sibling) is base and not built  # nothing changes when off
    on = SimpleNamespace(count_verify="single", date_repair=True)
    wrapped = with_post_steps(base, on, sibling)
    assert built == [SINGLE_PROMPT]
    assert wrapped.reader_id == "fake+count-single+daterepair"
    two = with_post_steps(base, SimpleNamespace(count_verify="two_call", date_repair=False), sibling)
    assert built[-1] == ENUMERATE_PROMPT and two.mode == "two_call"
    with pytest.raises(ValueError):
        with_post_steps(base, SimpleNamespace(count_verify="nope", date_repair=False), sibling)
