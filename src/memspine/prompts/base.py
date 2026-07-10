"""Prompt object + file format (D-43).

A prompt file is YAML frontmatter + ``---`` + a Jinja2 body::

    id: extract
    version: 1
    role: extract
    format: yaml            # E9: yaml/cod are token-lean vs json
    system: You extract facts.
    ---
    Extract facts from: {{ content }}

``version`` participates in E3 cache keys and record provenance
(``SourceInfo.prompt_version``), so a prompt change invalidates caches and is
traceable in `audit taint` (E1).
"""

from __future__ import annotations

from enum import StrEnum

import yaml
from jinja2 import Environment
from pydantic import BaseModel, ConfigDict, PrivateAttr

from memspine.exceptions import ConfigError
from memspine.prompts.env import default_environment

__all__ = ["Prompt", "PromptFormat", "parse_prompt_text"]


class PromptFormat(StrEnum):
    JSON = "json"
    YAML = "yaml"  # E9: ~half the output tokens of JSON
    COD = "cod"  # Chain-of-Draft (E9)
    TEXT = "text"


class Prompt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    version: int = 1
    role: str
    format: PromptFormat = PromptFormat.TEXT
    output_model: str | None = None  # pydantic model name -> instructor (D-31)
    token_budget: int | None = None
    system: str | None = None
    body: str

    #: Jinja environment used to resolve ``{% include %}`` partials, and a digest
    #: of the partials this body references. Both are bound by the PromptRegistry
    #: (B1): default (shipped ``_partials/`` only) until then, so a Prompt built
    #: straight from YAML still renders and versions correctly on its own.
    _env: Environment | None = PrivateAttr(default=None)
    _version_suffix: str = PrivateAttr(default="")

    @property
    def prompt_version(self) -> str:
        """Provenance/cache-key identity: ``<id>@<version>`` (E3/E1), plus a
        ``+<digest>`` suffix when the body includes partials so a fragment edit
        shifts the identity too (B1)."""
        return f"{self.id}@{self.version}{self._version_suffix}"

    def render(self, context: dict[str, object]) -> list[dict[str, str]]:
        """Render to chat messages. Unknown template variables fail loudly;
        ``{% include %}`` names resolve against the bound partials loader."""
        env = self._env or default_environment()
        try:
            user = env.from_string(self.body).render(**context)
            system = env.from_string(self.system).render(**context) if self.system else None
        except Exception as exc:
            raise ConfigError(f"prompt {self.prompt_version} failed to render: {exc}") from exc
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        return messages


def parse_prompt_text(text: str, source: str = "<inline>") -> Prompt:
    front, sep, body = text.partition("\n---\n")
    if not sep:
        raise ConfigError(f"prompt file {source} has no '---' frontmatter separator")
    try:
        meta = yaml.safe_load(front)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML frontmatter in {source}: {exc}") from exc
    if not isinstance(meta, dict):
        raise ConfigError(f"frontmatter of {source} must be a mapping")
    try:
        return Prompt.model_validate({**meta, "body": body.strip()})
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"invalid prompt definition in {source}: {exc}") from exc
