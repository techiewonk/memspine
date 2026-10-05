"""Hard forget reaches consolidation/reorganize summaries (also ones written
before they carried parents), verify walks descendants transitively, and the
vector table keeps no erased row in older versions (ADR-037)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, SourceInfo

SECRET = "SECRETWORD0"


def _engine(**extra: Any) -> Engine:
    config: dict[str, Any] = {
        "template": "core",
        "dotenv_path": None,
        "storage": {"path": ":memory:"},
        "embedding": {"provider": "hash"},
        "memories": {"episodic": {"enabled": True}, "semantic": {"enabled": True}},
    }
    config.update(extra)
    return Engine(**config)


@pytest.fixture
async def engine() -> AsyncIterator[Engine]:
    eng = _engine()
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


async def _closed_session(eng: Engine, ns: str = "a") -> list[MemoryRecord]:
    """Three episodic turns of one session that closed two hours ago."""
    start = datetime.now(UTC) - timedelta(hours=2)
    turns = [
        {"role": "user", "content": f"{SECRET} my locker code is 4471", "timestamp": start},
        {
            "role": "user",
            "content": "the gym is on Elm street",
            "timestamp": start + timedelta(minutes=1),
        },
        {
            "role": "user",
            "content": "I go there every Tuesday morning",
            "timestamp": start + timedelta(minutes=2),
        },
    ]
    return await eng.write_messages(turns, namespace=ns, session_id="s1")  # type: ignore[arg-type]


async def _summaries(eng: Engine, ns: str = "a") -> list[MemoryRecord]:
    storage = eng._require_started()
    return [
        r for r in await storage.list_records(ns, "semantic") if r.source.channel == "consolidation"
    ]


# ── summaries are descendants of their members ────────────────────────────


async def test_session_summary_names_its_members_as_parents(engine: Engine) -> None:
    turns = await _closed_session(engine)
    await engine.sleep()
    [summary] = await _summaries(engine)
    assert summary.source.parents == [t.record_id for t in turns]


async def test_hard_forget_of_a_member_erases_its_session_summary(engine: Engine) -> None:
    turns = await _closed_session(engine)
    await engine.sleep()
    [summary] = await _summaries(engine)
    assert SECRET in summary.content

    await engine.forget(turns[0].record_id, namespace="a", hard=True)

    storage = engine._require_started()
    assert await storage.get_record(summary.record_id) is None
    log = " ".join(str(e.payload) for e in await storage.read_events())
    assert SECRET not in log
    report = await engine.verify_forget(turns[0].record_id, namespace="a")
    assert report["clean"] is True


async def test_legacy_summary_without_parents_is_still_found(engine: Engine) -> None:
    """A summary written before the fix names its members only in the WRITE
    payload; erasure and its proof must still reach it."""
    turns = await _closed_session(engine)
    legacy = MemoryRecord(
        namespace="a",
        memory_type="semantic",
        content=f"summary: {SECRET} locker 4471",
        source=SourceInfo(role=constants.DERIVED_ROLE, channel="consolidation", message_id="k"),
    )
    await engine._append_and_project(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace="a",
            actor="system",
            payload={
                "record": legacy.model_dump(mode="json"),
                "consolidation": {
                    "session_key": "k",
                    "member_record_ids": [t.record_id for t in turns],
                },
            },
        )
    )
    # Before the erasure, verify reports the legacy summary as a remaining descendant.
    before = await engine.verify_forget(turns[0].record_id, namespace="a")
    assert legacy.record_id in before["descendants_remaining"]  # type: ignore[operator]

    await engine.forget(turns[0].record_id, namespace="a", hard=True)
    storage = engine._require_started()
    assert await storage.get_record(legacy.record_id) is None
    log = " ".join(str(e.payload) for e in await storage.read_events())
    assert SECRET not in log
    assert (await engine.verify_forget(turns[0].record_id, namespace="a"))["clean"] is True


async def test_verify_forget_walks_descendants_transitively(engine: Engine) -> None:
    root = await engine.write("root fact Quokka", namespace="a")
    child = await engine.write("child of root", namespace="a", derived_from=[root.record_id])
    grandchild = await engine.write(
        "grandchild of root", namespace="a", derived_from=[child.record_id]
    )
    # Erase root and child only (no cascade), leaving the grandchild behind.
    await engine.forget(root.record_id, namespace="a", hard=True, cascade=False)
    await engine.forget(child.record_id, namespace="a", hard=True, cascade=False)
    report = await engine.verify_forget(root.record_id, namespace="a")
    assert report["descendants_remaining"] == [grandchild.record_id]
    assert report["clean"] is False


# ── erased vectors leave no older table version behind ────────────────────


def _files_holding(directory: Path, needle: bytes) -> list[Path]:
    return [p for p in directory.rglob("*") if p.is_file() and needle in p.read_bytes()]


async def test_hard_forget_leaves_no_vector_in_older_versions(tmp_path: Path) -> None:
    eng = _engine(storage={"path": str(tmp_path / "m.db")})
    await eng.start()
    try:
        keep = await eng.write("the sky is teal today", namespace="a")
        gone = await eng.write(f"{SECRET} is my vault phrase", namespace="a")
        await eng.forget(gone.record_id, namespace="a", hard=True)
        report = await eng.verify_forget(gone.record_id, namespace="a")
        assert report["vector_history_absent"] is True
        assert report["clean"] is True
        assert not _files_holding(Path(f"{tmp_path / 'm.db'}.lance"), gone.record_id.encode())
        vector = eng._vector
        assert vector is not None
        assert await vector.exists(keep.record_id)  # type: ignore[attr-defined]
    finally:
        await eng.stop()


async def test_verify_forget_is_unproven_when_vector_history_was_not_purged(
    tmp_path: Path,
) -> None:
    eng = _engine(storage={"path": str(tmp_path / "m.db")})
    await eng.start()
    try:
        gone = await eng.write(f"{SECRET} is my vault phrase", namespace="a")
        vector = eng._vector
        assert vector is not None

        async def _no_purge() -> bool:
            return False

        vector.purge_deleted = _no_purge  # type: ignore[attr-defined]
        await eng.forget(gone.record_id, namespace="a", hard=True)
        report = await eng.verify_forget(gone.record_id, namespace="a")
        assert report["vector_absent"] is True
        assert report["vector_history_absent"] is not True
        assert report["clean"] is False
    finally:
        await eng.stop()
