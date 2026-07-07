"""Lifecycle pipelines (M2/M3/M6): consolidation, decay sweep, compression.

Harness: real SQLiteStorage (:memory:) + RecordProjector behind an
append-and-project callable — the same shape the engine injects, so events
written by pipelines materialize exactly as they would in production.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine.clients.sqlite import SQLiteClient
from memspine.config.loader import load_config
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.workers.pipelines import PipelineContext, compress, consolidate, decay_sweep

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


async def make_ctx(**config_overrides: object) -> tuple[PipelineContext, Harness]:
    client = SQLiteClient(":memory:")
    await client.connect()
    storage = SQLiteStorage(client)
    await storage.start()
    harness = Harness(storage, RecordProjector(storage))
    resolved = load_config(overrides=dict(config_overrides))
    ctx = PipelineContext(storage=storage, config=resolved.config, append_event=harness.append)
    return ctx, harness


def episode(content: str, minutes_ago: float, ns: str = "agent/a") -> MemoryRecord:
    moment = NOW - timedelta(minutes=minutes_ago)
    return MemoryRecord(
        namespace=ns,
        memory_type="episodic",
        content=content,
        valid_from=moment,
        recorded_at=moment,
    )


async def write(harness: Harness, record: MemoryRecord) -> None:
    await harness.append(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace=record.namespace,
            actor="test",
            payload={"record": record.model_dump(mode="json")},
        )
    )


# ── consolidation (M2) ──────────────────────────────────────────────────────


async def test_consolidate_summarizes_closed_sessions_idempotently() -> None:
    ctx, harness = await make_ctx()
    # A closed session (3 records, 2h ago) and an open one (5 min ago).
    for content, minutes in [
        ("alice arrived. x", 125),
        ("bob spoke. y", 122),
        ("deal closed. z", 120),
    ]:
        await write(harness, episode(content, minutes))
    await write(harness, episode("fresh chatter", 5))

    stats = await consolidate(ctx)
    assert stats == {"status": "ok", "summaries": 1}

    summaries = [
        record
        for record in await ctx.storage.list_records("agent/a", "semantic")
        if record.source.channel == "consolidation"
    ]
    assert len(summaries) == 1
    assert "alice arrived." in summaries[0].content  # extractive, deterministic
    assert summaries[0].source.message_id  # session key for idempotence

    events = await harness.storage.read_events()
    consolidate_events = [e for e in events if e.kind is EventKind.CONSOLIDATE]
    assert len(consolidate_events) == 1
    assert consolidate_events[0].payload["summarizer"] == "extractive"
    assert len(consolidate_events[0].payload["member_record_ids"]) == 3

    rerun = await consolidate(ctx)
    assert rerun == {"status": "ok", "summaries": 0}  # idempotent


async def test_consolidate_skips_small_and_open_sessions() -> None:
    ctx, harness = await make_ctx()
    await write(harness, episode("lonely event", 120))  # closed but too small
    for minutes in (10, 8, 6):  # big enough but still open
        await write(harness, episode(f"live {minutes}", minutes))
    stats = await consolidate(ctx)
    assert stats == {"status": "ok", "summaries": 0}


async def test_consolidate_uses_llm_summarizer_and_survives_its_failure() -> None:
    ctx, harness = await make_ctx()
    for minutes in (125, 122, 120):
        await write(harness, episode(f"event {minutes}. detail", minutes))

    async def failing_summarize(text: str) -> str:
        raise RuntimeError("llm down")

    ctx.summarize = failing_summarize
    stats = await consolidate(ctx)
    assert stats == {"status": "ok", "summaries": 1}  # extractive fallback (N6)
    events = await harness.storage.read_events()
    marker = next(e for e in events if e.kind is EventKind.CONSOLIDATE)
    assert marker.payload["summarizer"] == "llm"  # attempted; content fell back


async def test_consolidate_never_reconsolidates_summaries() -> None:
    """A summary is semantic and channel=consolidation — the next sweep must
    not treat it as episodic material or as a session member."""
    ctx, harness = await make_ctx()
    for minutes in (125, 122, 120):
        await write(harness, episode(f"event {minutes}. detail", minutes))
    await consolidate(ctx)
    stats = await consolidate(ctx)
    assert stats["summaries"] == 0


# ── decay sweep (M3) ────────────────────────────────────────────────────────


async def test_decay_sweep_transitions_and_is_idempotent() -> None:
    ctx, harness = await make_ctx()
    await write(harness, episode("ancient memory", minutes_ago=200 * 24 * 60))  # 200 days
    await write(harness, episode("fresh memory", minutes_ago=5))

    stats = await decay_sweep(ctx)
    assert stats == {"status": "ok", "transitions": 1}
    records = {r.content: r for r in await ctx.storage.list_records("agent/a", "episodic")}
    assert records["ancient memory"].tier == "dormant"
    assert records["fresh memory"].tier == "hot"

    rerun = await decay_sweep(ctx)
    assert rerun == {"status": "ok", "transitions": 0}  # no change, no events

    events = await harness.storage.read_events()
    decay_events = [e for e in events if e.kind is EventKind.DECAY_TRANSITION]
    assert len(decay_events) == 1
    assert decay_events[0].payload["transition"] == "hot->dormant"


async def test_decay_sweep_skips_deleted_and_quarantined() -> None:
    ctx, harness = await make_ctx()
    old = episode("deleted one", minutes_ago=200 * 24 * 60)
    await write(harness, old.model_copy(update={"status": RecordStatus.DELETED}))
    quarantined = episode("suspicious one", minutes_ago=200 * 24 * 60)
    await write(harness, quarantined.model_copy(update={"status": RecordStatus.QUARANTINED}))
    stats = await decay_sweep(ctx)
    assert stats == {"status": "ok", "transitions": 0}


# ── compression (M6/D-32) ───────────────────────────────────────────────────


async def test_decay_then_compress_then_rebuild_round_trips() -> None:
    ctx, harness = await make_ctx()
    content = "the full original text of a dormant memory " * 10
    await write(harness, episode(content, minutes_ago=200 * 24 * 60))

    await decay_sweep(ctx)
    stats = await compress(ctx)
    assert stats == {"status": "ok", "compressed": 1}

    [record] = await ctx.storage.list_records("agent/a", "episodic")
    assert record.content == "" and record.content_zstd is not None

    rerun = await compress(ctx)
    assert rerun == {"status": "ok", "compressed": 0}  # idempotent

    # Rebuild identity (D0.1): replaying the log reproduces the compressed row.
    before = record.model_dump(mode="json")
    await harness.storage.delete_all_records()
    for event in await harness.storage.read_events():
        await harness.projector.apply(event)
    [rebuilt] = await ctx.storage.list_records("agent/a", "episodic")
    assert rebuilt.model_dump(mode="json") == before


async def test_compress_respects_legal_hold() -> None:
    ctx, harness = await make_ctx(
        memories={
            "episodic": {
                "enabled": True,
                "policies": {"retention": {"legal_hold_namespaces": ["agent/a"]}},
            }
        }
    )
    await write(harness, episode("held evidence", minutes_ago=200 * 24 * 60))
    await decay_sweep(ctx)
    stats = await compress(ctx)
    assert stats == {"status": "ok", "compressed": 0}  # frozen representation


async def test_read_only_context_skips_mutating_pipelines() -> None:
    ctx, _ = await make_ctx()
    ctx.append_event = None
    for pipeline in (consolidate, decay_sweep, compress):
        stats = await pipeline(ctx)
        assert stats["status"] == "skipped"
