"""ADR-081 / RM-111 WP6: ``deployment.locality`` and ``data_dir`` rules (pure, before open).

One test per rule; the Engine tests pin that the check runs before any provider opens (no
database file appears). Postgres URLs here are never dialled: the rules read descriptors only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.config.loader import load_config
from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError
from memspine.providers.locality import (
    DeploymentLocalityError,
    slot_localities,
    validate_deployment_locality,
)

PG = {
    "provider": "postgres",
    "settings": {
        "url": "postgresql://u:p@db.internal:5432/m",
        "connect_timeout_s": 5.0,
        "statement_timeout_s": 30.0,
    },
}
SQLITE_MEM = {"provider": "sqlite", "settings": {"path": ":memory:", "busy_timeout_s": 5.0}}
OPENSEARCH = {
    "provider": "opensearch",
    "settings": {"connect_timeout_s": 2.0, "request_timeout_s": 5.0},
}
REDIS = {
    "provider": "redis",
    "settings": {"url": "redis://cache:6379/0", "connect_timeout_s": 2.0, "request_timeout_s": 2.0},
}


def _sqlite(path: Path) -> dict[str, Any]:
    return {"provider": "sqlite", "settings": {"path": str(path), "busy_timeout_s": 5.0}}


def _cluster_storage(**slots: Any) -> dict[str, Any]:
    storage: dict[str, Any] = {
        "records": PG,
        "vector": {"provider": "pgvector"},
        "lexical": OPENSEARCH,
        "graph": {"provider": "sql_adjacency"},
        "cache": {"provider": "memory"},
    }
    storage.update(slots)
    return storage


_RUNTIME = {
    "provider_services": {
        "health_timeout_s": 5.0,
        "close_timeout_s": 10.0,
        "startup_timeout_s": None,
    }
}
FILE_DB = {"provider": "sqlite", "settings": {"path": "./m.db", "busy_timeout_s": 5.0}}


def _config(locality: str = "single_process", **kwargs: Any) -> MemspineConfig:
    deployment = {"locality": locality, **kwargs.pop("deployment", {})}
    storage = {"records": FILE_DB, **kwargs.pop("storage", {})}
    return MemspineConfig.model_validate(
        {"runtime": _RUNTIME, "deployment": deployment, "storage": storage, **kwargs}
    )


def _by_slot(config: MemspineConfig) -> dict[str, tuple[str, str | None]]:
    return {s.slot: (s.provider, s.locality) for s in slot_localities(config)}


# ------------------------------------------------------------------------- resolution
def test_default_embedded_slots_resolve_to_host_and_a_process_cache() -> None:
    slots = _by_slot(_config())
    assert slots["records"] == ("sqlite", "host")
    assert slots["vector"] == ("lancedb", "host")
    assert slots["lexical"] == ("tantivy", "host")
    assert slots["graph"] == ("auto", None)
    assert slots["cache"] == ("memory", "process")


def test_memory_records_make_the_placed_stores_process_local() -> None:
    slots = _by_slot(_config(storage={"records": SQLITE_MEM}))
    assert slots["records"] == ("sqlite", "process")
    assert slots["vector"] == ("lancedb", "process")
    assert slots["lexical"] == ("tantivy", "process")


@pytest.mark.parametrize(
    ("records", "expected"),
    [("postgres", "cluster"), ("file", "host"), ("memory", "process")],
)
def test_sql_adjacency_takes_the_records_slot_locality(
    tmp_path: Path, records: str, expected: str
) -> None:
    slot = {"postgres": PG, "file": _sqlite(tmp_path / "m.db"), "memory": SQLITE_MEM}[records]
    config = _config(storage={"records": slot, "graph": {"provider": "sql_adjacency"}})
    assert _by_slot(config)["graph"] == ("sql_adjacency", expected)


def test_cluster_providers_keep_their_declared_trait_on_a_loopback_endpoint() -> None:
    # A sidecar proxy (cloud-sql-proxy) listens on 127.0.0.1 but reaches a shared server.
    local = {**PG, "settings": {**PG["settings"], "url": "postgresql://u:p@127.0.0.1:5432/m"}}
    config = _config("multi_node", storage=_cluster_storage(records=local))
    assert _by_slot(config)["records"] == ("postgres", "cluster")
    validate_deployment_locality(config)


# ------------------------------------------------------------------------- multi_node
def test_multi_node_accepts_cluster_slots_and_a_process_cache() -> None:
    validate_deployment_locality(_config("multi_node", storage=_cluster_storage()))
    validate_deployment_locality(_config("multi_node", storage=_cluster_storage(cache=REDIS)))


@pytest.mark.parametrize(
    ("slot", "selected", "fix"),
    [
        ("vector", {"provider": "lancedb"}, "pgvector"),
        ("lexical", {"provider": "tantivy"}, "opensearch"),
        ("graph", {"provider": "ladybug"}, "neo4j"),
        ("cache", {"provider": "disk"}, "redis"),
        ("documents", {"provider": "filesystem", "settings": {"root": "/srv/docs"}}, "RM-112"),
    ],
)
def test_multi_node_rejects_a_host_local_slot(slot: str, selected: Any, fix: str) -> None:
    config = _config(
        "multi_node",
        storage=_cluster_storage(**{slot: selected}),
        deployment={"data_dir": "/var/lib/memspine"},
    )
    with pytest.raises(DeploymentLocalityError) as raised:
        validate_deployment_locality(config)
    message = str(raised.value)
    assert f"storage.{slot} provider {selected['provider']!r} is host-local" in message
    assert "deployment.locality=multi_node" in message
    assert fix in message


def test_multi_node_rejects_sqlite_records_and_lists_every_violation(tmp_path: Path) -> None:
    config = _config("multi_node", storage={"records": _sqlite(tmp_path / "m.db")})
    with pytest.raises(DeploymentLocalityError) as raised:
        validate_deployment_locality(config)
    message = str(raised.value)
    for slot in ("records", "vector", "lexical"):
        assert f"storage.{slot} provider" in message
    assert "storage.graph provider 'auto' must be explicit" in message


def test_multi_node_rejects_in_memory_records() -> None:
    with pytest.raises(DeploymentLocalityError, match="'sqlite' is in-process"):
        validate_deployment_locality(
            _config("multi_node", storage=_cluster_storage(records=SQLITE_MEM))
        )


def test_slots_that_are_not_built_are_not_checked() -> None:
    storage = _cluster_storage(lexical={"provider": "tantivy"}, graph={"provider": "ladybug"})
    config = _config("multi_node", storage=storage, read={"hybrid": False})
    validate_deployment_locality(config, enabled=frozenset({"semantic"}))  # no associative
    with pytest.raises(DeploymentLocalityError, match="storage.graph provider 'ladybug'"):
        validate_deployment_locality(config, enabled=frozenset({"associative"}))


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        (
            {"workers": {"runner": "temporal", "temporal": {"deployment_id": "d1"}}},
            "workers.resonate.journal_path",
        ),
        ({"model": {"embedding": {"pin_preset": True}}}, "model.embedding.pin_preset"),
        ({"ingest": {"assets": "on", "asset_dir": "/var/cache/assets"}}, "ingest.asset_dir"),
    ],
)
def test_multi_node_rejects_host_local_features(overrides: dict[str, Any], fragment: str) -> None:
    config = _config("multi_node", storage=_cluster_storage(), **overrides)
    with pytest.raises(DeploymentLocalityError, match=fragment.replace(".", r"\.")):
        validate_deployment_locality(config)


# ------------------------------------------------------------------------- multi_process
@pytest.mark.parametrize(
    ("slot", "selected"),
    [
        ("lexical", {"provider": "tantivy"}),
        ("graph", {"provider": "ladybug"}),
        ("documents", {"provider": "filesystem"}),
    ],
)
def test_multi_process_single_writer_store_needs_the_writer_process(
    tmp_path: Path, slot: str, selected: Any
) -> None:
    storage = {"records": _sqlite(tmp_path / "m.db"), "graph": {"provider": "sql_adjacency"}}
    storage[slot] = selected
    reader = _config("multi_process", storage=storage)
    with pytest.raises(DeploymentLocalityError) as raised:
        validate_deployment_locality(reader)
    message = str(raised.value)
    assert f"storage.{slot} provider {selected['provider']!r} is a single-writer" in message
    assert "deployment.writer: true" in message
    assert "MEMSPINE_DEPLOYMENT__WRITER=true" in message
    writer = _config("multi_process", storage=storage, deployment={"writer": True})
    validate_deployment_locality(writer)


def test_multi_process_allows_shared_host_stores_without_a_writer(tmp_path: Path) -> None:
    storage = {
        "records": _sqlite(tmp_path / "m.db"),
        "vector": {"provider": "lancedb"},
        "lexical": OPENSEARCH,
        "graph": {"provider": "sql_adjacency"},
        "cache": {"provider": "disk"},
    }
    validate_deployment_locality(_config("multi_process", storage=storage))


def test_multi_process_rejects_in_memory_records() -> None:
    config = _config(
        "multi_process", storage={"records": SQLITE_MEM, "graph": {"provider": "sql_adjacency"}}
    )
    with pytest.raises(DeploymentLocalityError, match="'sqlite' is in-process"):
        validate_deployment_locality(config, enabled=frozenset({"semantic"}))


def test_multi_process_rejects_graph_auto(tmp_path: Path) -> None:
    config = _config(
        "multi_process",
        storage={"records": _sqlite(tmp_path / "m.db")},
        deployment={"writer": True},
    )
    with pytest.raises(DeploymentLocalityError, match="storage.graph provider 'auto'"):
        validate_deployment_locality(config)


def test_the_writer_flag_comes_from_the_environment_layer(tmp_path: Path) -> None:
    resolved = load_config(
        template="core",
        user_config={
            "deployment": {"locality": "multi_process"},
            "storage": {
                "records": _sqlite(tmp_path / "m.db"),
                "graph": {"provider": "sql_adjacency"},
            },
        },
        env={"MEMSPINE_DEPLOYMENT__WRITER": "true"},
    )
    assert resolved.config.deployment.writer is True
    validate_deployment_locality(resolved.config)


def test_single_process_ignores_the_writer_flag(tmp_path: Path) -> None:
    validate_deployment_locality(_config(storage={"records": _sqlite(tmp_path / "m.db")}))


# ------------------------------------------------------------------------- data_dir
def test_data_dir_is_required_only_for_placed_host_slots() -> None:
    no_host = _config(storage=_cluster_storage())
    validate_deployment_locality(no_host)  # remote-only: no data_dir needed
    lance = _config(storage=_cluster_storage(vector={"provider": "lancedb"}))
    with pytest.raises(
        DeploymentLocalityError,
        match=r"storage\.vector provider 'lancedb' is host-local and needs deployment\.data_dir",
    ):
        validate_deployment_locality(lance)
    placed = _config(
        storage=_cluster_storage(vector={"provider": "lancedb"}),
        deployment={"data_dir": "/var/lib/memspine"},
    )
    validate_deployment_locality(placed)


def test_documents_with_an_explicit_root_need_no_data_dir() -> None:
    explicit = _config(
        storage=_cluster_storage(
            documents={"provider": "filesystem", "settings": {"root": "/srv/docs"}}
        )
    )
    validate_deployment_locality(explicit)
    implicit = _config(storage=_cluster_storage(documents={"provider": "filesystem"}))
    with pytest.raises(DeploymentLocalityError, match=r"storage\.documents provider 'filesystem'"):
        validate_deployment_locality(implicit)


def test_pin_file_needs_data_dir_on_a_non_file_records_store() -> None:
    config = _config(storage=_cluster_storage(), model={"embedding": {"pin_preset": True}})
    with pytest.raises(
        DeploymentLocalityError, match=r"model\.embedding\.pin_preset writes a pin file"
    ):
        validate_deployment_locality(config)


def test_disk_cache_has_its_own_path_and_needs_no_data_dir() -> None:
    validate_deployment_locality(_config(storage=_cluster_storage(cache={"provider": "disk"})))


# ------------------------------------------------------------------------- engine
async def test_engine_refuses_before_opening_anything(tmp_path: Path) -> None:
    database = tmp_path / "never.db"
    eng = Engine(
        template="core",
        dotenv_path=None,
        deployment={"locality": "multi_node"},
        storage={"records": _sqlite(database)},
        model={"embedding": {"provider": "hash", "settings": {}}},
    )
    with pytest.raises(ConfigError, match="deployment.locality=multi_node"):
        await eng.start()
    await eng.stop()
    assert not database.exists()  # rejected before the records store was opened
    assert not list(tmp_path.iterdir())


async def test_engine_refuses_a_non_writer_process_before_opening_tantivy(tmp_path: Path) -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        deployment={"locality": "multi_process"},
        storage={"records": _sqlite(tmp_path / "w.db")},
        model={"embedding": {"provider": "hash", "settings": {}}},
        read={"hybrid": True},
    )
    with pytest.raises(ConfigError, match="storage.lexical provider 'tantivy' is a single-writer"):
        await eng.start()
    await eng.stop()
    assert not (tmp_path / "w.db").exists()
