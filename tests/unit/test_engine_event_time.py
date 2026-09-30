"""C-1: event time (valid_from) on write and write_messages."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from memspine import Engine


@pytest.fixture
async def engine() -> AsyncIterator[Engine]:
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
    )
    await eng.start()
    yield eng
    await eng.stop()


async def test_default_event_time_is_now(engine: Engine) -> None:
    before = datetime.now(UTC)
    rec = await engine.write("plain fact", namespace="a", memory_type="episodic")
    assert rec.valid_from >= before


async def test_write_sets_event_time_and_makes_it_aware(engine: Engine) -> None:
    naive = datetime(2023, 5, 8, 13, 56)
    rec = await engine.write("met Mel", namespace="a", memory_type="episodic", valid_from=naive)
    assert rec.valid_from == naive.replace(tzinfo=UTC)
    assert rec.recorded_at > rec.valid_from  # event time is not ingestion time


async def test_write_messages_per_message_timestamp_beats_batch(engine: Engine) -> None:
    batch = datetime(2023, 1, 1, tzinfo=UTC)
    records = await engine.write_messages(
        [
            {"role": "user", "content": "first", "timestamp": "2023-05-08T13:56:00"},
            {"role": "user", "content": "second"},
        ],
        namespace="a",
        valid_from=batch,
    )
    assert records[0].valid_from == datetime(2023, 5, 8, 13, 56, tzinfo=UTC)
    assert records[1].valid_from == batch
