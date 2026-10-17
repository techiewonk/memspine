"""W2-05: segmentation v2 (``write.segmentation_v2``): time-gap cut, adaptive growth, call batching.

Model-free: the fake window LLM of ``test_window_write`` counts calls. The measured numbers of the
200-turn workload are printed by ``test_calls_per_turn_200_turn_workload`` (run with ``-s``)."""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_window_write import _SENTENCES, FakeWindowLLM, _facts, _send

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.core.window import WindowSegmenter, WindowSettings, WindowTurn
from memspine.derive.call_batcher import BatchItem, CallBatcher
from memspine.derive.segmenter import SegmentationSettings, StreamSegmenter
from memspine.models.providers.llm.base import LLMRouter
from memspine.workers.windows import split_batch_section

T0 = datetime(2023, 5, 8, 9, 0, tzinfo=UTC)


# --- config ----------------------------------------------------------------------------------


def test_the_flag_is_off_by_default() -> None:
    w = MemspineConfig(runtime={"provider_services": {"health_timeout_s": 1.0, "close_timeout_s": 1.0, "startup_timeout_s": None}}).write
    assert w.segmentation_v2 is False
    assert (w.segment_gap_s, w.segment_calendar_day_cut) == (1800.0, False)
    assert (w.batch_max_tokens, w.batch_max_windows, w.batch_max_age_s) == (3000, 8, 30.0)


# --- CallBatcher (pure) ----------------------------------------------------------------------


def _item(key: str, tokens: int, at: float = 0.0, ref: object = None) -> BatchItem:
    return BatchItem(key, ref if ref is not None else (key, tokens, at), tokens, at)


def test_batcher_fills_to_the_token_budget_and_holds_the_open_tail() -> None:
    b = CallBatcher(max_tokens=100, max_windows=10, max_age_s=30)
    items = [_item("a", 40, i) for i in range(5)]  # 40+40, 40+40, 40
    plan = b.plan(items, now=5.0)
    assert [len(p.items) for p in plan] == [2, 2]
    assert [p.reason for p in plan] == ["tokens", "tokens"]  # the fifth window is held
    assert b.next_wake(items[4:], 5.0) == pytest.approx(30 - (5.0 - 4.0))


def test_batcher_releases_the_tail_by_age_or_final() -> None:
    b = CallBatcher(max_tokens=1000, max_windows=10, max_age_s=30)
    items = [_item("a", 10, 0.0), _item("a", 10, 5.0)]
    assert b.plan(items, now=29.0) == []
    aged = b.plan(items, now=30.0)
    assert [(p.reason, len(p.items)) for p in aged] == [("age", 2)]
    final = b.plan(items, now=1.0, final=True)
    assert [(p.reason, len(p.items)) for p in final] == [("final", 2)]


def test_batcher_caps_the_window_count() -> None:
    b = CallBatcher(max_tokens=10_000, max_windows=3, max_age_s=30)
    plan = b.plan([_item("a", 5) for _ in range(7)], now=0.0)
    assert [len(p.items) for p in plan] == [3, 3]  # the seventh waits


def test_batcher_never_mixes_keys_and_keeps_order() -> None:
    b = CallBatcher(max_tokens=10_000, max_windows=10, max_age_s=0)
    items = [_item("a", 5, 0), _item("a", 5, 0), _item("b", 5, 0), _item("a", 5, 0)]
    plan = b.plan(items, now=1.0)
    assert [(p.key, len(p.items)) for p in plan] == [("a", 2), ("b", 1), ("a", 1)]
    assert [p.reason for p in plan[:2]] == ["key", "key"]


def test_batcher_oversize_window_goes_alone() -> None:
    b = CallBatcher(max_tokens=100, max_windows=10, max_age_s=30)
    plan = b.plan([_item("a", 500), _item("a", 10)], now=0.0, final=True)
    assert [len(p.items) for p in plan] == [1, 1]


# --- StreamSegmenter (pure) ------------------------------------------------------------------


def _wt(i: int, minutes: float, session: str = "s", text: str = "word " * 4) -> WindowTurn:
    return WindowTurn(
        record_id=f"r{i}", text=text, session=session, when=T0 + timedelta(minutes=minutes)
    )


