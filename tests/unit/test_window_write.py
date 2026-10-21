"""W1: the adaptive-window write (``write.window_extract``), call 1, with fake LLMs.

Boundaries, per-store self-calibration, per-section fail-closed, split mode, the call cache,
parents and forget cancellation, namespace isolation, async == sync, the counters, the
session-closed hook and the call-3 wiring (``extract_graph`` consumes the window edges)."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.core.window import (
    WindowSegmenter,
    WindowSettings,
    WindowTurn,
    segment,
)
from memspine.exceptions import ConfigError
from memspine.models.providers.llm.base import LLMRouter
from memspine.prompts.registry import PromptRegistry
from memspine.workers.windows import (
    build_transcript,
    grounded,
    validate_edges,
    validate_episode,
    validate_facts,
)

T0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
LINE = re.compile(r"^\[(\d+)\] \[[\d-]+\] (.+)$", re.M)


class FakeWindowLLM:
    """A deterministic stand-in for the ``window_extract`` role: one fact, one edge and an
    episode per transcript line, each citing its line, with a real verbatim span."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []  # the sections of each call
        self.transcripts: list[str] = []
        self.break_sections: set[str] = set()  # sections returned malformed
        self.garbage_when_joint = False  # unparseable reply when 2+ sections are asked
        self.fake_span = False  # facts with a span that is in no turn
        self.block: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.edges_override: list[dict[str, Any]] | None = None

    @property
    def provider_id(self) -> str:
        return "fake:window"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        user = messages[-1]["content"]
        sections = re.search(r"sections: (.+)\.$", user.strip(), re.M).group(1).split(", ")  # type: ignore[union-attr]
        self.calls.append(sections)
        self.transcripts.append(user)
        self.entered.set()
        if self.block is not None:
            await self.block.wait()
        if self.garbage_when_joint and len(sections) == 3:
            return "[1, 2, 3]"  # valid JSON, not an object: fails validation
        lines = [(int(n), text) for n, text in LINE.findall(user)]
        out: dict[str, Any] = {}
        if "facts" in sections:
            out["facts"] = [
                {
                    "entity": text.split(":")[0],
                    "attribute": "event",
                    "value": f"{text.split(':')[0]} said {text.split(': ', 1)[1]}",
                    "kind": "event",
                    "turns": [n],
                    "span": "no such words" if self.fake_span else text.split(": ", 1)[1],
                    "persons": [text.split(":")[0]],
                    "topic": "activities",
                }
                for n, text in lines
            ]
        if "edges" in sections and self.edges_override is not None:
            out["edges"] = self.edges_override
        elif "edges" in sections:
            out["edges"] = [
                {
                    "src_entity": text.split(":")[0],
                    "rel": "mentioned",
                    "dst_entity": f"item{n}",
                    "fact": f"{text.split(':')[0]} mentioned item{n}",
                    "turns": [n],
                }
                for n, text in lines
            ]
        if "episode" in sections:
            out["episode"] = {
                "title": "A chat",
                "summary": "People chat.",
                "narrative": " ".join(text for _, text in lines),
                "turns": [n for n, _ in lines],
            }
        for name in self.break_sections & set(sections):
            out[name] = "oops" if name != "episode" else 17
        return json.dumps(out)


_SENTENCES = (
    "I repotted the orchid on the balcony",
    "We booked a kayak tour along the northern coast",
    "The violin recital starts at seven on Friday",
    "My brother moved to a flat near the harbour",
    "Last spring I planted apple trees behind the barn",
    "Our dog Pip learned to open the garden gate",
    "I am saving for a second-hand motorbike",
    "She wants to repaint the kitchen in pale green",
    "The bakery downtown sells rye bread with caraway",
    "We watched a documentary about deep sea fish",
    "He fixed the bicycle chain with a borrowed wrench",
    "Next month the choir performs in the old cathedral",
    "I finished knitting a scarf in grey wool",
    "The train to the mountains leaves before dawn",
    "Our neighbour lends us a ladder every autumn",
    "I tried climbing the granite wall at the gym",
    "The museum opened a new wing about printing presses",
    "We cooked lentil soup for the whole street",
    "She trains for a half marathon along the river",
    "I keep a notebook of bird sightings from the roof",
    "The festival of lanterns fills the square each winter",
    "He teaches chess to children on Saturday mornings",
    "I bought a telescope to look at Saturn",
    "They renovated the cellar into a small workshop",
)


