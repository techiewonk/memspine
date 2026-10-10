"""E01: read-time query-directed assertions joined into derived chains, and the separate
write-time projection arm (``memories.semantic.policies.fact_projection``). Fake LLMs only."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.core.fact_chain import (
    Assertion,
    fact_projection_on,
    join_chains,
    norm_entity,
    relation_unresolved,
    render_chains,
    slot_name,
    validate_assertions,
)
from memspine.core.query_contract import build_contract
from memspine.engine import search_forensics
from memspine.exceptions import ConfigError
from memspine.prompts.models import AssertionOut
from memspine.services.llm import structured
from memspine.services.llm.base import LLMRouter, LLMService

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)
MOVED = "Sven: I moved away from my home country when I was twenty"
HOME = "Sven: my home country is Sweden"
OTHER = "Anna: my home country is Peru"
QUESTION = "Which country did Sven move from?"


def A(s: str, r: str, o: str, line: int, span: str, modality: str = "asserted") -> Assertion:
    return Assertion(s, r, o, line, span, modality)


# -- pure parts -----------------------------------------------------------------------------


def test_normalisation_is_exact_not_fuzzy() -> None:
    assert norm_entity("The Sven's") == norm_entity("sven") == "sven"
    assert norm_entity("he") == "" and norm_entity("") == ""
    assert slot_name("home_country") == slot_name("his home country") == "home country"
    assert slot_name("Sven's home country") == "home country"
    assert norm_entity("Sweden") != norm_entity("Swedish")


def test_spans_must_be_in_their_line() -> None:
    lines = ["Sven: my home country is Sweden", "Anna: nothing here"]
    good = AssertionOut(
        subject="Sven",
        relation="home_country",
        object="Sweden",
        line=1,
        span="my home country is Sweden",
    )
    paraphrase = AssertionOut(
        subject="Sven",
        relation="home_country",
        object="Sweden",
        line=2,
        span="Sven comes from Sweden",
    )
    wrong_line = AssertionOut(
        subject="Sven", relation="home_country", object="Sweden", line=3, span="my home country"
    )
    no_span = AssertionOut(subject="Sven", relation="home_country", object="Sweden", line=1)
    got = validate_assertions([good, good, paraphrase, wrong_line, no_span], lines)
    assert [(a.subject, a.line) for a in got] == [("Sven", 1)]  # duplicates dropped too


def test_a_relation_reference_joins_same_subject() -> None:
    a = A("Sven", "moved_from", "home country", 1, "moved away from my home country")
    b = A("Sven", "home_country", "Sweden", 2, "my home country is Sweden")
    [chain] = join_chains([a, b], focus=["country", "move"])
    assert chain.via == "relation" and chain.first is a and chain.second is b
    text = render_chains([chain], lambda n: f"[line {n}]")
    assert "Sven moved_from Sweden (its home_country)" in text
    assert '"moved away from my home country" [line 1]' in text
    assert '"my home country is Sweden" [line 2]' in text


def test_an_entity_bridge_joins_through_a_resolved_entity() -> None:
    a = A("Ana", "sister_of", "Bea", 1, "Bea is my sister")
    b = A("Bea", "works_at", "Acme", 2, "Bea works at Acme")
    [chain] = join_chains([a, b], focus=["Ana", "sister", "works"])
    assert chain.via == "entity" and chain.bridge == "Bea"


def test_no_join_without_an_evidenced_relation() -> None:
    a = A("Sven", "moved_from", "home country", 1, "x")
    other_subject = A("Anna", "home_country", "Peru", 2, "x")  # a different entity
    other_relation = A("Sven", "birthplace", "Sweden", 2, "x")  # the relation is not named
    near = A("Sven", "home_country", "Sweden", 2, "x")
    assert join_chains([a, other_subject]) == []
    assert join_chains([a, other_relation]) == []
    assert join_chains([A("Sven", "moved_from", "home", 1, "x"), near]) == []  # no fuzzy match
    pronoun = [A("Ana", "sister_of", "she", 1, "x"), A("she", "works_at", "Acme", 2, "x")]
    assert join_chains(pronoun) == []
    assert join_chains([A("Ana", "knows", "Ana", 1, "x"), A("Ana", "knows", "Ana", 2, "x")]) == []


def test_a_negated_hypothetical_or_uncertain_assertion_never_joins() -> None:
    for mode in ("negated", "hypothetical", "uncertain", "conditional"):
        a = A("Sven", "moved_from", "home country", 1, "x", mode)
        b = A("Sven", "home_country", "Sweden", 2, "x")
        assert join_chains([a, b]) == [] and join_chains([b, a]) == []
    planned = A("Sven", "moved_from", "home country", 1, "x", "planned")
    assert join_chains([planned, A("Sven", "home_country", "Sweden", 2, "x")])


def test_focus_directs_the_chains() -> None:
    a = A("Sven", "moved_from", "home country", 1, "x")
    b = A("Sven", "home_country", "Sweden", 2, "x")
    assert join_chains([a, b], focus=["Lucas", "violin"]) == []
    assert len(join_chains([a, b], focus=["Sven"])) == 1


def test_relation_unresolved_is_a_lexical_check_on_the_contract() -> None:
    contract = build_contract("Which country did Sven move from?")
    assert relation_unresolved(contract, ["Sven: my home country is Sweden"])
    assert not relation_unresolved(contract, ["Sven: I move to Spain next year"])
    assert not relation_unresolved(build_contract("What?"), ["anything"])  # nothing to resolve


@pytest.mark.parametrize(
    ("value", "want"),
    [("off", False), (False, False), (None, False), ("on", True), (True, True), ("ON", True)],
)
def test_fact_projection_switch(value: Any, want: bool) -> None:
    assert fact_projection_on({"fact_projection": value}) is want
    assert fact_projection_on({}) is False
    with pytest.raises(ValueError):
        fact_projection_on({"fact_projection": "maybe"})


def test_defaults_are_off() -> None:
    assert MemspineConfig().read.fact_chain == "off"


# -- the read-time arm ------------------------------------------------------------------------


class _ChainStub:
    """An ``extract`` stub: finds the numbered lines in the prompt and states what ``rules``
    says about them, so the test does not depend on the retrieval order."""

    def __init__(self, rules: list[tuple[str, str, str, str, str, str]]) -> None:
        self.rules = rules  # (needle in the line, subject, relation, object, span, modality)
        self.prompts: list[str] = []

    @property
    def provider_id(self) -> str:
        return "stub:chain"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        text = messages[-1]["content"]
        self.prompts.append(text)
        out = ["assertions:"]
        for n, line in re.findall(r"^\[(\d+)\] (.*)$", text, flags=re.M):
            for needle, s, r, o, span, mode in self.rules:
                if needle in line:
                    out.append(
                        f"  - subject: {s}\n    relation: {r}\n    object: {o}\n"
                        f"    line: {n}\n    span: {span}\n    modality: {mode}"
                    )
        return "\n".join(out) if len(out) > 1 else "assertions: []"


RULES = [
    (
        "moved away",
        "Sven",
        "moved_from",
        "home country",
        "moved away from my home country",
        "asserted",
    ),
    ("is Sweden", "Sven", "home_country", "Sweden", "my home country is Sweden", "asserted"),
]


def _with_roles(monkeypatch: pytest.MonkeyPatch, stubs: dict[str, LLMService]) -> None:
    async def router(self: Engine, config: MemspineConfig) -> LLMRouter:
        return LLMRouter(stubs)

    monkeypatch.setattr(Engine, "_build_llm_router", router)


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )


async def _seed(eng: Engine, texts: list[str], ns: str = "a") -> list[str]:
    ids = []
    for i, text in enumerate(texts):
        rec = await eng.write(
            text, namespace=ns, memory_type="episodic", valid_from=T0 + timedelta(days=i)
        )
        ids.append(rec.record_id)
    return ids


async def _read(eng: Engine, query: str = QUESTION, ns: str = "a") -> Any:
    return await eng.read(query, namespace=ns, mode="retrieve", top_k=5, budget_tokens=2000)


@pytest.fixture(autouse=True)
def _structured_defaults() -> Iterator[None]:
    yield
    structured.configure(None)


async def test_off_makes_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _ChainStub(RULES)
    _with_roles(monkeypatch, {"extract": stub})
    eng = _engine()
    await eng.start()
    try:
        await _seed(eng, [MOVED, HOME])
        with search_forensics() as stages:
            await _read(eng)
        assert stub.prompts == [] and "fact_chain" not in stages
    finally:
        await eng.stop()


async def test_a_chain_joins_two_lines_with_their_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _ChainStub(RULES)
    _with_roles(monkeypatch, {"extract": stub})
    base, eng = _engine(), _engine(fact_chain="read_time", fact_chain_trigger="always")
    await base.start()
    await eng.start()
    try:
        await _seed(base, [MOVED, HOME])
        ids = await _seed(eng, [MOVED, HOME])
        before = len(await eng._require_started().list_records("a"))
        want = [r.content for r in (await _read(base)).context.records]
        with search_forensics() as stages:
            out = await _read(eng)
        info = stages["fact_chain"]
        assert info["fired"] and info["llm_calls"] == 1 and info["chains"] == 1
        assert info["assertions"] == 2 and info["valid"] == 2 and info["stop"] == "joined"
        block = out.context.records[-1]
        assert "fact_chain" in block.tags
        assert "Sven moved_from Sweden (its home_country)" in block.content
        assert '"moved away from my home country" [2023-05-07]' in block.content
        assert '"my home country is Sweden" [2023-05-08]' in block.content
        assert set(block.source.parents) == set(ids)  # provenance of every link
        # raw turns stay authoritative, in their order, and nothing derived is stored
        assert [r.content for r in out.context.records[:-1]] == want
        assert len(await eng._require_started().list_records("a")) == before
        assert await eng._require_started().get_record(block.record_id) is None
    finally:
        await base.stop()
        await eng.stop()


async def test_no_join_without_the_relation_in_the_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules = [
        RULES[0],
        ("Peru", "Anna", "home_country", "Peru", "my home country is Peru", "asserted"),
    ]
    stub = _ChainStub(rules)
    _with_roles(monkeypatch, {"extract": stub})
    eng = _engine(fact_chain="read_time", fact_chain_trigger="always")
    await eng.start()
    try:
        await _seed(eng, [MOVED, OTHER])
        with search_forensics() as stages:
            out = await _read(eng)
        assert stages["fact_chain"]["stop"] == "no_join" and stages["fact_chain"]["valid"] == 2
        assert not any("fact_chain" in r.tags for r in out.context.records)
    finally:
        await eng.stop()


async def test_an_assertion_with_an_invented_span_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules = [
        RULES[0],
        ("is Sweden", "Sven", "home_country", "Sweden", "he was born in Sweden", "asserted"),
    ]
    _with_roles(monkeypatch, {"extract": _ChainStub(rules)})
    eng = _engine(fact_chain="read_time", fact_chain_trigger="always")
    await eng.start()
    try:
        await _seed(eng, [MOVED, HOME])
        with search_forensics() as stages:
            await _read(eng)
        assert stages["fact_chain"]["assertions"] == 2 and stages["fact_chain"]["valid"] == 1
        assert stages["fact_chain"]["stop"] == "no_join"
    finally:
        await eng.stop()


async def test_a_negated_assertion_does_not_join(monkeypatch: pytest.MonkeyPatch) -> None:
    rules = [(*RULES[0][:5], "negated"), RULES[1]]
    _with_roles(monkeypatch, {"extract": _ChainStub(rules)})
    eng = _engine(fact_chain="read_time", fact_chain_trigger="always")
    await eng.start()
    try:
        await _seed(eng, [MOVED, HOME])
        with search_forensics() as stages:
            await _read(eng)
        assert stages["fact_chain"]["stop"] == "no_join"
    finally:
        await eng.stop()


async def test_the_trigger_is_multi_hop_or_unresolved_relation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _ChainStub(RULES)
    _with_roles(monkeypatch, {"extract": stub})
    eng = _engine(fact_chain="read_time", fact_chain_trigger="triggered")
    await eng.start()
    try:
        await _seed(eng, [MOVED, HOME, "Sven: I love baking on Sundays"])
        with search_forensics() as stages:  # 'emigrate' is stated beside Sven nowhere
            await _read(eng, "Which country did Sven emigrate from?")
        assert stages["fact_chain"]["fires_on"] == "relation_unresolved"
        calls = len(stub.prompts)
        with search_forensics() as stages:  # a resolved single-hop question
            await _read(eng, "What does Sven love baking on?")
        assert stages["fact_chain"]["stop"] == "not_triggered" and len(stub.prompts) == calls
    finally:
        await eng.stop()


async def test_no_cross_namespace_join(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _ChainStub(RULES)
    _with_roles(monkeypatch, {"extract": stub})
    eng = _engine(fact_chain="read_time", fact_chain_trigger="always")
    await eng.start()
    try:
        await _seed(eng, [MOVED], ns="a")
        await _seed(eng, [HOME], ns="b")  # the other half of the chain lives elsewhere
        with search_forensics() as stages:
            out = await _read(eng, ns="a")
        assert all("is Sweden" not in p for p in stub.prompts)
        assert stages["fact_chain"]["stop"] == "no_join"
        assert not any("Sweden" in r.content for r in out.context.records)
    finally:
        await eng.stop()


async def test_an_unbound_role_or_a_failing_call_keeps_the_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_roles(monkeypatch, {})
    base, eng = _engine(), _engine(fact_chain="read_time", fact_chain_trigger="always")
    await base.start()
    await eng.start()
    try:
        await _seed(base, [MOVED, HOME])
        await _seed(eng, [MOVED, HOME])
        want = [r.content for r in (await _read(base)).context.records]
        with search_forensics() as stages:
            assert [r.content for r in (await _read(eng)).context.records] == want
        assert stages["fact_chain"]["stop"] == "unbound"
    finally:
        await base.stop()
        await eng.stop()
    _with_roles(monkeypatch, {"extract": SimpleNamespace(provider_id="x", chat=_boom)})  # type: ignore[dict-item]
    eng2 = _engine(fact_chain="read_time", fact_chain_trigger="always")
    await eng2.start()
    try:
        await _seed(eng2, [MOVED, HOME])
        with search_forensics() as stages:
            await _read(eng2)
        assert stages["fact_chain"]["stop"] == "error"
    finally:
        await eng2.stop()


async def _boom(messages: list[dict[str, str]], **options: Any) -> str:
    raise RuntimeError("down")


# -- the write-time arm -----------------------------------------------------------------------


SECRET = "Caroline: my passport number is X4471920"


def _projecting_engine(value: Any = "on") -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            # sessions come from the consolidate stage, which needs an active trigger
            "episodic": {"enabled": True, "policies": {"consolidation": {"mine_facts": True}}},
            "semantic": {"enabled": True, "policies": {"fact_projection": value}},
        },
    )


async def _session(eng: Engine, lines: list[str], ns: str = "a", sid: str = "s1") -> list[str]:
    t0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
    msgs = [
        {"role": "user", "content": c, "timestamp": (t0 + timedelta(minutes=i)).isoformat()}
        for i, c in enumerate(lines)
    ]
    await eng.write_messages(msgs, namespace=ns, session_id=sid, group_id=sid)
    turns = await eng.retrieve(namespace=ns, memory_type="episodic")
    by_text = {t.content: t.record_id for t in turns}
    return [by_text[next(k for k in by_text if line.split(": ", 1)[1] in k)] for line in lines]


def _projector(*items: AssertionOut) -> Any:
    async def project(transcript: str) -> list[AssertionOut]:
        return list(items)

    return project


def _passport(
    line: int = 1, span: str = "my passport number is X4471920", mode: str = "asserted"
) -> AssertionOut:
    return AssertionOut(
        subject="Caroline",
        relation="passport_number",
        object="X4471920",
        line=line,
        span=span,
        modality=mode,
    )


async def _projected(eng: Engine, ns: str = "a") -> list[Any]:
    return [
        r
        for r in await eng._require_started().list_records(ns, "semantic")
        if "fact_projection" in r.tags and r.status.value == "activated"
    ]


async def test_projection_is_off_by_default_and_adds_no_stage() -> None:
    eng = _projecting_engine("off")
    await eng.start()
    try:
        await _session(eng, [SECRET, "Melanie: noted", "Caroline: thanks"])
        assert "project_facts" not in await eng.sleep()
    finally:
        await eng.stop()


async def test_projection_writes_sourced_atomic_facts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _projecting_engine()
    monkeypatch.setattr(eng, "_build_assertion_projector", lambda: _projector(_passport()))
    await eng.start()
    try:
        [turn_id, *_] = await _session(eng, [SECRET, "Melanie: noted", "Caroline: thanks"])
        stats = await eng.sleep()
        assert stats["project_facts"]["projected"] == 1 and stats["project_facts"]["errors"] == []
        [fact] = await _projected(eng)
        assert fact.source.parents == [turn_id]  # exactly the turn that holds the span
        assert "atomic_fact" in fact.tags and fact.namespace == "a"
        assert fact.content.startswith("Caroline passport_number: X4471920")
        again = await eng.sleep()
        assert again["project_facts"]["projected"] == 0 and len(await _projected(eng)) == 1
    finally:
        await eng.stop()


async def test_projection_drops_unsourced_and_non_asserted(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _projecting_engine()
    items = (_passport(span="Caroline lost her passport"), _passport(mode="negated"))
    monkeypatch.setattr(eng, "_build_assertion_projector", lambda: _projector(*items))
    await eng.start()
    try:
        await _session(eng, [SECRET, "Melanie: noted", "Caroline: thanks"])
        stats = await eng.sleep()
        assert stats["project_facts"]["projected"] == 0 and await _projected(eng) == []
    finally:
        await eng.stop()


async def test_forgetting_the_parent_removes_the_projected_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _projecting_engine()
    monkeypatch.setattr(eng, "_build_assertion_projector", lambda: _projector(_passport()))
    await eng.start()
    try:
        [turn_id, *_] = await _session(eng, [SECRET, "Melanie: noted", "Caroline: thanks"])
        await eng.sleep()
        assert len(await _projected(eng)) == 1
        await eng.forget(turn_id, namespace="a", hard=True)
        assert await _projected(eng) == []
        assert (await eng.verify_forget(turn_id, namespace="a"))["clean"] is True
        assert not any(
            "X4471920" in r.content for r in await eng._require_started().list_records("a")
        )
        assert (await eng.sleep())["project_facts"]["projected"] == 0  # never re-derived
    finally:
        await eng.stop()


async def test_a_forget_during_projection_leaves_no_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _projecting_engine()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocking(transcript: str) -> list[AssertionOut]:
        entered.set()
        await release.wait()
        return [_passport()]

    monkeypatch.setattr(eng, "_build_assertion_projector", lambda: blocking)
    await eng.start()
    try:
        [turn_id, *_] = await _session(eng, [SECRET, "Melanie: noted", "Caroline: thanks"])
        sleeper = asyncio.create_task(eng.sleep())
        await asyncio.wait_for(entered.wait(), 10)
        await eng.forget(turn_id, namespace="a", hard=True)  # I72: the parent goes meanwhile
        release.set()
        stats = await sleeper
        assert stats["project_facts"]["errors"] == [] and await _projected(eng) == []
        assert (await eng.verify_forget(turn_id, namespace="a"))["clean"] is True
    finally:
        await eng.stop()


async def test_projected_facts_never_cross_namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _projecting_engine()
    monkeypatch.setattr(eng, "_build_assertion_projector", lambda: _projector(_passport()))
    await eng.start()
    try:
        [a_turn, *_] = await _session(
            eng, [SECRET, "Melanie: noted", "Caroline: thanks"], ns="a", sid="s1"
        )
        [b_turn, *_] = await _session(
            eng, [SECRET, "Melanie: noted", "Caroline: thanks"], ns="b", sid="s2"
        )
        await eng.sleep()
        [fa], [fb] = await _projected(eng, "a"), await _projected(eng, "b")
        assert fa.source.parents == [a_turn] and fb.source.parents == [b_turn]
        await eng.forget(a_turn, namespace="a", hard=True)
        assert await _projected(eng, "a") == [] and len(await _projected(eng, "b")) == 1
    finally:
        await eng.stop()


async def test_an_unknown_projection_value_is_a_config_error() -> None:
    eng = _projecting_engine("maybe")
    with pytest.raises(ConfigError):
        await eng.start()
    await eng.stop()
