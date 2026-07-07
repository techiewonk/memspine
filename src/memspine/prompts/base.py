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
from jinja2 import Environment, StrictUndefined
from pydantic import BaseModel, ConfigDict

from memspine.exceptions import ConfigError

__all__ = ["Prompt", "PromptFormat", "parse_prompt_text"]

_JINJA = Environment(undefined=StrictUndefined, autoescape=False)


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

    @property
    def prompt_version(self) -> str:
        """Provenance/cache-key identity: ``<id>@<version>`` (E3/E1)."""
        return f"{self.id}@{self.version}"

    def render(self, context: dict[str, object]) -> list[dict[str, str]]:
        """Render to chat messages. Unknown template variables fail loudly."""
        try:
            user = _JINJA.from_string(self.body).render(**context)
        except Exception as exc:
            raise ConfigError(f"prompt {self.prompt_version} failed to render: {exc}") from exc
        messages: list[dict[str, str]] = []
        if self.system:
            messages.append({"role": "system", "content": self.system})
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
