"""LLM port: per-role provider routing (D-07/D-22) + json-repair safety net (D-31).

Roles are capabilities, not models: ``extract`` / ``judge`` / ``chat`` each bind
their own provider in config (``llm.roles.<role>``), so a cheap local model can
extract while a stronger endpoint chats. The instructor structured-output
wrapper joins under ``[structured]`` in Phase 2 (D-31); the always-on lenient
JSON parser ships now.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from memspine.models.providers.llm.structured import StructuredOptions

from json_repair import repair_json

from memspine.config.constants import TOKEN_ESTIMATE_CHARS_PER_TOKEN
from memspine.economy import ledger as _costs
from memspine.exceptions import ConfigError
from memspine.models.concurrency import model_slot
from memspine.observability import inspect as insp
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.observability.usage import UsageLedger

__all__ = [
    "ROLE_CHAT",
    "ROLE_EXTRACT",
    "ROLE_JUDGE",
    "LLMRouter",
    "LLMService",
    "ThinkingCapped",
    "lenient_json",
]

ROLE_EXTRACT = "extract"
ROLE_JUDGE = "judge"
ROLE_CHAT = "chat"

_CHARS_PER_TOKEN = TOKEN_ESTIMATE_CHARS_PER_TOKEN

_log = get_logger(__name__)


@runtime_checkable
class LLMService(Protocol):
    """One provider bound to one role."""

    @property
    def provider_id(self) -> str: ...

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str: ...


def lenient_json(text: str) -> Any:
    """Always-on safety net (D-31): repair near-JSON LLM output before parsing."""
    return repair_json(text, return_objects=True)


def _reported_usage(provider: object) -> tuple[int, int] | None:
    """#33: ``(input, output)`` tokens the provider reported for its last call, or None.

    A provider opts in by setting ``last_usage`` to a pair of ints after each call
    (``None`` when the backend gave no usage). Anything else is treated as absent."""
    usage = getattr(provider, "last_usage", None)
    if (
        isinstance(usage, tuple | list)
        and len(usage) == 2
        and all(isinstance(n, int) and not isinstance(n, bool) for n in usage)
    ):
        return int(usage[0]), int(usage[1])
    return None


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
        usage: UsageLedger | None = None,
        structured_options: StructuredOptions | None = None,
    ) -> None:
        self._inner = inner
        self._role = role
        self._counts = counts
        self._estimates = estimates if estimates is not None else {}
        self._usage = usage
        self.structured_options = structured_options

    @property
    def provider_id(self) -> str:
        return self._inner.provider_id

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self._counts[self._role] = self._counts.get(self._role, 0) + 1
        costed = _costs.installed() is not None  # W2-12: economy.ledger_v2
        t0 = time.perf_counter() if costed else 0.0
        try:
            async with model_slot("llm"):  # RT-02: no-op unless runtime.model_concurrency is on
                if tap.enabled():
                    reply = await self._tapped_chat(messages, options)
                else:
                    reply = await self._inner.chat(messages, **options)
        except BaseException as exc:
            if costed:
                _costs.record_call(
                    "generative",
                    model=str(getattr(self._inner, "model", None) or self._inner.provider_id),
                    tokens_in=sum(len(str(m.get("content", ""))) for m in messages)
                    // _CHARS_PER_TOKEN,
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                    outcome="timeout" if isinstance(exc, TimeoutError) else "error",
                )
            raise
        # Chars/4 estimate, used only for providers that report no usage of their own.
        est_in = sum(len(str(m.get("content", ""))) for m in messages) // _CHARS_PER_TOKEN
        est_out = len(reply) // _CHARS_PER_TOKEN
        acc = self._estimates.setdefault(self._role, [0, 0])
        acc[0] += est_in
        acc[1] += est_out
        if costed:
            reported_cost = _reported_usage(self._inner)
            _costs.record_call(
                "generative",
                model=str(getattr(self._inner, "model", None) or self._inner.provider_id),
                tokens_in=reported_cost[0] if reported_cost else est_in,
                tokens_out=reported_cost[1] if reported_cost else est_out,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        if self._usage is not None:
            # #33: no await since the provider returned, so ``last_usage`` is still
            # this call's report even with concurrent calls on the same provider.
            reported = _reported_usage(self._inner)
            tokens_in, tokens_out = reported if reported is not None else (est_in, est_out)
            prompt_id = getattr(messages, "prompt_id", None)
            prompt_version = getattr(messages, "prompt_version", None)
            self._usage.record(
                prompt_id=prompt_id,
                prompt_version=prompt_version,
                role=self._role,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                estimated=reported is None,
            )
            _log.debug(
                "llm.usage",
                prompt=prompt_version,
                role=self._role,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                estimated=reported is None,
            )
        return reply

    async def _tapped_chat(self, messages: list[dict[str, str]], options: dict[str, Any]) -> str:
        """WT-02: one ``model.call`` event per LLM call (role, model, tokens, duration, outcome)."""
        t0 = time.perf_counter()
        model = str(getattr(self._inner, "model", None) or self._inner.provider_id)
        try:
            reply = await self._inner.chat(messages, **options)
        except BaseException as exc:
            timed_out = isinstance(exc, TimeoutError) or isinstance(exc.__cause__, TimeoutError)
            tap.event(
                "model.call",
                role=self._role,
                model=model,
                outcome="timeout" if timed_out else "error",
                error=type(exc).__name__,
                duration_ms=round((time.perf_counter() - t0) * 1000.0, 3),
            )
            raise
        usage = _reported_usage(self._inner)
        est_in = sum(len(str(m.get("content", ""))) for m in messages) // _CHARS_PER_TOKEN
        if insp.io_active():  # TI-1: tokens of the extraction call being captured
            insp.io_tokens(
                usage[0] if usage else est_in,
                usage[1] if usage else len(reply) // _CHARS_PER_TOKEN,
                usage is None,
            )
        tap.event(
            "model.call",
            role=self._role,
            model=model,
            outcome="ok",
            prompt_tokens=usage[0] if usage else est_in,
            completion_tokens=usage[1] if usage else len(reply) // _CHARS_PER_TOKEN,
            tokens_estimated=usage is None,
            duration_ms=round((time.perf_counter() - t0) * 1000.0, 3),
        )
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


class ThinkingCapped:
    """MD-01 (P-15): a provider whose reply budget is bounded when thinking is on. A call that sets
    no ``max_tokens`` gets ``max_tokens + cap``: the answer budget plus the thinking cap. Everything
    else (``model``, ``usage_totals``, ...) is the wrapped provider's."""

    def __init__(self, inner: LLMService, *, cap_tokens: int, max_tokens: int) -> None:
        self._inner = inner
        self.cap_tokens = cap_tokens
        self._budget = max_tokens + cap_tokens

    @property
    def provider_id(self) -> str:
        return self._inner.provider_id

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        options.setdefault("max_tokens", self._budget)
        return await self._inner.chat(messages, **options)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)


