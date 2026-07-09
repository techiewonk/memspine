"""Runner parity: the same sleep-cycle work → the same materialized result
under every available runner (D-16).

``inline`` always participates. ``taskiq`` participates against ``fakeredis``
(a dev dependency — the same seam the taskiq unit test drives, so the extra
itself need not be installed). ``dbos`` participates only when ``[dbos]`` is
installed (``pytest.importorskip``), else it is silently absent from the
comparison — never a failure.

Each runner gets its OWN file-backed database seeded with identical data, so the
comparison is of independent runs of the same idempotent pipelines, not of
shared state. Parity is asserted on both the stats-dict shape (keys ==
SLEEP_CYCLE_ORDER, every stage ok/noop/skipped) and the materialized effect
(records consolidated + records decayed).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memspine.clients.sqlite import SQLiteClient
from memspine.config.loader import load_config
from memspine.core.events import EventKind, EventLogMode, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.workers.inline import InlineRunner
from memspine.workers.pipelines import PIPELINES, PipelineContext
from memspine.workers.runner import TaskRunner
from memspine.workers.schedule import SLEEP_CYCLE_ORDER, run_sleep_cycle

_OK = {"ok", "noop", "skipped"}
_SESSION_START = datetime(2025, 1, 1, 9, 0, 0, tzinfo=UTC)
_ANCIENT = datetime(2019, 1, 1, 9, 0, 0, tzinfo=UTC)


async def _build_ctx(path: str) -> tuple[PipelineContext, SQLiteStorage, SQLiteClient]:
    """A file-backed pipeline context with a real RecordProjector behind the
    append-and-project door — the shape the engine injects, materialized to disk."""
    client = SQLiteClient(path)
    await client.connect()
    storage = SQLiteStorage(client, mode=EventLogMode.FULL)
    await storage.start()
    projector = RecordProjector(storage)

    async def append(event: MemoryEvent) -> None:
        appended = await storage.append_event(event)
        assert appended.seq is not None
        await projector.apply(appended)
        await storage.set_offset(projector.name, appended.seq)

    config = load_config().config
    return PipelineContext(storage=storage, config=config, append_event=append), storage, client


async def _seed(ctx: PipelineContext) -> None:
    async def episode(content: str, when: datetime) -> None:
        record = MemoryRecord(
            namespace="default",
            memory_type="episodic",
            content=content,
            valid_from=when,
            recorded_at=when,
        )
        assert ctx.append_event is not None
        await ctx.append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="default",
                actor="test",
                payload={"record": record.model_dump(mode="json")},
            )
        )

    await episode("alice arrived. detail", _SESSION_START)
    await episode("bob spoke. detail", _SESSION_START + timedelta(minutes=2))
    await episode("deal closed. detail", _SESSION_START + timedelta(minutes=4))
    await episode("ancient chatter. detail", _ANCIENT)


async def _summary_count(storage: SQLiteStorage) -> int:
    records = await storage.list_records("default", "semantic")
    return sum(
        1
        for record in records
        if record.source.channel == "consolidation" and record.status is RecordStatus.ACTIVATED
    )


def _available_runners(tmp_path: Path) -> dict[str, TaskRunner]:
    runners: dict[str, TaskRunner] = {"inline": InlineRunner()}
    try:
        import fakeredis.aioredis

        from memspine.workers.taskiq_runner import TaskiqRunner

        runners["taskiq"] = TaskiqRunner(redis=fakeredis.aioredis.FakeRedis())
    except ImportError:  # pragma: no cover - fakeredis is a dev dependency
        pass
    try:
        import dbos  # noqa: F401

        from memspine.workers.dbos_runner import DBOSRunner, default_system_database_url

        # No PipelineContext exists yet at this point — this test builds one
        # PER RUNNER NAME below, unlike the engine, which builds DBOSRunner
        # AFTER its own storage/config already exist (see engine.py's
        # `_build_runner`). `context_factory` is reassigned once the real
        # `ctx` is built, just before `run_sleep_cycle` needs it.
        runners["dbos"] = DBOSRunner(
            system_database_url=default_system_database_url(str(tmp_path / "dbos.db")),
        )
    except ImportError:  # pragma: no cover - env-dependent
        pass
    return runners


async def test_sleep_cycle_parity_across_available_runners(tmp_path: Path) -> None:
    runners = _available_runners(tmp_path)
    effects: dict[str, tuple[int, int]] = {}

    for name, runner in runners.items():
        for pipeline_name, pipeline in PIPELINES.items():
            runner.register(pipeline_name, pipeline)
        ctx, storage, client = await _build_ctx(str(tmp_path / f"{name}.db"))
        if hasattr(runner, "_context_factory"):  # dbos: bind the context built above
            runner._context_factory = lambda ctx=ctx: ctx  # type: ignore[attr-defined]
            # Pipelines are registered (above) BEFORE launch(), same ordering
            # `Engine._build_runner` follows — see dbos_runner's docstring on
            # why launch() must never race an empty pipeline registry.
            runner.launch()  # type: ignore[attr-defined]
        try:
            await _seed(ctx)
            stats = await run_sleep_cycle(runner, ctx)

            # Stats-dict shape parity: one entry per ordered stage, all healthy.
            assert set(stats) == set(SLEEP_CYCLE_ORDER), name
            assert all(stage["status"] in _OK for stage in stats.values()), (name, stats)

            summaries = await _summary_count(storage)
            transitions = int(stats["decay_sweep"].get("transitions", 0))
            assert summaries == 1, name  # the one closed session was consolidated
            assert transitions > 0, name  # the ancient record decayed
            effects[name] = (summaries, transitions)
        finally:
            await runner.close()
            await storage.stop()
            await client.close()

    # Effect parity: every available runner materialized the identical result.
    assert "inline" in effects
    baseline = effects["inline"]
    for name, effect in effects.items():
        assert effect == baseline, f"{name} diverged from inline: {effect} != {baseline}"

    if len(effects) == 1:  # only inline available in this env — record why
        pytest.skip("only the inline runner is available (no fakeredis, no dbos) — parity trivial")
