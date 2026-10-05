"""GP-7 (#18): entity resolution in ``extract_graph`` (``extract_graph.resolve``).

The resolver ladder (exact -> alias -> candidates -> entropy gate -> MinHash ->
one batched LLM call), the trust guard (contested, never merged), and the
decisions as MARKER events (the alias table is rebuilt from the log).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.exceptions import ConfigError
from memspine.memories.associative.resolution import (
    EntityResolver,
    KnownEntity,
    high_entropy,
    name_jaccard,
)
from memspine.prompts.models import ExtractedEdge
from memspine.workers.pipelines import (
    ENTITY_RESOLVED_MARKER,
    SessionIndex,
    extract_graph,
    resolve_mode,
)

T0 = datetime(2023, 5, 1, tzinfo=UTC)


class CountingResolver:
    """A fake ``resolve_entity@batch`` LLM: answers from a table, counts calls."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[list[tuple[str, list[str]]]] = []

    async def __call__(self, items: list[tuple[str, list[str]]]) -> dict[int, str]:
        self.calls.append(items)
        return {i: self.answers.get(name, "") for i, (name, _cands) in enumerate(items, 1)}


def _known(*names: str, trust: float = 1.0) -> dict[str, KnownEntity]:
    return {n.lower(): KnownEntity(n.lower(), n, trust) for n in names}


# ── the resolver ladder ───────────────────────────────────────────────────────


async def test_exact_and_alias_need_no_llm() -> None:
    llm = CountingResolver({})
    resolver = EntityResolver(_known("Melanie"), {"mel": "melanie"}, llm=llm)
    out = await resolver.resolve([(" MELANIE ", 1.0), ("Mel", 1.0)])
    assert [(r.method, r.target) for r in out] == [("exact", None), ("alias", "Melanie")]
    assert llm.calls == []


async def test_high_entropy_spelling_variant_merges_by_minhash() -> None:
    assert high_entropy("nothing is impossible")
    assert name_jaccard("nothing is impossible", "nothing is impossible!") >= 0.9
    resolver = EntityResolver(_known("Nothing Is Impossible"), {})
    [out] = await resolver.resolve([("Nothing is impossible!", 1.0)])
    assert (out.method, out.target) == ("minhash", "Nothing Is Impossible")


async def test_low_entropy_names_skip_minhash_and_go_to_one_batched_call() -> None:
    assert not high_entropy("mel") and not high_entropy("jo")
    llm = CountingResolver({"Mel": "Melanie", "Jo": "Joanna"})
    resolver = EntityResolver(_known("Melanie", "Joanna", "Caroline"), {}, llm=llm)
    out = await resolver.resolve([("Mel", 1.0), ("Jo", 1.0), ("Mel", 1.0), ("Bo", 1.0)])
    assert [r.target for r in out] == ["Melanie", "Joanna", "Melanie", None]
    assert [r.method for r in out] == ["llm", "llm", "llm", "new"]
    assert len(llm.calls) == 1 and resolver.llm_calls == 1
    assert [name for name, _ in llm.calls[0]] == ["Mel", "Jo", "Bo"]  # one row per name
    assert all(len(cands) <= 15 for _, cands in llm.calls[0])


async def test_an_answer_outside_the_candidates_is_a_new_entity() -> None:
    llm = CountingResolver({"Mel": "Mallory"})
    [out] = await EntityResolver(_known("Melanie"), {}, llm=llm).resolve([("Mel", 1.0)])
    assert (out.method, out.target) == ("new", None)


async def test_without_an_llm_ambiguous_names_stay_new() -> None:
    [out] = await EntityResolver(_known("Melanie"), {}).resolve([("Mel", 1.0)])
    assert (out.method, out.target) == ("new", None)


async def test_different_trust_stays_contested() -> None:
    llm = CountingResolver({"Mel": "Melanie"})
    resolver = EntityResolver(_known("Melanie", trust=1.0), {"mely": "melanie"}, llm=llm)
    out = await resolver.resolve([("Mel", 0.3), ("Mely", 0.3), ("Mel", 0.95)])
    assert [(r.method, r.target, r.candidate) for r in out] == [
        ("contested", None, "Melanie"),
        ("contested", None, "Melanie"),
        ("llm", "Melanie", None),
    ]


