"""I2: one read lists each (namespace, type, group) at most once; outside a read, as before."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.read.pipeline.state import _READ_SNAPSHOT

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)

LEGS = {
    "record_access": False,
    "temporal_leg": True,
    "metadata_leg": True,
    "subject_leg": True,
    "sentence_leg": True,
    "entity_leg": True,
    "cohesion_leg": True,
    "entity_expand_leg": True,
    "recent_exchanges": 3,
    "session_digest": True,
}


async def test_a_read_lists_each_scope_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from memspine.services.storage.sql_base import SqlStorage

    calls: Counter[tuple[Any, ...]] = Counter()
    counting_on = False
    real = SqlStorage.list_records

    async def counting(self: Any, ns: str, memory_type: Any = None, group_id: Any = None) -> Any:
        if counting_on:
            calls[(ns, memory_type, group_id)] += 1
        return await real(self, ns, memory_type, group_id)

    monkeypatch.setattr(SqlStorage, "list_records", counting)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}},
        read=LEGS,
    )
    await eng.start()
    try:
        for i in range(5):
            await eng.write(
                f"Ana: we visited Paris on 7 May 2023, day {i}",
                namespace="a",
                memory_type="episodic",
                valid_from=T0 + timedelta(minutes=i),
            )
        counting_on = True
        result = await eng.read(
            "What did Ana do in Paris on 7 May 2023?", namespace="a", mode="replay"
        )
    finally:
        await eng.stop()
    assert result.context.records
    assert calls and max(calls.values()) == 1, calls
    assert _READ_SNAPSHOT.get() is None  # the scope ends with the read


def _engine() -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}},
        read={**LEGS, "session_cap": 2},  # the session cap and replay both use sessions
    )


async def _read_work(
    monkeypatch: pytest.MonkeyPatch, turns: int
) -> tuple[Counter[str], list[str], str]:
    """RM-161: the store work of one warm replay read over ``turns`` stored turns: rows
    validated into records, SQL listings, single-record SQL lookups and session detections."""
    from memspine.memories.episodic import store as episodic_store
    from memspine.services.storage.sql_base import SqlStorage

    work: Counter[str] = Counter()
    counting = False
    real_row, real_list = SqlStorage._row_to_record, SqlStorage.list_records
    real_get, real_detect = SqlStorage.get_record, episodic_store.detect_sessions

    def row(self: Any, mapping: Any) -> Any:
        work["rows_validated"] += counting
        return real_row(self, mapping)

    async def listing(self: Any, ns: str, memory_type: Any = None, group_id: Any = None) -> Any:
        work["sql_listings"] += counting
        return await real_list(self, ns, memory_type, group_id)

    async def get(self: Any, record_id: str) -> Any:
        work["sql_get_record"] += counting
        return await real_get(self, record_id)

    def detect(records: Any, gap: Any) -> Any:
        work["session_detections"] += counting
        return real_detect(records, gap)

    monkeypatch.setattr(SqlStorage, "_row_to_record", row)
    monkeypatch.setattr(SqlStorage, "list_records", listing)
    monkeypatch.setattr(SqlStorage, "get_record", get)
    monkeypatch.setattr(episodic_store, "detect_sessions", detect)
    eng = _engine()
    await eng.start()
    try:
        for i in range(turns):
            await eng.write(
                f"Ana: we visited Paris on 7 May 2023, day {i}",
                namespace="a",
                memory_type="episodic",
                # a two-hour gap every ten turns: several sessions
                valid_from=T0 + timedelta(minutes=i + 120 * (i // 10)),
            )
        query = "What did Ana do in Paris on 7 May 2023?"
        await eng.read(query, namespace="a", mode="replay")  # warm: builds the shared records
        counting = True
        result = await eng.read(query, namespace="a", mode="replay")
        counting = False
    finally:
        await eng.stop()
    context = result.context
    rendered = "\n".join(r.content for r in context.records) + f"\n{context.tokens_used}"
    # record ids are fresh per run: compare what the context shows, in order
    return work, [f"{r.memory_type}:{r.content}" for r in context.records], rendered


async def test_read_work_stays_flat_as_the_store_grows(monkeypatch: pytest.MonkeyPatch) -> None:
    """RM-161: a warm read validates no row again, lists each scope once and detects
    sessions once, whether the namespace holds 12 or 48 turns (counts, no timing)."""
    small, _, _ = await _read_work(monkeypatch, 12)
    monkeypatch.undo()
    large, _, _ = await _read_work(monkeypatch, 48)
    assert small["rows_validated"] == large["rows_validated"] == 0, (small, large)
    assert small["sql_listings"] == large["sql_listings"], (small, large)
    assert small["session_detections"] == large["session_detections"] == 1, (small, large)
    assert large["sql_get_record"] <= small["sql_get_record"], (small, large)


async def test_shared_records_leave_the_read_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """RM-161: the same context with the shared records, the by-id lookups and the session
    memo all switched off (the plain I2 listing snapshot)."""
    from memspine.read.pipeline import timeview
    from memspine.services.storage import sql_base

    _, shared_ids, shared_text = await _read_work(monkeypatch, 30)
    monkeypatch.undo()
    real_listings = timeview._snapshot_listings

    def listings_only(storage: Any) -> None:
        get, batch = storage.get_record, getattr(storage, "get_records", None)
        real_listings(storage)
        storage.get_record = get
        if batch is not None:
            storage.get_records = batch

    monkeypatch.setattr(sql_base, "shares_listed_records", lambda: False)
    monkeypatch.setattr("memspine.api.engine._snapshot_listings", listings_only)
    monkeypatch.setattr("memspine.api.engine._snapshot_sessions", lambda episodic: None)
    work, plain_ids, plain_text = await _read_work(monkeypatch, 30)
    assert work["rows_validated"] > 0  # really the unshared path
    assert shared_ids == plain_ids
    assert shared_text == plain_text
