"""I25: data-shape profiles.

One set of read defaults is tuned on one shape of data (the shipped ``base`` template on
LoCoMo-like named, dated, short-turn chat). A *data profile* maps facts about the data to
config presets (``config/presets/*.yaml``), each preset a small partial config layered
between the template and the user's own settings (so the user always wins).

Opt-in: ``data_profile: off`` (default) changes nothing. ``data_profile: auto`` selects
presets from ``data_shape`` (declared by the data adapter, see ``DataShapeConfig``);
``data_profile: "chat_roles,ts_none"`` names presets explicitly. (The key is not called
``profile``: that name already selects the usage profile, ``simple`` / ``benchmark``.)

The mapping below is written from the gap analysis (what each shape makes unsafe or
meaningless), never fitted to benchmark scores (rule I37):

* ``has_timestamps`` true: ``ts_dated`` (date prefix, relative-date anchoring, event-time
  legs); false: ``ts_none`` (no date prefix or date legs, defaulted clocks skipped) (I7).
* ``turn_length`` long: ``long_turns`` (token-unit window, budget scaling, chunked rerank)
  (I6, I9, I20).
* ``history_size`` large: ``large_history`` (window and pool scale with the budget) (I20).
* ``speaker_kind`` named: ``named_speakers`` (speaker vote by the named participant) (I5);
  ``user_assistant``: ``chat_roles`` (perspective metadata, role vote, assistant boilerplate
  exempt from the prefix-repeat signal) (I5, I39, I21).
* ``language`` not English: ``non_english`` (English-only regex features off) (I23).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from memspine.exceptions import ConfigError

__all__ = ["load_preset", "preset_names", "presets_dir", "resolve_presets", "select_presets"]


def presets_dir() -> Path:
    return Path(__file__).parent / "presets"


def preset_names() -> tuple[str, ...]:
    return tuple(sorted(p.stem for p in presets_dir().glob("*.yaml")))


def load_preset(name: str) -> dict[str, Any]:
    path = presets_dir() / f"{name}.yaml"
    if not path.exists():
        raise ConfigError(
            f"data preset {name!r} not found (available: {', '.join(preset_names())})"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"top level of preset {path} must be a mapping")
    return data


def select_presets(shape: Mapping[str, Any] | None) -> list[str]:
    """The presets a shape calls for (order = layering order, later wins). Unknown or
    undeclared facts select nothing: ``auto`` never guesses."""
    s = dict(shape or {})
    chosen: list[str] = []
    ts = s.get("has_timestamps")
    if ts is True:
        chosen.append("ts_dated")
    elif ts is False:
        chosen.append("ts_none")
    if s.get("turn_length") == "long":
        chosen.append("long_turns")
    if s.get("history_size") == "large":
        chosen.append("large_history")
    kind = s.get("speaker_kind")
    if kind == "named":
        chosen.append("named_speakers")
    elif kind == "user_assistant":
        chosen.append("chat_roles")
    lang = s.get("language")
    if isinstance(lang, str) and lang.strip() and not lang.strip().lower().startswith("en"):
        chosen.append("non_english")
    return chosen


def resolve_presets(spec: str, shape: Mapping[str, Any] | None) -> list[str]:
    """``off`` -> []; ``auto`` -> :func:`select_presets`; else a comma-separated name list."""
    text = (spec or "off").strip()
    if text.lower() == "off":
        return []
    if text.lower() == "auto":
        return select_presets(shape)
    names = [part.strip() for part in text.split(",") if part.strip()]
    known = set(preset_names())
    for name in names:
        if name not in known:
            raise ConfigError(
                f"unknown data preset {name!r} (available: {', '.join(sorted(known))})"
            )
    return names
