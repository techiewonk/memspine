"""Bounded, explicit LLM escalation across named provider profiles.

Escalation is based only on a provider error or the configured empty/strict-JSON
conditions. It has a total deadline and a per-attempt generation cap. It does not
infer quality or confidence. Providers may ignore the generation cap, so it is not
a hard generation or billing guarantee.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import hashlib
import json
from collections.abc import Mapping
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, SecretStr

from memspine.exceptions import ConfigError, LLMError
from memspine.observability import tap
from memspine.services.llm.base import LLMService


class ProviderIdentity(Protocol):
    """The descriptor identity a profile hash names (``providers.contracts.ProviderRef``;
    structural, so this adapter layer never imports providers)."""

    @property
    def category(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def contract_version(self) -> int: ...


__all__ = [
    "EscalatingLLM",
    "EscalationError",
    "EscalationOutputError",
    "ProfiledLLM",
    "profiled_provider_identity",
]


class EscalationError(LLMError):
    """All configured profiles failed for a call."""


class EscalationOutputError(EscalationError):
    """All configured attempts returned empty or invalid strict JSON output."""


class ProfiledLLM:
    """Delegate a provider with an opaque, settings-sensitive cache identity."""

    def __init__(self, inner: LLMService, identity: str) -> None:
        self._inner = inner
        self._identity = identity

    @property
    def provider_id(self) -> str:
        return self._identity

    @property
    def metrics_provider_id(self) -> str:
        """The selected adapter identity for per-attempt metrics, never the cache digest."""
        return self._inner.provider_id

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        return await self._inner.chat(messages, **options)

    def __getattr__(self, name: str) -> Any:
        if name == "_inner" or (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __repr__(self) -> str:
        return f"ProfiledLLM(identity={self._identity!r})"


def profiled_provider_identity(
    provider: ProviderIdentity, settings: BaseModel, service: LLMService
) -> str:
    """Hash provider identity and normalized native settings without exposing secrets."""
    normalized_settings = _identity_value(settings)
    identity = {
        "provider": {
            "category": provider.category,
            "name": provider.name,
            "contract_version": provider.contract_version,
        },
        "settings": normalized_settings,
        "provider_id": service.provider_id,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"profile:{provider.category}:{digest}"


def _identity_value(value: Any) -> Any:
    """Convert provider settings into a stable JSON shape, hashing secret values first."""
    if isinstance(value, SecretStr):
        digest = hashlib.sha256(value.get_secret_value().encode("utf-8")).hexdigest()
        return {"secret_sha256": digest}
    if isinstance(value, BaseModel):
        return _identity_value(value.model_dump(mode="python"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _identity_value(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _identity_value(item) for key, item in value.items()}
    if isinstance(value, set | frozenset):
        items = [_identity_value(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if isinstance(value, tuple | list):
        return [_identity_value(item) for item in value]
    if isinstance(value, Enum):
        return _identity_value(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest()}
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise ConfigError(
        f"unsupported provider setting type for profile identity: {type(value).__name__}"
    )


class EscalatingLLM:
    """Try a bounded sequence of providers for one routed ``chat`` invocation.

    The attempt count and deadline are per invocation, not an Engine-operation
    budget; a higher-level structured-output retry can start another invocation.
    """

    def __init__(
        self,
        role: str,
        profile_names: tuple[str, ...],
        providers: tuple[LLMService, ...],
        policy: Any,
        profile_max_tokens: tuple[int | None, ...] | None = None,
    ) -> None:
        if len(profile_names) != len(providers) or not providers:
            raise ConfigError("escalation profiles and providers must be non-empty and aligned")
        if profile_max_tokens is None:
            profile_max_tokens = (None,) * len(providers)
        if len(profile_max_tokens) != len(providers):
            raise ConfigError("escalation profile token caps must align with providers")
        self.role = role
        self.profile_names = profile_names
        self._attempt_providers = providers
        self.profile_max_tokens = profile_max_tokens
        self.policy = policy

    @property
    def provider_id(self) -> str:
        """Opaque cache identity for the ordered candidate chain and its policy."""
        identity = {
            "role": self.role,
            "profiles": self.profile_names,
            "providers": tuple(provider.provider_id for provider in self._attempt_providers),
            "triggers": tuple(sorted(self.policy.triggers)),
            "max_attempts": self.policy.max_attempts,
            "timeout_s": self.policy.timeout_s,
            "max_tokens_per_attempt": self.policy.max_tokens_per_attempt,
            "profile_max_tokens": self.profile_max_tokens,
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"escalation:{self.role}:{digest}"

    @property
    def usage_totals(self) -> None:
        """Let the router aggregate the estimates from every counted attempt."""
        return None

    @property
    def _memspine_attempt_providers(self) -> tuple[LLMService, ...]:
        return self._attempt_providers

    def _bind_counted_attempts(self, providers: tuple[LLMService, ...]) -> None:
        self._attempt_providers = providers

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        requested = options.get("max_tokens")
        if requested is not None and (
            isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0
        ):
            raise ValueError("max_tokens must be a positive integer")
        caller_cap = requested if requested is not None else self.policy.max_tokens_per_attempt

        triggers = set(self.policy.triggers)
        attempts = min(self.policy.max_attempts, len(self._attempt_providers))
        last_error: BaseException | None = None
        last_reason = "error"
        active_attempt = 0
        active_profile = ""
        deadline = asyncio.timeout(self.policy.timeout_s)
        try:
            async with deadline:
                for index, provider in enumerate(self._attempt_providers[:attempts]):
                    attempt = index + 1
                    profile = self.profile_names[index]
                    active_attempt, active_profile = attempt, profile
                    native_cap = self.profile_max_tokens[index]
                    options["max_tokens"] = min(
                        self.policy.max_tokens_per_attempt,
                        caller_cap,
                        native_cap if native_cap is not None else caller_cap,
                    )
                    try:
                        reply = await provider.chat(messages, **options)
                    except asyncio.CancelledError:
                        raise
                    except (ConfigError, TypeError, ValueError):
                        raise
                    except (LLMError, RuntimeError, OSError, TimeoutError) as exc:
                        last_error = exc
                        last_reason = "error"
                        self._event(attempt, profile, last_reason)
                        if "error" not in triggers:
                            raise
                        if attempt >= attempts:
                            raise EscalationError(
                                f"LLM escalation exhausted for role {self.role!r} after {attempt} attempt(s)"
                            ) from exc
                        continue

                    if not reply.strip() and "empty" in triggers:
                        last_reason = "empty"
                        self._event(attempt, profile, last_reason)
                        if attempt < attempts:
                            continue
                        raise EscalationOutputError(
                            f"LLM escalation returned empty output for role {self.role!r}"
                        )

                    if "invalid_json" in triggers:
                        try:
                            json.loads(reply, parse_constant=_reject_json_constant)
                        except (json.JSONDecodeError, TypeError, ValueError) as exc:
                            last_reason = "invalid_json"
                            self._event(attempt, profile, last_reason)
                            if attempt < attempts:
                                continue
                            raise EscalationOutputError(
                                f"LLM escalation returned invalid JSON for role {self.role!r}"
                            ) from exc
                    self._event(attempt, profile, "ok")
                    return reply
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            if not deadline.expired():
                raise
            self._event(active_attempt or 1, active_profile, "timeout")
            raise EscalationError(
                f"LLM escalation deadline exceeded for role {self.role!r}"
            ) from exc

        if last_error is not None:
            raise EscalationError(
                f"LLM escalation exhausted for role {self.role!r}"
            ) from last_error
        raise EscalationOutputError(
            f"LLM escalation exhausted without valid output for role {self.role!r} ({last_reason})"
        )

    def _event(self, attempt: int, profile: str, reason: str) -> None:
        if tap.enabled():
            tap.event(
                "model.escalation",
                role=self.role,
                reason=reason,
                attempt=attempt,
                provider=profile,
            )

    def __getattr__(self, name: str) -> Any:
        if name == "_attempt_providers" or (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        return getattr(self._attempt_providers[0], name)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")
