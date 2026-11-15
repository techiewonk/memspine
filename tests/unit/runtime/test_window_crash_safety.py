"""RM-143: window crash-safety.

Each crash is injected deterministically: one patched outbox call raises at the step under test,
the engine stops (the "crash": nothing after the failed step runs) and a second engine over the
same database recovers. Nothing sleeps."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_outbox_windows import _engine
from test_window_write import FakeWindowLLM, _facts, _send, _turns

from memspine import Engine
from memspine.clients.sqlite.sync import sqlite_sync_engine
from memspine.config.schema import MemspineConfig
from memspine.core.window import Window, WindowSettings, WindowTurn
from memspine.derive.backpressure import Backpressure
from memspine.runtime.outbox import SqlJobStore
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.ingestion import AnnotationBatch
from memspine.services.llm.base import LLMRouter
from memspine.services.storage.outbox_tables import outbox_effect
from memspine.store.records import MemoryRecord, SourceInfo
from memspine.workers.windows import WINDOW_STAGE, WindowPipeline, _Job

T0 = datetime(2024, 3, 2, 10, tzinfo=UTC)
TEXTS = ("I adopted a greyhound named Juno.", "Juno loves the beach.")


def _fail_once(target: Any, name: str) -> list[int]:
    """Make ``target.<name>`` raise exactly once (the crash point); returns the call log."""
    real = getattr(target, name)
    calls: list[int] = []

    async def crash(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError(f"crash in {name}")
        return await real(*args, **kwargs)

    setattr(target, name, crash)
    return calls


async def _write(eng: Engine, i: int) -> MemoryRecord:
    return await eng.write(  # type: ignore[no-any-return]
        f"Mira: {TEXTS[i]}",
        namespace="a",
        memory_type="episodic",
        session_id="s1",
        valid_from=T0 + timedelta(minutes=i),
    )


@pytest.mark.parametrize("crash_at", ["update_draft", "mark_sources"])
async def test_a_crash_inside_observe_never_loses_the_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, crash_at: str
) -> None:
    """(a) The membership is published last. A crash before the draft is durable leaves no
    membership, so discovery replays the turn; a crash after it leaves the turn in the draft
    without membership, and discovery repairs only the membership."""
    db = tmp_path / f"{crash_at}.db"
    first = await _engine(monkeypatch, FakeWindowLLM(), db, outbox=True)
    assert first._windows is not None and first._windows._outbox is not None
    await _write(first, 0)
    calls = _fail_once(first._windows._outbox, crash_at)
    second_turn = await _write(first, 1)  # observe fails at the crash point (logged, not raised)
    assert calls, "the crash point was reached"
    scope = first._windows._scope("a")
    assert not first._windows._outbox.has_source(scope, WINDOW_STAGE, second_turn.record_id)
    await first.stop()  # the crash: nothing after the failed step runs

    second = await _engine(monkeypatch, FakeWindowLLM(), db, outbox=True)
    try:
        assert second._windows is not None
        await second.flush_windows()  # discovery first
        await second.flush_windows("a")
        assert len(await _facts(second)) == 2
        outbox = second._windows._outbox
        assert outbox.has_source(scope, WINDOW_STAGE, second_turn.record_id)
        repaired = second._windows._counts["membership_repaired"]
        assert repaired == (1 if crash_at == "mark_sources" else 0)
    finally:
        await second.stop()


async def test_a_transient_draft_load_error_keeps_the_open_turns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """(b) The namespace is marked restored only after the draft was read."""
    db = tmp_path / "draft.db"
    first = await _engine(monkeypatch, FakeWindowLLM(), db, outbox=True)
    await _send(first, "a", "s1", _turns("partial", 3))
    await first.stop()

    second = await _engine(monkeypatch, FakeWindowLLM(), db, outbox=True)
    try:
        assert second._windows is not None
        calls = _fail_once(second._windows._outbox, "load_draft")
        with pytest.raises(RuntimeError, match="crash in load_draft"):
            await second._windows.flush("a")
        assert "a" not in second._windows._draft_restored
        await second.flush_windows("a")  # the retry restores the three open turns
        assert len(calls) == 2
        assert len(await _facts(second)) == 3
    finally:
        await second.stop()


async def _rules_engine(
    monkeypatch: pytest.MonkeyPatch, path: Path, window_max_turns: int = 2
) -> Engine:
    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter({})

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    eng = Engine(
        template="core",
        dotenv_path=None,
        deployment={"id": "test-deployment"},  # RM-155: shared LLM
        storage={
            "records": {"provider": "sqlite", "settings": {"path": str(path), "busy_timeout_s": 5}}
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={
            "online_extractor": True,
            "window_extract": "facts",
            "window_async": False,
            "window_max_turns": window_max_turns,
            "segment_max_turns": window_max_turns,
            "window_idle_s": 0.0,
        },
        workers={"outbox": True},
    )
    await eng.start()
    return eng


async def _rule_messages(eng: Engine) -> list[MemoryRecord]:
    return await eng.ingest_messages(  # type: ignore[no-any-return]
        [
            {
                "message_id": "m-1",
                "role": "user",
                "sender": {"id": "Mira"},
                "content": "I work for Acme.",
                "created_at": "2026-10-24T10:00:00+00:00",
            },
            {
                "message_id": "m-2",
                "role": "user",
                "sender": {"id": "Nora"},
                "content": "I live in Lisbon.",
                "created_at": "2026-10-24T10:01:00+00:00",
            },
        ],
        namespace="a",
        session_id="s1",
        conversation_id="s1",
    )


async def test_backpressure_cut_survives_restart_with_order_and_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "backpressure-restart.db"
    first = await _rules_engine(monkeypatch, db, window_max_turns=1)
    assert first._windows is not None and first._windows._outbox is not None
    first._windows._outbox.configure_scheduler(Backpressure(max_items=1, retry_after_s=60))
    records = await first.ingest_messages(
        [
            {
                "message_id": f"bp-{i}",
                "role": "user",
                "sender": {"id": "Mira"},
                "content": f"I work for Acme {i}.",
                "created_at": f"2026-10-24T10:0{i}:00+00:00",
            }
            for i in range(3)
        ],
        namespace="a",
        session_id="s1",
        conversation_id="s1",
    )
    scope = first._windows._scope("a")
    pending = first._windows._outbox.pending_items(scope, WINDOW_STAGE)
    assert len(pending) == 1
    assert pending[0][1]["record_ids"] == [records[0].record_id]
    assert first._windows._outbox.contains_record(scope, WINDOW_STAGE, records[1].record_id)
    assert len(first._windows._outbox.staged_items(scope, WINDOW_STAGE)) == 2
    await first.stop()

    second = await _rules_engine(monkeypatch, db, window_max_turns=1)
    try:
        assert second._windows is not None and second._windows._outbox is not None
        second._windows._outbox.configure_scheduler(Backpressure(max_items=1, retry_after_s=60))
        await second._windows.flush("a", final=False)
        items = second._windows._outbox.store.items_after(scope, WINDOW_STAGE, -1)
        assert [p["record_ids"] for _, p in items] == [[record.record_id] for record in records]
        assert items[1][1]["session"] == "s1"
        assert items[1][1]["conversation_id"] == "s1"
        assert all("_underived" not in payload for _, payload in items)
        assert second._windows._outbox.store.items_after(scope, f"{WINDOW_STAGE}.staged", -1) == []
    finally:
        await second.stop()


async def test_staged_promotion_ack_loss_reconciles_one_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "promotion-ack.db"
    first = await _rules_engine(monkeypatch, db, window_max_turns=1)
    assert first._windows is not None and first._windows._outbox is not None
    runner = first._windows._outbox
    real_enqueue = runner.book.enqueue
    failed = False

    def lose_ack(*args: Any, **kwargs: Any) -> Any:
        nonlocal failed
        if args[1] == WINDOW_STAGE and not failed:
            failed = True
            raise RuntimeError("lost promotion acknowledgment")
        return real_enqueue(*args, **kwargs)

    monkeypatch.setattr(runner.book, "enqueue", lose_ack)
    [record] = await first.ingest_messages(
        [
            {
                "message_id": "ack-1",
                "role": "user",
                "sender": {"id": "Mira"},
                "content": "I work for Acme.",
                "created_at": "2026-10-24T10:00:00+00:00",
            }
        ],
        namespace="a",
        session_id="s1",
        conversation_id="s1",
    )
    assert failed
    scope = first._windows._scope("a")
    assert runner.store.last_offset(scope, WINDOW_STAGE) == 0
    assert runner.staged_items(scope, WINDOW_STAGE) == []
    await first.stop()

    second = await _rules_engine(monkeypatch, db, window_max_turns=1)
    try:
        assert second._windows is not None and second._windows._outbox is not None
        await second.flush_windows("a", final=False)
        assert second._windows._outbox.store.last_offset(scope, WINDOW_STAGE) == 0
        assert second._windows._outbox.has_source(scope, WINDOW_STAGE, record.record_id)
        assert second._windows._outbox.book.progress(scope, WINDOW_STAGE).watermark == 0
    finally:
        await second.stop()


async def test_forget_scrubs_deferred_cut_before_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "deferred-erase.db"
    first = await _rules_engine(monkeypatch, db, window_max_turns=1)
    assert first._windows is not None and first._windows._outbox is not None
    first._windows._outbox.configure_scheduler(Backpressure(max_items=1, retry_after_s=60))
    records = await first.ingest_messages(
        [
            {
                "message_id": f"erase-{i}",
                "role": "user",
                "sender": {"id": "Mira"},
                "content": f"I work for Acme {i}.",
                "created_at": f"2026-10-24T10:0{i}:00+00:00",
            }
            for i in range(2)
        ],
        namespace="a",
        session_id="s1",
        conversation_id="s1",
    )
    scope = first._windows._scope("a")
    assert len(first._windows._outbox.staged_items(scope, WINDOW_STAGE)) == 1
    await first.forget(records[1].record_id, namespace="a", hard=True)
    assert all(
        records[1].record_id not in payload.get("record_ids", ())
        and records[1].record_id
        not in [source["record_id"] for source in payload.get("source_snapshots", ())]
        for _, payload in first._windows._outbox.store.items_after(
            scope, f"{WINDOW_STAGE}.staged", -1
        )
    )
    await first.stop()
    second = await _rules_engine(monkeypatch, db, window_max_turns=1)
    try:
        await second.flush_windows("a", final=False)
        assert second._windows is not None and second._windows._outbox is not None
        assert all(
            records[1].record_id not in payload.get("record_ids", ())
            for _, payload in second._windows._outbox.store.items_after(scope, WINDOW_STAGE, -1)
        )
    finally:
        await second.stop()


async def test_cut_handoff_survives_draft_delete_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "cut-handoff.db"
    first = await _rules_engine(monkeypatch, db, window_max_turns=2)
    assert first._windows is not None and first._windows._outbox is not None
    [one] = await first.ingest_messages(
        [
            {
                "message_id": "cut-1",
                "role": "user",
                "sender": {"id": "Mira"},
                "content": "I work for Acme.",
                "created_at": "2026-10-24T10:00:00+00:00",
            }
        ],
        namespace="a",
        session_id="s1",
        conversation_id="s1",
    )
    _fail_once(first._windows._outbox, "update_draft")
    [two] = await first.ingest_messages(
        [
            {
                "message_id": "cut-2",
                "role": "user",
                "sender": {"id": "Mira"},
                "content": "I live in Lisbon.",
                "created_at": "2026-10-24T10:01:00+00:00",
            }
        ],
        namespace="a",
        session_id="s1",
        conversation_id="s1",
    )
    scope = first._windows._scope("a")
    assert first._windows._outbox.store.last_offset(scope, WINDOW_STAGE) == 0
    assert first._windows._outbox.store.draft_has_record(scope, WINDOW_STAGE, one.record_id)
    assert first._windows._outbox.store.prune_promoted_staged(scope, WINDOW_STAGE, 0) == 0
    await first.stop()

    second = await _rules_engine(monkeypatch, db, window_max_turns=2)
    try:
        assert second._windows is not None and second._windows._outbox is not None
        await second.flush_windows("a", final=False)
        items = second._windows._outbox.store.items_after(scope, WINDOW_STAGE, -1)
        assert len(items) == 1
        assert items[0][1]["record_ids"] == [one.record_id, two.record_id]
        assert second._windows._outbox.book.progress(scope, WINDOW_STAGE).watermark == 0
    finally:
        await second.stop()


async def test_closed_session_marker_waits_behind_deferred_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = tmp_path / "deferred-close.db"
    first = await _rules_engine(monkeypatch, db, window_max_turns=1)
    assert first._windows is not None and first._windows._outbox is not None
    first._windows._outbox.configure_scheduler(Backpressure(max_items=1, retry_after_s=60))
    records = await first.ingest_messages(
        [
            {
                "message_id": f"close-{i}",
                "role": "user",
                "sender": {"id": "Mira"},
                "content": f"I work for Acme {i}.",
                "created_at": f"2026-10-24T10:0{i}:00+00:00",
            }
            for i in range(2)
        ],
        namespace="a",
        session_id="s1",
        conversation_id="s1",
    )
    first._windows._enqueue_closed("a", first._windows._store("a"), "s1")
    await first._windows._persist_staged()
    scope = first._windows._scope("a")
    assert [p["kind"] for _, p in first._windows._outbox.staged_items(scope, WINDOW_STAGE)] == [
        "window",
        "closed",
    ]
    await first.stop()

    second = await _rules_engine(monkeypatch, db, window_max_turns=1)
    try:
        assert second._windows is not None and second._windows._outbox is not None
        second._windows._outbox.configure_scheduler(Backpressure(max_items=1, retry_after_s=60))
        await second.flush_windows("a", final=False)
        items = second._windows._outbox.store.items_after(scope, WINDOW_STAGE, -1)
        assert [p["kind"] for _, p in items] == ["window", "window", "closed"]
        assert items[1][1]["record_ids"] == [records[1].record_id]
        assert items[2][1]["session"] == "s1"
        assert items[2][1]["windows"] == 2
        assert second._windows._counts["sessions_closed"] == 1
    finally:
        await second.stop()


async def test_erasure_before_membership_fences_later_staged_promotion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _rules_engine(monkeypatch, tmp_path / "staged-no-membership.db", window_max_turns=1)
    try:
        assert eng._windows is not None and eng._windows._outbox is not None
        runner = eng._windows._outbox
        scope = eng._windows._scope("a")
        payload = {
            "kind": "window",
            "_staging_id": "before-membership",
            "window_key": "session:s1",
            "session": "s1",
            "conversation_id": None,
            "reason": "turns",
            "record_ids": ["erased-source"],
            "source_snapshots": [
                {"record_id": "erased-source", "source_version": 1, "content_hash": "old"}
            ],
        }
        await runner.stage_items(scope, WINDOW_STAGE, [payload])
        assert not runner.has_source(scope, WINDOW_STAGE, "erased-source")
        assert await runner.erase_source(scope, WINDOW_STAGE, "erased-source") is False
        [(offset, staged)] = runner.staged_items(scope, WINDOW_STAGE)
        assert staged["record_ids"] == []
        assert staged["source_snapshots"] == []
        assert runner.store.erased_sources(scope, WINDOW_STAGE, ["erased-source"]) == [
            "erased-source"
        ]
        assert await runner.promote_staged(scope, WINDOW_STAGE, offset, staged) is False
        assert runner.store.last_offset(scope, WINDOW_STAGE) == -1
    finally:
        await eng.stop()


@pytest.mark.parametrize("handoff", ["stage", "promote"])
def test_erasure_serializes_with_staging_handoff_across_connections(
    tmp_path: Path, handoff: str
) -> None:
    """The fence begins before either handoff's first read. Erasure may commit before the
    handoff or after it, and neither order may retain an erased ID or hash."""
    db = str(tmp_path / f"{handoff}-erase-race.db")
    writer = SqlJobStore(sqlite_sync_engine(db, busy_timeout_s=10.0))
    eraser = SqlJobStore(sqlite_sync_engine(db, busy_timeout_s=10.0))
    scope = OwnerScope(tenant="t", owner="n")
    payload = {
        "kind": "window",
        "_staging_id": f"{handoff}-race",
        "window_key": "session:s1",
        "session": "s1",
        "conversation_id": None,
        "reason": "turns",
        "record_ids": ["private-source", "survivor"],
        "source_snapshots": [
            {"record_id": "private-source", "source_version": 1, "content_hash": "private-hash"},
            {"record_id": "survivor", "source_version": 1, "content_hash": "live-hash"},
        ],
    }
    writer.mark_sources(
        scope,
        WINDOW_STAGE,
        [{"record_id": "survivor", "source_version": 1, "content_hash": "live-hash"}],
        T0,
    )
    if handoff == "promote":
        writer.mark_sources(
            scope,
            WINDOW_STAGE,
            [{"record_id": "private-source", "source_version": 1, "content_hash": "private-hash"}],
            T0,
        )
        writer.stage_items(scope, WINDOW_STAGE, [payload], T0)
    paused = threading.Event()
    release = threading.Event()
    erasure_started = threading.Event()
    erasure_db_attempted = threading.Event()
    erasure_done = threading.Event()

    def pause_after_read(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        target = "outbox_item" if handoff == "stage" else "outbox_source_membership"
        if not statement.lstrip().upper().startswith("SELECT") or f"FROM {target}" not in statement:
            return
        if handoff == "promote" and "active" not in statement:
            return
        if not paused.is_set():
            paused.set()
            if not release.wait(8):
                raise TimeoutError("handoff was not released")

    event.listen(writer.engine, "after_cursor_execute", pause_after_read)

    def erase_begin(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.startswith("BEGIN IMMEDIATE"):
            erasure_db_attempted.set()

    event.listen(eraser.engine, "before_cursor_execute", erase_begin)

    def handoff_write() -> None:
        if handoff == "stage":
            writer.stage_items(scope, WINDOW_STAGE, [payload], T0)
        else:
            assert writer.promote_staged(scope, WINDOW_STAGE, 0, T0) == 0

    def erase() -> None:
        erasure_started.set()
        eraser.erase_source(scope, WINDOW_STAGE, "private-source")
        erasure_done.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            staged = pool.submit(handoff_write)
            assert paused.wait(5)
            erased = pool.submit(erase)
            assert erasure_started.wait(5)
            assert erasure_db_attempted.wait(5)
            assert not erasure_done.wait(0.1), "erasure crossed the handoff fence"
            release.set()
            staged.result(timeout=10)
            erased.result(timeout=10)
        staged_rows = writer.items_after(scope, f"{WINDOW_STAGE}.staged", -1)
        canonical_rows = writer.items_after(scope, WINDOW_STAGE, -1)
        assert [p["record_ids"] for _, p in staged_rows] == [["survivor"]]
        if handoff == "promote":
            assert [p["record_ids"] for _, p in canonical_rows] == [["survivor"]]
        assert "private-source" not in json.dumps((staged_rows, canonical_rows))
        assert "private-hash" not in json.dumps((staged_rows, canonical_rows))
        assert writer.erased_sources(scope, WINDOW_STAGE, ["private-source"]) == ["private-source"]
    finally:
        release.set()
        event.remove(writer.engine, "after_cursor_execute", pause_after_read)
        event.remove(eraser.engine, "before_cursor_execute", erase_begin)
        writer.close()
        eraser.close()


@pytest.mark.parametrize("first_actor", ["mark", "erase"])
def test_absent_membership_mark_and_erase_serialize_across_connections(
    tmp_path: Path, first_actor: str
) -> None:
    """Both insert paths must observe one committed predecessor when membership is absent.
    A staged copy proves the erasure also completed its scrub after either ordering."""
    db = str(tmp_path / f"mark-erase-{first_actor}.db")
    marker = SqlJobStore(sqlite_sync_engine(db, busy_timeout_s=10.0))
    eraser = SqlJobStore(sqlite_sync_engine(db, busy_timeout_s=10.0))
    scope = OwnerScope(tenant="t", owner="n")
    source = {"record_id": "erased-source", "source_version": 1, "content_hash": "erased-hash"}
    marker.stage_items(
        scope,
        WINDOW_STAGE,
        [
            {
                "kind": "window",
                "_staging_id": "cut-1",
                "record_ids": ["erased-source"],
                "source_snapshots": [source],
            }
        ],
        T0,
    )
    first = marker if first_actor == "mark" else eraser
    second = eraser if first_actor == "mark" else marker
    paused = threading.Event()
    release = threading.Event()
    second_db_attempted = threading.Event()
    second_done = threading.Event()

    def pause_after_missing_read(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "FROM outbox_source_membership" in statement
            and not paused.is_set()
        ):
            paused.set()
            if not release.wait(8):
                raise TimeoutError("membership read was not released")

    def second_begin(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.startswith("BEGIN IMMEDIATE"):
            second_db_attempted.set()

    event.listen(first.engine, "after_cursor_execute", pause_after_missing_read)
    event.listen(second.engine, "before_cursor_execute", second_begin)

    def mark() -> list[str]:
        return marker.mark_sources(scope, WINDOW_STAGE, [source], T0)

    def erase() -> bool:
        return eraser.erase_source(scope, WINDOW_STAGE, "erased-source")

    def second_action() -> list[str] | bool:
        result = erase() if first_actor == "mark" else mark()
        second_done.set()
        return result

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(mark if first_actor == "mark" else erase)
            assert paused.wait(5)
            second_future = pool.submit(second_action)
            assert second_db_attempted.wait(5)
            assert not second_done.wait(0.1), "second insertion crossed the first read fence"
            release.set()
            first_result = first_future.result(timeout=10)
            second_result = second_future.result(timeout=10)
        if first_actor == "mark":
            assert first_result == [] and second_result is True
        else:
            assert first_result is False and second_result == ["erased-source"]
        assert marker.erased_sources(scope, WINDOW_STAGE, ["erased-source"]) == ["erased-source"]
        staged = marker.items_after(scope, f"{WINDOW_STAGE}.staged", -1)
        assert staged[0][1]["record_ids"] == []
        assert "erased-source" not in json.dumps(staged)
        assert "erased-hash" not in json.dumps(staged)
    finally:
        release.set()
        event.remove(first.engine, "after_cursor_execute", pause_after_missing_read)
        event.remove(second.engine, "before_cursor_execute", second_begin)
        marker.close()
        eraser.close()


async def test_cancelled_staging_retains_the_cut_for_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _rules_engine(monkeypatch, tmp_path / "cancel-staging.db", window_max_turns=1)
    try:
        assert eng._windows is not None and eng._windows._outbox is not None
        pipeline = eng._windows
        runner = pipeline._outbox
        pipeline._enqueue_closed("a", pipeline._store("a"), "s1")
        started = asyncio.Event()
        release = asyncio.Event()
        real_stage = runner.stage_items

        async def blocked(*args: Any, **kwargs: Any) -> None:
            started.set()
            await release.wait()
            await real_stage(*args, **kwargs)

        monkeypatch.setattr(runner, "stage_items", blocked)
        task = asyncio.create_task(pipeline._persist_staged())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert [p["kind"] for _, p in pipeline._staged] == ["closed"]
        release.set()
        monkeypatch.setattr(runner, "stage_items", real_stage)
        await pipeline._persist_staged()
        scope = pipeline._scope("a")
        items = runner.store.items_after(scope, WINDOW_STAGE, -1)
        assert [p["kind"] for _, p in items] == ["closed"]
        await pipeline._persist_staged()
        assert runner.store.last_offset(scope, WINDOW_STAGE) == 0
    finally:
        await eng.stop()


async def test_an_excluded_turn_gets_an_empty_batch_not_a_permanent_hole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """(c) A turn that is in the window item but not a live input (quarantined after it was
    observed) is part of the effect identity, so it gets an explicit empty annotation batch.
    The offset completes once: no hole, no repeated deposit on later drains."""
    eng = await _rules_engine(monkeypatch, tmp_path / "rules.db")
    try:
        pipeline = eng._windows
        assert pipeline is not None and pipeline._outbox is not None
        records = await _rule_messages(eng)
        blocked = records[1].record_id
        real_get = pipeline._get

        async def get(record_id: str) -> MemoryRecord | None:
            record = await real_get(record_id)
            if record is not None and record_id == blocked:
                return record.model_copy(update={"quarantined": True})
            return record

        pipeline._get = get
        deposits: list[str] = []
        real_deposit = pipeline._deposit_fact
        assert real_deposit is not None

        async def deposit(*args: Any, **kwargs: Any) -> object:
            deposits.append(args[1])
            return await real_deposit(*args, **kwargs)

        pipeline._deposit_fact = deposit
        for _ in range(3):  # later drains retry nothing
            await eng.flush_windows("a")
        assert deposits == ["Mira employed_by Acme"]
        progress = pipeline._outbox.book.progress(pipeline._scope("a"), WINDOW_STAGE)
        assert progress.holes == []
        [annotations] = [
            row for row in pipeline.derivation_effect_status("a") if row["layer"] == "annotations"
        ]
        assert annotations["state"] == "complete"
        batches = [
            AnnotationBatch.model_validate(b) for b in annotations["artifact"]["annotation_batches"]
        ]
        by_source = {b.source.record_id: b for b in batches}
        assert set(by_source) == {r.record_id for r in records}
        assert by_source[blocked].mentions == ()
        counts = {
            row["offset"]: row["count"]
            for row in pipeline.derivation_effect_status("a")
            if row["layer"] == "facts"
        }
        assert counts[annotations["offset"]] == 1  # the window's offset (the next is the close)
    finally:
        await eng.stop()


async def test_forgetting_one_completed_window_source_rederives_survivors_after_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A committed multi-source window keeps its live fact and unresolved annotation durable
    when one source is erased. The rederive item survives restart and never restores erased text."""
    from memspine.ingest.windows import unresolved_annotations

    db = tmp_path / "survivor-rederive.db"
    eng = await _rules_engine(monkeypatch, db, window_max_turns=3)
    records = await eng.ingest_messages(
        [
            {
                "message_id": "m-1",
                "role": "user",
                "sender": {"id": "Mira"},
                "content": "I work for Acme.",
                "created_at": "2026-10-24T10:00:00+00:00",
            },
            {
                "message_id": "m-2",
                "role": "user",
                "sender": {"id": "Nora"},
                "content": "I live in Lisbon.",
                "created_at": "2026-10-24T10:01:00+00:00",
            },
            {
                "message_id": "m-3",
                "role": "user",
                "sender": {"id": "Kai"},
                "content": "I grew up in Vrbnikovo.",
                "created_at": "2026-10-24T10:02:00+00:00",
            },
        ],
        namespace="a",
        session_id="s1",
        conversation_id="s1",
    )
    erased, fact_source, unresolved_source = records
    try:
        await eng.flush_windows("a")
        assert any(f.source.parents == [fact_source.record_id] for f in await _facts(eng))
        annotations = unresolved_annotations(eng, "a")
        assert any(
            row.get("source", {}).get("record_id") == unresolved_source.record_id
            for row in annotations
        ), annotations
        assert eng._windows is not None and eng._windows._outbox is not None
        scope = eng._windows._scope("a")
        erased_source_hash = eng._windows._outbox.source_snapshots(
            scope, WINDOW_STAGE, [erased.record_id]
        )[0]["content_hash"]
        fact_source_hash = eng._windows._outbox.source_snapshots(
            scope, WINDOW_STAGE, [fact_source.record_id]
        )[0]["content_hash"]
        watermark = eng._windows._outbox.book.progress(scope, WINDOW_STAGE).watermark
        eng._windows._outbox.store.prune_items(scope, WINDOW_STAGE, watermark)

        # Simulate a process stop at the SQL erasure boundary, before on_forget's worker cleanup.
        assert eng._windows._outbox.store.erase_source(scope, WINDOW_STAGE, erased.record_id)
        durable_items = eng._windows._outbox.store.items_after(scope, WINDOW_STAGE, -1)
        assert all(
            erased.record_id not in str(payload) and erased_source_hash not in str(payload)
            for _, payload in durable_items
        )
        await eng.forget(erased.record_id, namespace="a", hard=True)
        assert unresolved_annotations(eng, "a") == []  # effect artifacts were erased atomically
        assert not any("Acme" in fact.content for fact in await _facts(eng))
        assert eng._windows is not None
        assert eng._windows._outbox is not None
        pending = eng._windows._outbox.pending_items(scope, WINDOW_STAGE)
        assert len(pending) == 1
        assert pending[0][1]["record_ids"] == [fact_source.record_id, unresolved_source.record_id]
        assert pending[0][1]["window_key"] == "conversation:s1"
        assert pending[0][1]["session"] == "s1"
        assert pending[0][1]["conversation_id"] == "s1"
        assert (
            len(
                eng._windows._outbox.source_snapshots(
                    scope, WINDOW_STAGE, pending[0][1]["record_ids"]
                )
            )
            == 2
        )
    finally:
        await eng.stop()

    restarted = await _rules_engine(monkeypatch, db, window_max_turns=3)
    try:
        await restarted.flush_windows("a")
        assert restarted._windows is not None and restarted._windows._outbox is not None
        assert (
            restarted._windows._outbox.pending_items(restarted._windows._scope("a"), WINDOW_STAGE)
            == []
        )
        facts = await _facts(restarted)
        assert any(f.source.parents == [fact_source.record_id] for f in facts)
        assert not any("Acme" in fact.content for fact in facts)
        annotations = unresolved_annotations(restarted, "a")
        assert any(
            row.get("source", {}).get("record_id") == unresolved_source.record_id
            for row in annotations
        )
        assert not any("Acme" in str(row) for row in annotations)

        # If every final source is then erased, the survivor item is stripped down to a no-op.
        await restarted.forget(fact_source.record_id, namespace="a", hard=True)
        assert restarted._windows is not None and restarted._windows._outbox is not None
        retained_payloads = restarted._windows._outbox.store.items_after(
            restarted._windows._scope("a"), WINDOW_STAGE, -1
        )
        assert all(
            fact_source.record_id not in str(payload) and fact_source_hash not in str(payload)
            for _, payload in retained_payloads
        )
        await restarted.forget(unresolved_source.record_id, namespace="a", hard=True)
        assert not any(await _facts(restarted))
        assert unresolved_annotations(restarted, "a") == []
        assert restarted._windows is not None and restarted._windows._outbox is not None
        remaining = restarted._windows._outbox.pending_items(
            restarted._windows._scope("a"), WINDOW_STAGE
        )
        assert not any(
            payload.get("kind") == "window" and payload.get("record_ids")
            for _, payload in remaining
        )
    finally:
        await restarted.stop()


