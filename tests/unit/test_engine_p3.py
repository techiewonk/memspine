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
        "decay_sweep",
        "compress",
        "sleep_compute",
        "event_log_prune",
    ]
    assert stats["consolidate"]["status"] == "ok"
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