class LLMRouter:
    """Role -> provider table resolved from config at engine start."""

    #: MD-01: the resolved model plan when ``models.tiers_v2`` built this router, else None.
    model_plan: Any = None

    def __init__(
        self,
        providers: dict[str, LLMService],
        *,
        structured_options: StructuredOptions | None = None,
    ) -> None:
        self._providers = providers
        self._counts: dict[str, int] = {}
        self._estimates: dict[str, list[int]] = {}
        self._usage = UsageLedger()
        self._structured_options = structured_options

    @property
    def roles(self) -> list[str]:
        return sorted(self._providers)

    def provider(self, role: str) -> LLMService:
        """The bound provider itself (uncounted); for wiring checks."""
        provider = self._providers.get(role)
        if provider is None:
            raise ConfigError(
                f"no LLM provider bound for role {role!r} — add llm.roles.{role} "
                "with provider and selected-provider settings to your config"
            )
        return provider

    def for_role(self, role: str) -> LLMService:
        """The provider for ``role``, counting every call (see ``call_counts``)."""
        return _Counted(
            self.provider(role),
            role,
            self._counts,
            self._estimates,
            self._usage,
            self._structured_options,
        )

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

    def prompt_usage(self, *, reset: bool = False) -> dict[str, dict[str, Any]]:
        """#33: calls and tokens per prompt version since start (or the last reset).

        See :class:`~memspine.observability.usage.UsageLedger` for the entry shape.
        ``reset=True`` clears the ledger after taking the snapshot."""
        snapshot = self._usage.snapshot()
        if reset:
            self._usage.reset()
        return snapshot

    def models(self) -> dict[str, str]:
        """The model id bound to each role (its ``model`` attribute, else provider id)."""
        return {
            role: str(getattr(provider, "model", None) or provider.provider_id)
            for role, provider in self._providers.items()
        }