async def test_candidates_are_the_embedding_top_15() -> None:
    names = [f"Person{i:02d}" for i in range(40)]
    seen: list[list[str]] = []

    async def embed(texts: list[str]) -> list[list[float]]:
        return [[1.0, float(len(t))] for t in texts]

    async def llm(items: list[tuple[str, list[str]]]) -> dict[int, str]:
        seen.extend(cands for _, cands in items)
        return {}

    resolver = EntityResolver(_known(*names), {}, embed=embed, llm=llm)
    await resolver.resolve([("Pe", 1.0)])
    assert len(seen) == 1 and len(seen[0]) == 15


def test_resolve_mode_is_validated() -> None:
    assert resolve_mode({}) == "off"
    assert resolve_mode({"resolve": "llm"}) == "llm"
    with pytest.raises(ConfigError):
        resolve_mode({"resolve": "maybe"})


# ── through extract_graph ─────────────────────────────────────────────────────


def _engine(resolve: str = "llm") -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {
                "enabled": True,
                "policies": {"extract_graph": {"resolve": resolve}},
            },
            "episodic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
    )


@pytest.fixture
async def engine() -> AsyncIterator[Engine]:
    eng = _engine()
    await eng.start()
    yield eng
    await eng.stop()


def _edges(table: dict[str, list[ExtractedEdge]]) -> Any:
    async def fake(content: str, _context: object = None) -> list[ExtractedEdge]:
        return list(table.get(content, []))

    return fake


def _edge(src: str, rel: str, dst: str, fact: str) -> ExtractedEdge:
    return ExtractedEdge(
        src_entity=src, rel=rel, dst_entity=dst, kind="event", fact=fact, confidence=0.9
    )


async def _facts(eng: Engine) -> dict[str, MemoryRecord]:
    records = await eng._require_started().list_records("a", "semantic")
    return {r.content: r for r in records if r.source.channel == "extract_graph"}


async def _sweep(eng: Engine, llm: CountingResolver | None) -> dict[str, object]:
    ctx = eng._pipeline_ctx()
    ctx.resolve_entities = llm
    return await extract_graph(ctx)


async def _melanie(eng: Engine) -> None:
    await eng.write(
        'Melanie read "Charlotte\'s Web"',
        namespace="a",
        entity="Melanie",
        tags=["kind:event", "rel:read", "dst:Charlotte's Web"],
        valid_from=T0,
    )


async def test_mel_is_melanie_with_one_call_then_none(engine: Engine) -> None:
    await _melanie(engine)
    engine._extract_edges = _edges(
        {
            "Mel went hiking": [_edge("Mel", "went", "hiking trip", "Mel went hiking")],
            "Mel baked a cake": [_edge("Mel", "baked", "chocolate cake", "Mel baked a cake")],
        }
    )
    await engine.write(
        "Mel went hiking", namespace="a", memory_type="episodic", valid_from=T0 + timedelta(1)
    )
    llm = CountingResolver({"Mel": "Melanie"})
    stats = await _sweep(engine, llm)
    assert len(llm.calls) == 1 and stats["resolve_llm_calls"] == 1 and stats["resolved"] == 1
    fact = (await _facts(engine))["Mel went hiking"]
    assert fact.entity == "Melanie"
    assert engine._graph is not None
    mentions = {
        e.dst for e in await engine._graph.edges_of(fact.record_id) if e.rel_type == "mentions"
    }
    assert "ent:a:melanie" in mentions and "ent:a:mel" not in mentions
    # The decision is an event: a later sweep reads the alias table, no LLM call.
    await engine.write(
        "Mel baked a cake", namespace="a", memory_type="episodic", valid_from=T0 + timedelta(2)
    )
    llm.calls.clear()
    await _sweep(engine, llm)
    # "Mel" is never asked again (only the new destination name is).
    assert all(name != "Mel" for call in llm.calls for name, _ in call)
    assert (await _facts(engine))["Mel baked a cake"].entity == "Melanie"


