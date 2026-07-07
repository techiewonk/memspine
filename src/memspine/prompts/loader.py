"""Load the shipped YAML default pack (D-43)."""

from __future__ import annotations

from pathlib import Path

from memspine.prompts.base import Prompt, parse_prompt_text

__all__ = ["defaults_dir", "load_default_pack"]


def defaults_dir() -> Path:
    return Path(__file__).parent / "defaults"


def load_default_pack() -> list[Prompt]:
    prompts = []
    for path in sorted(defaults_dir().glob("*.yaml")):
        prompts.append(parse_prompt_text(path.read_text(encoding="utf-8"), source=str(path)))
    return prompts