def _turns(prefix: str, count: int, speaker: str = "Mira") -> list[dict[str, str]]:
    """Distinct sentences (so semantic dedup keeps every fact); the same ``prefix`` gives the
    same texts and dates, which the cache test relies on."""
    start = sum(map(ord, prefix)) % len(_SENTENCES)
    return [
        {
            "role": "user",
            "content": f"{speaker}: {_SENTENCES[(start + i) % len(_SENTENCES)]}",
            "timestamp": (T0 + timedelta(minutes=i)).isoformat(),
        }
        for i in range(count)
    ]


async def _engine(
    monkeypatch: pytest.MonkeyPatch,
    llm: FakeWindowLLM,
    *,
    extra_roles: dict[str, Any] | None = None,
    mode: str = "facts",
    extract_graph: dict[str, Any] | None = None,
    path: Any = ":memory:",
    firewall: dict[str, Any] | None = None,
    **write: Any,
) -> Engine:
    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter({"window_extract": llm, **(extra_roles or {})})  # type: ignore[dict-item]

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    semantic: dict[str, Any] = {"enabled": True}
    if extract_graph is not None:
        semantic["policies"] = {"extract_graph": extract_graph}
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": str(path), "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}, "semantic": semantic},
        write={"window_extract": mode, "window_async": False, "window_idle_s": 0.0, **write},
        llm={
            "roles": {
                "window_extract": {
                    "provider": "litellm",
                    "settings": {"model": "fake/window", "timeout_seconds": 60.0},
                },
                **{
                    r: {
                        "provider": "litellm",
                        "settings": {"model": "fake/x", "timeout_seconds": 60.0},
                    }
                    for r in (extra_roles or {})
                },
            }
        },
        **({"firewall": firewall} if firewall is not None else {}),
    )
    await eng.start()
    return eng


async def _facts(eng: Engine, ns: str = "a") -> list[Any]:
    storage = eng._require_started()
    return [
        r
        for r in await storage.list_records(ns, "semantic")
        if r.source.channel == "mining" and "window_fact" in r.tags
    ]


async def _send(eng: Engine, ns: str, session: str, messages: list[dict[str, str]]) -> None:
    await eng.ingest_messages(
        messages,
        namespace=ns,
        session_id=session,
        conversation_id=session,
        group_id=session,
    )


async def test_records_without_conversation_metadata_keep_session_windowing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, window_max_turns=6)
    pipeline = eng._windows
    assert pipeline is not None
    try:
        eng._windows = None
        records = await eng.ingest_messages(
            _turns("trip", 2), namespace="a", session_id="S1", conversation_id="C1"
        )
        eng._windows = pipeline
        for record in records:
            raw = record.model_copy(
                update={"source": record.source.model_copy(update={"message": None})}
            )
            await pipeline.observe(raw, "S1")

        stats = await pipeline.flush()
        assert stats["windows"] == 1
        assert stats["windows_processed"] == 1
    finally:
        eng._windows = pipeline
        await eng.stop()


# --- segmenter -------------------------------------------------------------------------------


def _wt(i: int, vec: list[float] | None, session: str = "s", text: str = "word " * 4) -> WindowTurn:
    return WindowTurn(record_id=f"r{i}", text=text, session=session, vector=vec)


def test_turn_cap_closes_at_max_turns() -> None:
    windows = segment((_wt(i, None) for i in range(14)), WindowSettings(max_turns=6))
    assert [len(w.turns) for w in windows] == [6, 6, 2]
    assert [w.reason for w in windows] == ["turns", "turns", "flush"]


def test_token_cap_closes_before_the_turn_that_would_pass_it() -> None:
    big = "x" * 400  # 100 tokens
    windows = segment(
        (_wt(i, None, text=big) for i in range(5)), WindowSettings(max_turns=50, max_tokens=250)
    )
    assert [len(w.turns) for w in windows] == [2, 2, 1]
    assert windows[0].reason == "tokens" and windows[0].tokens <= 250


