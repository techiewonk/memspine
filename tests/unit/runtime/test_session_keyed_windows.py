"""ADR-090 item 2 (DS-06/DS-07): open windows are keyed by conversation, else session, else the
reply parent, else a per-namespace default; interleaving never cuts or closes another window,
and the restart draft carries each turn's window key, source version and content hash.

Cuts are driven deterministically (batch mode; the idle cut is invoked directly), no sleeps."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_outbox_windows import _engine
from test_window_write import FakeWindowLLM, _facts

from memspine import Engine
from memspine.workers.windows import WINDOW_STAGE, WindowPipeline

T0 = datetime(2024, 3, 2, 10, tzinfo=UTC)
SENTENCES = (
    "I repotted the orchid on the balcony",
    "We booked a kayak tour along the northern coast",
    "The violin recital starts at seven on Friday",
    "My brother moved to a flat near the harbour",
    "Last spring I planted apple trees behind the barn",
    "Our dog Pip learned to open the garden gate",
    "I am saving for a second-hand motorbike",
    "She wants to repaint the kitchen in pale green",
)


async def _say(eng: Engine, session: str, i: int, *, ns: str = "a") -> None:
    await eng.ingest_messages(
        [
            {
                "role": "user",
                "content": f"Mira: {SENTENCES[i % len(SENTENCES)]} ({session})",
                "created_at": (T0 + timedelta(minutes=i)).isoformat(),
            }
        ],
        namespace=ns,
        session_id=session,
        conversation_id=session,
    )


def _pipeline(eng: Engine) -> WindowPipeline:
    assert eng._windows is not None
    return eng._windows


def _open(eng: Engine, ns: str = "a") -> dict[str, list[str]]:
    store = _pipeline(eng)._store(ns)
    return {key: [t.text for t in w.segmenter.open_turns] for key, w in store.windows.items()}


async def test_interleaved_sessions_never_flush_or_close_each_other(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _engine(monkeypatch, FakeWindowLLM(), tmp_path / "mix.db", outbox=True)
    try:
        await _say(eng, "A", 0)
        await _say(eng, "B", 1)
        await _say(eng, "A", 2)
        stats = eng.window_stats()
        assert stats["windows"] == 0 and stats["sessions_closed"] == 0
        assert {k: len(v) for k, v in _open(eng).items()} == {
            "conversation:A": 2,
            "conversation:B": 1,
        }
        out = await eng.close_session("a", "A")  # closes A only
        assert out["closed"] == 1
        assert {k: len(v) for k, v in _open(eng).items()} == {"conversation:B": 1}
        assert len(await _facts(eng)) == 2
        await eng.flush_windows("a")
        assert len(await _facts(eng)) == 3
        assert eng.window_stats()["windows_by_reason"] == {"session_end": 1, "flush": 1}
    finally:
        await eng.stop()


async def test_the_window_key_falls_back_to_session_reply_parent_then_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _engine(monkeypatch, FakeWindowLLM(), tmp_path / "keys.db", outbox=True)
    try:
        pipeline = _pipeline(eng)
        [parent] = await eng.ingest_messages(
            [{"role": "user", "content": "Mira: I repotted the orchid"}],
            namespace="a",
            session_id="S",
            conversation_id="C",
        )
        child = await eng.write(
            "Mira: It flowers in May",
            namespace="a",
            memory_type="episodic",
            reply_to=parent.record_id,
        )
        loose = await eng.write("Mira: The bakery sells rye", namespace="a", memory_type="episodic")
        bare = parent.model_copy(
            update={"source": parent.source.model_copy(update={"message": None})}
        )
        assert (await pipeline._window_key(parent, "S"))[:2] == ("conversation:C", "C")
        assert (await pipeline._window_key(bare, "S"))[0] == "session:S"
        assert (await pipeline._window_key(child, None))[:2] == ("conversation:C", "C")
        assert (await pipeline._window_key(loose, None))[0] == "namespace"
    finally:
        await eng.stop()


@pytest.mark.parametrize("turns_before", [1, 2, 3, 4, 5])
async def test_restart_after_each_turn_restores_the_open_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, turns_before: int
) -> None:
    """Six turns alternate between two sessions; the process restarts after ``turns_before``.
    Every turn is derived exactly once, in its own session's window."""
    db = tmp_path / f"restart{turns_before}.db"
    order = ["A", "B"] * 3
    first = await _engine(monkeypatch, FakeWindowLLM(), db, outbox=True)
    for i in range(turns_before):
        await _say(first, order[i], i)
    await first.stop()

    llm = FakeWindowLLM()
    second = await _engine(monkeypatch, llm, db, outbox=True)
    try:
        for i in range(turns_before, len(order)):
            await _say(second, order[i], i)
        await second.flush_windows("a")
        facts = await _facts(second)
        assert len(facts) == len(order)
        assert second.window_stats()["windows"] == 2  # one per session
        for transcript in llm.transcripts:  # no window mixes the sessions
            assert not ("(A)" in transcript and "(B)" in transcript)
    finally:
        await second.stop()


