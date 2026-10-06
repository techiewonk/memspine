"""PromptRegistry (D-43): resolve the ACTIVE prompt per (role, name, version).

Resolution order rides the existing config layering (D-11): shipped defaults
pack → user overrides from ``prompts.overrides.<id>`` (which arrive through
template/user-YAML/env/kwargs like any other config). An override bumps the
version automatically unless it sets one, so caches (E3) and provenance (E1)
always see a changed identity for changed content.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from jinja2 import Environment

from memspine.core.query_shape import is_inference, is_temporal
from memspine.exceptions import ConfigError
from memspine.prompts.base import Prompt
from memspine.prompts.env import build_environment, partials_fingerprint
from memspine.prompts.loader import load_default_pack
from memspine.prompts.models import OUTPUT_MODELS

__all__ = ["BY_SHAPE_SUFFIX", "QUESTION_SHAPES", "PromptRegistry", "question_shape"]

#: C2: ``prompts.selection.<role>_by_shape`` maps a question shape to the ``condition``
#: of ``<role>`` used for questions of that shape (``chat_by_shape: {temporal: infer}``).
BY_SHAPE_SUFFIX = "_by_shape"
QUESTION_SHAPES = ("temporal", "inference")


def question_shape(question: str) -> str | None:
    """``temporal`` for a date / span question, else ``inference`` for a "would / likely
    / might / could ...?" question, else None (the harness ``routed`` QA prompt's rule)."""
    if is_temporal(question):
        return "temporal"
    if is_inference(question):
        return "inference"
    return None


#: Override keys a user may set; anything else is a config error.
_OVERRIDABLE = {"body", "system", "format", "version", "output_model", "token_budget"}