def test_session_change_closes_the_window_and_is_never_a_similarity_pair() -> None:
    seg = WindowSegmenter(WindowSettings(max_turns=10))
    out = []
    for i in range(3):
        out += seg.add(_wt(i, [1.0, 0.0], session="s1"))
    out += seg.add(_wt(3, [0.0, 1.0], session="s2"))
    assert [w.reason for w in out] == ["session_end"] and out[0].session == "s1"
    assert seg.pairs == 2  # the s1 -> s2 pair (orthogonal) was not counted


def _vec(angle: float) -> list[float]:
    import math

    return [math.cos(angle), math.sin(angle)]


def _steady(seg: WindowSegmenter, count: int, step: float) -> float:
    """Feed turns alternating between two directions ``step`` apart (every consecutive
    similarity is the same, cos(step)); returns the angle of the last turn."""
    angle = 0.0
    for i in range(count):
        angle = 0.0 if i % 2 == 0 else step
        assert seg.add(_wt(i, _vec(angle))) == []
    return angle


def test_topic_shift_uses_the_stores_own_running_level() -> None:
    seg = WindowSegmenter(WindowSettings(max_turns=1000, max_tokens=10**6, topic_k=2.0))
    last = _steady(seg, 40, 0.05)
    assert seg.topic_level is not None
    closed = seg.add(_wt(40, _vec(last + 1.4)))  # a different topic
    assert [w.reason for w in closed] == ["topic"] and len(closed[0].turns) == 40


def test_no_topic_cut_before_enough_pairs() -> None:
    seg = WindowSegmenter(WindowSettings(max_turns=1000, topic_k=2.0))
    out = []
    for i in range(10):
        out += seg.add(_wt(i, _vec(0.0 if i % 2 == 0 else 1.5)))  # wildly different turns
    assert out == [] and seg.topic_level is None


def test_topic_level_differs_by_store() -> None:
    """The same drop (0.7 rad) is a shift in a tight store and not in a loose one."""
    tight = WindowSegmenter(WindowSettings(max_turns=1000, max_tokens=10**6, topic_k=2.0))
    loose = WindowSegmenter(WindowSettings(max_turns=1000, max_tokens=10**6, topic_k=2.0))
    last_tight, last_loose = _steady(tight, 60, 0.05), _steady(loose, 60, 0.8)
    assert tight.topic_level is not None and loose.topic_level is not None
    assert tight.topic_level > loose.topic_level  # a tight store cuts on a smaller drop
    assert [w.reason for w in tight.add(_wt(99, _vec(last_tight + 0.7)))] == ["topic"]
    assert loose.add(_wt(99, _vec(last_loose + 0.7))) == []


def test_topic_k_zero_turns_the_boundary_off() -> None:
    seg = WindowSegmenter(WindowSettings(max_turns=1000, topic_k=0.0))
    for i in range(40):
        assert seg.add(_wt(i, _vec(0.0 if i % 2 else 1.5))) == []
    assert seg.topic_level is None


def test_discard_removes_a_turn_from_the_open_window() -> None:
    seg = WindowSegmenter(WindowSettings())
    seg.add(_wt(0, None))
    seg.add(_wt(1, None))
    assert seg.discard("r0") and not seg.discard("r0")
    assert [t.record_id for t in seg.open_turns] == ["r1"]


# --- validation helpers ----------------------------------------------------------------------


def test_grounded_ignores_case_punctuation_and_spacing_but_keeps_order() -> None:
    lines = ["Mira: We hiked up Ben Nevis, finally!"]
    assert grounded("hiked up ben nevis", lines)
    assert grounded("We  hiked up Ben Nevis finally", lines)
    assert not grounded("up hiked", lines) and not grounded("", lines)
    assert not grounded("hiked up Ben Lomond", lines)


def test_validate_facts_drops_invalid_ungrounded_and_uncited_items_alone() -> None:
    lines = ["Mira: I adopted a cat named Pip", "Tomas: nice"]
    good = {
        "entity": "Mira",
        "attribute": "pet",
        "value": "Mira adopted a cat named Pip",
        "turns": [1],
        "span": "adopted a cat named Pip",
    }
    fake = {**good, "span": "adopted a dog"}
    uncited = {**good, "turns": [9]}
    bad = {"entity": "Mira"}
    out = validate_facts([good, fake, uncited, bad, "junk"], lines)
    assert out is not None and len(out.facts) == 1
    assert (out.returned, out.invalid, out.ungrounded, out.uncited) == (5, 2, 2, 1)
    assert validate_facts("not a list", lines) is None and validate_facts(None, lines) is None


