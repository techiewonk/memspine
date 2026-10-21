"""Engine persistence integration coverage for durable local step execution.
This verifies engine-level idempotence across a partial cycle and process reopen.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.derive.pipeline.stages.consolidate import consolidate
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus

# episodic + semantic: consolidation reads closed episodic sessions and writes
# semantic summaries. Both enabled so the engine is a realistic lifecycle host.
_CONFIG: dict[str, Any] = {
    "memories": {"episodic": {"enabled": True}, "semantic": {"enabled": True}}
}

#: Deterministic instants. The session sits far in the past (months before any
#: wall clock this test runs at), so it is unambiguously a CLOSED session the
#: consolidator will summarize, and the ancient record is old enough to decay.
_SESSION_START = datetime(2025, 1, 1, 9, 0, 0, tzinfo=UTC)
_ANCIENT = datetime(2019, 1, 1, 9, 0, 0, tzinfo=UTC)


async def _seed_episode(engine: Engine, content: str, when: datetime) -> MemoryRecord:
    """Append a past-dated episodic record through the engine's real write door
    (``write()`` stamps ``now``; consolidation needs a closed, past session)."""
    record = MemoryRecord(
        namespace="default",
        memory_type="episodic",
        content=content,
        valid_from=when,
        recorded_at=when,
    )
    await engine._append_and_project(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace="default",
            actor="test",
            payload={"record": record.model_dump(mode="json")},
        )
    )
    return record


async def _seed_closed_session(engine: Engine) -> None:
    # Three episodes minutes apart = one closed session (summarizable), plus one
    # ancient record that the decay sweep will demote exactly once.
    await _seed_episode(engine, "alice arrived. detail", _SESSION_START)
    await _seed_episode(engine, "bob spoke. detail", _SESSION_START + timedelta(minutes=2))
    await _seed_episode(engine, "deal closed. detail", _SESSION_START + timedelta(minutes=4))
    await _seed_episode(engine, "ancient chatter. detail", _ANCIENT)


async def _consolidation_summaries(engine: Engine) -> list[MemoryRecord]:
    storage = engine._storage
    assert storage is not None
    return [
        record
        for record in await storage.list_records("default", "semantic")
        if record.source.channel == "consolidation" and record.status is RecordStatus.ACTIVATED
    ]


async def test_inline_crash_resume_is_idempotent(make_file_engine: Any) -> None:
    """PROPERTY (inline, always runs): a cycle that crashes after consolidate
    but before decay, then re-runs fully against the persisted log, produces
    exactly one summary and applies each decay transition exactly once."""
    # ── run ONE step, then "crash" (stop before the rest of the cycle) ────────
    engine = make_file_engine(**_CONFIG)
    await engine.start()
    try:
        await _seed_closed_session(engine)
        partial = await consolidate(engine._pipeline_ctx())
        assert partial == {"status": "ok", "summaries": 1, "superseded": 0}
        assert len(await _consolidation_summaries(engine)) == 1
    finally:
        await engine.stop()  # crash: decay/compress/prune for this cycle never ran

    # ── resume: fresh engine on the SAME file, run the FULL cycle ─────────────
    resumed = make_file_engine(**_CONFIG)
    await resumed.start()
    try:
        # The persisted summary WRITE replays on start; consolidate must now be a
        # no-op (its session key already exists) — the crash did not cost, nor
        # duplicate, the summary.
        assert len(await _consolidation_summaries(resumed)) == 1

        first_cycle = await resumed.sleep()
        assert first_cycle["consolidate"]["summaries"] == 0  # not re-created
        assert first_cycle["decay_sweep"]["status"] in ("ok", "partial")
        first_transitions = int(first_cycle["decay_sweep"].get("transitions", 0))
        assert first_transitions > 0  # the ancient record (at least) decayed
        assert len(await _consolidation_summaries(resumed)) == 1  # still exactly one

        # Running the whole cycle AGAIN changes nothing (full idempotence): no
        # second summary, no double decay.
        second_cycle = await resumed.sleep()
        assert second_cycle["consolidate"]["summaries"] == 0
        assert int(second_cycle["decay_sweep"].get("transitions", 0)) == 0
        assert len(await _consolidation_summaries(resumed)) == 1
    finally:
        await resumed.stop()


async def test_temporal_engine_cycle_persists_across_engine_reopen(
    make_file_engine: Any, tmp_path: Path
) -> None:
    """Exercise the configured Engine→Temporal runner against a real server.

    The event database and application journal remain at the same file paths
    across fresh Engine instances. The gate is opt-in because Temporal is an
    external service; conftest preserves the MEMSPINE_TEST_* namespace.
    """
    address = os.environ.get("MEMSPINE_TEST_TEMPORAL_ADDRESS")
    if not address:
        pytest.skip("set MEMSPINE_TEST_TEMPORAL_ADDRESS for the live Engine/Temporal gate")
    pytest.importorskip("temporalio", reason="Temporal SDK extra is not installed")

    journal_path = tmp_path / "engine-temporal-journal.sqlite"
    deployment_id = f"engine-integration-{uuid.uuid4().hex}"
    workers = {
        "runner": "temporal",
        "temporal": {
            "address": address,
            "namespace": os.environ.get("MEMSPINE_TEST_TEMPORAL_NAMESPACE", "default"),
            "task_queue": f"memspine-engine-test-{uuid.uuid4().hex}",
            "deployment_id": deployment_id,
            "rpc_timeout_s": 30,
            "workflow_timeout_s": 240,
            "activity_timeout_s": 60,
            "max_concurrent_activities": 1,
            "retry_max_attempts": 2,
        },
        "resonate": {
            "journal_path": str(journal_path),
            "task_timeout_s": 20,
            "step_timeout_s": 15,
            "step_lease_s": 90,
        },
    }

    def engine_factory() -> Engine:
        return make_file_engine(**_CONFIG, workers=workers)

    first = engine_factory()
    try:
        await first.start()
        await _seed_closed_session(first)
        first_cycle = await first.sleep()
        assert first_cycle["consolidate"]["summaries"] == 1
        assert int(first_cycle["decay_sweep"].get("transitions", 0)) > 0
        assert len(await _consolidation_summaries(first)) == 1

        stable_cycle = await first.sleep()
        assert stable_cycle["consolidate"]["summaries"] == 0
        assert int(stable_cycle["decay_sweep"].get("transitions", 0)) == 0
        assert len(await _consolidation_summaries(first)) == 1
    finally:
        await first.stop()

    resumed = engine_factory()
    try:
        await resumed.start()
        assert len(await _consolidation_summaries(resumed)) == 1
        resumed_cycle = await resumed.sleep()
        assert resumed_cycle["consolidate"]["summaries"] == 0
        assert int(resumed_cycle["decay_sweep"].get("transitions", 0)) == 0
        assert len(await _consolidation_summaries(resumed)) == 1

        storage = resumed._storage
        assert storage is not None
        episodic = await storage.list_records("default", "episodic")
        semantic = await storage.list_records("default", "semantic")
        ids = [record.record_id for record in [*episodic, *semantic]]
        assert len(ids) == len(set(ids))
        assert journal_path.is_file()
    finally:
        await resumed.stop()


async def test_activity_environment_reuses_committed_micro_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("temporalio", reason="Temporal SDK extra is not installed")
    pytest.importorskip("resonate", reason="Resonate SDK extra is not installed")
    from docs.examples.temporal_resonate.activities import run_micro, verify_result
    from docs.examples.temporal_resonate.contracts import Job
    from temporalio.testing import ActivityEnvironment

    journal_path = tmp_path / "local-resonate.sqlite"
    monkeypatch.setenv("MEMSPINE_MICRO_JOURNAL_PATH", str(journal_path))
    monkeypatch.setenv("MEMSPINE_DEPLOYMENT", "integration-test")
    # The local runtime must remain embedded even if the ambient URL selects a server.
    monkeypatch.setenv("RESONATE_URL", "http://127.0.0.1:1")
    environment = ActivityEnvironment()
    job = Job("synthetic-tenant", "synthetic-operation", [" ALPHA ", "beta", "ALPHA", ""])

    result = await environment.run(run_micro, job)
    verified = await environment.run(verify_result, job, result)
    retried = await environment.run(run_micro, job)

    assert result.items == ["alpha", "beta"]
    assert verified == result
    assert retried == result
    with closing(sqlite3.connect(journal_path)) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM micro_steps WHERE state = 'COMPLETED'"
            ).fetchone()[0]
            == 3
        )
