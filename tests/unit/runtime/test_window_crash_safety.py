"""RM-143: window crash-safety.

Each crash is injected deterministically: one patched outbox call raises at the step under test,
the engine stops (the "crash": nothing after the failed step runs) and a second engine over the
same database recovers. Nothing sleeps."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_outbox_windows import _engine
from test_window_write import FakeWindowLLM, _facts, _send, _turns

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.core.window import Window, WindowSettings, WindowTurn
from memspine.schemas.durable.ingestion import AnnotationBatch
from memspine.services.llm.base import LLMRouter
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


@pytest.mark.parametrize("crash_at", ["save_draft", "mark_sources"])
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


async def _rules_engine(monkeypatch: pytest.MonkeyPatch, path: Path) -> Engine:
    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter({})

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {"provider": "sqlite", "settings": {"path": str(path), "busy_timeout_s": 5}}
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={
            "online_extractor": True,
            "window_extract": "facts",
            "window_async": False,
            "window_max_turns": 2,
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
