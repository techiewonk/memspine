"""I25: data-shape profiles (``data_profile`` / ``data_shape`` and ``config/presets``)."""

from __future__ import annotations

import pytest

from memspine import Engine
from memspine.config.loader import load_config
from memspine.config.presets import preset_names, resolve_presets, select_presets
from memspine.exceptions import ConfigError


def _cfg(**kw: object):  # type: ignore[no-untyped-def]
    return load_config(template="base", **kw)  # type: ignore[arg-type]


def test_off_is_the_default_and_changes_nothing() -> None:
    plain = _cfg().config
    shaped = _cfg(overrides={"data_shape": {"has_timestamps": False}}).config
    assert plain.data_profile == "off"
    assert plain.read == shaped.read and plain.firewall == shaped.firewall


def test_select_presets_from_shapes_and_unknown_selects_nothing() -> None:
    assert select_presets({}) == []
    assert select_presets({"speaker_kind": "unknown", "turn_length": "unknown"}) == []
    locomo = {"has_timestamps": True, "speaker_kind": "named", "turn_length": "short"}
    assert select_presets(locomo) == ["ts_dated", "named_speakers"]
    chat = {"has_timestamps": False, "speaker_kind": "user_assistant", "turn_length": "long"}
    assert select_presets(chat) == ["ts_none", "long_turns", "chat_roles"]
    assert select_presets({"language": "de"}) == ["non_english"]
    assert select_presets({"language": "en-GB"}) == []
    assert select_presets({"history_size": "large"}) == ["large_history"]


def test_auto_without_a_shape_is_a_no_op() -> None:
    assert _cfg(overrides={"data_profile": "auto"}).config.read == _cfg().config.read


def test_no_timestamps_turns_the_date_features_off() -> None:
    base = _cfg().config.read
    assert base.resolve_relative_dates and base.temporal_leg  # the LoCoMo-tuned template
    got = _cfg(
        overrides={"data_profile": "auto", "data_shape": {"has_timestamps": False}}
    ).config.read
    assert not got.resolve_relative_dates and not got.temporal_leg
    assert got.render == "plain" and got.skip_defaulted_dates and not got.rerank_date_prefix


def test_real_timestamps_turn_the_date_prefix_on() -> None:
    got = _cfg(overrides={"data_profile": "auto", "data_shape": {"has_timestamps": True}}).config
    assert got.read.render == "dated" and got.read.rerank_date_prefix


def test_long_turns_use_the_token_window_and_chunked_rerank() -> None:
    got = _cfg(overrides={"data_profile": "auto", "data_shape": {"turn_length": "long"}}).config
    assert got.read.replay_window_unit == "tokens"
    assert got.read.rerank_chunk_chars == 2000


def test_named_speakers_vs_chat_roles() -> None:
    named = _cfg(overrides={"data_profile": "auto", "data_shape": {"speaker_kind": "named"}})
    assert named.config.read.speaker_vote_mode == "name"
    assert named.config.firewall.signals.minja_bridge_exempt_roles == []
    chat = _cfg(
        overrides={"data_profile": "auto", "data_shape": {"speaker_kind": "user_assistant"}}
    )
    assert chat.config.read.speaker_vote_mode == "perspective"
    assert chat.config.firewall.signals.minja_bridge_exempt_roles == ["assistant"]
    assert chat.config.memories["episodic"].policies["perspective"] == "heuristic"


def test_explicit_user_settings_beat_a_preset() -> None:
    resolved = _cfg(
        overrides={
            "data_profile": "auto",
            "data_shape": {"has_timestamps": False},
            "read": {"temporal_leg": True},
        }
    )
    assert resolved.config.read.temporal_leg is True  # the user's key wins
    assert resolved.config.read.resolve_relative_dates is False  # the preset's still applies
    assert resolved.sources["read.temporal_leg"] == "kwargs"
    assert resolved.sources["read.resolve_relative_dates"] == "preset:ts_none"


def test_named_presets_and_unknown_name() -> None:
    assert resolve_presets("chat_roles, ts_none", None) == ["chat_roles", "ts_none"]
    with pytest.raises(ConfigError):
        resolve_presets("no_such_preset", None)
    with pytest.raises(ConfigError):
        _cfg(overrides={"data_profile": "no_such_preset"})


@pytest.mark.parametrize("name", preset_names())
def test_every_preset_validates_against_the_schema(name: str) -> None:
    assert _cfg(overrides={"data_profile": name}).config.data_profile == name
    load_config(template="core", overrides={"data_profile": name})


def test_the_expected_presets_ship() -> None:
    assert set(preset_names()) == {
        "chat_roles",
        "large_history",
        "long_turns",
        "named_speakers",
        "non_english",
        "ts_dated",
        "ts_none",
    }


async def test_chat_roles_profile_stops_assistant_boilerplate_quarantine() -> None:
    opener = (
        "Sure! Here's a detailed answer that walks through the question step by step, "
        "covering the background and the main options at length. "
    )
    turns = []
    for i in range(6):
        turns.append({"role": "user", "content": f"question number {i} about gardens"})
        turns.append({"role": "assistant", "content": f"{opener}for garden topic {i}."})

    async def held(**kw: object) -> int:
        eng = Engine(
            template="core",
            dotenv_path=None,
            storage={"path": ":memory:"},
            embedding={"provider": "hash"},
            **kw,  # type: ignore[arg-type]
        )
        await eng.start()
        try:
            records = await eng.write_messages(turns, namespace="c", channel="web")
        finally:
            await eng.stop()
        return sum(1 for r in records if r.quarantined)

    assert await held() == 5  # default: every assistant turn after the first
    assert await held(data_profile="auto", data_shape={"speaker_kind": "user_assistant"}) == 0
