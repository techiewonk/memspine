"""I2: one read lists each (namespace, type, group) at most once; outside a read, as before."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.engine import _READ_SNAPSHOT

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
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
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
