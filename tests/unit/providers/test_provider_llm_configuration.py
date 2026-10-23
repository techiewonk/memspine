from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from memspine import Engine
from memspine.config.schema.llm import LLMRoleConfig, ModelDecl
from memspine.exceptions import LLMError
from memspine.prompts.base import Prompt, PromptFormat
from memspine.providers.contexts import LLMBuildContext
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.registry import LLM_PROVIDERS, ProviderDescriptor
from memspine.providers.settings import StrictSettings
from memspine.services.llm.structured import structured_call


class PluginOptions(StrictSettings):
    deployment_name: str
    request_timeout_s: float


class PluginLLM:
    provider_id = "plugin-model"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        return "local test answer"


def test_role_requires_explicit_provider_and_catalogue_llm_requires_timeout() -> None:
    with pytest.raises(ValidationError, match="provider"):
        LLMRoleConfig(settings={"model": "ollama/model", "timeout_seconds": 30.0})
    with pytest.raises(ValidationError, match="explicit provider"):
        ModelDecl(id="ollama/model", kind="llm", timeout_seconds=30.0)
    with pytest.raises(ValidationError, match="explicit timeout_seconds"):
        ModelDecl(id="ollama/model", kind="llm", provider="litellm")
    with pytest.raises(ValidationError):
        ModelDecl(id="ollama/model", kind="llm", provider="litellm", timeout_seconds=True)
    assert (
        ModelDecl(
            id="ollama/model", kind="llm", provider="litellm", timeout_seconds=13.0
        ).timeout_seconds
        == 13.0
    )


@pytest.mark.asyncio
async def test_explicit_plugin_uses_its_schema_and_role_context_and_closes_once() -> None:
    ref = ProviderRef("llm", "custom_role_test")
    seen: list[tuple[str, str, float]] = []
    closed: list[str] = []

    class Builder:
        async def build(
            self, settings: PluginOptions, ctx: LLMBuildContext
        ) -> BuildResult[PluginLLM]:
            async def close() -> None:
                closed.append(ctx.role)

            resource = ctx.shared.resources.own(f"plugin-role:{ctx.role}", close)
            seen.append((ctx.role, settings.deployment_name, settings.request_timeout_s))
            return BuildResult(PluginLLM(), ref, (resource,), frozenset({"llm"}))

    LLM_PROVIDERS.register(
        ref.name, ProviderDescriptor(ref, PluginOptions, frozenset({"llm"}), (), Builder())
    )
    try:
        engine = Engine(
            template="core",
            dotenv_path=None,
            storage={"records": {"settings": {"path": ":memory:", "busy_timeout_s": 5.0}}},
            model={
                "embedding": {"provider": "hash", "settings": {}},
                "llm": {
                    "chat": {
                        "provider": ref.name,
                        "settings": {"deployment_name": "tenant-a", "request_timeout_s": 7.0},
                    }
                },
            },
        )
        async with engine:
            assert seen == [("chat", "tenant-a", 7.0)]
            row = next(
                row
                for row in await engine.provider_catalog()
                if row["logical_service_id"] == "llm.chat"
            )
            assert row["resolved_provider"] == ref.name
        assert closed == ["chat"]
    finally:
        LLM_PROVIDERS.unregister(ref.name)


@pytest.mark.asyncio
async def test_two_engines_keep_independent_structured_retry_policies() -> None:
    ref = ProviderRef("llm", "independent_policy_test")

    class Answer(BaseModel):
        n: int

    class Replies(PluginLLM):
        calls = 0

        async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
            self.calls += 1
            return '{"n":"wrong"}' if self.calls == 1 else '{"n":7}'

    class Builder:
        async def build(
            self, settings: PluginOptions, ctx: LLMBuildContext
        ) -> BuildResult[Replies]:
            return BuildResult(Replies(), ref, (), frozenset({"llm"}))

    LLM_PROVIDERS.register(
        ref.name, ProviderDescriptor(ref, PluginOptions, frozenset({"llm"}), (), Builder())
    )

    def engine(retry: bool) -> Engine:
        return Engine(
            template="core",
            dotenv_path=None,
            storage={"records": {"settings": {"path": ":memory:", "busy_timeout_s": 5.0}}},
            model={
                "embedding": {"provider": "hash", "settings": {}},
                "llm": {
                    "chat": {
                        "provider": ref.name,
                        "settings": {"deployment_name": "test", "request_timeout_s": 1.0},
                        "structured": {"retry_on_error": retry},
                    }
                },
            },
        )

    prompt = Prompt(
        id="independent", version=1, role="chat", format=PromptFormat.JSON, body="give n"
    )
    try:
        async with engine(True) as first, engine(False) as second:
            assert first._llm is not None and second._llm is not None
            assert (await structured_call(first._llm.for_role("chat"), prompt, {}, Answer)).n == 7
            with pytest.raises(LLMError):
                await structured_call(second._llm.for_role("chat"), prompt, {}, Answer)
            assert first._llm.call_counts() == {"chat": 2}
            assert second._llm.call_counts() == {"chat": 1}
    finally:
        LLM_PROVIDERS.unregister(ref.name)
