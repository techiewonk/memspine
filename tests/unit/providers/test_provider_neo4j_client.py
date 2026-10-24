"""RM-113: the Neo4j connector owns the driver; the adapter's executor borrows it."""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from memspine.clients.neo4j import Neo4jClient, Neo4jSpec
from memspine.providers.contexts import GraphBuildContext, SharedBuildContext
from memspine.providers.placement import ProjectionPlacement
from memspine.providers.settings import Neo4jSettings


class _Result:
    async def single(self) -> dict[str, int]:
        return {"ok": 1}


class _Session:
    def __init__(self, driver: FakeDriver) -> None:
        self.driver = driver

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def run(self, text: str) -> _Result:
        self.driver.queries.append(text)
        return _Result()


class FakeDriver:
    def __init__(self) -> None:
        self.closed = 0
        self.verified = 0
        self.queries: list[str] = []
        self.kwargs: dict[str, Any] = {}

    async def verify_connectivity(self) -> None:
        self.verified += 1

    def session(self, **_: Any) -> _Session:
        return _Session(self)

    async def close(self) -> None:
        self.closed += 1


class FakeResources:
    def __init__(self) -> None:
        self.registered: dict[str, Callable[[], Awaitable[None]]] = {}

    def own(self, resource_id: str, close: Callable[[], Awaitable[None]], **_: Any):
        self.registered[resource_id] = close
        from memspine.providers.contracts import ResourceRef

        return ResourceRef(resource_id, "owned")


def spec() -> Neo4jSpec:
    return Neo4jSpec(connect_timeout_s=1, request_timeout_s=1, max_connections=7)


def install_fake_driver(monkeypatch: pytest.MonkeyPatch, driver: FakeDriver) -> None:
    class Factory:
        @staticmethod
        def driver(*_: Any, **kwargs: Any) -> FakeDriver:
            driver.kwargs = kwargs
            return driver

    monkeypatch.setitem(sys.modules, "neo4j", SimpleNamespace(AsyncGraphDatabase=Factory))
    monkeypatch.setenv("MEMSPINE_NEO4J_URL", "neo4j+s://unit.test")


async def test_connector_owns_driver_health_round_trip_and_idempotent_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = FakeDriver()
    install_fake_driver(monkeypatch, driver)
    client = Neo4jClient(spec())
    assert not await client.health(1.0)  # not open
    await client.open()
    await client.open()  # idempotent
    assert driver.verified == 1
    assert driver.kwargs["max_connection_pool_size"] == 7
    assert client.handle is driver
    assert await client.health(1.0)
    assert driver.queries == ["RETURN 1 AS ok"]
    await client.close()
    await client.close()
    assert driver.closed == 1


async def test_executor_borrows_the_driver_and_never_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memspine.services.graph.neo4j import connector_executor

    driver = FakeDriver()
    install_fake_driver(monkeypatch, driver)
    client = Neo4jClient(spec())
    await client.open()
    executor = connector_executor(client, node_label="MemNode_x", rel_type="MemRel_x")
    await executor.close()
    assert driver.closed == 0  # the connector's owner closes the driver
    await client.close()
    assert driver.closed == 1


async def test_graph_builder_opens_connector_and_builds_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import memspine.services.graph.neo4j as service_module
    from memspine.providers.builtin.graph import Neo4jBuilder

    driver = FakeDriver()
    install_fake_driver(monkeypatch, driver)
    resources = FakeResources()
    shared = SharedBuildContext(resources, "deployment", 1)  # type: ignore[arg-type]
    ctx = GraphBuildContext(
        shared,
        ProjectionPlacement(None, True, "owner"),
        None,
        resource_id="graph.primary",
    )
    seen: dict[str, Any] = {}

    class CaptureGraph:
        def __init__(self, executor: Any) -> None:
            seen["executor"] = executor

        async def node_count(self) -> int:
            return 0

    monkeypatch.setattr(service_module, "Neo4jGraphStore", CaptureGraph)
    result = await Neo4jBuilder().build(
        Neo4jSettings(connect_timeout_s=1, request_timeout_s=1), ctx
    )
    executor = seen["executor"]
    assert isinstance(executor, service_module.DriverExecutor)
    assert executor._driver is driver
    assert executor._database == "neo4j"
    assert executor._timeout == 1
    assert executor._fmt["N"].startswith("MemNode_")
    assert executor._fmt["R"].startswith("MemRel_")
    await resources.registered["graph.primary"]()
    assert driver.closed == 1
    assert result.provider.name == "neo4j"
