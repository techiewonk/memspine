"""PromptRegistry (D-43): resolve the ACTIVE prompt per (role, name, version).

Resolution order rides the existing config layering (D-11): shipped defaults
pack → user overrides from ``prompts.overrides.<id>`` (which arrive through
template/user-YAML/env/kwargs like any other config). An override bumps the
version automatically unless it sets one, so caches (E3) and provenance (E1)
always see a changed identity for changed content.
"""

from __future__ import annotations

from typing import Any

from memspine.exceptions import ConfigError
from memspine.prompts.base import Prompt
from memspine.prompts.loader import load_default_pack
from memspine.prompts.models import OUTPUT_MODELS

__all__ = ["PromptRegistry"]

#: Override keys a user may set; anything else is a config error.
_OVERRIDABLE = {"body", "system", "format", "version", "output_model", "token_budget"}


class PromptRegistry:
    def __init__(self, overrides: dict[str, dict[str, Any]] | None = None) -> None:
        self._prompts: dict[str, Prompt] = {p.id: p for p in load_default_pack()}
        self._overridden: set[str] = set()
        for prompt_id, raw in (overrides or {}).items():
            self._apply_override(prompt_id, raw)

    def _apply_override(self, prompt_id: str, raw: dict[str, Any]) -> None:
        base = self._prompts.get(prompt_id)
        if base is None:
            raise ConfigError(
                f"prompts.overrides.{prompt_id}: no such prompt "
                f"(shipped: {', '.join(sorted(self._prompts))})"
            )
        unknown = set(raw) - _OVERRIDABLE
        if unknown:
            raise ConfigError(
                f"prompts.overrides.{prompt_id}: unknown key(s) {sorted(unknown)} "
                f"(overridable: {sorted(_OVERRIDABLE)})"
            )
        data = base.model_dump()
        data.update(raw)
        if "version" not in raw:
            # Changed content must change identity (E3 cache keys, E1 provenance).
            data["version"] = base.version + 1
        prompt = Prompt.model_validate(data)
        if prompt.output_model is not None and prompt.output_model not in OUTPUT_MODELS:
            raise ConfigError(
                f"prompts.overrides.{prompt_id}: unknown output_model "
                f"{prompt.output_model!r} (known: {sorted(OUTPUT_MODELS)})"
            )
        self._prompts[prompt_id] = prompt
        self._overridden.add(prompt_id)

    def get(self, prompt_id: str) -> Prompt:
        prompt = self._prompts.get(prompt_id)
        if prompt is None:
            raise ConfigError(f"unknown prompt {prompt_id!r}")
        return prompt

    def for_role(self, role: str) -> Prompt:
        """The active prompt whose id equals the role name (shipped convention)."""
        return self.get(role)

    def list(self) -> list[Prompt]:
        return sorted(self._prompts.values(), key=lambda p: p.id)

    def source_of(self, prompt_id: str) -> str:
        return "override" if prompt_id in self._overridden else "defaults"
