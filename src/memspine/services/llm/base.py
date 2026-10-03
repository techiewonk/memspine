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

_CHARS_PER_TOKEN = 4


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

    def __init__(
        self,
        inner: LLMService,
        role: str,
        counts: dict[str, int],
        estimates: dict[str, list[int]] | None = None,
    ) -> None:
        self._inner = inner
        self._role = role
        self._counts = counts
        self._estimates = estimates if estimates is not None else {}

    @property
    def provider_id(self) -> str:
        return self._inner.provider_id

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self._counts[self._role] = self._counts.get(self._role, 0) + 1
        reply = await self._inner.chat(messages, **options)
        # Chars/4 estimate, used only for providers that report no usage of their own.
        acc = self._estimates.setdefault(self._role, [0, 0])
        acc[0] += sum(len(str(m.get("content", ""))) for m in messages) // _CHARS_PER_TOKEN
        acc[1] += len(reply) // _CHARS_PER_TOKEN
        return reply

    def __getattr__(self, name: str) -> Any:
        # ``__getattr__`` only runs for names not found normally. During copy/pickle the
        # instance is built without ``__init__``, so ``_inner`` is absent and delegating
        # would recurse; dunder lookups (``__deepcopy__``, ``__getstate__``…) must not
        # leak to the provider either.
        if name == "_inner" or (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __repr__(self) -> str:
        return f"_Counted(role={self._role!r}, inner={self._inner!r})"


class LLMRouter:
    """Role -> provider table resolved from config at engine start."""

    def __init__(self, providers: dict[str, LLMService]) -> None:
        self._providers = providers
        self._counts: dict[str, int] = {}
        self._estimates: dict[str, list[int]] = {}

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
        return _Counted(self.provider(role), role, self._counts, self._estimates)

    def call_counts(self) -> dict[str, int]:
        """Model calls made so far, per role."""
        return dict(self._counts)

    def token_counts(self) -> dict[str, dict[str, int]]:
        """Tokens spent so far, per role: ``{"prompt": n, "completion": n}``.

        A provider that reports its own usage (``usage_totals``, e.g. LiteLLM) is
        read directly; any other provider is estimated at four characters per
        token from the counted calls' messages and replies.
        """
        out: dict[str, dict[str, int]] = {}
        for role, provider in self._providers.items():
            totals = getattr(provider, "usage_totals", None)
            if not (isinstance(totals, list) and len(totals) == 2):
                totals = self._estimates.get(role)
            if totals and (totals[0] or totals[1]):
                out[role] = {"prompt": int(totals[0]), "completion": int(totals[1])}
        return out

    def models(self) -> dict[str, str]:
        """The model id bound to each role (its ``model`` attribute, else provider id)."""
        return {
            role: str(getattr(provider, "model", None) or provider.provider_id)
            for role, provider in self._providers.items()
        }
