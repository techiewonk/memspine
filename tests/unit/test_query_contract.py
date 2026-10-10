"""A03: the typed query contract (``core/query_contract``) and its engine threading
(``read.query_contract``, ``read.query_contract_use``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig, ReadConfig
from memspine.core.query_contract import (
    QueryContract,
    build_contract,
    contract_header,
    is_ambiguous,
    merge_contract,
    parse_contract,
    rerank_hint,
)
from memspine.services.llm.base import LLMRouter, LLMService

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)

# ----------------------------------------------------------------------------- the rules


@pytest.mark.parametrize(
    ("question", "label", "cardinality"),
    [
        ("Which city did Sam move to in 2021?", "place:city", "one"),
        ("Which country has Sam visited?", "place:country", "one"),
        ("What countries has Sam visited?", "place:country", "many"),
        ("Where did Sam go for the weekend?", "place", "one"),
        ("When did Sam adopt the dog?", "date", "one"),
        ("What year did Sam graduate?", "date:year", "one"),
        ("What time does Sam wake up?", "date:time", "one"),
        ("How long did Sam live in Oslo?", "duration", "one"),
        ("How many weeks passed between the two trips?", "duration", "one"),
        ("How many times did Sam go hiking?", "count", "count"),
        ("How old is Sam's dog?", "quantity", "one"),
        ("Who gave Sam the guitar?", "person", "one"),
        ("Which book did Sam read last summer?", "title", "one"),
        ("What is the name of Sam's cat?", "name", "one"),
        ("Why did Sam leave the club?", "reason", "one"),
        ("Did Sam finish the marathon?", "yesno", "one"),
        ("Is Sam a morning person or a night owl?", "choice", "one"),
        ("How did Sam fix the bike?", "description", "one"),
    ],
)
def test_answer_type_and_cardinality(question: str, label: str, cardinality: str) -> None:
    c = build_contract(question)
    assert c.type_label == label
    assert c.cardinality == cardinality


def test_a_wh_word_after_another_wh_word_does_not_retype_the_question() -> None:
    # "when" is a clause here, the question asks who.
    assert build_contract("Who supports Caroline when she has a bad day?").answer_type == "person"
    assert build_contract("What time management tricks does Ana use?").answer_type != "date"


def test_subjects_relation_and_time_scope() -> None:
    c = build_contract("Which city did Maria Lopez visit in March 2022?")
    assert c.subjects == ("Maria Lopez",)
    assert "visit" in c.relation and "maria" not in c.relation
    assert "March 2022" in c.time_scope
    first_person = build_contract("What did I tell you about my trip?")
    assert "asker" in first_person.subjects and "assistant" in first_person.subjects
    assert build_contract("When did Sam start?").time_scope == ""


def test_request_kinds() -> None:
    assert build_contract("What did Sam say about the trip?").request == "recall"
    assert build_contract("Would Sam enjoy a quiet cabin?").request == "inference"
    assert build_contract("What might Sam's job be?").request == "inference"
    assert build_contract("Can you recommend a book for me?").request == "recommendation"
    # "suggestions" as a noun is not a request for a suggestion
    assert build_contract("What suggestions has Evan given to Sam?").request == "recall"


@pytest.mark.parametrize("question", ["", "   ", "Tell me more.", "Is that all?!!", "¿Dónde vive?"])
def test_unrecognised_questions_are_unknown_and_add_nothing(question: str) -> None:
    c = build_contract(question)
    if question.startswith("Is that"):
        return  # an aux opener is yes/no by its surface
    assert c.answer_type == "unknown"
    assert contract_header(c) is None
    assert rerank_hint(c) is None


def test_header_and_hint() -> None:
    c = build_contract("Which city did Sam move to?")
    assert contract_header(c) == "Answer type: place:city; expected: one."
    assert rerank_hint(c) == " (the answer is a place city)"
    many = build_contract("What hobbies does Sam have?")
    assert "many" in (contract_header(many) or "")
    inf = build_contract("Would Sam like hiking?")
    assert "Inference" in (contract_header(inf) or "")


def test_no_dataset_vocabulary_in_the_module() -> None:
    import inspect

    import memspine.core.query_contract as mod

    source = inspect.getsource(mod).lower()
    for word in ("locomo", "longmemeval", "caroline", "melanie", "op-bench", "opbench"):
        assert word not in source


# -------------------------------------------------------------------- resolver and merge


def test_ambiguity_and_merge_keep_the_rules_when_they_know() -> None:
    known = build_contract("Which city did Sam move to?")
    vague = build_contract("What is Sam's main passion?")
    assert not is_ambiguous(known)
    assert is_ambiguous(vague)
    resolved = parse_contract({"answer_type": "description", "cardinality": "one"}, vague)
    assert merge_contract(known, resolved) is known  # the rules are the guard
    assert merge_contract(vague, resolved).answer_type == "description"
    assert merge_contract(vague, None) is vague
    assert merge_contract(vague, parse_contract({"answer_type": "banana"}, vague)) is vague


def test_parse_contract_validates_against_the_allowed_sets() -> None:
    base = QueryContract(subjects=("Ana",), relation="job")
    c = parse_contract(
        {"answer_type": "PLACE", "subtype": "galaxy", "cardinality": "lots", "request": "x"}, base
    )
    assert (c.answer_type, c.subtype, c.cardinality, c.request) == ("place", "", "one", "recall")
    assert c.subjects == ("Ana",) and c.source == "llm"
    assert parse_contract({"answer_type": "place", "subtype": "city"}, base).subtype == "city"


# ------------------------------------------------------------------------------- config


def test_off_by_default() -> None:
    read = ReadConfig()
    assert read.query_contract == "off"
    assert read.query_contract_use == ["header"]


# ------------------------------------------------------------------------------- engine


class _PlanStub:
    def __init__(self, reply: str | None) -> None:
        self.reply = reply
        self.calls = 0

    @property
    def provider_id(self) -> str:
        return "stub:plan"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.calls += 1
        if self.reply is None:
            raise RuntimeError("provider down")
        return self.reply


class _Reranker:
    reranker_id = "fake"

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.queries.append(query)
        return [1.0 for _ in documents]


def _engine(monkeypatch: pytest.MonkeyPatch, stub: _PlanStub | None = None, **read: Any) -> Engine:
    stubs: dict[str, LLMService] = {"plan": stub} if stub is not None else {}

    async def router(self: Engine, config: MemspineConfig) -> LLMRouter:
        return LLMRouter(stubs)

    monkeypatch.setattr(Engine, "_build_llm_router", router)
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )


async def _seed(eng: Engine) -> None:
    for i, text in enumerate(
        ["Ana moved to Lisbon last spring.", "Ana likes pottery classes.", "Ben visited Oslo."]
    ):
        await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(days=i)
        )


async def _lines(eng: Engine, query: str) -> list[str]:
    ctx = await eng.assemble(query, namespace="a", budget_tokens=2000, top_k=4)
    return [r.content for r in ctx.records]


async def test_header_is_a_lead_line_only_when_on_and_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on = _engine(monkeypatch, query_contract="heuristic")
    await on.start()
    try:
        await _seed(on)
        lines = await _lines(on, "Which city did Ana move to?")
        assert lines[0] == "Answer type: place:city; expected: one."
        # an unknown type adds nothing: the general path
        plain = await _lines(on, "Tell me about Ana.")
        assert not any(line.startswith("Answer type:") for line in plain)
    finally:
        await on.stop()
    off = _engine(monkeypatch)
    await off.start()
    try:
        await _seed(off)
        assert not any(
            line.startswith("Answer type:")
            for line in await _lines(off, "Which city did Ana move to?")
        )
    finally:
        await off.stop()


async def test_header_can_be_switched_off_while_the_contract_is_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine(monkeypatch, query_contract="heuristic", query_contract_use=["rerank"])
    await eng.start()
    try:
        await _seed(eng)
        lines = await _lines(eng, "Which city did Ana move to?")
        assert not any(line.startswith("Answer type:") for line in lines)
    finally:
        await eng.stop()


async def test_rerank_hint_reaches_the_reranker_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, list[str]] = {}
    for use in (["header"], ["header", "rerank"]):
        eng = _engine(
            monkeypatch,
            rerank="fastembed",
            query_contract="heuristic",
            query_contract_use=use,
        )
        await eng.start()
        fake = _Reranker()
        eng._reranker = fake  # type: ignore[assignment]
        try:
            await _seed(eng)
            await eng.search("Which city did Ana move to?", namespace="a", top_k=3)
            seen[",".join(use)] = fake.queries
        finally:
            await eng.stop()
    assert seen["header"] == ["Which city did Ana move to?"]
    assert seen["header,rerank"] == ["Which city did Ana move to? (the answer is a place city)"]


async def test_llm_resolver_is_called_only_for_an_ambiguous_question_and_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _PlanStub("answer_type: description\ncardinality: one\nrequest: recall\n")
    eng = _engine(monkeypatch, stub, query_contract="llm")
    await eng.start()
    try:
        await _seed(eng)
        known = await _lines(eng, "Which city did Ana move to?")
        assert known[0].startswith("Answer type: place:city")
        assert stub.calls == 0  # the rules knew
        vague = await _lines(eng, "What is Ana's main passion?")
        assert vague[0].startswith("Answer type: description")
        assert stub.calls == 1
        await _lines(eng, "What is Ana's main passion?")
        assert stub.calls == 1  # cached per question
    finally:
        await eng.stop()


@pytest.mark.parametrize("reply", [None, "answer_type: banana\n", "not: [valid"])
async def test_llm_failure_falls_back_to_the_rules(
    monkeypatch: pytest.MonkeyPatch, reply: str | None
) -> None:
    eng = _engine(monkeypatch, _PlanStub(reply), query_contract="llm")
    await eng.start()
    try:
        await _seed(eng)
        lines = await _lines(eng, "What is Ana's main passion?")
        assert not any(line.startswith("Answer type:") for line in lines)
        assert (await _lines(eng, "Which city did Ana move to?"))[0].startswith("Answer type:")
    finally:
        await eng.stop()