def test_validate_edges_and_episode() -> None:
    edge = {
        "src_entity": "Mira",
        "rel": "owns",
        "dst_entity": "Pip",
        "fact": "Mira owns Pip",
        "turns": [1],
    }
    out = validate_edges([edge, {**edge, "turns": [7]}, {**edge, "rel": " "}], 2)
    assert out is not None and len(out.edges) == 1 and out.ungrounded == 1 and out.invalid == 1
    assert validate_edges({"edges": []}, 2) is None
    ep = validate_episode({"narrative": "Mira talked.", "turns": [2, 9]}, 2)
    assert ep is not None and ep.episode is not None and ep.lines == [2] and ep.uncited == 0
    nocite = validate_episode({"narrative": "Mira talked."}, 3)
    assert nocite is not None and nocite.lines == [1, 2, 3] and nocite.uncited == 1
    assert validate_episode(17, 2) is None
    empty = validate_episode({"narrative": ""}, 2)
    assert empty is not None and empty.episode is None


# --- prompt ----------------------------------------------------------------------------------


def test_prompt_renders_only_the_requested_sections() -> None:
    prompt = PromptRegistry().for_role("window_extract")
    assert prompt.prompt_version.startswith("window_extract@1")
    base = {"content": "[1] [2023-01-01] Ana: hi", "participants": "Ana", "turn_count": 1}
    facts = prompt.render({**base, "sections": ["facts"]})[0]["content"]
    both = prompt.render({**base, "sections": ["facts", "episode"]})[0]["content"]
    assert "SECTION facts" in facts and "SECTION edges" not in facts
    assert "SECTION episode" not in facts and "SECTION episode" in both
    assert "facts, episode" in both


# --- the engine ------------------------------------------------------------------------------


async def test_off_by_default_is_inert() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": ":memory:", "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
    )
    await eng.start()
    try:
        await _send(eng, "a", "s1", _turns("trip", 3))
        assert eng._windows is None and eng.window_stats() == {}
        assert await eng.flush_windows() == {}
        assert "window_write" not in eng.describe()
    finally:
        await eng.stop()


async def test_missing_role_or_layer_fails_at_start() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": ":memory:", "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={"window_extract": "facts"},
    )
    with pytest.raises(ConfigError, match="window_extract"):
        await eng.start()


async def test_facts_are_grounded_deposited_with_the_cited_turns_as_parents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm)
    try:
        await _send(eng, "a", "s1", _turns("trip", 8))  # windows of 6 and 2
        assert await _facts(eng) == []  # batch mode: nothing runs until flush
        stats = await eng.flush_windows()
        facts = await _facts(eng)
        turns = {r.record_id: r for r in await eng.retrieve(namespace="a", memory_type="episodic")}
        assert len(facts) == 8 and stats["calls"] == 2
        for fact in facts:
            [parent] = fact.source.parents
            assert parent in turns and turns[parent].content.split(": ", 1)[1] in fact.content
            assert "atomic_fact" in fact.tags and any(t.startswith("mined:win:") for t in fact.tags)
            assert "person:mira" in fact.tags and "topic:activities" in fact.tags
            assert fact.trust <= turns[parent].trust
        assert stats["windows_by_reason"] == {"turns": 1, "flush": 1}
        assert stats["calls_by_layer"] == {"facts": 2}
    finally:
        await eng.stop()


async def test_each_section_fails_closed_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    llm.break_sections = {"edges"}
    eng = await _engine(monkeypatch, llm, mode="all")
    try:
        await _send(eng, "a", "s1", _turns("trip", 4))
        stats = await eng.flush_windows()
        assert len(await _facts(eng)) == 4  # facts landed
        assert stats["episodes_deposited"] == 1  # the episode landed
        assert stats["sections_dropped_by_layer"] == {"edges": 1}
        assert stats["edges_pending"] == 0
        llm.break_sections = {"facts"}
        await _send(eng, "b", "s1", _turns("other", 4, speaker="Tomas"))
        stats = await eng.flush_windows()
        assert stats["sections_dropped_by_layer"] == {"edges": 1, "facts": 1}
        assert len(await _facts(eng, "b")) == 0 and stats["episodes_deposited"] == 2
    finally:
        await eng.stop()


