"""check_watches sleep step (M13.8/ADR-016): read-only fired-count reporting.

Same harness shape as test_lifecycle_pipelines: real SQLiteStorage +
RecordProjector behind an append-and-project callable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine.clients.sqlite import SQLiteClient
from memspine.config.loader import load_config
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord
from memspine.memories.prospective.watches import make_watch_record
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.workers.pipelines import PipelineContext, check_watches

NOW = datetime.now(UTC)


class Harness:
    def __init__(self, storage: SQLiteStorage, projector: RecordProjector) -> None:
        self.storage = storage
        self.projector = projector

    async def append(self, event: MemoryEvent) -> None:
        appended = await self.storage.append_event(event)
        assert appended.seq is not None
        await self.projector.apply(appended)
        await self.storage.set_offset(self.projector.name, appended.seq)


async def make_ctx() -> tuple[PipelineContext, Harness]:
    client = SQLiteClient(":memory:")
    await client.connect()
    storage = SQLiteStorage(client)
    await storage.start()
    harness = Harness(storage, RecordProjector(storage))
    resolved = load_config()
    ctx = PipelineContext(storage=storage, config=resolved.config, append_event=harness.append)
    return ctx, harness


async def write(harness: Harness, record: MemoryRecord) -> None:
    await harness.append(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace=record.namespace,
            actor="test",
            payload={"record": record.model_dump(mode="json")},
        )
    )


async def test_check_watches_reports_zero_when_nothing_fires() -> None:
    ctx, harness = await make_ctx()
    await write(harness, make_watch_record("agent/a", "later", due_at=NOW + timedelta(days=1)))
    stats = await check_watches(ctx)
    assert stats == {"status": "ok", "fired": 0, "due": 0, "invalidated": 0}


async def test_check_watches_counts_due_and_invalidated() -> None:
    ctx, harness = await make_ctx()
    watch_at = NOW - timedelta(hours=2)
    await write(harness, make_watch_record("agent/a", "overdue", due_at=NOW - timedelta(hours=1)))
    target = make_watch_record("agent/a", "recheck", entity="alice", attribute="city").model_copy(
        update={"recorded_at": watch_at}
    )
    await write(harness, target)
    # A truth-changing M4 conflict on the watched key, after the watch existed.
    await harness.append(
        MemoryEvent(
            kind=EventKind.CONFLICT,
            namespace="agent/a",
            actor="system",
            ts=NOW - timedelta(minutes=10),
            payload={"verdict": "update", "action": "updated", "fact_key": ["alice", "city"]},
        )
    )
    stats = await check_watches(ctx)
    assert stats["status"] == "ok"
    assert stats["fired"] == 2
    assert stats["due"] == 1
    assert stats["invalidated"] == 1


async def test_check_watches_is_read_only_and_idempotent() -> None:
    ctx, harness = await make_ctx()
    await write(harness, make_watch_record("agent/a", "overdue", due_at=NOW - timedelta(hours=1)))
    events_before = len(await ctx.storage.read_events())
    first = await check_watches(ctx)
    second = await check_watches(ctx)
    assert first == second  # nothing mutated: pull-based delivery (ADR-016)
    assert len(await ctx.storage.read_events()) == events_before
