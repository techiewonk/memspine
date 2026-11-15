"""RM-163: the window draft is persisted incrementally, and assigned ids never open windows.

* Each write upserts only its own turn's draft row (no vectors), so the draft work of one
  write is constant in rows and bytes however many windows are open (1 to 64 here).
* A message whose conversation and session were assigned by message identity (not given by
  the caller) joins the per-namespace default window (ADR-090), instead of opening a one-turn
  window of its own that only the idle timer closes."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_outbox_windows import _engine
from test_window_write import FakeWindowLLM, _facts

from memspine import Engine
from memspine.derive.segmenter import StreamSegmenter
from memspine.workers.windows import WindowPipeline

T0 = datetime(2024, 3, 2, 10, tzinfo=UTC)
OPEN_WINDOWS = 64


def _pipeline(eng: Engine) -> WindowPipeline:
    assert eng._windows is not None
    return eng._windows


def _draft(path: Path) -> list[dict[str, object]]:
    con = sqlite3.connect(path)
    try:
        rows = con.execute("SELECT payload FROM outbox_draft").fetchall()
    finally:
        con.close()
    return [json.loads(payload) for (payload,) in rows]


def _counters(eng: Engine) -> tuple[int, int, int]:
    stats = eng.window_stats()
    return (
        int(stats["draft_rows_written"]),
        int(stats["draft_rows_deleted"]),
        int(stats["draft_bytes_written"]),
    )


async def _root(eng: Engine, i: int, **ids: str) -> None:
    await eng.ingest_messages(
        [
            {
                "role": "user",
                "content": f"Mira: note {i:04d} about the orchard",
                "created_at": (T0 + timedelta(minutes=i)).isoformat(),
            }
        ],
        namespace="a",
        **ids,
    )


async def test_per_write_draft_work_is_constant_as_open_windows_grow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "grow.db"
    eng = await _engine(
        monkeypatch, FakeWindowLLM(), db, outbox=True, window_max_turns=50, segment_max_turns=50
    )
    try:
        per_write: list[tuple[int, int, int]] = []
        for i in range(OPEN_WINDOWS):
            before = _counters(eng)
            # one caller conversation per message: each write opens one more window
            await _root(eng, i, session_id=f"s{i:04d}", conversation_id=f"c{i:04d}")
            after = _counters(eng)
            per_write.append(tuple(b - a for a, b in zip(before, after, strict=True)))  # type: ignore[arg-type]
        stats = eng.window_stats()
        assert stats["open_windows"] == OPEN_WINDOWS
        assert stats["open_turns"] == OPEN_WINDOWS
        assert {(rows, deleted) for rows, deleted, _ in per_write} == {(1, 0)}
        sizes = [size for _, _, size in per_write]
        # constant up to the digits of the row's sequence number (1 to 64)
        assert max(sizes) - min(sizes) <= 2, sizes
        assert max(sizes) < 1024  # no vector: one small row per turn
        draft = _draft(db)
        assert len(draft) == OPEN_WINDOWS
        assert all("vector" not in row for row in draft)
        assert sorted(int(row["seq"]) for row in draft) == list(range(1, OPEN_WINDOWS + 1))
        await eng.flush_windows("a")  # every window closes; every row goes
        assert _draft(db) == []
        assert eng.window_stats()["windows"] == OPEN_WINDOWS
    finally:
        await eng.stop()


async def test_assigned_ids_join_the_default_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "default.db"
    eng = await _engine(
        monkeypatch, FakeWindowLLM(), db, outbox=True, window_max_turns=50, segment_max_turns=50
    )
    try:
        for i in range(5):  # no conversation, no session: identity assigns both
            await _root(eng, i)
        for i in range(5, 8):  # a caller session, no conversation: the session's window
            await _root(eng, i, session_id="S")
        [parent] = await eng.ingest_messages(
            [{"role": "user", "content": "Mira: I repotted the orchid", "message_id": "p1"}],
            namespace="a",
        )
        await eng.ingest_messages(  # a reply inherits the parent's (assigned) conversation
            [{"role": "user", "content": "Mira: it flowers in May", "reply_to": "p1"}],
            namespace="a",
        )
        assert parent.source.message is not None
        assert parent.source.message["conversation_generated"] is True
        assert parent.source.message["session_generated"] is True
        store = _pipeline(eng)._store("a")
        assert {k: len(w.segmenter.open_turns) for k, w in store.windows.items()} == {
            "namespace": 7,
            "session:S": 3,
        }
        assert eng.window_stats()["windows"] == 0  # nothing waits for an idle cut alone
    finally:
        await eng.stop()


async def test_the_default_window_survives_a_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "restart.db"
    first = await _engine(
        monkeypatch, FakeWindowLLM(), db, outbox=True, window_max_turns=50, segment_max_turns=50
    )
    for i in range(3):
        await _root(first, i)
    await first.stop()
    assert len(_draft(db)) == 3

    second = await _engine(
        monkeypatch, FakeWindowLLM(), db, outbox=True, window_max_turns=50, segment_max_turns=50
    )
    try:
        await second.flush_windows("a", final=False)  # restores; the unchanged rows stay
        assert second.window_stats()["draft_rows_written"] == 0
        store = _pipeline(second)._store("a")
        assert [t.text[-15:] for t in store.windows["namespace"].segmenter.open_turns] == [
            f"{i:04d} about the orchard"[-15:] for i in range(3)
        ]
        await second.flush_windows("a")
        assert second.window_stats()["windows"] == 1
        assert len(await _facts(second)) == 3
        assert _draft(db) == []
    finally:
        await second.stop()


async def test_persist_visits_only_touched_turns_even_with_many_open_turns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "visits.db"
    eng = await _engine(
        monkeypatch, FakeWindowLLM(), db, outbox=True, window_max_turns=50, segment_max_turns=50
    )
    try:
        for i in range(24):
            await _root(eng, i, session_id=f"s{i}", conversation_id=f"c{i}")
        for i in range(24, 48):
            await _root(eng, i, session_id="s0", conversation_id="c0")
        pipeline = _pipeline(eng)
        store = pipeline._store("a")
        win = store.windows["conversation:c0"]
        turn = win.segmenter.open_turns[-1]
        turn.tokens += 1
        pipeline._draft_upsert("a", win, turn)
        before = _counters(eng)

        def forbidden(_self: object) -> list[object]:
            raise AssertionError("draft persistence visited unrelated open turns")

        with monkeypatch.context() as patch:
            patch.setattr(StreamSegmenter, "open_turns", property(forbidden))
            await pipeline._persist_draft("a", store)
        after = _counters(eng)
        assert (after[0] - before[0], after[1] - before[1]) == (1, 0)
        assert len(_draft(db)) == 48
    finally:
        await eng.stop()


async def test_observe_visits_only_its_own_open_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real ingest must not inspect other windows to compute its draft delta."""
    eng = await _engine(
        monkeypatch,
        FakeWindowLLM(),
        tmp_path / "observe-visits.db",
        outbox=True,
        window_max_turns=50,
        segment_max_turns=50,
    )
    try:
        for i in range(24):
            await _root(eng, i, session_id=f"s{i}", conversation_id=f"c{i}")
        store = _pipeline(eng)._store("a")
        target = store.windows["conversation:c0"].segmenter
        original = StreamSegmenter.open_turns
        visited: list[StreamSegmenter] = []

        def counted(self: StreamSegmenter) -> list[object]:
            visited.append(self)
            return original.fget(self)  # type: ignore[misc]

        with monkeypatch.context() as patch:
            patch.setattr(StreamSegmenter, "open_turns", property(counted))
            await _root(eng, 24, session_id="s0", conversation_id="c0")
        assert visited
        assert all(segmenter is target for segmenter in visited)
    finally:
        await eng.stop()


