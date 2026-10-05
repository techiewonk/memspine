"""The assistant template carries every setting of the measured combo-A arm."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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


async def test_core_template_is_bare_and_base_has_the_advantages() -> None:
    """ADR-033: `core` is the bare configuration; `base` (profile simple) carries combo-A."""
    core = _engine("core")
    await core.start()
    try:
        assert core._config().read.default_mode == "auto"
        assert "computed from" not in core.chat_messages("hi")[0]["content"]
    finally:
        await core.stop()
    base = _engine("base")
    await base.start()
    try:
        read = base._config().read
        assert base.describe()["profile"] == "simple"
        assert read.default_mode == "replay" and read.resolve_relative_dates
        assert "computed from the line's own date" in base.chat_messages("hi")[0]["content"]
        conflict = base._config().memories["semantic"].policies["conflict"]
        assert conflict["contest_lower_trust"] is True
    finally:
        await base.stop()


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


@pytest.mark.shipped_default
@pytest.mark.parametrize("where", ["kwargs", "user_config"])
async def test_profile_simple_without_a_template_is_base(where: str) -> None:
    """ADR-032 (A-5): naming a profile but no template resolves to ``base``, so
    ``Engine(profile="simple")`` never picks up the assistant settings."""
    from memspine.config.loader import load_config

    common = {"storage": {"path": ":memory:"}, "embedding": {"provider": "hash"}}
    if where == "kwargs":
        eng = Engine(dotenv_path=None, profile="simple", **common)
    else:
        eng = Engine(dotenv_path=None, user_config={"profile": "simple"}, **common)
    await eng.start()
    try:
        expected = load_config(template="base", overrides=common).config
        got = eng._config()
        assert got.read == expected.read
        assert got.memories == expected.memories
        assert got.prompts == expected.prompts
        assert eng.describe()["profile"] == "simple"
    finally:
        await eng.stop()


@pytest.mark.shipped_default
def test_cli_engine_ops_run_on_base(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-032 (A-5): ``memspine audit taint`` / ``forget`` boot their engine on ``base``."""
    from memspine import cli

    seen: dict[str, Any] = {}
    real_init = Engine.__init__

    def spy(self: Engine, *args: Any, **kwargs: Any) -> None:
        seen.update(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(Engine, "__init__", spy)
    with pytest.raises(Exception):  # noqa: B017 - the unknown record is irrelevant here
        cli._run_engine_op(tmp_path / "m.db", "taint", record_id="nope", namespace="default")
    assert seen.get("template") == "base"