async def test_ungrounded_facts_are_dropped_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    llm.fake_span = True
    eng = await _engine(monkeypatch, llm)
    try:
        await _send(eng, "a", "s1", _turns("trip", 3))
        stats = await eng.flush_windows()
        assert await _facts(eng) == []
        assert stats["facts_returned"] == 3 and stats["facts_ungrounded"] == 3
        assert stats["facts_deposited"] == 0 and stats["sections_dropped"] == 0
    finally:
        await eng.stop()


async def test_split_always_makes_two_calls_per_window(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, mode="all", window_split="always")
    try:
        await _send(eng, "a", "s1", _turns("trip", 4))
        stats = await eng.flush_windows()
        assert sorted(llm.calls) == [["episode"], ["facts", "edges"]]
        assert stats["calls"] == 2 and stats["split_calls"] == 2 and stats["split_active"]
        assert stats["calls_by_layer"] == {"facts": 1, "edges": 1, "episode": 1}
    finally:
        await eng.stop()


async def test_split_never_keeps_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, mode="all", window_split="never")
    try:
        await _send(eng, "a", "s1", _turns("trip", 4))
        await eng.flush_windows()
        assert llm.calls == [["facts", "edges", "episode"]]
    finally:
        await eng.stop()


async def test_auto_split_after_repeated_parse_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    llm.garbage_when_joint = True  # the joint reply never parses; single-group calls do
    eng = await _engine(monkeypatch, llm, mode="all", window_split="auto", window_max_turns=2)
    try:
        await _send(eng, "a", "s1", _turns("trip", 24))  # 12 windows
        stats = await eng.flush_windows()
        assert stats["split_active"] and stats["parse_failures"] == 8  # the first 8 failed
        assert stats["split_calls"] > 0
        assert len(await _facts(eng)) == 8  # windows 9-12 (2 turns each) after the split
        assert stats["sections_dropped"] == 24  # 8 failed joint calls x 3 sections
    finally:
        await eng.stop()


async def test_cache_makes_a_rerun_on_the_same_text_free(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm)
    try:
        await _send(eng, "a", "s1", _turns("trip", 6))
        await _send(eng, "b", "s1", _turns("trip", 6))  # same text, same dates, another store
        stats = await eng.flush_windows()
        assert stats["calls"] == 1 and stats["cache_hits"] == 1 and len(llm.calls) == 1
        assert len(await _facts(eng, "a")) == 6 and len(await _facts(eng, "b")) == 6
    finally:
        await eng.stop()


async def test_cache_off_calls_every_time(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, window_cache=False)
    try:
        await _send(eng, "a", "s1", _turns("trip", 6))
        await _send(eng, "b", "s1", _turns("trip", 6))
        stats = await eng.flush_windows()
        assert stats["calls"] == 2 and stats["cache_hits"] == 0
    finally:
        await eng.stop()


async def test_namespaces_never_mix(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, window_max_turns=3)
    try:
        for i in range(3):  # interleave two users' turns
            await _send(eng, "a", "s1", _turns(f"alpha{i}", 1, speaker="Mira"))
            await _send(eng, "b", "s1", _turns(f"beta{i}", 1, speaker="Tomas"))
        await eng.flush_windows()
        for transcript in llm.transcripts:
            assert ("Mira" in transcript) != ("Tomas" in transcript)
        storage = eng._require_started()
        for ns in ("a", "b"):
            ids = {r.record_id for r in await storage.list_records(ns)}
            for fact in await _facts(eng, ns):
                assert set(fact.source.parents) <= ids and fact.namespace == ns
        assert {f.content.split()[0] for f in await _facts(eng, "a")} == {"Mira"}
        assert {f.content.split()[0] for f in await _facts(eng, "b")} == {"Tomas"}
    finally:
        await eng.stop()