async def test_failed_draft_delta_is_retried_without_losing_later_mutations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "retry.db"
    eng = await _engine(
        monkeypatch, FakeWindowLLM(), db, outbox=True, window_max_turns=50, segment_max_turns=50
    )
    try:
        await _root(eng, 0, session_id="s", conversation_id="c")
        pipeline = _pipeline(eng)
        store = pipeline._store("a")
        win = store.windows["conversation:c"]
        turn = win.segmenter.open_turns[0]
        original = int(_draft(db)[0]["tokens"])
        turn.tokens = original + 1
        pipeline._draft_upsert("a", win, turn)
        assert pipeline._outbox is not None
        update = pipeline._outbox.update_draft

        async def fail_once(*args: object, **kwargs: object) -> int:
            monkeypatch.setattr(pipeline._outbox, "update_draft", update)
            raise OSError("injected draft transaction failure")

        monkeypatch.setattr(pipeline._outbox, "update_draft", fail_once)
        with pytest.raises(OSError, match="injected"):
            await pipeline._persist_draft("a", store)
        assert int(_draft(db)[0]["tokens"]) == original
        assert len(pipeline._draft_pending["a"]) == 1
        await pipeline._persist_draft("a", store)
        assert int(_draft(db)[0]["tokens"]) == original + 1
        assert not pipeline._draft_pending["a"]

        # A delete made while an upsert is awaiting SQL must survive its completion.
        turn.tokens += 1
        pipeline._draft_upsert("a", win, turn)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def paused(*args: object, **kwargs: object) -> int:
            entered.set()
            await release.wait()
            return int(await update(*args, **kwargs))

        monkeypatch.setattr(pipeline._outbox, "update_draft", paused)
        writing = asyncio.create_task(pipeline._persist_draft("a", store))
        await entered.wait()
        key = (win.key, turn.record_id)
        pipeline._draft_delete("a", key)
        release.set()
        await writing
        assert pipeline._draft_pending["a"][key].row is None
        monkeypatch.setattr(pipeline._outbox, "update_draft", fail_once)
        with pytest.raises(OSError, match="injected"):
            await pipeline._persist_draft("a", store)
        assert len(_draft(db)) == 1
        assert pipeline._draft_pending["a"][key].row is None
        await pipeline._persist_draft("a", store)
        assert _draft(db) == []
        pipeline._draft_upsert("a", win, turn)
        await pipeline._persist_draft("a", store)
        assert len(_draft(db)) == 1
        assert not pipeline._draft_pending["a"]
    finally:
        await eng.stop()


