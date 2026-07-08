"""The named runner test (structure plan §6): dbos crash-resume.

The sleep-cycle pipelines (consolidate / decay / compress / reorganize /
check_watches) are idempotent step functions (D-17). The property under test is
that a cycle torn apart mid-way — the process crashes after *some* steps have
committed but before the rest run — and then re-run against the persisted log
neither double-applies (no duplicate consolidation summary, no double decay)
nor leaves inconsistent durable state.

Two layers, each labelled with what it proves:

* ``test_inline_crash_resume_is_idempotent`` — ALWAYS runs. It drives the
  idempotency property directly: run ONE step (consolidate) against a
  file-backed engine, ``stop()`` (the "crash": the remaining steps never ran),
  reopen a fresh engine on the same file, run the FULL cycle, and assert the
  summary is not duplicated and decay is not double-applied. This is the
  property DBOS durability is supposed to preserve — asserted on the runner
  that ships in every core install.

* ``test_dbos_durable_resume`` — runs only when ``[dbos]`` is installed
  (``pytest.importorskip("dbos")``); otherwise it SKIPS with a reason. It
  repeats the same crash/resume scenario under ``workers.runner="dbos"`` and
  additionally asserts the durable-runner-specific surface (the engine reports
  the dbos runner and the same idempotent outcome).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.workers.pipelines import consolidate

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


async def test_dbos_durable_resume(make_file_engine: Any) -> None:
    """DURABLE LAYER (skips without ``[dbos]``): the same crash/resume scenario
    under the DBOS runner, plus the durable-runner-specific surface."""
    pytest.importorskip("dbos", reason="dbos extra not installed — skipping durable-resume layer")

    engine = make_file_engine(**_CONFIG, workers={"runner": "dbos"})
    await engine.start()
    try:
        assert engine.describe()["runner"] == "dbos"
        await _seed_closed_session(engine)
        partial = await engine._runner.run("consolidate", engine._pipeline_ctx())  # type: ignore[union-attr]
        assert partial == {"status": "ok", "summaries": 1, "superseded": 0}
    finally:
        await engine.stop()  # crash mid-cycle

    resumed = make_file_engine(**_CONFIG, workers={"runner": "dbos"})
    await resumed.start()
    try:
        assert resumed.describe()["runner"] == "dbos"
        cycle = await resumed.sleep()
        assert cycle["consolidate"]["summaries"] == 0  # durable re-run: no duplicate
        assert len(await _consolidation_summaries(resumed)) == 1
        again = await resumed.sleep()
        assert again["consolidate"]["summaries"] == 0
        assert int(again["decay_sweep"].get("transitions", 0)) == 0
    finally:
        await resumed.stop()
