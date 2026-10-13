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
from memspine.models.encoders.embedding.base import EmbeddingService
from memspine.models.providers.llm.base import LLMRouter
from memspine.models.registry import EMBEDDERS
from memspine.runtime import container as rc
from memspine.runtime.container import (
    Capabilities,
    EngineSpec,
    JobInfo,
    JobState,
    RequestInfo,
    RequestState,
    Wiring,
    WiringHooks,
    job_scope,
    make_container,
    request_scope,
)
from memspine.services.lexical.base import LexicalStore
from memspine.services.storage.sql_base import SqlStorage
from memspine.services.vector.base import VectorStore
from memspine.workers.runner import TaskRunner

_SPEC = EngineSpec(
    template="core",
    dotenv_path=None,
    overrides={"storage": {"path": ":memory:"}, "embedding": {"provider": "hash"}},
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
    assert isinstance(await container.get(LLMRouter), LLMRouter)
    assert isinstance(await container.get(TaskRunner), TaskRunner)


async def test_app_scope_objects_are_shared(container: Any) -> None:
    storage = await container.get(SqlStorage)
    wiring = await container.get(Wiring)
    assert (await container.get(SqlStorage)) is storage
    assert (await container.get(Wiring)) is wiring
    assert wiring.client is not None  # the storage build filled the holder
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
    config = MemspineConfig(storage={"path": ":memory:"}, embedding={"provider": "hash"})
    c = make_container(config)
    try:
        assert (await c.get(MemspineConfig)) is config
    finally:
        await c.close()


async def test_plugin_pins_in_config_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    class Dist:
        version = "1.2.3"
        metadata = {"Name": "acme-embed"}

        def read_text(self, name: str) -> None:
            return None

    class EP:
        name, value, dist = "acme", "acme:acme", Dist()

        def load(self) -> Any:
            return lambda cfg: "acme-embedder"

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
            "storage": {"path": ":memory:"},
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
        assert rc.make_embedder(config) == "acme-embedder"
    finally:
        await c.close()


async def test_close_order_matches_the_engine_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    class Closer:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self) -> None:
            order.append(self.name)

    c = make_container(_SPEC)
    wiring = await c.get(Wiring)
    # populate every slot, in scrambled order: the finaliser fixes the sequence
    for name in ("cashews", "ladybug", "lance", "pg", "client"):
        setattr(wiring, name, Closer(name))
    await c.close()
    assert order == ["client", "pg", "lance", "ladybug", "cashews"]


async def test_closing_the_container_closes_the_real_client() -> None:
    c = make_container(_SPEC)
    await c.get(SqlStorage)
    wiring = await c.get(Wiring)
    client = wiring.client
    assert client is not None
    closed: list[bool] = []
    original = client.close

    async def spy() -> None:
        closed.append(True)
        await original()

    client.close = spy  # type: ignore[method-assign]
    await c.close()
    assert closed == [True]
