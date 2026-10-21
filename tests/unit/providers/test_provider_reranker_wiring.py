from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from memspine import Engine
from memspine.exceptions import ConfigError
from memspine.models.providers.rerank.factory import (
    RerankSettings,
    build_reranker_result,
    register_reranker,
    validate_reranker_settings,
)
from memspine.providers.contexts import RerankerBuildContext, SharedBuildContext
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.providers.registry import ProviderDescriptor, get_registry
from memspine.providers.settings import StrictSettings


class PluginSettings(StrictSettings):
    model: str


class FakeReranker:
    reranker_id = "plugin:fake"

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        return [0.0] * len(documents)


@pytest.mark.asyncio
async def test_async_plugin_builder_receives_typed_settings_and_lifecycle_context() -> None:
    ref = ProviderRef("reranker", "async_test")
    calls: list[tuple[PluginSettings, RerankerBuildContext]] = []

    class Builder:
        async def build(
            self, settings: PluginSettings, context: RerankerBuildContext
        ) -> BuildResult[FakeReranker]:
            await asyncio.sleep(0)
            calls.append((settings, context))
            return BuildResult(FakeReranker(), ref, (), frozenset({"rerank"}))

    register_reranker(
        ProviderDescriptor(
            ref,
            PluginSettings,
            frozenset({"rerank"}),
            (),
            Builder(),
        )
    )
    try:
        context = RerankerBuildContext(
            SharedBuildContext(ProviderLifecycleManager(close_timeout_s=1.0), "owner-a", 1.0),
            chunk_chars=256,
            chunk_overlap=32,
        )
        built = await build_reranker_result(
            RerankSettings(
                mode="async_test", model="test-model", chunk_chars=256, chunk_overlap=32
            ),
            context,
        )
        assert built is not None and built.provider == ref
        assert built.service.reranker_id == "plugin:fake+chunkmax256"
        assert calls == [(PluginSettings(model="test-model"), context)]
    finally:
        get_registry("rerankers").unregister("async_test")


@pytest.mark.asyncio
async def test_permission_error_from_plugin_builder_propagates() -> None:
    ref = ProviderRef("reranker", "permission_test")

    class PermissionBuilder:
        async def build(self, _settings: PluginSettings, _context: RerankerBuildContext) -> Any:
            raise PermissionError("private path")

    register_reranker(
        ProviderDescriptor(ref, PluginSettings, frozenset({"rerank"}), (), PermissionBuilder())
    )
    try:
        with pytest.raises(PermissionError, match="private path"):
            await build_reranker_result(
                RerankSettings(mode="permission_test", model="test-model"),
                RerankerBuildContext(
                    SharedBuildContext(ProviderLifecycleManager(close_timeout_s=1.0), "owner", 1.0),
                    chunk_chars=None,
                    chunk_overlap=0,
                ),
            )
    finally:
        get_registry("rerankers").unregister("permission_test")


def test_startup_validation_checks_shared_provider_identity_without_building() -> None:
    with pytest.raises(ConfigError, match="runtime.deployment_id is required"):
        validate_reranker_settings(
            "litellm",
            {"model": "cohere/rerank-v3.5", "request_timeout_s": 5.0},
            explicit_deployment_id=False,
        )

    validated = validate_reranker_settings(
        "litellm",
        {"model": "cohere/rerank-v3.5", "request_timeout_s": 5.0},
        explicit_deployment_id=True,
    )
    assert validated is not None
    descriptor, typed = validated
    assert descriptor.ref == ProviderRef("reranker", "litellm")
    assert isinstance(typed, BaseModel)
    assert typed.request_timeout_s == 5.0


def test_startup_validation_rejects_unknown_provider_options() -> None:
    with pytest.raises(ConfigError, match="invalid read.rerank_settings"):
        validate_reranker_settings(
            "qwen3",
            {"model": "qwen3", "secretish_typo": "never shown"},
            explicit_deployment_id=True,
        )


