"""F-08: the dishka container -- app / job / request scopes, plug-in pins, close order."""

from __future__ import annotations

import importlib.metadata
from collections.abc import AsyncIterator
from typing import Any

import pytest

from memspine import plugins
from memspine.config.loader import ResolvedConfig
from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError
from memspine.models.cache.base import KVCache
from memspine.models.encoders.embedding.base import EmbedderManifest, EmbeddingService
from memspine.models.providers.llm.base import LLMRouter
from memspine.providers.contracts import BuildResult, ProviderRef, ResourceRef
from memspine.providers.lifecycle import LifecycleState, ProviderLifecycleManager
from memspine.providers.registry import EMBEDDERS, ProviderDescriptor
from memspine.providers.settings import EmptySettings
from memspine.runtime.container import (
    Capabilities,
    EngineSpec,
    JobInfo,
    JobState,
    RequestInfo,
    RequestState,
    Wiring,
    WiringHooks,
    _build_provider,
    _graph_descriptor,
    _validate_provider_result,
    job_scope,
    make_container,
    request_scope,
    resolve_config,
)
from memspine.services.graph.base import GraphStore
from memspine.services.lexical.base import LexicalStore
from memspine.services.secrets.env import EnvSecrets
from memspine.services.storage.sql_base import SqlStorage
from memspine.services.vector.base import VectorStore
from memspine.workers.runner import TaskRunner

_SPEC = EngineSpec(
    template="core",
    dotenv_path=None,
    overrides={
        "storage": {"settings": {"path": ":memory:"}},
        "embedding": {"provider": "hash"},
        "runtime": {
            "provider_services": {
                "health_timeout_s": 2.0,
                "close_timeout_s": 2.0,
                "startup_timeout_s": None,
            }
        },
    },
)


@pytest.fixture(autouse=True)
def _clean_plugins() -> Any:
    plugins.disable_all()
    yield
    plugins.disable_all()


@pytest.fixture
async def container() -> AsyncIterator[Any]:
    c = make_container(_SPEC, hooks=WiringHooks(pipeline_ctx=lambda: object()))  # type: ignore[arg-type,return-value]
    yield c
    await c.close()


async def test_builds_the_default_services(container: Any) -> None:
    resolved = await container.get(ResolvedConfig)
    assert (await container.get(MemspineConfig)) is resolved.config
    assert resolved.config.embedding.provider == "hash"
    caps = await container.get(Capabilities)
    assert caps.enabled  # the dependency closure of the enabled memory types
    assert isinstance(await container.get(SqlStorage), SqlStorage)
    assert isinstance(await container.get(KVCache), KVCache)
    embedder = await container.get(EmbeddingService)
    assert embedder.manifest.dim > 0
    assert await container.get(VectorStore) is not None
    assert await container.get(LexicalStore) is not None
    assert await container.get(GraphStore) is not None
    assert isinstance(await container.get(LLMRouter), LLMRouter)
    assert isinstance(await container.get(TaskRunner), TaskRunner)
    wiring = await container.get(Wiring)
    assert {"embedding", "vector", "lexical", "graph.primary"} <= wiring.provider_results.keys()


def test_explicit_engine_config_revalidates_mutated_nested_model() -> None:
    config = MemspineConfig.model_validate(
        {
            "runtime": {
                "provider_services": {
                    "health_timeout_s": 1.0,
                    "close_timeout_s": 1.0,
                    "startup_timeout_s": None,
                }
            }
        }
    )
    config.read.candidate_pool = "3"  # type: ignore[assignment]

    with pytest.raises(ConfigError, match=r"read\.candidate_pool.*integer"):
        resolve_config(EngineSpec(config=config), EnvSecrets(dotenv_path=None))


def test_explicit_engine_config_accepts_valid_edits_before_startup() -> None:
    config = MemspineConfig.model_validate(
        {
            "runtime": {
                "provider_services": {
                    "health_timeout_s": 1.0,
                    "close_timeout_s": 1.0,
                    "startup_timeout_s": None,
                }
            }
        }
    )
    config.read.candidate_pool = 6

    effective = resolve_config(EngineSpec(config=config), EnvSecrets(dotenv_path=None)).config
    assert effective is not config
    assert effective.read.candidate_pool == 6