async def test_decisions_rebuild_from_the_log(engine: Engine) -> None:
    await _melanie(engine)
    engine._extract_edges = _edges(
        {"Mel went hiking": [_edge("Mel", "went", "hiking trip", "Mel went hiking")]}
    )
    await engine.write("Mel went hiking", namespace="a", memory_type="episodic")
    await _sweep(engine, CountingResolver({"Mel": "Melanie"}))
    storage = engine._require_started()
    markers = [
        e for e in await storage.read_events() if e.payload.get("marker") == ENTITY_RESOLVED_MARKER
    ]
    assert len(markers) == 1
    [decision] = markers[0].payload["decisions"]
    assert decision["entity"] == "Mel" and decision["attribute"] == "Melanie"
    assert decision["method"] == "llm" and decision["namespace"] == "a"
    await engine.rebuild()
    index = SessionIndex()
    await index.refresh(storage)
    assert index.entity_aliases["a"] == {"mel": "melanie"}
    assert (await _facts(engine))["Mel went hiking"].entity == "Melanie"


async def test_a_low_trust_source_stays_contested(engine: Engine) -> None:
    await _melanie(engine)
    engine._extract_edges = _edges(
        {"Mel owes money": [_edge("Mel", "owes", "money lender", "Mel owes money")]}
    )
    turn = await engine.write(
        "Mel owes money",
        namespace="a",
        memory_type="episodic",
        source=SourceInfo(role="tool", channel="web"),
        actor="tool",
    )
    assert not turn.quarantined and turn.trust < 0.8
    llm = CountingResolver({"Mel": "Melanie"})
    stats = await _sweep(engine, llm)
    assert stats["contested"] == 1 and stats["resolved"] == 0
    assert (await _facts(engine))["Mel owes money"].entity == "Mel"
    storage = engine._require_started()
    [marker] = [
        e for e in await storage.read_events() if e.payload.get("marker") == ENTITY_RESOLVED_MARKER
    ]
    assert marker.payload["decisions"][0]["method"] == "contested"
    index = SessionIndex()
    await index.refresh(storage)
    assert index.entity_aliases.get("a", {}) == {}  # contested is never an alias


async def test_rules_mode_makes_no_llm_call() -> None:
    eng = _engine("rules")
    await eng.start()
    try:
        await _melanie(eng)
        eng._extract_edges = _edges(
            {"Mel went hiking": [_edge("Mel", "went", "hiking trip", "Mel went hiking")]}
        )
        await eng.write("Mel went hiking", namespace="a", memory_type="episodic")
        llm = CountingResolver({"Mel": "Melanie"})
        stats = await _sweep(eng, llm)
        assert llm.calls == [] and stats["resolve_llm_calls"] == 0
        assert (await _facts(eng))["Mel went hiking"].entity == "Mel"
    finally:
        await eng.stop()


async def test_off_by_default_changes_nothing() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
    )
    await eng.start()
    try:
        await _melanie(eng)
        eng._extract_edges = _edges(
            {"Mel went hiking": [_edge("Mel", "went", "hiking trip", "Mel went hiking")]}
        )
        await eng.write("Mel went hiking", namespace="a", memory_type="episodic")
        llm = CountingResolver({"Mel": "Melanie"})
        stats = await _sweep(eng, llm)
        assert llm.calls == [] and "resolved" not in stats
        assert (await _facts(eng))["Mel went hiking"].entity == "Mel"
    finally:
        await eng.stop()


async def test_engine_rejects_an_unknown_resolve_mode() -> None:
    eng = _engine("sometimes")
    with pytest.raises(ConfigError):
        await eng.start()
    await eng.stop()


# ── the engine's bound callables (prompt render + parse, fake LLM) ────────────


class _ScriptedLLM:
    provider_id = "fake"
    model = "fake/model"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.seen: list[list[dict[str, str]]] = []

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.seen.append(messages)
        return self.reply


