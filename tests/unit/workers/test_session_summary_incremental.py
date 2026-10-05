"""#56: incremental session summaries with a periodic full rebuild (ADR-048).

Same harness as ``test_lifecycle_pipelines``: real SQLite storage + projector behind
an append-and-project callable. Counting fake summarisers pin the call budget.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine.clients.sqlite import SQLiteClient
from memspine.config import constants
from memspine.config.loader import load_config
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.workers.pipelines import PipelineContext, SessionIndex, consolidate

NOW = datetime.now(UTC)
NS = "agent/a"


class Harness:
    def __init__(self, storage: SQLiteStorage, projector: RecordProjector) -> None:
        self.storage = storage
        self.projector = projector

    async def append(self, event: MemoryEvent) -> None:
        appended = await self.storage.append_event(event)
        assert appended.seq is not None
        await self.projector.apply(appended)
        await self.storage.set_offset(self.projector.name, appended.seq)


class Counter:
    """Counting fake LLM for the full and the incremental summary calls."""

    def __init__(self) -> None:
        self.full: list[str] = []
        self.incremental: list[tuple[str, str]] = []

    async def summarize(self, content: str) -> str:
        self.full.append(content)
        return f"FULL[{len(content.splitlines())}]"

    async def update(self, previous: str, content: str) -> str:
        self.incremental.append((previous, content))
        return f"{previous}+INC[{len(content.splitlines())}]"


async def make_ctx(
    policy: dict[str, object] | None, counter: Counter | None = None
) -> tuple[PipelineContext, Harness]:
    client = SQLiteClient(":memory:")
    await client.connect()
    storage = SQLiteStorage(client)
    await storage.start()
    harness = Harness(storage, RecordProjector(storage))
    overrides: dict[str, object] = {}
    if policy is not None:
        overrides = {"memories": {"episodic": {"policies": {"consolidation": policy}}}}
    resolved = load_config(overrides=overrides)
    ctx = PipelineContext(storage=storage, config=resolved.config, append_event=harness.append)
    if counter is not None:
        ctx.summarize = counter.summarize
        ctx.summarize_incremental = counter.update
    return ctx, harness


async def add_turn(harness: Harness, content: str, minutes_ago: float, trust: float = 0.9) -> str:
    moment = NOW - timedelta(minutes=minutes_ago)
    record = MemoryRecord(
        namespace=NS,
        memory_type="episodic",
        content=content,
        valid_from=moment,
        recorded_at=moment,
        trust=trust,
    )
    await harness.append(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace=NS,
            actor="test",
            payload={"record": record.model_dump(mode="json")},
        )
    )
    return record.record_id


async def active_summaries(ctx: PipelineContext) -> list[MemoryRecord]:
    return [
        r
        for r in await ctx.storage.list_records(NS, "semantic")
        if r.source.channel == "consolidation" and r.status is RecordStatus.ACTIVATED
    ]


def _policy(rebuild_every: int = 3) -> dict[str, object]:
    return {"session_summary": {"incremental": True, "rebuild_every": rebuild_every}}


async def test_off_by_default_open_sessions_are_not_summarised() -> None:
    counter = Counter()
    ctx, harness = await make_ctx(None, counter)
    for minute in (6, 5, 4):
        await add_turn(harness, f"turn {minute}", minute)
    assert await consolidate(ctx) == {"status": "ok", "summaries": 0, "superseded": 0}
    assert counter.full == [] and counter.incremental == []


async def test_incremental_updates_then_periodic_full_rebuild() -> None:
    counter = Counter()
    ctx, harness = await make_ctx(_policy(rebuild_every=5), counter)
    ids = [await add_turn(harness, f"turn {i}", 20 - i) for i in range(3)]
    # First summary of the (open) session: one full call over all three turns.
    await consolidate(ctx)
    assert len(counter.full) == 1 and counter.incremental == []
    [first] = await active_summaries(ctx)
    assert first.content == "FULL[3]" and set(first.source.parents) == set(ids)
    assert constants.SUMMARY_OPEN_TAG in first.tags
    # Nothing new: no call (idempotent).
    await consolidate(ctx)
    assert len(counter.full) == 1 and counter.incremental == []
    # One new turn: ONE incremental call carrying only the new turn.
    ids.append(await add_turn(harness, "turn 3", 16))
    stats = await consolidate(ctx)
    assert stats["summaries"] == 1 and stats["superseded"] == 1
    assert len(counter.full) == 1 and len(counter.incremental) == 1
    assert counter.incremental[0] == ("FULL[3]", "turn 3")
    # Two new turns in one batch: still one call.
    ids += [await add_turn(harness, f"turn {i}", 15 - i / 10) for i in (4, 5)]
    await consolidate(ctx)
    assert len(counter.incremental) == 2 and counter.incremental[1][1] == "turn 4\nturn 5"
    [current] = await active_summaries(ctx)
    assert current.content == "FULL[3]+INC[1]+INC[2]"
    assert f"{constants.SUMMARY_SINCE_REBUILD_PREFIX}3" in current.tags
    assert set(current.source.parents) == set(ids)  # parents = every member turn


async def test_rebuild_every_n_turns_exact_call_budget() -> None:
    counter = Counter()
    ctx, harness = await make_ctx(_policy(rebuild_every=3), counter)
    for i in range(3):
        await add_turn(harness, f"turn {i}", 30 - i)
    await consolidate(ctx)  # full #1
    modes = []
    for i in range(3, 9):  # one new turn per sweep, six sweeps
        await add_turn(harness, f"turn {i}", 30 - i)
        await consolidate(ctx)
        modes.append((len(counter.full), len(counter.incremental)))
    # since-rebuild: 1 inc, 2 inc, 3 -> rebuild, 1 inc, 2 inc, 3 -> rebuild
    assert modes == [(1, 1), (1, 2), (2, 2), (2, 3), (2, 4), (3, 4)]
    [current] = await active_summaries(ctx)
    assert current.content == "FULL[9]"  # the rebuild reads every turn
    assert len(current.source.parents) == 9
    events = await ctx.storage.read_events()
    consolidations = [e.payload for e in events if e.kind is EventKind.CONSOLIDATE]
    assert [p["summary_mode"] for p in consolidations] == [
        "rebuild",
        "incremental",
        "incremental",
        "rebuild",
        "incremental",
        "incremental",
        "rebuild",
    ]
    # Every version but the newest is superseded, with an evolve_to chain.
    summaries = [
        r
        for r in await ctx.storage.list_records(NS, "semantic")
        if r.source.channel == "consolidation"
    ]
    assert len(summaries) == 7
    assert sum(r.status is RecordStatus.ACTIVATED for r in summaries) == 1
    assert all(r.evolve_to for r in summaries if r.status is RecordStatus.ARCHIVED)


async def test_trust_is_min_of_all_members_and_flags_are_inherited() -> None:
    counter = Counter()
    ctx, harness = await make_ctx(_policy(rebuild_every=10), counter)
    for i in range(3):
        await add_turn(harness, f"turn {i}", 20 - i, trust=0.9)
    await consolidate(ctx)
    await add_turn(harness, "low trust turn", 16, trust=0.3)
    await consolidate(ctx)
    [current] = await active_summaries(ctx)
    assert current.trust == 0.3
    assert counter.incremental[-1][1] == "low trust turn"


async def test_closing_an_open_summary_costs_no_call_and_feeds_derived_stages() -> None:
    counter = Counter()
    ctx, harness = await make_ctx(_policy(), counter)
    for i in range(3):
        await add_turn(harness, f"turn {i}", 20 - i)
    await consolidate(ctx)
    index = SessionIndex()
    await index.refresh(ctx.storage)
    assert index.sessions == {}  # an open-session summary is not a consolidated session
    # The session closes (gap passes): simulate by narrowing the gap.
    closing = _policy() | {"session_gap_minutes": 5}
    ctx2, _ = await make_ctx(closing, counter)
    ctx2.storage, ctx2.append_event = ctx.storage, harness.append
    stats = await consolidate(ctx2)
    assert stats == {"status": "ok", "summaries": 1, "superseded": 1}
    assert len(counter.full) == 1 and counter.incremental == []  # no new call
    [closed] = await active_summaries(ctx2)
    assert constants.SUMMARY_OPEN_TAG not in closed.tags and closed.content == "FULL[3]"
    await index.refresh(ctx.storage)
    assert len(index.sessions) == 1  # the derived stages now see it
    assert await consolidate(ctx2) == {"status": "ok", "summaries": 0, "superseded": 0}


async def test_without_an_llm_the_extractive_summary_is_rebuilt() -> None:
    ctx, harness = await make_ctx(_policy())
    for i in range(3):
        await add_turn(harness, f"turn {i}. x", 20 - i)
    await consolidate(ctx)
    await add_turn(harness, "turn 3. y", 16)
    await consolidate(ctx)
    [current] = await active_summaries(ctx)
    assert current.content == "turn 0. turn 1. turn 2. turn 3."


async def test_an_open_summary_is_closed_after_incremental_is_switched_off() -> None:
    """Review fix: an open-session summary written under ``incremental: true`` is
    still closed once the flag is off, or its session never reaches the derived
    stages (mine_facts, anticipate)."""
    counter = Counter()
    ctx, harness = await make_ctx(_policy(), counter)
    for i in range(3):
        await add_turn(harness, f"turn {i}", 20 - i)
    await consolidate(ctx)
    [opened] = await active_summaries(ctx)
    assert constants.SUMMARY_OPEN_TAG in opened.tags
    # The flag is switched off, and the session closes (gap passes).
    ctx2, _ = await make_ctx({"session_gap_minutes": 5}, counter)
    ctx2.storage, ctx2.append_event = ctx.storage, harness.append
    stats = await consolidate(ctx2)
    assert stats == {"status": "ok", "summaries": 1, "superseded": 1}
    [closed] = await active_summaries(ctx2)
    assert constants.SUMMARY_OPEN_TAG not in closed.tags and closed.content == "FULL[3]"
    index = SessionIndex()
    await index.refresh(ctx.storage)
    assert len(index.sessions) == 1