def _stream(**kw: Any) -> StreamSegmenter:
    base = WindowSettings(max_turns=kw.pop("max_turns", 6), max_tokens=kw.pop("max_tokens", 1500))
    return StreamSegmenter(base, segmentation=SegmentationSettings(**kw))


def test_a_time_gap_cuts_inside_one_session() -> None:
    seg = _stream(gap_s=1800.0, calendar_day_cut=False, grow_tokens=1, max_turns_cap=6)
    out: list[Any] = []
    for i in range(3):
        out += seg.add(_wt(i, i))
    assert out == []
    out += seg.add(_wt(3, 2 + 31))  # 31 minutes of silence, same session
    assert [w.reason for w in out] == ["time_gap"]
    assert out[0].record_ids == ["r0", "r1", "r2"]
    tail = seg.close()
    assert tail is not None and tail.record_ids == ["r3"]


def test_a_gap_below_the_limit_and_a_zero_gap_do_not_cut() -> None:
    seg = _stream(gap_s=1800.0, calendar_day_cut=False)
    assert [seg.add(_wt(i, i * 29)) for i in range(4)] == [[], [], [], []]
    off = _stream(gap_s=0.0, calendar_day_cut=False)
    assert [off.add(_wt(i, i * 600)) for i in range(4)] == [[], [], [], []]


def test_a_calendar_day_change_cuts() -> None:
    seg = _stream(gap_s=10 * 3600.0, calendar_day_cut=True)
    seg.add(_wt(0, 14 * 60 + 50))  # 23:50
    out = seg.add(_wt(1, 15 * 60 + 5))  # 00:05 next day, a 15 minute gap
    assert [w.reason for w in out] == ["calendar_day"]


def test_short_turns_make_the_window_grow_past_max_turns() -> None:
    seg = _stream(max_turns=6, grow_tokens=400, max_turns_cap=24)
    out: list[Any] = []
    for i in range(60):
        out += seg.add(_wt(i, i * 0.1))  # 4 tokens a turn: 6 turns hold 24 tokens
    assert [len(w.turns) for w in out] == [24, 24] and {w.reason for w in out} == {"turns"}


def test_long_turns_close_at_max_turns_and_the_token_cap_always_binds() -> None:
    big = "x" * 400  # 100 tokens
    seg = _stream(max_turns=6, grow_tokens=400, max_turns_cap=24, max_tokens=10_000)
    out: list[Any] = []
    for i in range(12):
        out += seg.add(_wt(i, i * 0.1, text=big))
    assert [len(w.turns) for w in out] == [6, 6]  # 600 tokens >= 400: no growth
    tight = _stream(max_turns=6, grow_tokens=10_000, max_turns_cap=24, max_tokens=250)
    out = []
    for i in range(6):
        out += tight.add(_wt(i, i * 0.1, text=big))
    assert [len(w.turns) for w in out] == [2, 2] and out[0].reason == "tokens"


def test_without_a_gap_or_growth_it_matches_the_v1_segmenter() -> None:
    plain = WindowSettings(max_turns=6)
    v1 = WindowSegmenter(plain)
    v2 = StreamSegmenter(
        plain,
        segmentation=SegmentationSettings(gap_s=0.0, calendar_day_cut=False, max_turns_cap=6),
    )
    a: list[Any] = []
    b: list[Any] = []
    for i in range(50):
        turn = _wt(i, i * 3, session="s1" if i < 30 else "s2")
        a += v1.add(turn)
        b += v2.add(turn)
    assert [(w.reason, w.record_ids) for w in a] == [(w.reason, w.record_ids) for w in b]


# --- split_batch_section (pure) --------------------------------------------------------------


def test_split_maps_cited_lines_to_the_right_window_and_renumbers() -> None:
    spans = [(0, 3), (3, 2), (5, 4)]  # lines 1-3, 4-5, 6-9
    facts = [
        {"turns": [2], "span": "x"},
        {"turns": [5], "span": "y"},
        {"turns": [6, 8], "span": "z"},
        {"turns": [3, 4], "span": "cross"},
        {"turns": [99], "span": "nowhere"},
    ]
    parts, cross = split_batch_section("facts", facts, spans)
    assert cross == 1
    assert [[i["span"] for i in p] for p in parts] == [["x", "nowhere"], ["y"], ["z"]]
    assert parts[1][0]["turns"] == [2] and parts[2][0]["turns"] == [1, 3]


