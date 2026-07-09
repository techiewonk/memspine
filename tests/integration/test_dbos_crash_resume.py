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
  additionally proves the DBOS-specific surface: the pipeline invocation is
  genuinely checkpointed in DBOS's own system database — a SUCCESS row with
  the exact returned stats exists after ``run()`` returns (via
  ``DBOS.list_workflows``) — not merely executed inline behind a warning log
  (the old seam's behavior). A true mid-pipeline process kill is not
  simulated here: the workflow body has no internal ``@DBOS.step()``
  checkpoints (deliberately — pipelines must never import ``dbos``, D-17), so
  the only two points a "crash" can tear at are before the invocation starts
  (recovery re-runs it — already covered by the idempotent
  stop()/reopen()/resume() sequence below, the same property
  ``test_inline_crash_resume_is_idempotent`` proves for the default runner)
  or after it fully completes (irrelevant to durability). Racing a real OS
  thread to kill it strictly *between* those points would be flaky without
  step-level checkpoints to pause on.

  Note: this test calls the SYNC ``DBOS.list_workflows`` (off the event loop,
  via ``asyncio.to_thread``) rather than ``list_workflows_async``. The async
  variants call DBOS's internal ``_configure_asyncio_thread_pool()``, which
  repoints the CURRENT event loop's default executor at DBOS's own
  (``loop.set_default_executor(dbos_instance._executor)``) — and
  ``DBOS.destroy()`` later shuts that executor down. Since this test runs two
  engines on the same event loop (pytest-asyncio's per-function loop), that
  leaves the loop's default executor dead for the second engine's own
  ``asyncio.to_thread`` calls (its SQLite schema setup) with a confusing
  "cannot schedule new futures after shutdown" — a sharp, DBOS-SDK-level edge
  worth knowing about if this module's assertions are ever extended.
"""

from __future__ import annotations

import asyncio
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
    dbos = pytest.importorskip(
        "dbos", reason="dbos extra not installed — skipping durable-resume layer"
    )

    engine = make_file_engine(**_CONFIG, workers={"runner": "dbos"})
    await engine.start()
    try:
        assert engine.describe()["runner"] == "dbos"
        await _seed_closed_session(engine)
        partial = await engine._runner.run("consolidate", engine._pipeline_ctx())  # type: ignore[union-attr]
        assert partial == {"status": "ok", "summaries": 1, "superseded": 0}

        # PROOF this went through a real DBOS workflow, not the old fake-inline
        # seam: the invocation is durably recorded SUCCESS with the exact
        # stats `run()` returned to us — DBOS's own checkpoint, not memspine's.
        # (SYNC list_workflows off-thread — see module docstring note on why
        # the async variant is unsafe to call here.)
        completed = await asyncio.to_thread(dbos.DBOS.list_workflows, name="memspine.run_pipeline")
        successes = [wf for wf in completed if wf.status == "SUCCESS" and wf.output == partial]
        assert successes, f"no checkpointed SUCCESS workflow matched {partial!r}: {completed!r}"
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

        # No PENDING workflow leaked across the stop()/reopen() boundary: the
        # crashed cycle's consolidate() had already reached SUCCESS before the
        # "crash", so recovery had nothing outstanding to re-run.
        pending = await asyncio.to_thread(
            dbos.DBOS.list_workflows, name="memspine.run_pipeline", status="PENDING"
        )
        assert pending == []
    finally:
        await resumed.stop()
