"""#19 interval arithmetic (``conflict.interval_order``): out-of-order statements on
one fact key end with the correct current fact and non-overlapping world-time
intervals; with the option off the ladder behaves exactly as before."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import permutations
from typing import Any

import pytest

from memspine import Engine
from memspine.clients.sqlite import SQLiteClient
from memspine.core.events import MemoryEvent
from memspine.core.policies.conflict import ConflictPolicy
from memspine.core.policies.dedup import DedupPolicy
from memspine.core.records import MemoryRecord
from memspine.memories.semantic.store import SemanticMemory
from memspine.services.embedding.hash_local import HashEmbedding
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage

T1 = datetime(2023, 1, 1, tzinfo=UTC)
T2 = datetime(2023, 6, 1, tzinfo=UTC)
T3 = datetime(2024, 1, 1, tzinfo=UTC)
STATEMENTS = {
    "a": ("Ana resides in Lyon near the old river quarter", T1),
    "b": ("Ana moved to Nice for a seaside hospital job", T2),
    "c": ("Ana relocated to Oslo after accepting a research post", T3),
}


async def _semantic(interval_order: bool) -> tuple[SemanticMemory, list[MemoryEvent]]:
    client = SQLiteClient(":memory:")
    await client.connect()
    storage = SQLiteStorage(client)
    await storage.start()
    projector = RecordProjector(storage)
    log: list[MemoryEvent] = []

    async def append_and_project(event: MemoryEvent) -> None:
        appended = await storage.append_event(event)
        log.append(appended)
        await projector.apply(appended)

    memory = SemanticMemory(
        storage=storage,
        embedder=HashEmbedding(dim=64),
        append_event=append_and_project,
        conflict=ConflictPolicy.bind({"interval_order": interval_order}),
        dedup=DedupPolicy.bind({"lsh_threshold": 0.5, "cosine_threshold": 0.9}),
    )
    return memory, log


def _fact(label: str, *, tags: list[str] | None = None) -> MemoryRecord:
    content, when = STATEMENTS[label]
    return MemoryRecord(
        namespace="ns",
        memory_type="semantic",
        content=content,
        entity="ana",
        attribute="city",
        valid_from=when,
        tags=tags or [],
    )


async def _intervals(memory: SemanticMemory) -> dict[str, tuple[Any, Any, Any]]:
    by_content = {content: label for label, (content, _w) in STATEMENTS.items()}
    return {
        by_content[r.content]: (r.valid_from, r.valid_to, r.invalid_at)
        for r in await memory._storage.list_records("ns", "semantic")
    }


@pytest.mark.parametrize("order", list(permutations("abc")))
async def test_out_of_order_arrival_ends_in_the_correct_state(order: tuple[str, ...]) -> None:
    memory, _log = await _semantic(interval_order=True)
    for label in order:
        await memory.write(_fact(label))
    current = await memory._storage.find_active_fact("ns", "ana", "city")
    assert current is not None and current.content == STATEMENTS["c"][0]
    assert await _intervals(memory) == {
        "a": (T1, T2, T2),
        "b": (T2, T3, T3),
        "c": (T3, None, None),
    }


async def test_off_keeps_the_plain_backfill_and_no_invalid_at() -> None:
    memory, log = await _semantic(interval_order=False)
    for label in ("a", "c", "b"):
        await memory.write(_fact(label))
    # Today's R4 backfill: b closes at the current fact, a keeps its UPDATE end.
    assert await _intervals(memory) == {
        "a": (T1, T3, None),
        "b": (T2, T3, None),
        "c": (T3, None, None),
    }
    assert all("invalid_at" not in str(event.payload) for event in log)


async def test_in_order_update_is_unchanged_except_invalid_at() -> None:
    off, off_log = await _semantic(interval_order=False)
    on, _on_log = await _semantic(interval_order=True)
    for memory in (off, on):
        for label in ("a", "b"):
            await memory.write(_fact(label))
    assert await _intervals(off) == {"a": (T1, T2, None), "b": (T2, None, None)}
    assert await _intervals(on) == {"a": (T1, T2, T2), "b": (T2, None, None)}
    assert all("invalid_at" not in str(event.payload) for event in off_log)


async def test_same_endpoints_are_a_duplicate_not_a_contradiction() -> None:
    def edge(label: str) -> MemoryRecord:
        return _fact(label, tags=["kind:state", "rel:city", "dst:lyon"])

    on, _ = await _semantic(interval_order=True)
    await on.write(edge("a"))
    result = await on.write(edge("b"))  # different wording, same (src, rel, dst)
    assert result.action == "merged"
    current = await on._storage.find_active_fact("ns", "ana", "city")
    assert current is not None and current.content == STATEMENTS["a"][0]

    off, _ = await _semantic(interval_order=False)
    await off.write(edge("a"))
    assert (await off.write(edge("b"))).action == "updated"


async def test_engine_reads_interval_order_from_config() -> None:
    engine = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {"enabled": True, "policies": {"conflict": {"interval_order": True}}}
        },
    )
    await engine.start()
    try:
        for label in ("c", "a", "b"):
            content, when = STATEMENTS[label]
            await engine.write(
                content, namespace="ns", entity="ana", attribute="city", valid_from=when
            )
        storage = engine._require_started()
        current = await storage.find_active_fact("ns", "ana", "city")
        assert current is not None and current.content == STATEMENTS["c"][0]
        records = {r.content: r for r in await storage.list_records("ns", "semantic")}
        older = records[STATEMENTS["a"][0]]
        assert (older.valid_to, older.invalid_at) == (T2, T2)
    finally:
        await engine.stop()


def test_off_by_default() -> None:
    assert ConflictPolicy.bind().interval_order is False
