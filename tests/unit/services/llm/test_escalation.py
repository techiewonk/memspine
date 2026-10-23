"""Bounded named-profile LLM escalation and per-attempt accounting."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
from pydantic import SecretStr

from memspine.config.schema import MemspineConfig
from memspine.observability import tap
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.providers.settings import LiteLLMRoleSettings
from memspine.runtime import container
from memspine.services.llm import base as llm_base
from memspine.services.llm.base import LLMRouter
from memspine.services.llm.escalation import (
    EscalatingLLM,
    EscalationError,
    EscalationOutputError,
    ProfiledLLM,
    _identity_value,
    profiled_provider_identity,
)
from memspine.services.llm.tier_gate import WITHHELD_MARKER, TierGatedLLM


class _Fake:
    def __init__(self, name: str, replies: list[str | BaseException]) -> None:
        self.model = name
        self.replies = list(replies)
        self.calls: list[tuple[list[dict[str, str]], dict[str, Any]]] = []

    @property
    def provider_id(self) -> str:
        return self.model

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.calls.append((messages, options))
        value = self.replies.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def _policy(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "triggers": ("error", "empty", "invalid_json"),
        "max_attempts": 2,
        "timeout_s": 0.5,
        "max_tokens_per_attempt": 80,
    }
    values.update(overrides)
    return type("Policy", (), values)()


async def test_escalation_returns_primary_success_without_fallback() -> None:
    primary, target = _Fake("local", ['{"ok": true}']), _Fake("remote", ["unused"])
    llm = EscalatingLLM("chat", ("primary", "target"), (primary, target), _policy())

    assert await llm.chat([], max_tokens=100) == '{"ok": true}'
    assert len(primary.calls) == 1 and target.calls == []
    assert primary.calls[0][1]["max_tokens"] == 80


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (RuntimeError("transport"), '{"ok": true}'),
        ("", '{"ok": true}'),
        ("not json", '{"ok": true}'),
    ],
)
async def test_each_explicit_failure_reason_can_escalate(first: Any, second: str) -> None:
    primary, target = _Fake("local", [first]), _Fake("remote", [second])
    llm = EscalatingLLM("extract", ("primary", "backup"), (primary, target), _policy())

    assert await llm.chat([], max_tokens=25) == second
    assert primary.calls[0][1]["max_tokens"] == 25
    assert target.calls[0][1]["max_tokens"] == 25


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
async def test_nonstandard_json_constants_trigger_escalation(constant: str) -> None:
    primary, target = _Fake("local", [constant]), _Fake("remote", ['{"ok":true}'])
    llm = EscalatingLLM("judge", ("primary", "backup"), (primary, target), _policy())

    assert await llm.chat([]) == '{"ok":true}'
    assert target.calls


async def test_exhausted_invalid_json_is_typed_and_arbitrary_type_errors_are_not_retried() -> None:
    primary, target = _Fake("a", ["bad"]), _Fake("b", ["also bad"])
    llm = EscalatingLLM("judge", ("a", "b"), (primary, target), _policy())
    with pytest.raises(EscalationOutputError):
        await llm.chat([])
    assert len(primary.calls) == len(target.calls) == 1

    primary, target = _Fake("a", [TypeError("bug")]), _Fake("b", ["unused"])
    llm = EscalatingLLM("judge", ("a", "b"), (primary, target), _policy())
    with pytest.raises(TypeError):
        await llm.chat([])
    assert target.calls == []


async def test_unconfigured_errors_propagate_without_wrapping_or_fallback() -> None:
    error = RuntimeError("transport")
    primary, target = _Fake("a", [error]), _Fake("b", ["unused"])
    llm = EscalatingLLM("chat", ("a", "b"), (primary, target), _policy(triggers=("empty",)))
    with pytest.raises(RuntimeError) as raised:
        await llm.chat([])
    assert raised.value is error
    assert target.calls == []

    timeout = TimeoutError("provider timeout")
    primary, target = _Fake("a", [timeout]), _Fake("b", ["unused"])
    llm = EscalatingLLM("chat", ("a", "b"), (primary, target), _policy(triggers=("empty",)))
    with pytest.raises(TimeoutError) as raised:
        await llm.chat([])
    assert raised.value is timeout
    assert target.calls == []


async def test_each_profile_cap_clamps_caller_and_policy_budget() -> None:
    primary, target = _Fake("a", [RuntimeError("transport")]), _Fake("b", ['{"ok":true}'])
    llm = EscalatingLLM(
        "chat",
        ("a", "b"),
        (primary, target),
        _policy(max_tokens_per_attempt=80),
        (30, 10),
    )
    assert await llm.chat([], max_tokens=50) == '{"ok":true}'
    assert primary.calls[0][1]["max_tokens"] == 30
    assert target.calls[0][1]["max_tokens"] == 10


async def test_provider_identity_fingerprints_full_chain_and_effective_policy() -> None:
    def identity(
        primary_id: str = "primary-model",
        target_id: str = "target-model",
        policy: Any | None = None,
    ) -> str:
        primary, target = _Fake(primary_id, ["unused"]), _Fake(target_id, ["unused"])
        return EscalatingLLM(
            "extract",
            ("fast", "reasoning"),
            (primary, target),
            policy or _policy(),
            (32, 128),
        ).provider_id

    original = identity()
    assert identity() == original
    assert identity(target_id="different-target") != original
    assert identity(policy=_policy(max_tokens_per_attempt=81)) != original
    assert identity(policy=_policy(timeout_s=0.75)) != original


async def test_named_profile_identity_includes_normalized_endpoint_settings_without_secrets() -> (
    None
):
    ref = ProviderRef(category="llm", name="litellm")

    def profiled(
        api_base: str, temperature: float, api_key: str = "private-api-key"
    ) -> ProfiledLLM:
        settings = LiteLLMRoleSettings(
            model="openai/shared-model",
            timeout_seconds=30.0,
            temperature=temperature,
            api_base=api_base,
            api_key=SecretStr(api_key),
        )
        provider = _Fake("litellm:openai/shared-model", ["unused"])
        return ProfiledLLM(provider, profiled_provider_identity(ref, settings, provider))

    original = profiled("https://one.example/v1", 0.2)
    assert profiled("https://one.example/v1", 0.2).provider_id == original.provider_id
    assert profiled("https://two.example/v1", 0.2).provider_id != original.provider_id
    assert profiled("https://one.example/v1", 0.7).provider_id != original.provider_id
    assert (
        profiled("https://one.example/v1", 0.2, "different-api-key").provider_id
        != original.provider_id
    )
    assert "private-api-key" not in original.provider_id


async def test_settings_identity_normalizes_sets_independent_of_iteration_order() -> None:
    assert _identity_value({"gamma", "alpha", "beta"}) == _identity_value(
        frozenset({"beta", "gamma", "alpha"})
    )


async def test_attempt_metrics_keep_each_candidate_provider_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BareProvider:
        def __init__(self, provider_id: str, reply: str | BaseException) -> None:
            self.provider_id = provider_id
            self.reply = reply

        async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
            if isinstance(self.reply, BaseException):
                raise self.reply
            return self.reply

    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(tap, "enabled", lambda: True)
    monkeypatch.setattr(tap, "event", lambda name, **fields: events.append((name, fields)))
    primary = ProfiledLLM(
        BareProvider("local-adapter", RuntimeError("transport")), "opaque-primary"
    )
    target = ProfiledLLM(BareProvider("remote-adapter", '{"ok":true}'), "opaque-target")
    router = LLMRouter(
        {"chat": EscalatingLLM("chat", ("fast", "reasoning"), (primary, target), _policy())}
    )

    await router.for_role("chat").chat([])

    assert [fields["model"] for name, fields in events if name == "model.call"] == [
        "local-adapter",
        "remote-adapter",
    ]


async def test_deadline_and_cancellation_are_not_retried() -> None:
    class _Wait:
        provider_id = "wait"

        async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
            await asyncio.sleep(1)
            return "late"

    target = _Fake("target", ["unused"])
    llm = EscalatingLLM("chat", ("slow", "target"), (_Wait(), target), _policy(timeout_s=0.01))
    with pytest.raises(EscalationError, match="deadline"):
        await llm.chat([])
    assert target.calls == []

    task = asyncio.create_task(
        EscalatingLLM("chat", ("slow", "target"), (_Wait(), target), _policy()).chat([])
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert target.calls == []


async def test_router_counts_every_actual_attempt() -> None:
    primary, target = _Fake("a", [RuntimeError("transport")]), _Fake("b", ['{"ok":true}'])
    router = LLMRouter({"chat": EscalatingLLM("chat", ("a", "b"), (primary, target), _policy())})

    assert await router.for_role("chat").chat([]) == '{"ok":true}'
    assert router.call_counts() == {"chat": 2}
    assert router.token_counts()["chat"]["completion"] == 2


async def test_shared_profile_usage_is_accounted_per_role_under_concurrency() -> None:
    class _Shared:
        provider_id = "shared"
        last_usage: tuple[int, int] | None = None
        started = 0
        both_started = asyncio.Event()

        async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
            self.started += 1
            if self.started == 2:
                self.both_started.set()
            await self.both_started.wait()
            text = messages[0]["content"]
            self.last_usage = (5, 3) if text == "chat call" else (11, 7)
            return "response"

    shared = _Shared()
    router = LLMRouter({"chat": shared, "extract": shared})  # type: ignore[dict-item]
    await asyncio.wait_for(
        asyncio.gather(
            router.for_role("chat").chat([{"role": "user", "content": "chat call"}]),
            router.for_role("extract").chat([{"role": "user", "content": "extract call"}]),
        ),
        timeout=1,
    )
    assert shared.started == 2
    assert router.token_counts() == {
        "chat": {"prompt": 5, "completion": 3},
        "extract": {"prompt": 11, "completion": 7},
    }


async def test_usage_is_snapshotted_before_model_slot_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _Provider("one")

    @asynccontextmanager
    async def slot(_kind: str) -> Any:
        yield
        provider.last_usage = (99, 99)

    monkeypatch.setattr(llm_base, "model_slot", slot)
    router = LLMRouter({"chat": provider})  # type: ignore[dict-item]
    await router.for_role("chat").chat([{"role": "user", "content": "hello"}])

    assert router.token_counts() == {"chat": {"prompt": 5, "completion": 3}}


class _Hooks:
    async def withheld_texts(self) -> list[str]:
        return ["private memory phrase"]


class _Provider:
    def __init__(self, model: str) -> None:
        self.model = model
        self.calls: list[tuple[list[dict[str, str]], dict[str, Any]]] = []
        self.last_usage: tuple[int, int] | None = None

    @property
    def provider_id(self) -> str:
        return self.model

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.calls.append((messages, options))
        self.last_usage = (5, 3)
        return "" if self.model == "ollama/local" else "ok"


async def test_target_remote_candidate_is_individually_privacy_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: dict[str, _Provider] = {}
    real_build = container._build_provider

    async def build(
        wiring: Any,
        descriptor: Any,
        settings: Any,
        context: Any,
        *,
        result_key: str | None = None,
    ) -> BuildResult[Any]:
        if descriptor.ref.category != "llm":
            return await real_build(wiring, descriptor, settings, context, result_key=result_key)
        provider = made.setdefault(settings.model, _Provider(settings.model))
        result = BuildResult(provider, descriptor.ref, (), descriptor.declared_capabilities)
        wiring.provider_results[result_key or descriptor.ref.name] = result
        return result

    monkeypatch.setattr(container, "_build_provider", build)
    config = MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 2.0,
                "close_timeout_s": 2.0,
                "startup_timeout_s": None,
            }
        },
        consent={"remote_llm_max_tier": "low"},
        model={
            "llm": {
                "chat": {
                    "provider": "litellm",
                    "timeout_s": 30,
                    "settings": {"model": "ollama/local", "max_tokens": 20},
                    "escalation": {
                        "targets": [
                            {
                                "provider": "litellm",
                                "timeout_s": 30,
                                "settings": {"model": "openai/remote", "max_tokens": 7},
                            },
                            {"provider": "missing-provider-plugin", "settings": {}},
                        ],
                        "triggers": ["empty"],
                        "max_attempts": 2,
                        "timeout_s": 1,
                        "max_tokens_per_attempt": 16,
                    },
                }
            }
        },
    )
    wiring = container.Wiring(
        resources=ProviderLifecycleManager(close_timeout_s=2),
        health_timeout_s=2,
        startup_timeout_s=None,
    )
    router = await container.build_llm_router(config, _Hooks(), wiring)  # type: ignore[arg-type]
    wrapper = router.provider("chat")
    assert isinstance(wrapper, EscalatingLLM)
    assert isinstance(wrapper._memspine_attempt_providers[1], TierGatedLLM)
    await router.for_role("chat").chat(
        [{"role": "user", "content": "private memory phrase"}], max_tokens=12
    )
    assert set(made) == {"ollama/local", "openai/remote"}
    assert made["ollama/local"].calls[0][1]["max_tokens"] == 12
    assert made["openai/remote"].calls[0][1]["max_tokens"] == 7
    target_message = made["openai/remote"].calls[0][0][0]["content"]
    assert "private memory phrase" not in target_message
    assert WITHHELD_MARKER in target_message


async def test_profiles_reuse_one_provider_and_profiles_satisfy_tier_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[_Provider] = []
    real_build = container._build_provider

    async def build(
        wiring: Any,
        descriptor: Any,
        settings: Any,
        context: Any,
        *,
        result_key: str | None = None,
    ) -> BuildResult[Any]:
        if descriptor.ref.category != "llm":
            return await real_build(wiring, descriptor, settings, context, result_key=result_key)
        provider = _Provider(settings.model)
        built.append(provider)
        result = BuildResult(provider, descriptor.ref, (), descriptor.declared_capabilities)
        wiring.provider_results[result_key or descriptor.ref.name] = result
        return result

    monkeypatch.setattr(container, "_build_provider", build)
    config = MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 2.0,
                "close_timeout_s": 2.0,
                "startup_timeout_s": None,
            }
        },
        write={"window_extract": "facts"},
        model={
            "llm": {
                "extract": {
                    "provider": "litellm",
                    "timeout_s": 30,
                    "settings": {"model": "ollama/shared"},
                },
                "chat": {
                    "provider": "litellm",
                    "timeout_s": 30,
                    "settings": {"model": "ollama/shared"},
                },
                "window_extract": {
                    "provider": "litellm",
                    "timeout_s": 30,
                    "settings": {"model": "ollama/reasoning"},
                },
            },
            "placement": {"tiers_v2": True},
        },
    )
    wiring = container.Wiring(
        resources=ProviderLifecycleManager(close_timeout_s=2),
        health_timeout_s=2,
        startup_timeout_s=None,
    )
    router = await container.build_llm_router(config, _Hooks(), wiring)  # type: ignore[arg-type]
    assert router.roles == ["chat", "extract", "window_extract"]
    assert len(built) == 2
    assert router.model_plan["explicit_profiles"] == {
        "chat": "shared",
        "extract": "shared",
        "window_extract": "reasoning",
    }
    extract_plan = next(role for role in router.model_plan["roles"] if role["role"] == "extract")
    assert extract_plan["status"] == "explicit_multiple"
    assert extract_plan["model"] == "multiple profiles"
    assert extract_plan["profile_bindings"] == {
        "extract": "shared",
        "window_extract": "reasoning",
    }
    await router.for_role("chat").chat([{"role": "user", "content": "hello"}])
    assert router.token_counts() == {"chat": {"prompt": 5, "completion": 3}}
