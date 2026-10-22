"""LLM port: per-role provider routing (D-07/D-22) + json-repair safety net (D-31).

Roles are capabilities, not models: ``extract`` / ``judge`` / ``chat`` each bind
their own provider in config (``llm.roles.<role>``), so a cheap local model can
extract while a stronger endpoint chats. The instructor structured-output
wrapper joins under ``[structured]`` in Phase 2 (D-31); the always-on lenient
JSON parser ships now.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from memspine.services.llm.structured import StructuredOptions

from json_repair import repair_json

from memspine.config.constants import TOKEN_ESTIMATE_CHARS_PER_TOKEN
from memspine.economy import ledger as _costs
from memspine.exceptions import ConfigError
from memspine.observability import inspect as insp
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.observability.usage import UsageLedger
from memspine.services.concurrency import model_slot

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


def _provider_model(provider: LLMService) -> str:
    """Keep metric labels on the selected adapter, not an opaque cache identity."""
    return str(
        getattr(provider, "model", None)
        or getattr(provider, "metrics_provider_id", None)
        or provider.provider_id
    )


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
        reported: tuple[int, int] | None = None

        try:
            async with model_slot("llm"):  # RT-02: no-op unless runtime.model_concurrency is on
                if tap.enabled():
                    reply = await self._tapped_chat(messages, options)
                else:
                    reply = await self._inner.chat(messages, **options)
                # Snapshot before model_slot.__aexit__: it may await and another
                # concurrent call can update a provider's last_usage meanwhile.
                reported = _reported_usage(self._inner)
        except BaseException as exc:
            if costed:
                _costs.record_call(
                    "generative",
                    model=_provider_model(self._inner),
                    tokens_in=sum(len(str(m.get("content", ""))) for m in messages)
                    // _CHARS_PER_TOKEN,
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                    outcome="timeout" if isinstance(exc, TimeoutError) else "error",
                )
            raise
        # Chars/4 estimate, used only for providers that report no usage of their own.
        est_in = sum(len(str(m.get("content", ""))) for m in messages) // _CHARS_PER_TOKEN
        est_out = len(reply) // _CHARS_PER_TOKEN
        totals = self._estimates.setdefault(self._role, [0, 0])
        tokens_in, tokens_out = reported if reported is not None else (est_in, est_out)
        totals[0] += tokens_in
        totals[1] += tokens_out
        if costed:
            _costs.record_call(
                "generative",
                model=_provider_model(self._inner),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
            )
        if self._usage is not None:
            # #33: no await since the provider returned, so ``last_usage`` is still
            # this call's report even with concurrent calls on the same provider.
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
        model = _provider_model(self._inner)
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
        self._counted_attempt_roles: set[str] = set()
        self._counts: dict[str, int] = {}
        self._estimates: dict[str, list[int]] = {}
        self._usage = UsageLedger()
        self._structured_options = structured_options

    @property
    def roles(self) -> list[str]:
        return sorted(self._providers)

    def services(self) -> list[LLMService]:
        """Every distinct bound provider, including fallback attempts (RM-74 warm-up)."""
        seen: dict[int, LLMService] = {}
        for provider in self._providers.values():
            for item in (provider, *getattr(provider, "_memspine_attempt_providers", ())):
                seen.setdefault(id(item), item)
        return list(seen.values())

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
        provider = self.provider(role)
        attempt_providers = getattr(provider, "_memspine_attempt_providers", None)
        bind_attempts = getattr(provider, "_bind_counted_attempts", None)
        if attempt_providers is not None and callable(bind_attempts):
            if role not in self._counted_attempt_roles:
                bind_attempts(
                    tuple(
                        _Counted(
                            attempt_provider,
                            role,
                            self._counts,
                            self._estimates,
                            self._usage,
                            self._structured_options,
                        )
                        for attempt_provider in attempt_providers
                    )
                )
                self._counted_attempt_roles.add(role)
            cast(Any, provider).structured_options = self._structured_options
            return provider
        return _Counted(
            provider,
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

        Every counted invocation contributes provider-reported usage when available,
        otherwise a four-characters-per-token estimate. Totals are role-local even
        when multiple roles share one provider profile.
        """
        out: dict[str, dict[str, int]] = {}
        for role in self._providers:
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