async def test_idle_and_session_close_cuts_after_a_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "cuts.db"
    first = await _engine(monkeypatch, FakeWindowLLM(), db, outbox=True)
    for i, session in enumerate(["A", "B", "A"]):
        await _say(first, session, i)
    await first.stop()

    second = await _engine(monkeypatch, FakeWindowLLM(), db, outbox=True)
    try:
        await second.flush_windows("a", final=False)  # restores the draft, cuts nothing
        assert {k: len(v) for k, v in _open(second).items()} == {
            "conversation:A": 2,
            "conversation:B": 1,
        }
        pipeline = _pipeline(second)
        store = pipeline._store("a")
        win_b = store.windows["conversation:B"]
        await pipeline._idle_cut("a", store, win_b, win_b.touch)  # B's idle timer fires
        assert {k: len(v) for k, v in _open(second).items()} == {"conversation:A": 2}
        out = await second.close_session("a", "A")
        assert out["status"] == "ok" and out["closed"] == 1
        assert _open(second) == {}
        await second.flush_windows("a")
        assert second.window_stats()["windows_by_reason"] == {"idle": 1, "session_end": 1}
        assert len(await _facts(second)) == 3
    finally:
        await second.stop()


@pytest.mark.parametrize("field", ["source_version", "content_hash"])
async def test_a_draft_turn_with_a_stale_source_is_refreshed_not_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str
) -> None:
    db = tmp_path / f"stale-{field}.db"
    first = await _engine(monkeypatch, FakeWindowLLM(), db, outbox=True)
    await _say(first, "A", 0)
    await _say(first, "A", 1)
    pipeline = _pipeline(first)
    assert pipeline._outbox is not None
    scope = pipeline._scope("a")
    draft = await pipeline._outbox.load_draft(scope, WINDOW_STAGE)
    assert draft is not None
    turn = draft["turns"][0]
    assert {"window_key", "source_version", "content_hash"} <= set(turn)
    turn[field] = 99 if field == "source_version" else "stale"
    await pipeline._outbox.save_draft(scope, WINDOW_STAGE, draft)  # what an old process left
    await first.stop()

    second = await _engine(monkeypatch, FakeWindowLLM(), db, outbox=True)
    try:
        await second.flush_windows("a", final=False)
        restored = _pipeline(second)
        assert restored._counts["draft_turns_stale"] == 1
        reloaded = await restored._outbox.load_draft(scope, WINDOW_STAGE)
        assert reloaded is not None
        stale_id = turn["record_id"]
        record = await second.get_record(stale_id, namespace="a")
        assert record is not None
        [entry] = [t for t in reloaded["turns"] if t["record_id"] == stale_id]
        assert entry["source_version"] == record.version  # refreshed from the record
        assert entry[field] != turn[field]
        await second.flush_windows("a")
        assert len(await _facts(second)) == 2
    finally:
        await second.stop()