class PromptRegistry:
    def __init__(
        self,
        overrides: dict[str, dict[str, Any]] | None = None,
        partials: dict[str, str] | None = None,
        selection: dict[str, dict[str, str]] | None = None,
        extra_prompts: Iterable[Prompt] | None = None,
    ) -> None:
        # One environment per registry (B1): its loader consults the config
        # ``prompts.partials`` overrides before the shipped ``_partials/`` dir.
        self._env: Environment = build_environment(partials)
        # ``prompts.selection`` (B2): per-role default selectors merged into
        # every select() query that doesn't override them.
        self._selection: dict[str, dict[str, str]] = {}
        #: C2: ``<role>_by_shape`` entries, keyed by the base role.
        self._by_shape: dict[str, dict[str, str]] = {}
        for key, sel in (selection or {}).items():
            if key.endswith(BY_SHAPE_SUFFIX):
                unknown = set(sel) - set(QUESTION_SHAPES)
                if unknown:
                    raise ConfigError(
                        f"prompts.selection.{key}: unknown question shape(s) {sorted(unknown)} "
                        f"(valid: {', '.join(QUESTION_SHAPES)})"
                    )
                self._by_shape[key.removesuffix(BY_SHAPE_SUFFIX)] = dict(sel)
            else:
                self._selection[key] = sel
        for role, sel in self._selection.items():
            unknown = set(sel) - {"memory_type", "condition"}
            if unknown:
                raise ConfigError(
                    f"prompts.selection.{role}: unknown selector key(s) {sorted(unknown)} "
                    "(valid: memory_type, condition)"
                )
        self._prompts: dict[str, Prompt] = {}
        for prompt in load_default_pack():
            self._prompts[prompt.id] = self._bind(prompt)
        for prompt in extra_prompts or ():  # programmatic/test-only variants
            self._prompts[prompt.id] = self._bind(prompt)
        self._overridden: set[str] = set()
        for prompt_id, raw in (overrides or {}).items():
            self._apply_override(prompt_id, raw)
        for role, by_shape in self._by_shape.items():
            known = {p.when.condition for p in self._prompts.values() if p.role == role and p.when}
            for shape, cond in by_shape.items():
                # "" is the base prompt; anything else must name a shipped condition
                # (select() would silently fall back to the base prompt on a typo).
                if cond and cond not in known:
                    raise ConfigError(
                        f"prompts.selection.{role}{BY_SHAPE_SUFFIX}.{shape}: no {role!r} "
                        f"prompt with condition {cond!r} "
                        f"(known: {', '.join(sorted(c for c in known if c))})"
                    )

    def _bind(self, prompt: Prompt) -> Prompt:
        """Attach this registry's Jinja environment and fold the digest of the
        partials the prompt includes into its ``prompt_version`` (B1) — so a
        partial edit invalidates E3 caches and shifts E1 provenance."""
        fp = partials_fingerprint(self._env, prompt.system, prompt.body)
        prompt._env = self._env
        prompt._version_suffix = f"+{fp}" if fp else ""
        return prompt

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
        self._prompts[prompt_id] = self._bind(prompt)
        self._overridden.add(prompt_id)

    def get(self, prompt_id: str) -> Prompt:
        prompt = self._prompts.get(prompt_id)
        if prompt is None:
            raise ConfigError(f"unknown prompt {prompt_id!r}")
        return prompt

    def for_role(self, role: str) -> Prompt:
        """The active prompt for a role, honoring any ``prompts.selection``
        defaults. Kept for callers that don't pass a scenario; equivalent to
        ``select(role)``."""
        return self.select(role)

    def select(
        self,
        role: str,
        *,
        memory_type: str | None = None,
        condition: str | None = None,
    ) -> Prompt:
        """Pick the most-specific prompt for ``role`` given the scenario (B2).

        Variants declare a ``when:`` block; the base prompt (``id == role``) has
        none and matches everything at specificity 0. A variant is *eligible*
        only if every field its ``when`` sets equals the corresponding query
        value; among eligible prompts the highest-specificity one wins. Ties are
        a config error (two equally-specific variants). With no variants shipped
        this returns the base prompt — ``profile="simple"`` is unchanged."""
        defaults = self._selection.get(role, {})
        query = {
            "memory_type": memory_type if memory_type is not None else defaults.get("memory_type"),
            "condition": condition if condition is not None else defaults.get("condition"),
        }
        eligible: list[tuple[int, Prompt]] = []
        for prompt in self._prompts.values():
            if prompt.role != role:
                continue
            when = prompt.when
            if when is None:
                eligible.append((0, prompt))
                continue
            constraints = {"memory_type": when.memory_type, "condition": when.condition}
            if all(v is None or query[k] == v for k, v in constraints.items()):
                eligible.append((when.specificity, prompt))
        if not eligible:
            raise ConfigError(
                f"no prompt for role {role!r}"
                + (f" matching {query}" if any(query.values()) else "")
            )
        best = max(spec for spec, _ in eligible)
        winners = [p for spec, p in eligible if spec == best]
        if len(winners) > 1:
            raise ConfigError(
                f"ambiguous prompt selection for role {role!r} {query}: "
                f"{sorted(p.id for p in winners)}"
            )
        return winners[0]

    def select_for_question(
        self,
        role: str,
        question: str,
        *,
        memory_type: str | None = None,
        condition: str | None = None,
    ) -> Prompt:
        """C2: :meth:`select`, with the condition picked by ``question``'s shape when
        ``prompts.selection.<role>_by_shape`` maps it (:func:`question_shape`). An
        explicit ``condition`` wins; without a mapping this is exactly ``select``."""
        if condition is None:
            shape = question_shape(question) if self._by_shape.get(role) else None
            if shape is not None:
                condition = self._by_shape[role].get(shape)
        return self.select(role, memory_type=memory_type, condition=condition)

    def list(self) -> list[Prompt]:
        return sorted(self._prompts.values(), key=lambda p: p.id)

    def source_of(self, prompt_id: str) -> str:
        return "override" if prompt_id in self._overridden else "defaults"