def test_explicit_engine_config_revalidates_opaque_policy_values() -> None:
    config = MemspineConfig.model_validate(
        {
            "runtime": {
                "provider_services": {
                    "health_timeout_s": 1.0,
                    "close_timeout_s": 1.0,
                    "startup_timeout_s": None,
                }
            }
        }
    )
    config.read.scoring["custom"] = {"weights": [float("inf")]}

    with pytest.raises(ConfigError, match=r"read\.scoring\.custom.*finite") as err:
        resolve_config(EngineSpec(config=config), EnvSecrets(dotenv_path=None))
    assert "inf" not in str(err.value).lower()


async def test_storage_result_must_expose_sync_sql_lease() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from memspine.providers.contracts import StorageBuildResult
    from memspine.providers.registry import get_registry

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    class IncompleteSqlAccess:
        capabilities = frozenset({"sql", "sql.sqlite"})
        is_memory = True

        @property
        def engine(self) -> Any:
            return engine

        async def checkpoint(self) -> None:
            return None

        async def acquire_sync(self, resources: Any, purpose: str) -> Any:
            return None

    try:
        descriptor = get_registry("storage_backends").get("sqlite")
        result = StorageBuildResult(
            object(), descriptor.ref, (), descriptor.declared_capabilities, IncompleteSqlAccess()
        )
        with pytest.raises(ConfigError, match="incomplete SQL access contract"):
            _validate_provider_result(descriptor, result, None)
    finally:
        await engine.dispose()


async def test_app_scope_objects_are_shared(container: Any) -> None:
    storage = await container.get(SqlStorage)
    wiring = await container.get(Wiring)
    assert (await container.get(SqlStorage)) is storage
    assert (await container.get(Wiring)) is wiring
    assert wiring.sql is not None
    assert wiring.owner_id is None  # Engine resolves a persistent owner after SQL startup.
    async with request_scope(container) as request:
        assert (await request.get(SqlStorage)) is storage  # APP objects reach inner scopes
    async with job_scope(container) as job:
        assert (await job.get(Wiring)) is wiring


async def test_request_scope_is_fresh_per_scope(container: Any) -> None:
    info = RequestInfo(namespace="agent/alice", viewer="bob")
    async with request_scope(container, info) as first:
        state = await first.get(RequestState)
        assert (await first.get(RequestState)) is state  # one per scope
        assert state.info == info
    async with request_scope(container, info) as second:
        other = await second.get(RequestState)
    assert other is not state
    assert other.request_id != state.request_id


async def test_job_scope_builds_a_fresh_context_per_job() -> None:
    made: list[object] = []

    def ctx() -> Any:
        made.append(object())
        return made[-1]

    c = make_container(_SPEC, hooks=WiringHooks(pipeline_ctx=ctx))
    try:
        async with job_scope(c, JobInfo("sleep")) as one:
            first = await one.get(JobState)
            assert first.info.name == "sleep"
            assert (await one.get(JobState)) is first
        async with job_scope(c, JobInfo("sleep")) as two:
            second = await two.get(JobState)
        assert second.context is not first.context
        assert len(made) == 2
    finally:
        await c.close()


async def test_job_scope_needs_the_pipeline_hook() -> None:
    c = make_container(_SPEC)
    try:
        with pytest.raises(ConfigError, match="pipeline_ctx"):
            async with job_scope(c) as job:
                await job.get(JobState)
    finally:
        await c.close()


async def test_a_ready_config_skips_the_bootstrap() -> None:
    config = MemspineConfig(
        storage={"settings": {"path": ":memory:"}},
        embedding={"provider": "hash"},
        runtime={
            "provider_services": {
                "health_timeout_s": 2.0,
                "close_timeout_s": 2.0,
                "startup_timeout_s": None,
            }
        },
    )
    c = make_container(config)
    try:
        resolved = await c.get(MemspineConfig)
        assert resolved is not config
        assert resolved.storage.settings["path"] == config.storage.settings["path"] == ":memory:"
        with pytest.raises(TypeError, match="immutable"):
            resolved.storage.settings["path"] = "changed.db"
    finally:
        await c.close()


