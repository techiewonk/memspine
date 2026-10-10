"""I25: adapter-declared / inferred data shape reaches the engine's ``data_profile: auto``."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from memspine_evals.contracts import DatasetInfo, DataShape, EvalItem, Turn
from memspine_evals.shape import infer_shape, shape_for_item
from memspine_evals.systems.memspine_system import MemspineSystem

_HASH = {"embedding": {"provider": "hash"}, "dotenv_path": None}


def _item(turns: list[Turn], meta: dict | None = None) -> EvalItem:
    return EvalItem(item_id="i", history=tuple(turns), queries=(), meta=meta or {})


def _t(n: int, speaker: str, text: str, stamp: str | None = None) -> Turn:
    return Turn(turn_id=f"t{n}", session_id="s", speaker=speaker, text=text, timestamp=stamp)


def test_infer_named_dated_short() -> None:
    shape = infer_shape(
        [_t(1, "Caroline", "hi there", "1:56 pm on 8 May, 2023"), _t(2, "Melanie", "hello")]
    )
    assert shape.has_timestamps is True
    assert (shape.speaker_kind, shape.turn_length, shape.history_size) == (
        "named",
        "short",
        "small",
    )


def test_infer_chat_roles_untimed_long() -> None:
    long = "word " * 400
    shape = infer_shape([_t(1, "user", long), _t(2, "assistant", long)])
    assert shape.has_timestamps is False
    assert (shape.speaker_kind, shape.turn_length) == ("user_assistant", "long")


def test_single_author_and_empty() -> None:
    assert infer_shape([_t(1, "me", "a"), _t(2, "me", "b")]).speaker_kind == "single_author"
    assert infer_shape([]) == DataShape()


def test_declared_wins_and_inference_fills_the_unknown(tmp_path: Path) -> None:
    info = DatasetInfo(
        dataset_id="d",
        revision_id="r",
        licence="x",
        source_path=str(tmp_path),
        content_sha256="0",
        n_items=1,
        n_queries=0,
        shape=DataShape(has_timestamps=True, language="en"),
    )
    item = _item([_t(1, "user", "hi"), _t(2, "assistant", "hello")])  # no stamps in the data
    got = shape_for_item(item, info)
    assert got.has_timestamps is True  # declared, not overridden by the inference
    assert got.speaker_kind == "user_assistant"  # filled in
    assert got.language == "en"
    # per-item meta beats the dataset-level declaration
    item2 = _item([_t(1, "a", "x")], meta={"shape": {"has_timestamps": False}})
    assert shape_for_item(item2, info).has_timestamps is False


def test_system_passes_the_shape_only_when_a_profile_is_set() -> None:
    off = MemspineSystem(config=_HASH)
    off.declare_shape(DataShape(has_timestamps=False))
    assert "data_shape" not in off.describe()
    on = MemspineSystem(config={**_HASH, "data_profile": "auto"})
    on.declare_shape(DataShape(has_timestamps=False, speaker_kind="user_assistant"))
    assert on.describe()["data_shape"]["has_timestamps"] is False


def test_auto_profile_changes_the_engine_config_per_item() -> None:
    pytest.importorskip("memspine")

    async def run(shape: DataShape) -> tuple[bool, str, bool]:
        system = MemspineSystem(template="base", config={**_HASH, "data_profile": "auto"})
        system.declare_shape(shape)
        await system.reset("item")
        read = system._engine._config().read
        out = (read.temporal_leg, read.render, system._perspective_metadata)
        await system.close()
        return out

    dated = asyncio.run(run(DataShape(has_timestamps=True, speaker_kind="named")))
    untimed_chat = asyncio.run(run(DataShape(has_timestamps=False, speaker_kind="user_assistant")))
    assert dated == (True, "dated", False)
    assert untimed_chat == (False, "plain", True)
