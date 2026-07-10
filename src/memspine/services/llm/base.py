"""LLM port: per-role provider routing (D-07/D-22) + json-repair safety net (D-31).

Roles are capabilities, not models: ``extract`` / ``judge`` / ``chat`` each bind
their own provider in config (``llm.roles.<role>``), so a cheap local model can
extract while a stronger endpoint chats. The instructor structured-output
wrapper joins under ``[structured]`` in Phase 2 (D-31); the always-on lenient
JSON parser ships now.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from json_repair import repair_json

from memspine.exceptions import ConfigError

__all__ = ["ROLE_CHAT", "ROLE_EXTRACT", "ROLE_JUDGE", "LLMRouter", "LLMService", "lenient_json"]

ROLE_EXTRACT = "extract"
ROLE_JUDGE = "judge"
ROLE_CHAT = "chat"


@runtime_checkable
class LLMService(Protocol):
    """One provider bound to one role."""

    @property
    def provider_id(self) -> str: ...

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str: ...


def lenient_json(text: str) -> Any:
    """Always-on safety net (D-31): repair near-JSON LLM output before parsing."""
    return repair_json(text, return_objects=True)


class LLMRouter:
    """Role -> provider table resolved from config at engine start."""

    def __init__(self, providers: dict[str, LLMService]) -> None:
        self._providers = providers

    @property
    def roles(self) -> list[str]:
        return sorted(self._providers)

    def for_role(self, role: str) -> LLMService:
        provider = self._providers.get(role)
        if provider is None:
            raise ConfigError(
                f"no LLM provider bound for role {role!r} — add llm.roles.{role} "
                "to your config (a LiteLLM model id: openai/…, ollama/…, bedrock/…, D-33)"
            )
        return provider
