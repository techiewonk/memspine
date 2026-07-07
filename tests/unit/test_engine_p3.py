"""Engine P3 surface: timeline/sessions/ingest verbs, sleep cycle, runners."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from memspine import Engine
from memspine.exceptions import ConfigError, MemspineError, MissingServiceError


@pytest.fixture
async def engine() -> AsyncIterator[Engine]:
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"resource": {"enabled": True}},
    )
    await eng.start()
    yield eng
    await eng.stop()


async def test_timeline_and_sessions_verbs(engine: Engine) -> None:
    for i in range(3):
        await engine.write(f"episode {i}", namespace="agent/a", memory_type="episodic")
    timeline = await engine.timeline(namespace="agent/a")
    assert [r.content for r in timeline] == ["episode 0", "episode 1", "episode 2"]

    sessions = await engine.sessions(namespace="agent/a")
    assert len(sessions) == 1 and sessions[0].size == 3


async def test_ingest_creates_provenance_tagged_chunks(engine: Engine, tmp_path: Path) -> None:
    doc = tmp_path / "guide.md"
    doc.write_text("First part.\n\nSecond part.", encoding="utf-8")
    records = await engine.ingest(doc, namespace="agent/a")
    assert records
    stored = await engine.retrieve(namespace="agent/a", memory_type="resource")
    assert len(stored) == len(records)
    assert all(r.source.channel == "ingest" for r in stored)
    assert all(r.source.doc_path == str(doc) for r in stored)
    assert stored[0].source.message_id == "chunk:0"  # E1 taint trail to the file


async def test_sleep_cycle_runs_all_stages_in_order(engine: Engine) -> None:
    stats = await engine.sleep()
    assert list(stats) == [
        "consolidate",
        "reorganize",  # D-42 optional stage (P6): skipped without a graph
        "check_watches",  # M13.8 read-only fired-count report (P7/ADR-016)
        "decay_sweep",
        "compress",
        "sleep_compute",
        "event_log_prune",
    ]
    assert stats["consolidate"]["status"] == "ok"
    assert stats["reorganize"]["status"] == "skipped"
    assert stats["check_watches"]["status"] == "ok"
    assert stats["decay_sweep"]["status"] == "ok"
    assert stats["compress"]["status"] == "ok"
    assert stats["sleep_compute"] == {"status": "noop", "hook": "E7"}
    assert stats["event_log_prune"]["status"] == "skipped"  # mode=full


async def test_describe_reports_p3_surface(engine: Engine) -> None:
    world = engine.describe()
    assert world["episodic"] is True
    assert world["resource_ingest"] is True
    assert world["consolidation_summarizer"] == "extractive"  # no summarize role
    assert world["runner"] == "inline"


async def test_verbs_fail_loudly_when_type_disabled() -> None:
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": False}},
    )
    await eng.start()
    try:
        with pytest.raises(MemspineError, match="episodic"):
            await eng.timeline()
        with pytest.raises(MemspineError, match="resource"):
            await eng.ingest("nothing.txt")
    finally:
        await eng.stop()


async def test_aged_lifecycle_end_to_end_through_the_engine(engine: Engine) -> None:
    """The engine-wiring gap the pipeline harness can't see: aged records must
    decay + compress through Engine.sleep(), stay retrievable (inflated) via
    search/retrieve, and survive a real Engine.rebuild() with both projectors."""
    from datetime import UTC, datetime, timedelta

    from memspine.core.events import EventKind, MemoryEvent
    from memspine.core.records import MemoryRecord

    original = "the shinagawa office badge code was rotated in march " * 5
    moment = datetime.now(UTC) - timedelta(days=200)
    aged = MemoryRecord(
        namespace="agent/a",
        memory_type="episodic",
        content=original,
        valid_from=moment,
        recorded_at=moment,
    )
    await engine._append_and_project(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace="agent/a",
            actor="test",
            payload={"record": aged.model_dump(mode="json")},
        )
    )

    stats = await engine.sleep()
    assert stats["decay_sweep"] == {"status": "ok", "transitions": 1}
    assert stats["compress"] == {"status": "ok", "compressed": 1}

    # At rest: compressed. Through every read verb: the original text.
    raw = await engine._require_started().get_record(aged.record_id)
    assert raw is not None and raw.content == "" and raw.content_zstd is not None
    [got] = await engine.retrieve(namespace="agent/a", memory_type="episodic")
    assert got.content == original
    scored = await engine.search("shinagawa badge code", namespace="agent/a")
    assert scored and scored[0][0].content == original
    [timed] = await engine.timeline(namespace="agent/a")
    assert timed.content == original

    # Real rebuild (both projectors) over a log containing DECAY_TRANSITIONs.
    counts = await engine.rebuild()
    assert counts["records"] >= 1 and counts["vectors"] >= 1
    [after] = await engine.retrieve(namespace="agent/a", memory_type="episodic")
    assert after.content == original
    raw_after = await engine._require_started().get_record(aged.record_id)
    assert raw_after is not None and raw_after.tier == "dormant"  # lifecycle survived replay


async def test_reingest_after_partial_failure_skips_landed_chunks(
    engine: Engine, tmp_path: Path
) -> None:
    doc = tmp_path / "guide.md"
    doc.write_text("First part.\n\nSecond part.", encoding="utf-8")
    first = await engine.ingest(doc, namespace="agent/a")
    again = await engine.ingest(doc, namespace="agent/a")  # e.g. retry after crash
    assert first and again == []  # nothing duplicated
    stored = await engine.retrieve(namespace="agent/a", memory_type="resource")
    assert len(stored) == len(first)


async def test_ingest_empty_document_is_a_loud_noop(engine: Engine, tmp_path: Path) -> None:
    doc = tmp_path / "empty.md"
    doc.write_text("   \n\n  ", encoding="utf-8")
    records = await engine.ingest(doc, namespace="agent/a")
    assert records == []
    assert await engine.retrieve(namespace="agent/a", memory_type="resource") == []


async def test_sessions_gap_override_and_timeline_window(engine: Engine) -> None:
    from datetime import UTC, datetime, timedelta

    from memspine.core.events import EventKind, MemoryEvent
    from memspine.core.records import MemoryRecord

    now = datetime.now(UTC)
    for minutes_ago in (30, 20, 5):
        moment = now - timedelta(minutes=minutes_ago)
        record = MemoryRecord(
            namespace="agent/w",
            memory_type="episodic",
            content=f"e{minutes_ago}",
            valid_from=moment,
            recorded_at=moment,
        )
        await engine._append_and_project(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="agent/w",
                actor="test",
                payload={"record": record.model_dump(mode="json")},
            )
        )
    assert len(await engine.sessions(namespace="agent/w", gap_minutes=60)) == 1
    assert len(await engine.sessions(namespace="agent/w", gap_minutes=8)) == 3
    windowed = await engine.timeline(
        namespace="agent/w", start=now - timedelta(minutes=25), end=now
    )
    assert [record.content for record in windowed] == ["e20", "e5"]


async def test_unknown_runner_is_config_error() -> None:
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        workers={"runner": "quantum"},
    )
    with pytest.raises(ConfigError, match=r"workers\.runner"):
        await eng.start()


async def test_dbos_runner_names_its_extra_when_missing() -> None:
    try:
        import dbos  # noqa: F401

        pytest.skip("dbos installed — MissingServiceError path not reachable")
    except ImportError:
        pass
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        workers={"runner": "dbos"},
    )
    with pytest.raises(MissingServiceError, match=r"memspine\[dbos\]"):
        await eng.start()