def test_split_renumbers_edges_and_passes_a_non_list_to_every_window() -> None:
    parts, cross = split_batch_section(
        "edges", [{"turns": [5], "episode_indices": [5]}], [(0, 3), (3, 3)]
    )
    assert cross == 0 and parts[1] == [{"turns": [2], "episode_indices": [2]}]
    parts, _ = split_batch_section("facts", "oops", [(0, 1), (1, 1)])
    assert parts == ["oops", "oops"]


# --- the engine ------------------------------------------------------------------------------


async def _engine(
    monkeypatch: pytest.MonkeyPatch,
    llm: FakeWindowLLM,
    *,
    path: Any = ":memory:",
    outbox: bool = False,
    mode: str = "facts",
    **write: Any,
) -> Engine:
    async def router(config: MemspineConfig, hooks: object = None, wiring: object = None) -> LLMRouter:
        return LLMRouter({"window_extract": llm})  # type: ignore[dict-item]

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    extra: dict[str, Any] = {"workers": {"outbox": True}} if outbox else {}
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={'provider': "sqlite", 'settings': {'path': str(path), 'busy_timeout_s': 5.0}},
        embedding={'provider': "hash", 'settings': {}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={"window_extract": mode, "window_async": False, "window_idle_s": 0.0, **write},
        llm={"roles": {"window_extract": {"provider": "litellm", "settings": {"model": "fake/window", "timeout_seconds": 60.0}}}},
        **extra,
    )
    await eng.start()
    return eng


def _chat(
    count: int,
    speaker: str = "Mira",
    start: datetime = T0,
    step_min: float = 1.0,
    first: int = 0,
) -> list[dict[str, str]]:
    """``count`` short, distinct turns: three sentences mixed by index, so semantic dedup keeps
    every fact apart and no two windows share a text (the call cache never answers)."""
    n = len(_SENTENCES)
    return [
        {
            "role": "user",
            "content": (
                f"{speaker}: {_SENTENCES[i % n]} and {_SENTENCES[(i // n + 5) % n].lower()} "
                f"then {_SENTENCES[(i * 5 + 1) % n].lower()} number {i}"
            ),
            "timestamp": (start + timedelta(minutes=(i - first) * step_min)).isoformat(),
        }
        for i in range(first, first + count)
    ]


def _contents(facts: list[Any]) -> list[str]:
    return sorted(re.sub(r"[0-9a-f]{12}$", "", f.content) for f in facts)


async def test_flag_off_is_the_old_segmenter_and_one_call_per_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm)
    try:
        # a 2-hour hole inside one session: v1 does not cut on it
        await _send(eng, "a", "s1", _chat(5) + _chat(5, start=T0 + timedelta(hours=2), first=5))
        stats = await eng.flush_windows()
        assert isinstance(eng._windows._stores["a"].segmenter, WindowSegmenter)
        assert eng._windows._seg is None and eng._windows._batcher is None
        assert "time_gap" not in stats["windows_by_reason"]
        assert stats["windows"] == 2 and stats["calls"] == len(llm.calls) == 2
        assert "segmentation_v2" not in stats and "batches" not in stats
    finally:
        await eng.stop()


async def test_a_time_gap_cuts_the_window_inside_one_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, segmentation_v2=True, batch_max_windows=1)
    try:
        await _send(
            eng, "a", "s1", _chat(4) + _chat(4, start=T0 + timedelta(minutes=3 + 45), first=4)
        )
        stats = await eng.flush_windows()
        assert stats["windows_by_reason"].get("time_gap") == 1
        assert stats["windows"] == 2 and stats["calls"] == 2  # batching off: one call each
        assert len(await _facts(eng)) == 8
    finally:
        await eng.stop()