def test_resolve_config_preserves_source_provenance_when_freezing() -> None:
    class Secrets:
        @staticmethod
        def get(name: str) -> None:
            return None

    resolved = resolve_config(_SPEC, Secrets())

    assert resolved.sources["embedding.provider"] == "kwargs"
    assert resolved.config.embedding.provider == "hash"


async def test_plugin_pins_in_config_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    class Dist:
        version = "1.2.3"
        metadata = {"Name": "acme-embed"}

        def read_text(self, name: str) -> None:
            return None

    class EP:
        name, value, dist = "acme", "acme:acme", Dist()

        def load(self) -> Any:
            class Builder:
                async def build(self, settings: EmptySettings, ctx: Any) -> BuildResult[str]:
                    return BuildResult(
                        "acme-embedder",
                        ProviderRef("embedding", "acme"),
                        (),
                        frozenset({"embedding"}),
                    )

            return ProviderDescriptor(
                ProviderRef("embedding", "acme"),
                EmptySettings,
                frozenset({"embedding"}),
                (),
                Builder(),
            )

    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda group=None: [EP()] if group == "memspine.embedders" else [],
    )
    pin = {"name": "acme", "version": "==1.2.3", "slot": "embedders:acme", "lifetime": "app"}
    spec = EngineSpec(
        template="core",
        dotenv_path=None,
        overrides={
            "storage": {"settings": {"path": ":memory:"}},
            "embedding": {"provider": "acme"},
            "plugins": [pin],
        },
    )
    c = make_container(spec)
    try:
        assert "acme" not in EMBEDDERS
        await c.get(Capabilities)  # enabling the pins is part of the capability check
        assert "acme" in EMBEDDERS
        config = await c.get(MemspineConfig)
        descriptor = EMBEDDERS.get(config.embedding.provider)
        assert descriptor.settings_model is EmptySettings
        result = await descriptor.builder.build(EmptySettings(), None)
        assert result.service == "acme-embedder"
    finally:
        await c.close()


async def test_provider_resources_close_in_dependency_order() -> None:
    order: list[str] = []

    class Closer:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self) -> None:
            order.append(self.name)

    resources = ProviderLifecycleManager(close_timeout_s=2.0)
    resources.own("sql", Closer("sql").close)
    resources.own("sync", Closer("sync").close, depends_on=("sql",))
    resources.own("vector", Closer("vector").close)
    report = await resources.close_all()
    assert report.complete
    assert order == ["vector", "sync", "sql"]
    assert set(resources.states.values()) == {LifecycleState.STOPPED}


async def test_provider_startup_timeout_is_distinct_from_native_timeout() -> None:
    ref = ProviderRef("embedding", "timeout-test")

    class NativeTimeoutBuilder:
        async def build(self, settings: EmptySettings, ctx: Any) -> BuildResult[str]:
            raise TimeoutError

    descriptor = ProviderDescriptor(ref, EmptySettings, frozenset(), (), NativeTimeoutBuilder())
    wiring = Wiring(ProviderLifecycleManager(close_timeout_s=2.0), 2.0, 1.0)
    with pytest.raises(ConfigError, match="configured operation timeout"):
        await _build_provider(wiring, descriptor, EmptySettings(), None)

    class SlowBuilder:
        async def build(self, settings: EmptySettings, ctx: Any) -> BuildResult[str]:
            import asyncio

            await asyncio.sleep(1.0)
            return BuildResult("ok", ref, (), frozenset())

    descriptor = ProviderDescriptor(ref, EmptySettings, frozenset(), (), SlowBuilder())
    wiring = Wiring(ProviderLifecycleManager(close_timeout_s=2.0), 2.0, 0.001)
    with pytest.raises(ConfigError, match="runtime.provider_services.startup_timeout_s"):
        await _build_provider(wiring, descriptor, EmptySettings(), None)