async def test_forget_cascades_to_window_facts_and_purges_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm)
    try:
        await _send(eng, "a", "s1", _turns("trip", 6))
        await eng.flush_windows()
        assert eng._cache is not None
        keys = list(next(iter(eng._windows._keys_by_record.values())))  # type: ignore[union-attr]
        assert await eng._cache.get(keys[0]) is not None
        turns = await eng.retrieve(namespace="a", memory_type="episodic")
        victim = sorted(turns, key=lambda r: r.valid_from)[2]
        await eng.forget(victim.record_id, namespace="a", hard=True)
        left = [f.content for f in await _facts(eng)]
        assert len(left) == 5 and not any(victim.content.split(": ", 1)[1] in c for c in left)
        assert await eng._cache.get(keys[0]) is None  # the cached reply held the erased text
    finally:
        await eng.stop()


async def test_forget_while_the_call_is_running_leaves_no_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeWindowLLM()
    llm.block = asyncio.Event()
    eng = await _engine(monkeypatch, llm)
    try:
        await _send(eng, "a", "s1", _turns("trip", 6))
        turns = await eng.retrieve(namespace="a", memory_type="episodic")
        runner = asyncio.create_task(eng.flush_windows())
        await asyncio.wait_for(llm.entered.wait(), 10)
        await eng.forget(turns[0].record_id, namespace="a", hard=True)
        llm.block.set()
        stats = await runner
        assert await _facts(eng) == []  # no fact of a window that held an erased turn
        assert stats["windows_cancelled_forget"] == 1 and stats["facts_deposited"] == 0
        assert (await eng.verify_forget(turns[0].record_id, namespace="a"))["clean"] is True
    finally:
        await eng.stop()


async def test_forget_removes_the_turn_from_queued_and_open_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm)
    try:
        await _send(eng, "a", "s1", _turns("trip", 8))  # one queued window (6), two open
        turns = sorted(
            await eng.retrieve(namespace="a", memory_type="episodic"), key=lambda r: r.valid_from
        )
        await eng.forget(turns[1].record_id, namespace="a", hard=True)  # in the queued window
        await eng.forget(turns[7].record_id, namespace="a", hard=True)  # in the open window
        erased = [turns[1].content.split(": ", 1)[1], turns[7].content.split(": ", 1)[1]]
        stats = await eng.flush_windows()
        assert stats["turns_dropped_forget"] == 2 and stats["windows_cancelled_forget"] == 0
        assert not any(text in t for t in llm.transcripts for text in erased)  # never sent
        said = sorted(f.content for f in await _facts(eng))
        assert len(said) == 6 and not any(text in c for c in said for text in erased)
        assert stats["turns_excluded"] == 0  # they left the windows before any call
    finally:
        await eng.stop()


async def test_forget_of_every_turn_of_a_queued_window_drops_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, window_max_turns=2)
    try:
        await _send(eng, "a", "s1", _turns("trip", 4))  # two queued windows of 2
        turns = sorted(
            await eng.retrieve(namespace="a", memory_type="episodic"), key=lambda r: r.valid_from
        )
        for turn in turns[:2]:
            await eng.forget(turn.record_id, namespace="a", hard=True)
        stats = await eng.flush_windows()
        assert stats["windows_cancelled_forget"] == 1 and stats["calls"] == 1
        assert len(await _facts(eng)) == 2
    finally:
        await eng.stop()