@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_cancelled_draft_write_cannot_resurrect_a_later_deleted_turn(
    cancel_count: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "cancelled-draft.db"
    eng = await _engine(
        monkeypatch, FakeWindowLLM(), db, outbox=True, window_max_turns=50, segment_max_turns=50
    )
    try:
        await _root(eng, 0, session_id="s", conversation_id="c")
        pipeline = _pipeline(eng)
        store = pipeline._store("a")
        win = store.windows["conversation:c"]
        turn = win.segmenter.open_turns[0]
        turn.tokens += 1
        pipeline._draft_upsert("a", win, turn)
        assert pipeline._outbox is not None
        update = pipeline._outbox.update_draft
        entered = asyncio.Event()
        release = asyncio.Event()
        delayed: list[asyncio.Task[int]] = []

        async def late_write(*args: object, **kwargs: object) -> int:
            async def finish() -> int:
                entered.set()
                await release.wait()
                return int(await update(*args, **kwargs))

            task = asyncio.create_task(finish())
            delayed.append(task)
            return await asyncio.shield(task)

        monkeypatch.setattr(pipeline._outbox, "update_draft", late_write)
        writing = asyncio.create_task(pipeline._persist_draft("a", store))
        await entered.wait()
        writing.cancel()
        await asyncio.sleep(0)
        pipeline._draft_delete("a", (win.key, turn.record_id))
        monkeypatch.setattr(pipeline._outbox, "update_draft", update)
        deleting = asyncio.create_task(pipeline._persist_draft("a", store))
        await asyncio.sleep(0)
        assert not deleting.done()  # the later delete waits behind the same SQL operation
        if cancel_count == 2:
            writing.cancel()
            await asyncio.sleep(0)
            assert not deleting.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await writing
        await deleting
        await delayed[0]
        assert _draft(db) == []
    finally:
        await eng.stop()


async def test_cancelled_failed_draft_write_keeps_delta_for_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "cancelled-failure.db"
    eng = await _engine(
        monkeypatch, FakeWindowLLM(), db, outbox=True, window_max_turns=50, segment_max_turns=50
    )
    try:
        await _root(eng, 0, session_id="s", conversation_id="c")
        pipeline = _pipeline(eng)
        store = pipeline._store("a")
        win = store.windows["conversation:c"]
        turn = win.segmenter.open_turns[0]
        original = int(_draft(db)[0]["tokens"])
        turn.tokens = original + 1
        pipeline._draft_upsert("a", win, turn)
        assert pipeline._outbox is not None
        update = pipeline._outbox.update_draft
        entered = asyncio.Event()
        release = asyncio.Event()
        attempts = 0

        async def failed(*_args: object, **_kwargs: object) -> int:
            nonlocal attempts
            attempts += 1
            entered.set()
            await release.wait()
            raise OSError("injected SQL failure")

        monkeypatch.setattr(pipeline._outbox, "update_draft", failed)
        writing = asyncio.create_task(pipeline._persist_draft("a", store))
        await entered.wait()
        writing.cancel()
        release.set()
        with pytest.raises(OSError, match="injected SQL failure"):
            await writing
        assert attempts == 1  # cancellation did not start a replacement SQL operation
        assert int(_draft(db)[0]["tokens"]) == original
        assert len(pipeline._draft_pending["a"]) == 1
        monkeypatch.setattr(pipeline._outbox, "update_draft", update)
        await pipeline._persist_draft("a", store)
        assert int(_draft(db)[0]["tokens"]) == original + 1
    finally:
        await eng.stop()


async def test_delete_then_reinsert_during_inflight_upsert_keeps_latest_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "reinsert-during-write.db"
    eng = await _engine(
        monkeypatch, FakeWindowLLM(), db, outbox=True, window_max_turns=50, segment_max_turns=50
    )
    try:
        await _root(eng, 0, session_id="s", conversation_id="c")
        pipeline = _pipeline(eng)
        store = pipeline._store("a")
        win = store.windows["conversation:c"]
        turn = win.segmenter.open_turns[0]
        baseline = int(_draft(db)[0]["tokens"])
        turn.tokens = baseline + 1
        pipeline._draft_upsert("a", win, turn)
        assert pipeline._outbox is not None
        original_update = pipeline._outbox.update_draft
        entered = asyncio.Event()
        release = asyncio.Event()

        async def paused(*args: object, **kwargs: object) -> int:
            entered.set()
            await release.wait()
            return int(await original_update(*args, **kwargs))

        with monkeypatch.context() as patch:
            patch.setattr(pipeline._outbox, "update_draft", paused)
            writing = asyncio.create_task(pipeline._persist_draft("a", store))
            await entered.wait()
            key = (win.key, turn.record_id)
            pipeline._draft_delete("a", key)
            turn.tokens = baseline + 2
            pipeline._draft_upsert("a", win, turn)
            release.set()
            await writing
        assert int(_draft(db)[0]["tokens"]) == baseline + 1
        await pipeline._persist_draft("a", store)
        assert int(_draft(db)[0]["tokens"]) == baseline + 2
        assert not pipeline._draft_pending["a"]
    finally:
        await eng.stop()