async def test_shared_provider_requires_explicit_deployment_id() -> None:
    ref = ProviderRef("embedding", "shared-test")

    class Embedder:
        embedder_id = "shared"
        dim = 1
        manifest = EmbedderManifest("shared", 1)

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] for _ in texts]

    class Builder:
        async def build(self, settings: EmptySettings, ctx: Any) -> BuildResult[EmbeddingService]:
            return BuildResult(Embedder(), ref, (), frozenset({"embedding"}))

    descriptor = ProviderDescriptor(
        ref,
        EmptySettings,
        frozenset({"embedding"}),
        (),
        Builder(),
        requires_deployment_id=True,
    )
    wiring = Wiring(ProviderLifecycleManager(close_timeout_s=2.0), 2.0, None, owner_id="generated")
    with pytest.raises(ConfigError, match="runtime.deployment_id is required"):
        await _build_provider(wiring, descriptor, EmptySettings(), None)

    wiring.explicit_owner_id = True
    result = await _build_provider(wiring, descriptor, EmptySettings(), None)
    assert isinstance(result.service, EmbeddingService)


async def test_provider_result_must_publish_its_mandatory_capabilities() -> None:
    ref = ProviderRef("cache", "lying-test")

    class Builder:
        async def build(self, settings: EmptySettings, ctx: Any) -> BuildResult[object]:
            return BuildResult(object(), ref, (), frozenset())

    descriptor = ProviderDescriptor(
        ref,
        EmptySettings,
        frozenset({"kv", "ttl"}),
        (),
        Builder(),
    )
    wiring = Wiring(ProviderLifecycleManager(close_timeout_s=2.0), 2.0, None)
    with pytest.raises(ConfigError, match="build omits required capabilities"):
        await _build_provider(wiring, descriptor, EmptySettings(), None, result_key="cache")
    assert "cache" not in wiring.provider_results


async def test_provider_result_cannot_claim_an_unregistered_resource() -> None:
    ref = ProviderRef("cache", "fabricated-resource-test")

    class Builder:
        async def build(self, settings: EmptySettings, ctx: Any) -> BuildResult[object]:
            return BuildResult(
                object(),
                ref,
                (ResourceRef("not-owned", "owned"),),
                frozenset({"kv", "ttl"}),
            )

    descriptor = ProviderDescriptor(
        ref,
        EmptySettings,
        frozenset({"kv", "ttl"}),
        (),
        Builder(),
    )
    wiring = Wiring(ProviderLifecycleManager(close_timeout_s=2.0), 2.0, None)

    with pytest.raises(ConfigError, match="unregistered resource"):
        await _build_provider(wiring, descriptor, EmptySettings(), None)
    assert wiring.provider_results == {}


def test_graph_auto_selects_ladybug_first_when_its_requirement_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util
    from types import SimpleNamespace

    config = MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 2.0,
                "close_timeout_s": 2.0,
                "startup_timeout_s": None,
            }
        }
    )
    wiring = Wiring(
        ProviderLifecycleManager(close_timeout_s=2.0),
        2.0,
        None,
        sql=SimpleNamespace(capabilities=frozenset({"sql", "sql.sqlite"})),  # type: ignore[arg-type]
    )
    real_find_spec = importlib.util.find_spec

    def find_spec(name: str, *args: Any, **kwargs: Any) -> Any:
        return object() if name == "ladybug" else real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    assert _graph_descriptor(config, wiring).ref.name == "ladybug"


def test_graph_auto_uses_sql_capability_not_storage_provider_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util
    from types import SimpleNamespace

    config = MemspineConfig(
        storage={"provider": "some-sqlite-compatible-plugin"},
        runtime={
            "provider_services": {
                "health_timeout_s": 2.0,
                "close_timeout_s": 2.0,
                "startup_timeout_s": None,
            }
        },
    )
    wiring = Wiring(
        ProviderLifecycleManager(close_timeout_s=2.0),
        2.0,
        None,
        sql=SimpleNamespace(capabilities=frozenset({"sql", "sql.sqlite"})),  # type: ignore[arg-type]
    )
    real_find_spec = importlib.util.find_spec

    def find_spec(name: str, *args: Any, **kwargs: Any) -> Any:
        return None if name == "ladybug" else real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    assert _graph_descriptor(config, wiring).ref.name == "sqlite_adjacency"


async def test_closing_the_container_closes_the_real_client() -> None:
    c = make_container(_SPEC)
    await c.get(SqlStorage)
    wiring = await c.get(Wiring)
    closed: list[bool] = []
    resource = wiring.resources._resources["sql"]
    original = resource.close

    async def spy() -> None:
        closed.append(True)
        await original()

    resource.close = spy
    await c.close()
    assert closed == [True]