async def test_async_then_flush_equals_sync(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    async def run(asynchronous: bool) -> tuple[list[str], dict[str, Any]]:
        llm = FakeWindowLLM()
        eng = await _engine(
            monkeypatch,
            llm,
            mode="all",
            window_async=asynchronous,
            path=tmp_path / f"{asynchronous}.db",
        )
        assert eng._windows is not None and eng._windows.asynchronous is asynchronous
        try:
            for ns, who in (("a", "Mira"), ("b", "Tomas")):
                await _send(eng, ns, "s1", _turns("trip", 9, speaker=who))
                await _send(eng, ns, "s2", _turns("walk", 4, speaker=who))
            stats = await eng.flush_windows()
            storage = eng._require_started()
            records = [x for ns in ("a", "b") for x in await storage.list_records(ns, "semantic")]
            # window ids hash the (random) record ids: compare everything else
            seen = sorted(
                f"{r.namespace}|{r.content}|"
                f"{sorted(re.sub(r'[0-9a-f]{12}$', '', t) for t in r.tags)}"
                for r in records
            )
            return seen, stats
        finally:
            await eng.stop()

    sync_seen, sync_stats = await run(False)
    async_seen, async_stats = await run(True)
    assert sync_seen and sync_seen == async_seen
    for key in ("windows", "calls", "facts_deposited", "episodes_deposited", "sessions_closed"):
        assert sync_stats[key] == async_stats[key], key


async def test_idle_timer_closes_the_open_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    llm = FakeWindowLLM()
    # 0.5 s, not 0.1 s: on a slow CI runner the gap between the three turns of one send passed
    # 0.1 s, so the timer (correctly) closed the window early and the test saw {"idle": 2}
    eng = await _engine(
        monkeypatch, llm, window_async=True, window_idle_s=0.5, path=tmp_path / "idle.db"
    )
    try:
        await _send(eng, "a", "s1", _turns("trip", 3))
        for _ in range(200):
            if len(await _facts(eng)) == 3:
                break
            await asyncio.sleep(0.05)
        assert len(await _facts(eng)) == 3
        assert eng.window_stats()["windows_by_reason"] == {"idle": 1}
    finally:
        await eng.stop()


async def test_counters_windows_per_session_and_calls_per_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm)
    try:
        await _send(eng, "a", "s1", _turns("trip", 13))  # 6 + 6 + 1
        await eng.close_session("a", "s1")
        await _send(eng, "a", "s2", _turns("walk", 5))  # 5
        stats = await eng.flush_windows()
        assert stats["turns"] == 18 and stats["windows"] == 4 and stats["calls"] == 4
        assert stats["windows_per_session"] == 2.0 and stats["turns_per_window"] == 4.5
        assert stats["calls_per_turn"] == round(4 / 18, 4)
        assert stats["windows_by_reason"] == {"turns": 2, "session_end": 1, "flush": 1}
        assert stats["parse_failures"] == 0 and stats["queued"] == 0
        assert eng.describe()["window_write"]["calls"] == 4
        reset = eng.window_stats(reset=True)
        assert reset["calls"] == 4 and eng.window_stats()["calls"] == 0
    finally:
        await eng.stop()


async def test_session_closed_hook_and_stub_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm)
    try:
        await _send(eng, "a", "s1", _turns("trip", 4))
        out = await eng.close_session("a", "s1")
        assert out["status"] == "ok" and out["closed"] == 1
        assert out["hook"]["status"] == "stub" and out["hook"]["windows"] == 1
        assert len(await _facts(eng)) == 4  # closing the session ran its pending window
        storage = eng._require_started()
        events = await storage.read_events(after_seq=0, limit=10000)
        marks = [e for e in events if (e.payload or {}).get("marker") == "session_closed"]
        assert [m.payload["session_id"] for m in marks] == ["s1"]
        # a second session starting closes the first implicitly
        await _send(eng, "a", "s2", _turns("walk", 2))
        await _send(eng, "a", "s3", _turns("swim", 2))
        await eng.flush_windows(final=False)
        assert eng.window_stats()["sessions_closed"] == 2  # s1 (explicit) and s2 (implicit)
        assert (await eng.close_session("zzz", None))["status"] == "skipped"
    finally:
        await eng.stop()


async def test_close_session_without_windows_runs_only_the_stub() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": ":memory:", "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}},
    )
    await eng.start()
    try:
        assert (await eng.close_session("a", None))["status"] == "skipped"
        out = await eng.close_session("a", "s9")
        assert out["hook"]["status"] == "stub"
    finally:
        await eng.stop()


async def test_sleep_flushes_windows_in_batch_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm)
    try:
        await _send(eng, "a", "s1", _turns("trip", 3))
        await eng.sleep()
        assert len(await _facts(eng)) == 3
    finally:
        await eng.stop()


async def test_call_three_consumes_the_window_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, mode="facts_graph", extract_graph={"resolve": "rules"})
    try:
        await _send(eng, "a", "s1", _turns("trip", 4))
        stats = await eng.flush_windows()
        assert stats["edges_pending"] == 4 and eng._extract_edges is None
        sleep = await eng.sleep()
        assert sleep["extract_graph"]["status"] == "ok"
        storage = eng._require_started()
        edges = [
            r
            for r in await storage.list_records("a", "semantic")
            if r.source.channel == "extract_graph"
        ]
        turn_ids = {r.record_id for r in await storage.list_records("a", "episodic")}
        assert len(edges) == 4
        assert all(e.source.parents and set(e.source.parents) <= turn_ids for e in edges)
        assert eng.window_stats()["edges_pending"] == 0
        again = await eng.sleep()  # nothing pending, nothing re-extracted
        assert again["extract_graph"].get("facts", 0) == 0
        assert (
            len(
                [
                    r
                    for r in await storage.list_records("a", "semantic")
                    if r.source.channel == "extract_graph"
                ]
            )
            == 4
        )
    finally:
        await eng.stop()


