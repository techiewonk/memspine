"""The assistant template carries the measured combo-A read settings."""

from __future__ import annotations

from memspine import Engine


async def test_assistant_template_settings() -> None:
    eng = Engine(
        template="assistant",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
    )
    await eng.start()
    try:
        read = eng._config().read
        assert read.resolve_relative_dates and read.order_by_time_for_ordering
        assert read.record_access is False
        assert read.assembly["relative_floor"] == 0.3
        assert read.scoring["recency_weight"] == 0.0
        assert eng.describe()["profile"] == "assistant"
    finally:
        await eng.stop()
