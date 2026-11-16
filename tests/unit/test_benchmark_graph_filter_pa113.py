"""PA-113 benchmark guards: private Neo4j cleanup and parity before timing."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def benchmark(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    import benchmark_graph_filter_pa113

    return benchmark_graph_filter_pa113


@pytest.mark.parametrize(
    ("load_fails", "cleanup_fails"), [(False, False), (True, False), (True, True)]
)
async def test_neo4j_private_loader_closes_after_success_or_partial_load(
    benchmark: Any, monkeypatch: pytest.MonkeyPatch, load_fails: bool, cleanup_fails: bool
) -> None:
    import neo4j

    from memspine.services.graph import neo4j as graph_module

    calls: list[str] = []

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def run(self, query: str) -> None:
            calls.append(query)

    class Driver:
        def session(self, *, database: str) -> Session:
            assert database == "neo4j"
            return Session()

        async def close(self) -> None:
            calls.append("driver.close")

    class Store:
        def __init__(self, executor: Any) -> None:
            self.executor = executor

        async def node_count(self) -> int:
            calls.append("node_count")
            return 0

    driver = Driver()
    monkeypatch.setenv("MEMSPINE_TEST_NEO4J_URL", "bolt://127.0.0.1:1")
    monkeypatch.setattr(neo4j.AsyncGraphDatabase, "driver", lambda *_args, **_kwargs: driver)
    monkeypatch.setattr(
        graph_module,
        "DriverExecutor",
        lambda _driver, *, node_label, rel_type: SimpleNamespace(
            node_label=node_label, rel_type=rel_type, _database="neo4j"
        ),
    )
    monkeypatch.setattr(graph_module, "Neo4jGraphStore", lambda executor: Store(executor))

    async def load(_store: Any, _data: Any) -> None:
        calls.append("load")
        if load_fails:
            raise RuntimeError("partial load")

    async def delete(_driver: Any, database: str, label: str) -> int:
        assert database == "neo4j"
        calls.append(f"delete:{label}")
        if cleanup_fails:
            raise RuntimeError("private cleanup")
        return 0

    monkeypatch.setattr(benchmark.reference, "_neo4j_load", load)
    monkeypatch.setattr(benchmark, "delete_private_neo4j_nodes", delete)
    if load_fails:
        with pytest.raises(RuntimeError, match="partial load") as caught:
            await benchmark.safe_neo4j_store([], Path("unused"))
        if cleanup_fails:
            assert isinstance(caught.value.__cause__, RuntimeError)
            assert str(caught.value.__cause__) == "private cleanup"
            assert "private Neo4j cleanup also failed" in " ".join(caught.value.__notes__)
    else:
        _store, close = await benchmark.safe_neo4j_store([], Path("unused"))
        assert "driver.close" not in calls
        await close()
    assert calls[0:2] == ["node_count", "load"]
    assert re.fullmatch(r"delete:Pa113N[0-9a-f]{10}", calls[2])
    label = calls[2].split(":")[1]
    assert calls[3:] == [
        f"DROP CONSTRAINT {label}_key IF EXISTS",
        f"DROP INDEX {label}_id IF EXISTS",
        "driver.close",
    ]


@pytest.mark.parametrize("failing_step", ["delete", "constraint", "index"])
async def test_neo4j_cleanup_closes_driver_even_when_cleanup_step_fails(
    benchmark: Any, monkeypatch: pytest.MonkeyPatch, failing_step: str
) -> None:
    import neo4j

    from memspine.services.graph import neo4j as graph_module

    calls: list[str] = []

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def run(self, query: str) -> None:
            calls.append(query)
            if failing_step == "constraint" and query.startswith("DROP CONSTRAINT"):
                raise RuntimeError("constraint")
            if failing_step == "index" and query.startswith("DROP INDEX"):
                raise RuntimeError("index")

    class Driver:
        def session(self, *, database: str) -> Session:
            assert database == "neo4j"
            return Session()

        async def close(self) -> None:
            calls.append("driver.close")

    class Store:
        async def node_count(self) -> int:
            return 0

    monkeypatch.setenv("MEMSPINE_TEST_NEO4J_URL", "bolt://127.0.0.1:1")
    monkeypatch.setattr(neo4j.AsyncGraphDatabase, "driver", lambda *_args, **_kwargs: Driver())
    monkeypatch.setattr(
        graph_module,
        "DriverExecutor",
        lambda _driver, *, node_label, rel_type: SimpleNamespace(
            node_label=node_label, rel_type=rel_type, _database="neo4j"
        ),
    )
    monkeypatch.setattr(graph_module, "Neo4jGraphStore", lambda _executor: Store())

    async def load(_store: Any, _data: Any) -> None:
        return None

    async def delete(_driver: Any, database: str, label: str) -> int:
        assert database == "neo4j" and re.fullmatch(r"Pa113N[0-9a-f]{10}", label)
        calls.append("delete")
        if failing_step == "delete":
            raise RuntimeError("delete")
        return 0

    monkeypatch.setattr(benchmark.reference, "_neo4j_load", load)
    monkeypatch.setattr(benchmark, "delete_private_neo4j_nodes", delete)
    _store, close = await benchmark.safe_neo4j_store([], Path("unused"))
    with pytest.raises(RuntimeError, match=failing_step):
        await close()
    assert calls[0] == "delete"
    assert re.fullmatch(r"DROP CONSTRAINT Pa113N[0-9a-f]{10}_key IF EXISTS", calls[1])
    assert re.fullmatch(r"DROP INDEX Pa113N[0-9a-f]{10}_id IF EXISTS", calls[2])
    assert calls[3] == "driver.close"


async def test_private_delete_is_exact_label_batched_and_finite(benchmark: Any) -> None:
    label = "Pa113N1234567890"
    queries: list[tuple[str, int]] = []
    removals = iter([100, 100, 50, 0])

    class Result:
        def __init__(self, row: dict[str, int]) -> None:
            self.row = row

        async def single(self) -> dict[str, int]:
            return self.row

    class Transaction:
        async def run(self, query: str, *, limit: int) -> Result:
            queries.append((query, limit))
            return Result({"removed": next(removals)})

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def run(self, query: str) -> Result:
            assert query == f"MATCH (n:{label}) RETURN count(n) AS n"
            return Result({"n": 250})

        async def execute_write(self, callback: Any) -> int:
            return await callback(Transaction())

    class Driver:
        def session(self, *, database: str) -> Session:
            assert database == "neo4j"
            return Session()

    driver = Driver()
    assert await benchmark.delete_private_neo4j_nodes(driver, "neo4j", label) == 250
    assert (
        queries
        == [
            (
                f"MATCH (n:{label}) WITH n LIMIT $limit DETACH DELETE n RETURN count(n) AS removed",
                100,
            )
        ]
        * 4
    )
    with pytest.raises(ValueError, match="private graph label"):
        await benchmark.delete_private_neo4j_nodes(driver, "neo4j", "MemNode")
    with pytest.raises(RuntimeError, match="exceeds cleanup bound"):
        await benchmark.delete_private_neo4j_nodes(driver, "neo4j", label, max_batches=3)
    assert len(queries) == 4  # reject oversize before any deletion


async def test_walk_and_leg_parity_fail_before_any_timing(
    benchmark: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Store:
        _ex = SimpleNamespace(compiles_qualifier_filters=True)

        async def edge_list(self, _namespace: str) -> list[Any]:
            return []

        async def neighbors(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            return []

        async def frontier_edges(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            return []

    async def leg(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(edges=[])

    def mismatch(*_args: Any) -> None:
        raise AssertionError("leg parity mismatch")

    def timing(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("timing started before parity")

    monkeypatch.setattr(benchmark, "_graph_leg_edges", leg)
    monkeypatch.setattr(benchmark.reference, "_check_same_hits", mismatch)
    monkeypatch.setattr(benchmark.reference, "_time", timing)
    with pytest.raises(AssertionError, match="leg parity mismatch"):
        await benchmark.measure_current(Store(), ["ent:bench/rm96:n000000"], 1, 1)


async def test_measurement_error_survives_close_failure(
    benchmark: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Store:
        async def edge_list(self, _namespace: str) -> list[Any]:
            return [object()] * 7

        async def node_count(self) -> int:
            return 2

    async def close() -> None:
        raise RuntimeError("private close failed")

    async def load(data: Any, _path: Path) -> Any:
        assert len(data) == 7
        return Store(), close

    async def measure(*_args: Any) -> Any:
        raise AssertionError("parity failed")

    monkeypatch.setattr(benchmark, "safe_sqlite_store", load)
    monkeypatch.setattr(benchmark, "measure_current", measure)
    monkeypatch.setattr(benchmark.reference, "build_edges", lambda *_args: [object()])
    monkeypatch.setattr(benchmark.reference, "build_mentions", lambda *_args: [object()] * 6)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_graph_filter_pa113.py",
            "--edges",
            "1",
            "--nodes",
            "10",
            "--mentions",
            "6",
            "--reps",
            "1",
            "--leg-reps",
            "1",
            "--backends",
            "sql_adjacency",
            "--out",
            str(tmp_path / "unused.json"),
        ],
    )
    with pytest.raises(AssertionError, match="parity failed") as caught:
        await benchmark.main()
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "private close failed"
    assert "cleanup also failed" in " ".join(caught.value.__notes__)
