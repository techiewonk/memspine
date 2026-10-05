"""The assistant template carries every setting of the measured combo-A arm."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memspine import Engine


def _engine(template: str) -> Engine:
    return Engine(
        template=template,
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
    )


async def test_assistant_template_settings() -> None:
    eng = _engine("assistant")
    await eng.start()
    try:
        read = eng._config().read
        assert read.resolve_relative_dates and read.order_by_time_for_ordering
        assert read.record_access is False
        assert read.default_mode == "replay"
        assert read.assembly["relative_floor"] == 0.3
        assert read.scoring["recency_weight"] == 0.0
        assert eng.describe()["profile"] == "assistant"
    finally:
        await eng.stop()


async def test_assistant_reads_replay_and_answers_with_the_dated_prompt() -> None:
    eng = _engine("assistant")
    await eng.start()
    try:
        await eng.write_messages(
            [{"role": "user", "content": "Ana: I went hiking last Friday."}],
            namespace="a",
            session_id="s1",
            valid_from=datetime(2023, 7, 18, tzinfo=UTC),
        )
        result = await eng.read("When did Ana go hiking?", namespace="a")
        assert result.mode == "replay"
        messages = eng.chat_messages("When did Ana go hiking?", "Ana went hiking")
        assert "computed from the line's own date" in messages[0]["content"]
        base = eng.chat_messages("hi", condition="")  # per-call override: base prompt
        assert "computed from the line's own date" not in base[0]["content"]
    finally:
        await eng.stop()


async def test_base_template_is_unchanged() -> None:
    eng = _engine("base")
    await eng.start()
    try:
        assert eng._config().read.default_mode == "auto"
        assert "computed from" not in eng.chat_messages("hi")[0]["content"]
    finally:
        await eng.stop()


@pytest.mark.shipped_default
async def test_engine_without_a_template_uses_assistant() -> None:
    """ADR-032: Engine() defaults to the assistant template; base stays simple."""
    eng = Engine(dotenv_path=None, storage={"path": ":memory:"}, embedding={"provider": "hash"})
    await eng.start()
    try:
        assert eng.describe()["profile"] == "assistant"
        assert eng._config().read.default_mode == "replay"
    finally:
        await eng.stop()
    simple = _engine("base")
    await simple.start()
    try:
        assert simple.describe()["profile"] == "simple"
    finally:
        await simple.stop()
