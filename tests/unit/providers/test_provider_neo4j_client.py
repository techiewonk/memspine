from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable, Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from memspine.clients.neo4j import Neo4jClient
from memspine.providers.contexts import GraphBuildContext, SharedBuildContext
from memspine.providers.placement import ProjectionPlacement
from memspine.providers.settings import Neo4jSettings


class FakeDriver:
    def __init__(self) -> None:
        self.closed = 0
        self.verified = 0

    async def verify_connectivity(self) -> None:
        self.verified += 1

    async def close(self) -> None:
        self.closed += 1


class FakeResources:
    def __init__(self) -> None:
        self.registered: dict[str, Callable[[], Awaitable[None]]] = {}

    def own(self, resource_id: str, close: Callable[[], Awaitable[None]], **_: Any):
        self.registered[resource_id] = close
        from memspine.providers.contracts import ResourceRef

        return ResourceRef(resource_id, "owned")


class FakeExecutor:
    def __init__(self, driver: FakeDriver) -> None:
        self.driver = driver
        self.calls: list[tuple[str, Mapping[str, Any]]] = []
        self.closed = 0

    async def run(self, op: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((op, params))
        return [{"ok": True}]

    async def close(self) -> None:
        self.closed += 1


def settings() -> Neo4jSettings:
    return Neo4jSettings(connect_timeout_s=1, request_timeout_s=1)


def install_fake_driver(monkeypatch: pytest.MonkeyPatch, driver: FakeDriver) -> None:
    class Factory:
        @staticmethod
        def driver(*_: Any, **__: Any) -> FakeDriver:
            return driver

    monkeypatch.setitem(sys.modules, "neo4j", SimpleNamespace(AsyncGraphDatabase=Factory))
    monkeypatch.setenv("MEMSPINE_NEO4J_URL", "neo4j+s://unit.test")


@pytest.mark.asyncio
async def test_client_injects_operation_executor_and_retains_transport_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = FakeDriver()
    install_fake_driver(monkeypatch, driver)
    executors: list[FakeExecutor] = []

    def make_executor(native: FakeDriver) -> FakeExecutor:
        executor = FakeExecutor(native)
        executors.append(executor)
        return executor

    client = Neo4jClient(settings(), executor_factory=make_executor)
    await client.connect()
    assert driver.verified == 1
    assert client.executor is not None
    assert await client.executor.run("node_count", {}) == [{"ok": True}]
    await client.executor.close()  # borrowed service wrapper never closes transport
    assert executors[0].closed == 0
    assert driver.closed == 0
    await client.close()
    assert driver.closed == 1


@pytest.mark.asyncio
async def test_graph_builder_injects_service_executor_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    import memspine.services.graph.neo4j as service_module
    from memspine.providers.builtin.graph import Neo4jBuilder

    driver = FakeDriver()
    install_fake_driver(monkeypatch, driver)
    resources = FakeResources()
    shared = SharedBuildContext(resources, "deployment", 1)
    ctx = GraphBuildContext(
        shared,
        ProjectionPlacement(None, True, "owner"),
        None,
        resource_id="graph.primary",
    )
    seen: dict[str, Any] = {}
    executor_class = service_module.DriverExecutor

    class CaptureGraph:
        def __init__(self, executor: Any, *, scoped: bool) -> None:
            seen["executor"] = executor
            seen["scoped"] = scoped

        async def node_count(self) -> int:
            return 0

    monkeypatch.setattr(service_module, "DriverExecutor", executor_class)
    monkeypatch.setattr(service_module, "Neo4jGraphStore", CaptureGraph)
    builder = Neo4jBuilder()
    result = await builder.build(settings(), ctx)
    borrowed = seen["executor"]
    executor = borrowed._owner._executor
    assert isinstance(executor, executor_class)
    assert seen["scoped"] is True
    assert executor._driver is driver
    assert executor._database == "neo4j"
    assert executor._fmt["N"].startswith("MemNode_")
    assert executor._fmt["R"].startswith("MemRel_")
    await resources.registered["graph.primary"]()
    assert driver.closed == 1
    assert result.provider.name == "neo4j"