async def test_windows_grow_while_turns_are_short(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    on = await _engine(
        monkeypatch, llm, segmentation_v2=True, batch_max_windows=1, segment_max_turns=24
    )
    try:
        await _send(on, "a", "s1", _chat(48))
        grown = await on.flush_windows()
    finally:
        await on.stop()
    off_llm = FakeWindowLLM()
    off = await _engine(monkeypatch, off_llm)
    try:
        await _send(off, "a", "s1", _chat(48))
        base = await off.flush_windows()
    finally:
        await off.stop()
    # ~25-token turns: the window grows from 6 turns until it holds 400 tokens (about 16 turns)
    assert base["turns_per_window"] <= 6.0 and grown["turns_per_window"] >= 8.0
    assert grown["windows"] < base["windows"]


async def test_calls_per_turn_200_turn_workload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stub-LLM calls per turn on a synthetic 200-turn, one-session workload, by arm."""
    arms: dict[str, dict[str, Any]] = {
        "off (v1)": {},
        "batch only": {"segmentation_v2": True, "segment_max_turns": 6},
        "adaptive only": {"segmentation_v2": True, "batch_max_windows": 1},
        "adaptive + batch": {"segmentation_v2": True},
    }
    results: dict[str, tuple[int, int, int, list[str]]] = {}
    for name, write in arms.items():
        llm = FakeWindowLLM()
        eng = await _engine(monkeypatch, llm, **write)
        try:
            await _send(eng, "a", "s1", _chat(200))
            stats = await eng.flush_windows()
            facts = await _facts(eng)
            assert stats["calls"] == len(llm.calls)
            assert stats["facts_ungrounded"] == 0 and stats["sections_dropped"] == 0
            results[name] = (
                stats["windows"],
                stats["calls"],
                stats["facts_deposited"],
                _contents(facts),
            )
        finally:
            await eng.stop()
    with capsys.disabled():
        print("\nW2-05 stub calls per turn (200 turns, one session, 1 min apart, facts mode)")
        for name, (windows, calls, deposited, _) in results.items():
            print(
                f"  {name:18s} windows={windows:3d} calls={calls:3d} "
                f"calls/turn={calls / 200:.4f} facts={deposited}"
            )
    off_calls = results["off (v1)"][1]
    assert off_calls == results["off (v1)"][0]  # one call per window
    assert results["batch only"][1] < off_calls
    assert results["adaptive only"][1] < off_calls
    assert results["adaptive + batch"][1] < results["adaptive only"][1]
    # grounding and recall inside the band: every arm deposits the same facts
    reference = results["off (v1)"][3]
    for name, (_, _, deposited, contents) in results.items():
        assert deposited == 200, name
        assert contents == reference, name


async def test_grounding_maps_back_to_the_right_turns_inside_a_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, segmentation_v2=True, segment_max_turns=6)
    try:
        await _send(eng, "a", "s1", _chat(40))
        stats = await eng.flush_windows()
        assert stats["batches"] >= 1 and stats["batched_windows"] >= 2
        assert any(t.count("=== window") >= 2 for t in llm.transcripts)
        storage = eng._require_started()
        facts = await _facts(eng)
        assert len(facts) == 40
        for fact in facts:
            assert len(fact.source.parents) == 1  # the fake cites exactly its own line
            turn = await storage.get_record(fact.source.parents[0])
            assert turn is not None
            number = re.search(r"number (\d+)", fact.content)
            assert number is not None and f"number {number.group(1)}" in turn.content
    finally:
        await eng.stop()


class _CrossWindowLLM(FakeWindowLLM):
    """Adds to every batched reply one fact citing a line of the first window and one of the
    last (both real, the span fits neither window alone)."""

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        raw = await super().chat(messages, **options)
        user = messages[-1]["content"]
        out = json.loads(raw)
        numbers = [int(n) for n in re.findall(r"^\[(\d+)\] ", user, re.M)]
        if user.count("=== window") >= 2 and "facts" in out and len(numbers) >= 2:
            out["facts"].append(
                {
                    "entity": "Mira",
                    "attribute": "event",
                    "value": "both",
                    "kind": "event",
                    "turns": [numbers[0], numbers[-1]],
                    "span": "orchid",
                    "persons": ["Mira"],
                    "topic": "activities",
                }
            )
        return json.dumps(out)


async def test_an_item_citing_two_windows_is_dropped_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _CrossWindowLLM()
    eng = await _engine(monkeypatch, llm, segmentation_v2=True, segment_max_turns=6)
    try:
        await _send(eng, "a", "s1", _chat(30))
        stats = await eng.flush_windows()
        assert stats["batch_cross_window"] >= 1
        assert stats["facts_ungrounded"] >= stats["batch_cross_window"]
        assert len(await _facts(eng)) == 30  # the 30 real facts, not the cross-window ones
    finally:
        await eng.stop()


async def test_no_batching_across_namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, segmentation_v2=True, segment_max_turns=6)
    try:
        for chunk in range(0, 24, 6):  # interleave the two namespaces
            await _send(eng, "a", "s1", _chat(24, "Mira")[chunk : chunk + 6])
            await _send(eng, "b", "s1", _chat(24, "Bram")[chunk : chunk + 6])
        stats = await eng.flush_windows()
        assert stats["batches"] >= 2
        for transcript in llm.transcripts:
            who = set(re.findall(r"\] (Mira|Bram):", transcript))
            assert len(who) == 1, who
        assert len(await _facts(eng, "a")) == 24 and len(await _facts(eng, "b")) == 24
    finally:
        await eng.stop()


async def test_a_batch_holds_for_company_until_it_is_full_or_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the background (async) mode a lone closed window waits; a second one releases both."""
    import asyncio

    llm = FakeWindowLLM()
    eng = await _engine(
        monkeypatch,
        llm,
        segmentation_v2=True,
        segment_max_turns=6,
        window_async=True,
        batch_max_age_s=3600.0,
    )
    try:
        await _send(eng, "a", "s1", _chat(6))  # one window closes (turn cap)
        await asyncio.sleep(0.1)
        assert llm.calls == []  # held for company
        await _send(eng, "a", "s1", _chat(6, start=T0 + timedelta(minutes=6), first=6))
        await asyncio.sleep(0.1)
        assert llm.calls == []  # two windows: still below the window and token caps
        stats = await eng.flush_windows()  # a flush never holds anything back
        assert stats["calls"] == 1 and stats["batched_windows"] == 2
    finally:
        await eng.stop()


# --- outbox ----------------------------------------------------------------------------------


async def test_outbox_batches_and_matches_the_in_memory_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    results: dict[bool, tuple[list[str], dict[str, Any]]] = {}
    for outbox in (False, True):
        llm = FakeWindowLLM()
        eng = await _engine(
            monkeypatch,
            llm,
            path=tmp_path / f"{outbox}.db",
            outbox=outbox,
            segmentation_v2=True,
            segment_max_turns=6,
        )
        try:
            await _send(eng, "a", "s1", _chat(40))
            stats = await eng.flush_windows()
            results[outbox] = (_contents(await _facts(eng)), stats)
        finally:
            await eng.stop()
    assert results[False][0] == results[True][0] and len(results[True][0]) == 40
    for key in ("windows", "calls", "batches", "batched_windows", "facts_deposited"):
        assert results[False][1][key] == results[True][1][key], key
    assert results[True][1]["batches"] >= 1 and results[True][1]["queued"] == 0


async def test_outbox_crash_before_the_batch_runs_loses_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "crash.db"
    first = await _engine(
        monkeypatch,
        FakeWindowLLM(),
        path=db,
        outbox=True,
        segmentation_v2=True,
        segment_max_turns=6,
    )
    await _send(first, "a", "s1", _chat(30))  # batch mode: closed windows wait as outbox rows
    assert first.window_stats()["queued"] >= 1 and await _facts(first) == []
    await first.stop()  # the process goes away with the windows still queued

    llm = FakeWindowLLM()
    second = await _engine(
        monkeypatch, llm, path=db, outbox=True, segmentation_v2=True, segment_max_turns=6
    )
    try:
        stats = await second.flush_windows()
        facts = await _facts(second)
        # the open window at the crash was in memory (as in v1); the queued ones all ran
        assert len(facts) >= 24 and stats["queued"] == 0
        assert stats["batches"] >= 1 and stats["calls"] < stats["windows_processed"]
    finally:
        await second.stop()


class _FailsFirstCall(FakeWindowLLM):
    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        if not self.calls:
            self.calls.append(["boom"])
            raise RuntimeError("provider down")
        return await super().chat(messages, **options)


async def test_outbox_a_failed_batched_call_is_retried_and_loses_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = _FailsFirstCall()
    eng = await _engine(
        monkeypatch,
        llm,
        path=tmp_path / "retry.db",
        outbox=True,
        segmentation_v2=True,
        segment_max_turns=6,
    )
    try:
        await _send(eng, "a", "s1", _chat(36))
        stats = await eng.flush_windows()
        assert stats["call_errors"] >= 1
        assert len(await _facts(eng)) == 36 and stats["queued"] == 0
    finally:
        await eng.stop()