class _Adjudicator:
    """Stands in for the ``invalidate_edge`` role: every new state edge replaces the old one."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    @property
    def provider_id(self) -> str:
        return "fake:invalidate"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.calls.append(messages[-1]["content"])
        return '{"verdict": "update", "reason": "newer"}'


async def test_call_three_runs_resolution_and_invalidation_on_window_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeWindowLLM()
    llm.edges_override = [
        {
            "src_entity": "Mira",
            "rel": "lives_in",
            "dst_entity": "Lyon",
            "kind": "state",
            "fact": "Mira lives in Lyon",
            "turns": [1],
            "valid_from": "2023-05-08",
        },
        {
            "src_entity": "Mira",
            "rel": "resides_in",
            "dst_entity": "Paris",
            "kind": "state",
            "fact": "Mira resides in Paris",
            "turns": [2],
            "valid_from": "2023-06-01",
        },
    ]
    judge = _Adjudicator()
    eng = await _engine(
        monkeypatch,
        llm,
        mode="facts_graph",
        extra_roles={"invalidate_edge": judge},
        extract_graph={"resolve": "rules", "contradictions": True},
    )
    try:
        await _send(eng, "a", "s1", _turns("trip", 2))
        await eng.flush_windows()
        assert judge.calls == []  # call 1 only extracts; invalidation is call 3 (sleep)
        sleep = await eng.sleep()
        assert sleep["extract_graph"]["status"] == "ok"
        assert len(judge.calls) == 1 and "Lyon" in judge.calls[0] and "Paris" in judge.calls[0]
    finally:
        await eng.stop()


async def test_forgotten_turn_loses_its_pending_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, mode="facts_graph", extract_graph={"resolve": "rules"})
    try:
        await _send(eng, "a", "s1", _turns("trip", 4))
        await eng.flush_windows()
        turns = await eng.retrieve(namespace="a", memory_type="episodic")
        await eng.forget(turns[0].record_id, namespace="a", hard=True)
        assert eng.window_stats()["edges_pending"] == 3
        await eng.sleep()
        storage = eng._require_started()
        edges = [
            r
            for r in await storage.list_records("a", "semantic")
            if r.source.channel == "extract_graph"
        ]
        assert len(edges) == 3 and all(turns[0].record_id not in e.source.parents for e in edges)
    finally:
        await eng.stop()


async def test_episode_fragment_is_derived_inferred_and_parented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, mode="all")
    try:
        await _send(eng, "a", "s1", _turns("trip", 3))
        await eng.flush_windows()
        storage = eng._require_started()
        [ep] = [
            r
            for r in await storage.list_records("a", "semantic")
            if r.source.channel == "window_episode"
        ]
        turn_ids = {r.record_id for r in await storage.list_records("a", "episodic")}
        assert set(ep.source.parents) == turn_ids and "episode_fragment" in ep.tags
        assert ep.entity is None and ep.attribute is None and ep.content.startswith("A chat: ")
        # an episode fragment is never a source of graph extraction
        assert ep.source.role == "assistant"
    finally:
        await eng.stop()


def test_build_transcript_names_the_speaker_and_numbers_the_lines() -> None:
    from memspine.store.records import MemoryRecord, SourceInfo

    rec = MemoryRecord(
        namespace="a",
        memory_type="episodic",
        content="we leave tomorrow",
        source=SourceInfo(role="user"),
        tags=["spk:ana"],
        valid_from=T0,
    )
    named = rec.model_copy(update={"content": "Ben: see you there", "tags": []})
    transcript, texts, people = build_transcript([rec, named])
    assert transcript.splitlines() == [
        "[1] [2023-05-08] ana: we leave tomorrow",
        "[2] [2023-05-08] Ben: see you there",
    ]
    assert texts[1] == "Ben: see you there" and people == ["ana", "Ben"]
