"""RM-163: the window draft is persisted incrementally, and assigned ids never open windows.

* Each write upserts only its own turn's draft row (no vectors), so the draft work of one
  write is constant in rows and bytes however many windows are open (1 to 64 here).
* A message whose conversation and session were assigned by message identity (not given by
  the caller) joins the per-namespace default window (ADR-090), instead of opening a one-turn
  window of its own that only the idle timer closes."""

from __future__ import annotations

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