async def test_erasure_commits_and_keeps_survivors_underived_when_context_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing compacted context never blocks erasure or silently changes session semantics."""
    eng = await _rules_engine(monkeypatch, tmp_path / "missing-context.db")
    try:
        records = await _rule_messages(eng)
        await eng.flush_windows("a")
        pipeline = eng._windows
        assert pipeline is not None and pipeline._outbox is not None
        scope = pipeline._scope("a")
        source_id = records[0].record_id
        rows = [
            row
            for row in pipeline.derivation_effect_status("a")
            if any(s.get("record_id") == source_id for s in row["identity"].get("sources", []))
        ]
        offsets = {int(row["offset"]) for row in rows}
        assert offsets
        with pipeline._outbox.store.engine.begin() as connection:
            connection.execute(
                update(outbox_effect)
                .where(
                    outbox_effect.c.tenant == scope.tenant,
                    outbox_effect.c.owner == scope.owner,
                    outbox_effect.c.owner_kind == scope.owner_kind,
                    outbox_effect.c.stage == WINDOW_STAGE,
                    outbox_effect.c.item_offset.in_(offsets),
                )
                .values(artifact=None)
            )
        watermark = pipeline._outbox.book.progress(scope, WINDOW_STAGE).watermark
        pipeline._outbox.store.prune_items(scope, WINDOW_STAGE, watermark)

        await eng.forget(source_id, namespace="a", hard=True)
        pending = pipeline._outbox.pending_items(scope, WINDOW_STAGE)
        [(_offset, payload)] = [
            item for item in pending if item[1].get("kind") == "window_context_missing"
        ]
        assert payload["record_ids"] == [records[1].record_id]
        assert not pipeline._outbox.has_source(scope, WINDOW_STAGE, source_id)

        await eng.flush_windows("a")
        progress = pipeline._outbox.book.progress(scope, WINDOW_STAGE)
        assert any(hole.reason == "raised" for hole in progress.holes)
        assert not any(
            row.get("kind") == "window" and row.get("record_ids")
            for _, row in pipeline._outbox.pending_items(scope, WINDOW_STAGE)
        )
    finally:
        await eng.stop()


def _record(content: str, sender: str) -> MemoryRecord:
    return MemoryRecord(
        namespace="a",
        memory_type="episodic",
        content=content,
        source=SourceInfo(role="user", channel="messages", message={"sender": {"id": sender}}),
    )


def _rules_pipeline(deposit: Any) -> WindowPipeline:
    async def extract(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("rules only: no model call")

    async def get_record(record_id: str) -> None:
        return None

    return WindowPipeline(
        settings=WindowSettings(),
        sections=("facts",),
        extractor=extract,  # type: ignore[arg-type]
        get_record=get_record,
        deposit_fact=deposit,
        asynchronous=False,
        rule_only=True,
    )


@pytest.mark.parametrize("with_door", [False, True])
async def test_the_reported_fact_count_is_the_stored_count(with_door: bool) -> None:
    """(d) The effect result counts the facts the commit stores, not the compiled ones."""
    stored: list[str] = []

    async def deposit(namespace: str, text: str, *args: Any, **kwargs: Any) -> object:
        stored.append(text)
        return object()

    pipeline = _rules_pipeline(deposit if with_door else None)
    record = _record("I work for Acme.", "Mira")
    window = Window([WindowTurn(record_id=record.record_id, text="", tokens=1)], "flush")
    job = _Job("a", window=window, members=frozenset(window.record_ids))
    prepared = await pipeline._prepare_records("a", job, [record], strict=True)
    assert prepared is not None
    reported = pipeline._outbox_effect_results(("window", prepared))["facts"]["count"]
    await pipeline._commit_job("a", pipeline._store("a"), prepared)
    assert reported == len(stored) == (1 if with_door else 0)


async def test_a_forget_during_the_commit_leaves_no_fact_of_the_fenced_projection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RM-141 compensate hook, through the real engine: a turn of the window is forgotten
    while the window's facts are being deposited. The fence refuses the projection after the
    commit, the compensation removes the facts already deposited, and the retry derives the
    window again from the remaining turn only."""
    eng = await _engine(monkeypatch, FakeWindowLLM(), tmp_path / "comp.db", outbox=True)
    try:
        pipeline = eng._windows
        assert pipeline is not None and pipeline._deposit_fact is not None
        await _send(eng, "a", "s1", _turns("trip", 2))
        turns = sorted(
            await eng.retrieve(namespace="a", memory_type="episodic"), key=lambda r: r.valid_from
        )
        kept, erased = turns
        real_deposit = pipeline._deposit_fact
        deposited: list[Any] = []

        async def deposit(*args: Any, **kwargs: Any) -> object:
            stored = await real_deposit(*args, **kwargs)
            deposited.append(stored)
            if len(deposited) == 1:  # the first fact is in; the erasure lands mid-commit
                await eng.forget(erased.record_id, namespace="a", hard=True)
            return stored

        pipeline._deposit_fact = deposit
        await eng.flush_windows("a")
        assert deposited[0] is not None  # the kept turn's fact was projected ...
        assert await _facts(eng) == []  # ... and compensated when the fence refused it
        assert pipeline._counts["windows_compensated"] == 1
        await eng.flush_windows("a")  # the retry: the rewritten item holds the kept turn only
        facts = await _facts(eng)
        assert [f.source.parents for f in facts] == [[kept.record_id]]
        erased_text = erased.content.split(": ", 1)[1]
        assert not any(erased_text in f.content for f in facts)
    finally:
        await eng.stop()
