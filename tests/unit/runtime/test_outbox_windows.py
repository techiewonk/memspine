"""F-04: the window pipeline over the durable outbox (``workers.outbox``), with the fake window LLM.

Flag off is the old in-memory queue, byte for byte; flag on queues rows, survives a stop, never
holds the namespace lock across the model call, and answers the read barrier."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_window_write import FakeWindowLLM, _facts, _send, _turns

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.ingest import windows as ingest_windows
from memspine.models.providers.llm.base import LLMRouter


async def _engine(
    monkeypatch: pytest.MonkeyPatch,
    llm: FakeWindowLLM,
    path: Path,
    *,
    outbox: bool,
    **write: Any,
) -> Engine:
    async def router(config: MemspineConfig, hooks: object = None) -> LLMRouter:
        return LLMRouter({"window_extract": llm})  # type: ignore[dict-item]

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": str(path)},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={"window_extract": "facts", "window_async": False, "window_idle_s": 0.0, **write},
        llm={"roles": {"window_extract": {"model": "fake/window"}}},
        workers={"outbox": outbox},
    )
    await eng.start()
    return eng


def _rows(path: Path, table: str) -> int:
    con = sqlite3.connect(path)
    try:
        return int(con.execute(f"select count(*) from {table}").fetchone()[0])
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


def _contents(facts: list[Any]) -> list[str]:
    return sorted(re.sub(r"[0-9a-f]{12}$", "", f.content) for f in facts)


def test_the_flag_is_off_by_default() -> None:
    assert MemspineConfig().workers.outbox is False


async def test_flag_off_keeps_the_in_memory_queue_and_writes_no_outbox_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "off.db"
    eng = await _engine(monkeypatch, FakeWindowLLM(), db, outbox=False)
    try:
        await _send(eng, "a", "s1", _turns("trip", 8))
        assert eng._windows is not None and eng._windows._outbox is None
        assert eng.window_stats()["queued"] == 1 and "outbox" not in eng.window_stats()
        await eng.flush_windows()
        assert len(await _facts(eng)) == 8
        assert await ingest_windows.readiness(eng, "a") is None
    finally:
        await eng.stop()
    assert _rows(db, "outbox") == 0 and _rows(db, "outbox_item") == 0


async def test_flag_on_gives_the_same_facts_as_flag_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run(outbox: bool) -> tuple[list[str], dict[str, Any]]:
        eng = await _engine(monkeypatch, FakeWindowLLM(), tmp_path / f"{outbox}.db", outbox=outbox)
        try:
            for ns in ("a", "b"):
                await _send(eng, ns, "s1", _turns("trip", 9))
                await _send(eng, ns, "s2", _turns("walk", 4))
            stats = await eng.flush_windows()
            return _contents(await _facts(eng, "a") + await _facts(eng, "b")), stats
        finally:
            await eng.stop()

    off_facts, off_stats = await run(False)
    on_facts, on_stats = await run(True)
    assert off_facts and off_facts == on_facts
    for key in ("windows", "calls", "facts_deposited", "sessions_closed", "windows_processed"):
        assert off_stats[key] == on_stats[key], key
    assert on_stats["outbox"] is True and on_stats["queued"] == 0


async def test_queued_windows_survive_a_stop_and_run_after_the_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "persist.db"
    first = await _engine(monkeypatch, FakeWindowLLM(), db, outbox=True)
    await _send(first, "a", "s1", _turns("trip", 8))  # batch mode: windows wait in the outbox
    assert first.window_stats()["queued"] >= 1
    assert await _facts(first) == []
    await first.stop()  # the known defect: this used to throw the queue away
    assert _rows(db, "outbox_item") >= 1

    llm = FakeWindowLLM()
    second = await _engine(monkeypatch, llm, db, outbox=True)
    try:
        stats = await second.flush_windows()  # leftovers drain
        assert stats["calls"] >= 1 and len(await _facts(second)) >= 6
        assert stats["queued"] == 0
    finally:
        await second.stop()


async def test_no_namespace_lock_is_held_while_the_window_call_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = FakeWindowLLM()
    llm.block = asyncio.Event()
    eng = await _engine(monkeypatch, llm, tmp_path / "lock.db", outbox=True)
    try:
        await _send(eng, "a", "s1", _turns("trip", 6))
        flushing = asyncio.create_task(eng.flush_windows())
        await asyncio.wait_for(llm.entered.wait(), 10)
        assert not eng._namespace_lock("a").locked()  # the model call is in flight, lock free
        async with asyncio.timeout(5):  # a foreground write is not queued behind the call
            await _send(eng, "a", "s2", _turns("other", 1))
        llm.block.set()
        await flushing
        assert len(await _facts(eng)) >= 6
    finally:
        llm.block.set()
        await eng.stop()


async def test_forget_removes_a_turn_from_a_queued_outbox_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    llm = FakeWindowLLM()
    eng = await _engine(monkeypatch, llm, tmp_path / "forget.db", outbox=True)
    try:
        await _send(eng, "a", "s1", _turns("trip", 8))
        turns = sorted(
            await eng.retrieve(namespace="a", memory_type="episodic"), key=lambda r: r.valid_from
        )
        await eng.forget(turns[1].record_id, namespace="a", hard=True)
        erased = turns[1].content.split(": ", 1)[1]
        stats = await eng.flush_windows()
        assert stats["turns_dropped_forget"] >= 1
        assert not any(erased in t for t in llm.transcripts)  # never sent to the model
        con = sqlite3.connect(tmp_path / "forget.db")
        payloads = " ".join(r[0] for r in con.execute("select payload from outbox_item"))
        con.close()
        assert turns[1].record_id not in payloads
    finally:
        await eng.stop()


async def test_readiness_is_partial_until_the_queue_is_drained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _engine(monkeypatch, FakeWindowLLM(), tmp_path / "ready.db", outbox=True)
    try:
        await _send(eng, "a", "s1", _turns("trip", 8))
        early = await ingest_windows.readiness(eng, "a", ("window", "graph"), timeout=0)
        assert early.status == "partial" and early.pending == ("window",)
        assert early.unavailable == ("graph",)
        done = await ingest_windows.readiness(eng, "a", ("window",), timeout=30)  # drains inline
        assert done.status == "ready" and len(await _facts(eng)) >= 6
    finally:
        await eng.stop()


async def test_async_mode_drains_after_each_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _engine(
        monkeypatch, FakeWindowLLM(), tmp_path / "async.db", outbox=True, window_async=True
    )
    try:
        await _send(eng, "a", "s1", _turns("trip", 7))  # one full window closes by size
        for _ in range(200):
            if len(await _facts(eng)) >= 6:
                break
            await asyncio.sleep(0.05)
        assert len(await _facts(eng)) >= 6
        out = await ingest_windows.readiness(eng, "a", ("window",), timeout=10)
        assert out.status == "ready"
    finally:
        await eng.stop()


async def test_concurrent_writes_and_drains_never_stall_on_the_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The outbox writes on worker threads: foreground writes (which hold SQLite's write lock
    across awaits) and the drain interleave without waiting out a busy timeout."""
    eng = await _engine(
        monkeypatch, FakeWindowLLM(), tmp_path / "busy.db", outbox=True, window_async=True
    )
    try:
        async with asyncio.timeout(60):
            await asyncio.gather(
                *(_send(eng, ns, f"s{i}", _turns(f"t{ns}{i}", 7)) for ns in "abc" for i in range(3))
            )
            await eng.flush_windows()
        assert eng.window_stats()["queued"] == 0
        assert sum([len(await _facts(eng, ns)) for ns in "abc"]) >= 3 * 3
    finally:
        await eng.stop()
