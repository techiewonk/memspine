"""Load the shipped YAML default pack (D-43)."""

from __future__ import annotations

from pathlib import Path

from memspine.prompts.base import Prompt, parse_prompt_text

__all__ = ["defaults_dir", "load_default_pack", "load_v2_pack", "v2_dir"]


def defaults_dir() -> Path:
    return Path(__file__).parent / "defaults"


def load_default_pack() -> list[Prompt]:
    prompts = []
    for path in sorted(defaults_dir().glob("*.yaml")):
        prompts.append(parse_prompt_text(path.read_text(encoding="utf-8"), source=str(path)))
    return prompts


def v2_dir() -> Path:
    """``defaults/v2/``: the layout-v2 variants (W2-07), selected by ``prompts.layout_v2``."""
    return defaults_dir() / "v2"


def load_v2_pack() -> list[Prompt]:
    """The layout-v2 prompts (no examples, stable system part, partials). Each replaces the
    default-pack prompt with the same ``id`` when ``prompts.layout_v2`` is on."""
    return [
        parse_prompt_text(path.read_text(encoding="utf-8"), source=str(path))
        for path in sorted(v2_dir().glob("*.yaml"))
    ]