async def test_engine_binds_the_batched_prompts() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {"enabled": True, "policies": {"extract_graph": {"resolve": "llm"}}},
            "episodic": {"enabled": True},
            "associative": {
                "enabled": True,
                "policies": {"entity_nodes": True, "entity_summaries": True},
            },
        },
        llm={
            "roles": {
                "extract_edges": {"model": "ollama/llama3"},
                "resolve_entity": {"model": "ollama/llama3"},
                "summarize_entity": {"model": "ollama/llama3"},
            }
        },
    )
    await eng.start()
    try:
        assert eng._llm is not None
        resolve_llm = _ScriptedLLM("matches:\n  - index: 1\n    match: Melanie\n")
        summary_llm = _ScriptedLLM("summaries:\n  - index: 1\n    summary: Melanie read Dune.\n")
        fakes = {"resolve_entity": resolve_llm, "summarize_entity": summary_llm}
        eng._llm.for_role = lambda role: fakes[role]  # type: ignore[method-assign]
        ctx = eng._pipeline_ctx()
        assert ctx.resolve_entities is not None and ctx.summarize_entities is not None
        assert await ctx.resolve_entities([("Mel", ["Melanie", "Caroline"])]) == {1: "Melanie"}
        sent = resolve_llm.seen[0][-1]["content"]
        assert "[1] Mel (candidates: Melanie; Caroline)" in sent
        out = await ctx.summarize_entities([("Melanie", ["[2023-05-01] Melanie read Dune"])])
        assert out == {1: "Melanie read Dune."}
        assert "[1] Melanie\n- [2023-05-01] Melanie read Dune" in summary_llm.seen[0][-1]["content"]
    finally:
        await eng.stop()


# ── erasure of session-level decisions (fix/graph-review #2) ──────────────────


@pytest.mark.parametrize("forget", ["lowest", "highest"])
async def test_forgetting_any_cited_turn_erases_a_session_decision(forget: str) -> None:
    """A session-level edge cites several turns; its ``entity_resolved`` decision
    is keyed by every cited turn, so hard-forgetting ANY of them (not just the
    least trusted "owner") erases the decision: the alias is gone after a
    rebuild and ``verify_forget`` is clean."""
    from memspine.core.events import EventKind, MemoryEvent

    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {
                "enabled": True,
                "policies": {"extract_graph": {"resolve": "llm", "granularity": "session"}},
            },
            "episodic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
    )
    await eng.start()
    try:
        await _melanie(eng)
        turns = [
            await eng.write(text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(i + 1))
            for i, text in enumerate(["we talked about her weekend", "Mel went hiking"])
        ]
        ctx = eng._pipeline_ctx()
        assert ctx.append_event is not None
        await ctx.append_event(
            MemoryEvent(
                kind=EventKind.CONSOLIDATE,
                namespace="a",
                actor="system",
                payload={"session_key": "s1", "member_record_ids": [t.record_id for t in turns]},
            )
        )

        async def session_extract(_content: str, _context: object = None) -> list[ExtractedEdge]:
            edge = _edge("Mel", "went", "hiking trip", "Mel went hiking")
            return [edge.model_copy(update={"episode_indices": [1, 2]})]

        ctx.extract_session_edges = session_extract
        ctx.extract_edges = _edges({})
        ctx.resolve_entities = CountingResolver({"Mel": "Melanie"})
        stats = await extract_graph(ctx)
        assert stats["resolved"] == 1, stats
        storage = eng._require_started()
        index = SessionIndex()
        await index.refresh(storage)
        assert index.entity_aliases["a"] == {"mel": "melanie"}

        ids = sorted(t.record_id for t in turns)
        victim = ids[0] if forget == "lowest" else ids[-1]
        await eng.forget(victim, namespace="a", hard=True)
        await eng.rebuild()
        index = SessionIndex()
        await index.refresh(storage)
        assert index.entity_aliases.get("a", {}) == {}
        report = await eng.verify_forget(victim, namespace="a")
        assert report["log_retained_fields"] == [] and report["clean"] is True
    finally:
        await eng.stop()
