"""I25: the data shape of an item, declared or inferred.

``shape_for_item`` returns the shape the engine's ``data_profile: auto`` reads: the item's own
``meta["shape"]`` first, then the adapter's ``DatasetInfo.shape``, with every fact still
unknown filled in by ``infer_shape`` from the history itself. Inference uses only the
history (never queries, gold or answers), so it is leakage-free and generic (rule I37).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from statistics import median
from typing import Any

from .contracts import DatasetInfo, DataShape, EvalItem, Turn

__all__ = ["infer_shape", "shape_for_item"]

#: median characters per turn: <= SHORT is short chat, >= LONG is long (assistant answers).
SHORT_TURN_CHARS = 300
LONG_TURN_CHARS = 800
#: turns in one item's history: >= LARGE is a long haystack.
LARGE_HISTORY_TURNS = 2000
SMALL_HISTORY_TURNS = 200


def _speaker_kind(turns: Sequence[Turn]) -> str:
    names = {str(t.speaker or "").strip().lower() for t in turns} - {""}
    if not names:
        return "unknown"
    if len(names) == 1:
        return "single_author"
    if names <= {"user", "assistant", "system", "tool"}:
        return "user_assistant"
    return "named"


def infer_shape(history: Sequence[Turn]) -> DataShape:
    """Facts readable from the history alone; language is left undeclared."""
    if not history:
        return DataShape()
    stamped = sum(1 for t in history if (t.timestamp or "").strip())
    lengths = [len(t.text or "") for t in history]
    typical = median(lengths)
    return DataShape(
        has_timestamps=stamped > 0,
        speaker_kind=_speaker_kind(history),
        turn_length=(
            "short"
            if typical <= SHORT_TURN_CHARS
            else "long"
            if typical >= LONG_TURN_CHARS
            else "medium"
        ),
        history_size=(
            "large"
            if len(history) >= LARGE_HISTORY_TURNS
            else "small"
            if len(history) <= SMALL_HISTORY_TURNS
            else "medium"
        ),
    )


def _unknown(value: Any) -> bool:
    return value is None or value == "unknown"


def shape_for_item(item: EvalItem, info: DatasetInfo | None = None) -> DataShape:
    declared: DataShape | None = None
    meta: Mapping[str, Any] = item.meta or {}
    raw = meta.get("shape")
    if isinstance(raw, DataShape):
        declared = raw
    elif isinstance(raw, Mapping):
        declared = DataShape.from_mapping(raw)
    elif info is not None and info.shape is not None:
        declared = info.shape
    inferred = infer_shape(item.history)
    if declared is None:
        return inferred
    fill = {
        name: getattr(inferred, name)
        for name in DataShape.__dataclass_fields__
        if _unknown(getattr(declared, name)) and not _unknown(getattr(inferred, name))
    }
    return replace(declared, **fill)
