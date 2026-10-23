"""ADR-081 / RM-111 WP1-WP2: storage slots, the deployment block, locality traits and the
byte-identical placement default.

This file spells the removed keys on purpose (migrate_config_keys_adr081: skip)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from memspine.config.loader import load_config
from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError
from memspine.providers.contracts import ProjectionPlacement, ProviderRef
from memspine.providers.placement import host_base_path, storage_placement
from memspine.providers.projection_bindings import projection_target_fingerprint
from memspine.providers.registry import (
    DATA_CATEGORIES,
    KIND_TO_CATEGORY,
    get_registry,
    validate_settings,
)

_RUNTIME = {
    "provider_services": {
        "health_timeout_s": 5.0,
        "close_timeout_s": 10.0,
        "startup_timeout_s": None,
    }
}


def _config(**overrides: Any) -> MemspineConfig:
    return MemspineConfig.model_validate({"runtime": _RUNTIME, **overrides})


# ---------------------------------------------------------------------------- old keys


@pytest.mark.parametrize(
    ("old", "new_location"),
    [
        ({"storage": {"profile": "server"}}, "Alembic always runs"),
        ({"storage": {"provider": "sqlite"}}, "storage.records.provider"),
        ({"storage": {"settings": {"path": "x.db"}}}, "storage.records.settings"),
        ({"vector": {"provider": "lancedb"}}, "storage.vector"),
        ({"cache": {"provider": "memory"}}, "storage.cache"),
        ({"graph": {"provider": "auto"}}, "storage.graph.provider"),
        ({"graph": {"settings": {}}}, "storage.graph.settings"),
        ({"graph": {"mirror": {"enabled": True}}}, "one graph client runs at a time"),
        ({"read": {"lexical_provider": "tantivy"}}, "storage.lexical.provider"),
        ({"read": {"lexical_settings": {}}}, "storage.lexical.settings"),
        ({"runtime": {"deployment_id": "x"}}, "deployment.id"),
    ],
)
def test_every_removed_key_is_rejected_naming_its_new_location(
    old: dict[str, Any], new_location: str
) -> None:
    with pytest.raises(ConfigError, match="removed by ADR-081") as raised:
        load_config(template="base", overrides=old)
    assert new_location in str(raised.value)


@pytest.mark.parametrize("slot", ["vector", "lexical", "records", "graph", "cache"])
def test_required_slots_cannot_be_null(slot: str) -> None:
    with pytest.raises(ConfigError, match=f"storage.{slot}"):
        load_config(template="base", overrides={"storage": {slot: None}})
    with pytest.raises(ConfigError, match="cannot be null"):
        MemspineConfig.model_validate({"runtime": _RUNTIME, "storage": {slot: None}})


def test_documents_slot_is_optional_and_every_shipped_provider_is_accepted() -> None:
    assert _config().storage.documents is None
    documents = _config(storage={"documents": {"provider": "filesystem"}}).storage.documents
    assert documents is not None and documents.provider == "filesystem"
    for name in ("s3", "weaviate", "mongodb"):  # all shipped (RM-112)
        slot = _config(storage={"documents": {"provider": name}}).storage.documents
        assert slot is not None and slot.provider == name


def test_slot_defaults_and_deployment_defaults() -> None:
    config = _config()
    storage = config.storage
    assert (
        storage.records.provider,
        storage.vector.provider,
        storage.lexical.provider,
        storage.graph.provider,
        storage.cache.provider,
    ) == ("sqlite", "lancedb", "tantivy", "auto", "memory")
    assert "graph_mirror" not in type(storage).model_fields
    assert config.deployment.id is None
    assert config.deployment.locality == "single_process"
    assert config.deployment.data_dir is None


def test_graph_auto_takes_no_settings() -> None:
    with pytest.raises(ConfigError, match="storage.graph.settings"):
        _config(storage={"graph": {"provider": "auto", "settings": {"x": 1}}})


def test_changing_a_slot_provider_resets_its_settings_across_layers() -> None:
    resolved = load_config(
        template="base",
        overrides={
            "storage": {
                "records": {
                    "provider": "postgres",
                    "settings": {
                        "url": "postgresql://h/db",
                        "connect_timeout_s": 5.0,
                        "statement_timeout_s": 30.0,
                    },
                }
            }
        },
    )
    # The base template's SQLite path does not leak into the Postgres settings.
    assert "path" not in resolved.config.storage.records.settings


def test_production_requires_deployment_id() -> None:
    with pytest.raises(ConfigError, match="deployment.id"):
        _config(runtime={**_RUNTIME, "environment": "production"})
    assert _config(runtime={**_RUNTIME, "environment": "production"}, deployment={"id": "p"})


# ---------------------------------------------------------------------------- locality


def test_every_builtin_data_provider_declares_its_locality() -> None:
    expected = {
        ("storage", "sqlite"): "host",
        ("storage", "postgres"): "cluster",
        ("vector", "lancedb"): "host",
        ("vector", "pgvector"): "cluster",
        ("vector", "weaviate"): "cluster",
        ("lexical", "tantivy"): "host",
        ("lexical", "opensearch"): "cluster",
        ("lexical", "weaviate"): "cluster",
        ("graph", "ladybug"): "host",
        ("graph", "neo4j"): "cluster",
        ("cache", "memory"): "process",
        ("cache", "disk"): "host",
        ("cache", "redis"): "cluster",
        ("cache", "valkey"): "cluster",
    }
    seen: dict[tuple[str, str], str | None] = {}
    for kind, category in KIND_TO_CATEGORY.items():
        registry = get_registry(kind)
        for name in registry.builtin_names():
            descriptor = registry.get(name)
            if category in DATA_CATEGORIES:
                assert descriptor.locality is not None, f"{category}:{name}"
                seen[(category, name)] = descriptor.locality
            else:
                assert descriptor.locality is None, f"{category}:{name}"
    for key, locality in expected.items():
        assert seen[key] == locality, key


@pytest.mark.parametrize(
    "value", [{"provider": "neo4j"}, {"provider": "neo4j", "settings": {}}, None]
)
def test_graph_mirror_slot_is_rejected_naming_storage_graph(value: Any) -> None:
    """ADR-081 amendment 2026-10-23: one graph client runs at a time. The key is rejected
    whatever its value (null included), naming ``storage.graph`` as the replacement."""
    for build in (
        lambda: _config(storage={"graph_mirror": value}),
        lambda: load_config(template="base", overrides={"storage": {"graph_mirror": value}}),
    ):
        with pytest.raises(ConfigError, match="storage.graph_mirror") as raised:
            build()
        message = str(raised.value)
        assert "one graph client runs at a time" in message
        assert "storage.graph" in message.replace("storage.graph_mirror", "")


def test_graph_mirror_env_var_is_rejected() -> None:
    env = {"MEMSPINE_STORAGE__GRAPH_MIRROR__PROVIDER": "neo4j"}
    with pytest.raises(ConfigError, match="one graph client runs at a time"):
        load_config(template="base", env=env)


def test_no_graph_provider_declares_a_mirror_capability() -> None:
    registry = get_registry("graph_stores")
    for name in registry.builtin_names():
        assert "mirror" not in registry.get(name).declared_capabilities, name


# ---------------------------------------------------------------------------- placement

#: Pinned from the pre-ADR-081 code (``storage.settings.path`` / ``storage.settings.data_dir``)
#: on Windows; the portable test below recomputes the same identity on any OS.
_PRE_ADR081_WINDOWS = {
    "sqlite": {
        "vector": "9d9523b509be88b132018196a1ee4af00294d8f73cc84c0ed7d208d296b2baad",
        "lexical": "1d111529d9f7f3cb765cd911960f69b6c2ae0da4c1a0c3a1eec416c07347c39b",
        "graph": "0cdfcceb9b2670ddf7b5e605042f96f8bdeb7d2628c7108ba79c6a98c01e78c4",
    },
    "postgres": {
        "vector": "c8ef2a1a2acf3b4b19c0057f3337c98b2d5d9e3bde66ee5fba80c2e99505df4d",
        "lexical": "b41c45796618d561576fa6594c7229ae8942b8496935eeebb33ef5ee42de9cea",
        "graph": "6a59b38d7e6e36fa7cad2bd3ad5758b4f5792a59818ab7936d47b5130cb43fa8",
    },
}
_CONFIGS = {
    "sqlite": (
        {
            "storage": {"records": {"settings": {"path": "data/mem.db", "busy_timeout_s": 5.0}}},
            "model": {"embedding": {"provider": "hash"}},
        },
        "local",
        Path("data/mem.db"),
    ),
    "postgres": (
        {
            "deployment": {"id": "dep-1", "data_dir": "/srv/memspine"},
            "storage": {
                "records": {
                    "provider": "postgres",
                    "settings": {
                        "url": "postgresql://u:p@h:5432/db",
                        "connect_timeout_s": 5.0,
                        "statement_timeout_s": 30.0,
                    },
                }
            },
            "model": {"embedding": {"provider": "hash"}},
        },
        "dep-1",
        Path("/srv/memspine") / "memspine",
    ),
}
_PROJECTIONS = (
    ("vector", "vector_stores", "lancedb"),
    ("lexical", "lexical_indexes", "tantivy"),
    ("graph", "graph_stores", "ladybug"),
)


@pytest.mark.parametrize("name", list(_CONFIGS))
def test_default_placement_base_is_unchanged(name: str) -> None:
    raw, owner, expected_base = _CONFIGS[name]
    placement = storage_placement(_config(**raw), owner)
    assert placement == ProjectionPlacement(expected_base, False, owner)


@pytest.mark.parametrize("name", list(_CONFIGS))
def test_projection_fingerprints_are_byte_identical(name: str) -> None:
    from memspine.providers.engine_changes import resolved_config_fingerprint

    raw, owner, expected_base = _CONFIGS[name]
    config = _config(**raw)
    for category, kind, provider in _PROJECTIONS:
        descriptor = get_registry(kind).get(provider)
        settings = validate_settings(descriptor, {})
        fingerprint = projection_target_fingerprint(
            config,
            category=category,
            provider=ProviderRef(category, provider),
            settings=settings,
            deployment_id=owner,
        )
        if os.name == "nt":
            assert fingerprint == _PRE_ADR081_WINDOWS[name][category], (name, category)
        if category != "vector":  # the vector identity also carries the SQL/embedding target
            identity = {
                "provider": {"category": category, "name": provider, "contract_version": 1},
                "settings": settings,
                "placement": {
                    "base_path": str(expected_base),
                    "in_memory": False,
                    "owner_id": owner,
                },
            }
            assert fingerprint == resolved_config_fingerprint(identity), (name, category)


def test_explicit_data_dir_moves_sqlite_siblings_under_it() -> None:
    config = _config(
        deployment={"data_dir": "/var/lib/memspine"},
        storage={"records": {"settings": {"path": "data/mem.db", "busy_timeout_s": 5.0}}},
    )
    assert storage_placement(config, "o").base_path == Path("/var/lib/memspine") / "mem.db"


def test_memory_records_store_is_in_process() -> None:
    config = _config(storage={"records": {"settings": {"path": ":memory:", "busy_timeout_s": 5.0}}})
    assert storage_placement(config, "o") == ProjectionPlacement(None, True, "o")


def test_host_local_slot_without_data_dir_names_the_key() -> None:
    config = _config(
        deployment={"id": "d"},
        storage={
            "records": {
                "provider": "postgres",
                "settings": {
                    "url": "postgresql://h/db",
                    "connect_timeout_s": 5.0,
                    "statement_timeout_s": 30.0,
                },
            }
        },
    )
    placement = storage_placement(config, "d")
    assert placement.base_path is None and not placement.in_memory
    with pytest.raises(
        ConfigError, match=r"storage.vector provider 'lancedb' .*deployment.data_dir"
    ):
        host_base_path(placement, "vector", "lancedb")


async def test_disk_cache_defaults_under_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from types import ModuleType

    from memspine.clients.cashews import CashewsClient, CashewsSpec
    from memspine.providers.builtin.cache import DiskBuilder
    from memspine.providers.contexts import CacheBuildContext, SharedBuildContext
    from memspine.providers.lifecycle import ProviderLifecycleManager

    seen: list[str] = []

    class FakeClient(CashewsClient):
        """The real connector's identity (it is shared through the registry); no backend."""

        def __init__(self, spec: CashewsSpec) -> None:
            super().__init__(spec)
            seen.append(spec.url)

        async def open(self) -> None:
            return None

        async def close(self) -> None:
            return None

    class FakeCache:
        def __init__(self, client: object, **kwargs: object) -> None:
            self.client = client

    # The optional ``cashews`` extra is not needed: both lazy imports are replaced.
    client_module = ModuleType("memspine.clients.cashews")
    client_module.CashewsClient = FakeClient  # type: ignore[attr-defined]
    client_module.CashewsSpec = CashewsSpec  # type: ignore[attr-defined]
    cache_module = ModuleType("memspine.services.cache.cashews")
    cache_module.CashewsCache = FakeCache  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "memspine.clients.cashews", client_module)
    monkeypatch.setitem(sys.modules, "memspine.services.cache.cashews", cache_module)
    descriptor = get_registry("cache_backends").get("disk")
    placement = ProjectionPlacement(None, False, "o")
    for raw in ({}, {"path": "explicit.cache"}):
        resources = ProviderLifecycleManager(close_timeout_s=1.0)
        await DiskBuilder().build(
            validate_settings(descriptor, raw),
            CacheBuildContext(
                SharedBuildContext(resources, "o", 1.0), placement, data_dir=str(tmp_path)
            ),
        )
        await resources.aclose()
    assert seen == [
        f"disk://?directory={tmp_path / 'memspine.cache'}",
        "disk://?directory=explicit.cache",
    ]
