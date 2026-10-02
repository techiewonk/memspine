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


class _Counted:
    """A provider that counts its own ``chat`` calls into the router's ledger.

    Cost must be measured, not assumed: evaluations report model calls per loop
    stage, and the write path (mining, anticipation, reflection) calls models.
    """

    def __init__(self, inner: LLMService, role: str, counts: dict[str, int]) -> None:
        self._inner = inner
        self._role = role
        self._counts = counts

    @property
    def provider_id(self) -> str:
        return self._inner.provider_id

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self._counts[self._role] = self._counts.get(self._role, 0) + 1
        return await self._inner.chat(messages, **options)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class LLMRouter:
    """Role -> provider table resolved from config at engine start."""

    def __init__(self, providers: dict[str, LLMService]) -> None:
        self._providers = providers
        self._counts: dict[str, int] = {}

    @property
    def roles(self) -> list[str]:
        return sorted(self._providers)

    def provider(self, role: str) -> LLMService:
        """The bound provider itself (uncounted); for wiring checks."""
        provider = self._providers.get(role)
        if provider is None:
            raise ConfigError(
                f"no LLM provider bound for role {role!r} — add llm.roles.{role} "
                "to your config (a LiteLLM model id: openai/…, ollama/…, bedrock/…, D-33)"
            )
        return provider

    def for_role(self, role: str) -> LLMService:
        """The provider for ``role``, counting every call (see ``call_counts``)."""
        return _Counted(self.provider(role), role, self._counts)

    def call_counts(self) -> dict[str, int]:
        """Model calls made so far, per role."""
        return dict(self._counts)
