"""#20: session-level extract_graph (``extract_graph.granularity: session``).

A fake session extractor stands in for ``extract_edges@session``: one call per
consolidated session with numbered turns, edges citing ``episode_indices``; the
facts' parents (and ``asserted`` links) are the cited turns.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_extract_graph import Harness, _make, _seed

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord
from memspine.memories.semantic.write_pipeline import EdgeContext
from memspine.prompts.models import ExtractedEdge
from memspine.workers.pipelines import PipelineContext, extract_graph

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)
TURNS = [
    "hi, how was your week",
    "Melanie finished reading Charlotte's Web",
    "Caroline said she moved to Denver",
    "Caroline loves the mountains in Denver",
]


def _edges() -> list[ExtractedEdge]:
    return [
        ExtractedEdge(
            src_entity="Melanie",
            rel="read",
            dst_entity="Charlotte's Web",
            fact="Melanie read Charlotte's Web",
            episode_indices=[2],
        ),
        ExtractedEdge(
            src_entity="Caroline",
            rel="lives_in",
            kind="state",
            dst_entity="Denver",
            fact="Caroline lives in Denver",
            episode_indices=[3, 4, 9],  # 9 is out of range: ignored
        ),
        ExtractedEdge(
            src_entity="Caroline",
            rel="likes",
            dst_entity="mountains",
            fact="Caroline likes the mountains",
        ),  # cites nothing: the whole session is its source
    ]


async def _session(
    granularity: str = "session",
    entities: list[str] | None = None,
) -> tuple[PipelineContext, Harness, list[tuple[str, EdgeContext | None]], list[MemoryRecord]]:
    ctx, harness, _graph = await _make(
        [], semantic_policies={"extract_graph": {"granularity": granularity}}
    )
    calls: list[tuple[str, EdgeContext | None]] = []
    record_calls: list[str] = []

    async def session_extract(
        content: str, context: EdgeContext | None = None
    ) -> list[ExtractedEdge]:
        calls.append((content, context))
        return _edges()

    async def record_extract(content: str, _context: object = None) -> list[ExtractedEdge]:
        record_calls.append(content)
        return []

    ctx.extract_session_edges = session_extract
    ctx.extract_edges = record_extract
    if entities is not None:

        async def find(_text: str) -> list[str]:
            return list(entities)

        ctx.find_entities = find
    turns = []
    for i, text in enumerate(TURNS):
        record = MemoryRecord(
            namespace="agent/a",
            memory_type="episodic",
            content=text,
            valid_from=T0 + timedelta(minutes=i),
        )
        await harness.append(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="agent/a",
                actor="user",
                payload={"record": record.model_dump(mode="json")},
            )
        )
        turns.append(record)
    await harness.append(
        MemoryEvent(
            kind=EventKind.CONSOLIDATE,
            namespace="agent/a",
            actor="system",
            payload={"session_key": "s1", "member_record_ids": [t.record_id for t in turns]},
        )
    )
    ctx.record_calls = record_calls  # type: ignore[attr-defined]
    return ctx, harness, calls, turns


async def _facts(harness: Harness) -> dict[str, MemoryRecord]:
    return {
        r.content: r
        for r in await harness.storage.list_records("agent/a", "semantic")
        if r.source.channel == "extract_graph"
    }


async def test_one_call_per_session_with_turn_attribution() -> None:
    ctx, harness, calls, turns = await _session()
    stats = await extract_graph(ctx)
    assert stats["status"] == "ok"
    assert len(calls) == 1 and ctx.record_calls == []  # type: ignore[attr-defined]
    transcript = calls[0][0]
    assert transcript.splitlines()[1] == (
        "[2] [2023-05-01] Melanie finished reading Charlotte's Web"
    )
    facts = await _facts(harness)
    ids = [t.record_id for t in turns]
    assert facts["Melanie read Charlotte's Web"].source.parents == [ids[1]]
    assert facts["Caroline lives in Denver"].source.parents == [ids[2], ids[3]]
    assert facts["Caroline likes the mountains"].source.parents == ids
    # The fact's event time falls back to its latest cited turn.
    assert facts["Caroline lives in Denver"].valid_from == turns[3].valid_from
    assert stats["edges_written"] == 3
    assert stats["links"] == 1 + 2 + 4  # one asserted link per cited turn


async def test_second_sweep_makes_no_call() -> None:
    ctx, harness, calls, _turns = await _session()
    await extract_graph(ctx)
    again = await extract_graph(ctx)
    assert len(calls) == 1
    assert again["edges_written"] == 0
    markers = [
        e.payload
        for e in await harness.storage.read_events(after_seq=0, limit=1000)
        if e.kind is EventKind.MARKER and e.payload.get("stage") == "extract_graph"
    ]
    assert [m["session_key"] for m in markers if m.get("marker") == "stage_done"] == ["s1"]


async def test_decision_entities_form_the_allowed_list() -> None:
    ctx, _harness, calls, _turns = await _session(entities=["Melanie", "Caroline", "Denver"])
    await extract_graph(ctx)
    context = calls[0][1]
    assert context is not None
    assert list(context.allowed_entities) == ["Melanie", "Caroline", "Denver"]


async def test_record_granularity_is_the_default_path() -> None:
    ctx, _harness, calls, _turns = await _session(granularity="record")
    await extract_graph(ctx)
    assert calls == []
    assert len(ctx.record_calls) == len(TURNS)  # type: ignore[attr-defined]


async def test_records_outside_a_session_stay_per_record() -> None:
    ctx, harness, calls, _turns = await _session()
    await _seed(harness, "a loose note outside any session")
    await extract_graph(ctx)
    assert len(calls) == 1
    assert ctx.record_calls == ["a loose note outside any session"]  # type: ignore[attr-defined]


class _StubChat:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[object] = []

    async def chat(self, messages: object, **_: object) -> str:
        self.prompts.append(messages)
        return self.reply


class _StubLLM:
    def __init__(self, chat: _StubChat) -> None:
        self.roles = ("extract_edges",)
        self._chat = chat

    def for_role(self, _role: str) -> _StubChat:
        return self._chat


def _graph_engine(granularity: str) -> object:
    from memspine import Engine

    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {
                "enabled": True,
                "policies": {"extract_graph": {"granularity": granularity, "max_rounds": 2}},
            },
            "episodic": {"enabled": True},
        },
    )


async def test_engine_session_extractor_uses_the_session_prompt() -> None:
    eng = _graph_engine("session")
    await eng.start()  # type: ignore[attr-defined]
    try:
        chat = _StubChat(
            "edges:\n"
            "  - src_entity: Caroline\n    rel: lives_in\n    dst_entity: Denver\n"
            "    fact: Caroline lives in Denver\n    kind: state\n    episode_indices: [3]\n"
        )
        eng._llm = _StubLLM(chat)  # type: ignore[attr-defined]
        extract = eng._build_session_edge_extractor(eng._config())  # type: ignore[attr-defined]
        assert extract is not None
        edges = await extract(
            "[1] [2023-05-01] hi\n[3] [2023-05-01] Caroline moved to Denver",
            EdgeContext(allowed_entities=["Caroline", "Denver"]),
        )
        assert [(e.fact, e.episode_indices) for e in edges] == [("Caroline lives in Denver", [3])]
        assert len(chat.prompts) == 2  # max_rounds reflexion rounds, merged
        rendered = str(chat.prompts[0])
        assert "allowed entities" in rendered and "episode_indices" in rendered
    finally:
        await eng.stop()  # type: ignore[attr-defined]


async def test_engine_rejects_an_unknown_granularity() -> None:
    import pytest

    from memspine.exceptions import ConfigError

    eng = _graph_engine("turn")
    try:
        with pytest.raises(ConfigError, match="granularity"):
            await eng.start()  # type: ignore[attr-defined]
    finally:
        await eng.stop()  # type: ignore[attr-defined]


async def test_session_granularity_with_rules_resolution_uses_resolved_names() -> None:
    """#20 x #18: session-extracted edges pass GP-7 resolution before they are
    written; the fact carries the known entity's spelling and its cited turns."""
    ctx, harness, _graph = await _make(
        [], semantic_policies={"extract_graph": {"granularity": "session", "resolve": "rules"}}
    )
    known = MemoryRecord(
        namespace="agent/a",
        memory_type="semantic",
        content="Nothing Is Impossible is a novel",
        entity="Nothing Is Impossible",
        attribute="genre",
    )
    await harness.append(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace="agent/a",
            actor="user",
            payload={"record": known.model_dump(mode="json")},
        )
    )
    turns = []
    for i, text in enumerate(["morning!", "Melanie is reading Nothing is impossible! now"]):
        turn = MemoryRecord(
            namespace="agent/a",
            memory_type="episodic",
            content=text,
            valid_from=T0 + timedelta(minutes=i),
        )
        await harness.append(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="agent/a",
                actor="user",
                payload={"record": turn.model_dump(mode="json")},
            )
        )
        turns.append(turn)
    await harness.append(
        MemoryEvent(
            kind=EventKind.CONSOLIDATE,
            namespace="agent/a",
            actor="system",
            payload={"session_key": "s1", "member_record_ids": [t.record_id for t in turns]},
        )
    )
    calls: list[str] = []

    async def session_extract(
        content: str, _context: EdgeContext | None = None
    ) -> list[ExtractedEdge]:
        calls.append(content)
        return [
            ExtractedEdge(
                src_entity="Melanie",
                rel="read",
                dst_entity="Nothing is impossible!",
                fact="Melanie read Nothing Is Impossible",
                episode_indices=[2],
            )
        ]

    ctx.extract_session_edges = session_extract
    stats = await extract_graph(ctx)
    assert len(calls) == 1
    assert stats["resolved"] >= 1
    [fact] = (await _facts(harness)).values()
    assert "dst:Nothing Is Impossible" in fact.tags  # the resolved spelling
    assert fact.source.parents == [turns[1].record_id]  # the cited turn