@pytest.mark.asyncio
async def test_engine_start_rejects_shared_reranker_before_builder_invocation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memspine.providers.registry import get_registry

    registry = get_registry("rerankers")
    descriptor = registry.get("litellm")
    called = False

    class NeverBuild:
        async def build(self, _settings: BaseModel, _context: object) -> Any:
            nonlocal called
            called = True
            raise AssertionError("startup validation must run before lazy build")

    monkeypatch.setitem(
        registry._items,
        "litellm",
        ProviderDescriptor(
            ref=descriptor.ref,
            settings_model=descriptor.settings_model,
            declared_capabilities=descriptor.declared_capabilities,
            requirements=descriptor.requirements,
            builder=NeverBuild(),
            service_manifest=descriptor.service_manifest,
            placement=descriptor.placement,
            target_identity=descriptor.target_identity,
            requires_deployment_id=descriptor.requires_deployment_id,
        ),
    )
    engine = Engine(
        template="core",
        dotenv_path=None,
        storage={"settings": {"path": str(tmp_path / "rerank-start.db")}},
        embedding={"provider": "hash", "settings": {}},
        read={
            "hybrid": False,
            "rerank": "litellm",
            "rerank_settings": {
                "model": "cohere/rerank-v3.5",
                "request_timeout_s": 5.0,
            },
        },
        memories={"semantic": {"enabled": True}},
    )
    with pytest.raises(ConfigError, match="runtime.deployment_id is required"):
        await engine.start()
    assert not called


@pytest.mark.asyncio
async def test_engine_shares_async_lazy_build_and_closes_owned_resource_once(tmp_path) -> None:
    ref = ProviderRef("reranker", "async_singleflight_test")
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    closes = 0

    async def close_connection() -> None:
        nonlocal closes
        closes += 1

    class SlowBuilder:
        async def build(
            self, _settings: PluginSettings, context: RerankerBuildContext
        ) -> BuildResult[FakeReranker]:
            nonlocal calls
            calls += 1
            resource = context.shared.resources.own("singleflight-connection", close_connection)
            entered.set()
            await release.wait()
            return BuildResult(FakeReranker(), ref, (resource,), frozenset({"rerank"}))

    register_reranker(
        ProviderDescriptor(ref, PluginSettings, frozenset({"rerank"}), (), SlowBuilder())
    )
    engine = Engine(
        template="core",
        dotenv_path=None,
        storage={"settings": {"path": str(tmp_path / "singleflight.db")}},
        embedding={"provider": "hash", "settings": {}},
        read={
            "hybrid": False,
            "rerank": "async_singleflight_test",
            "rerank_settings": {"model": "test-model"},
        },
        memories={"semantic": {"enabled": True}},
    )
    try:
        await engine.start()
        first = asyncio.create_task(engine._rerank_provider())
        await entered.wait()
        second = asyncio.create_task(engine._rerank_provider())
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        first_provider, second_provider = await asyncio.gather(first, second)
        assert first_provider is second_provider
        assert calls == 1
        await engine.stop()
        assert closes == 1
    finally:
        release.set()
        if engine._started:
            await engine.stop()
        get_registry("rerankers").unregister("async_singleflight_test")


@pytest.mark.asyncio
async def test_engine_stop_cancels_and_drains_blocked_async_builder(tmp_path) -> None:
    ref = ProviderRef("reranker", "async_stop_test")
    entered = asyncio.Event()
    calls = 0
    closes = 0

    async def close_connection() -> None:
        nonlocal closes
        closes += 1

    class BlockedBuilder:
        async def build(
            self, _settings: PluginSettings, context: RerankerBuildContext
        ) -> BuildResult[FakeReranker]:
            nonlocal calls
            calls += 1
            context.shared.resources.own("stop-test-connection", close_connection)
            entered.set()
            await asyncio.Event().wait()
            return BuildResult(FakeReranker(), ref, (), frozenset({"rerank"}))

    register_reranker(
        ProviderDescriptor(ref, PluginSettings, frozenset({"rerank"}), (), BlockedBuilder())
    )
    engine = Engine(
        template="core",
        dotenv_path=None,
        storage={"settings": {"path": str(tmp_path / "stop-build.db")}},
        embedding={"provider": "hash", "settings": {}},
        read={
            "hybrid": False,
            "rerank": "async_stop_test",
            "rerank_settings": {"model": "test-model"},
        },
        memories={"semantic": {"enabled": True}},
    )
    try:
        await engine.start()
        caller = asyncio.create_task(engine._rerank_provider())
        await entered.wait()
        await engine.stop()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert calls == 1
        assert closes == 1
    finally:
        if engine._started:
            await engine.stop()
        get_registry("rerankers").unregister("async_stop_test")
