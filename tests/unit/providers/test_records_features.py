"""RM-112: features that need a SQL records store are refused for one without SQL (MongoDB),
before any provider opens, naming every gap. No server is needed: the check reads the config
and the descriptors only."""

from __future__ import annotations

from typing import Any

import pytest

from memspine.config.schema import MemspineConfig
from memspine.providers import records_features
from memspine.providers.records_features import (
    RecordsFeatureError,
    records_has_sql,
    sql_feature_gaps,
    validate_records_features,
)

_RUNTIME = {
    "provider_services": {
        "health_timeout_s": 5.0,
        "close_timeout_s": 10.0,
        "startup_timeout_s": None,
    }
}
_MONGO = {"provider": "mongodb", "settings": {"url": "mongodb://db.example:27017/?replicaSet=rs0"}}


def _config(**overrides: Any) -> MemspineConfig:
    storage = {"records": _MONGO, **overrides.pop("storage", {})}
    return MemspineConfig.model_validate({"runtime": _RUNTIME, "storage": storage, **overrides})


def _with(config: MemspineConfig, area: str, **values: Any) -> MemspineConfig:
    """Set fields past their cross-field validators (the gaps check is independent of them)."""
    section = getattr(config, area).model_copy(update=values)
    return config.model_copy(update={area: section})


def test_sql_records_providers_have_no_gaps() -> None:
    sqlite = MemspineConfig.model_validate({"runtime": _RUNTIME, "workers": {"outbox": True}})
    assert records_has_sql(sqlite)
    validate_records_features(sqlite, enabled={"associative"})  # nothing refused


def test_a_plain_mongodb_config_has_no_gaps() -> None:
    config = _config()
    assert not records_has_sql(config)
    assert sql_feature_gaps(config, enabled={"semantic", "episodic"}) == []
    validate_records_features(config, enabled={"semantic", "episodic"})


def test_every_sql_only_feature_is_listed_at_once() -> None:
    config = _config(storage={"vector": {"provider": "pgvector"}})
    config = _with(config, "workers", outbox=True)
    config = _with(config, "offline", sleep=True)
    config = _with(config, "economy", ledger=True, ledger_persist=True)
    config = _with(config, "write", window_extract="facts")
    storage = config.storage.model_copy(
        update={
            "graph": config.storage.graph.model_copy(update={"provider": "sql_adjacency"}),
        }
    )
    config = config.model_copy(update={"storage": storage})
    with pytest.raises(RecordsFeatureError) as caught:
        validate_records_features(config, enabled={"associative"})
    message = str(caught.value)
    assert "storage.records.provider 'mongodb' has no SQL access" in message
    for key in (
        "workers.outbox",
        "offline.sleep",
        "economy.ledger_persist",
        "write.window_extract",
        "pgvector",
        "sql_adjacency",
    ):
        assert key in message, key


def test_graph_auto_needs_ladybug_without_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()
    monkeypatch.setattr(records_features, "_ladybug_installed", lambda: False)
    gaps = sql_feature_gaps(config, enabled={"associative"})
    assert len(gaps) == 1 and "storage.graph.provider 'auto'" in gaps[0]
    assert sql_feature_gaps(config, enabled={"semantic"}) == []  # no graph is built
    monkeypatch.setattr(records_features, "_ladybug_installed", lambda: True)
    assert sql_feature_gaps(config, enabled={"associative"}) == []


def test_mongodb_records_descriptor_is_cluster_without_sql() -> None:
    from memspine.providers.registry import get_registry

    descriptor = get_registry("storage_backends").get("mongodb")
    assert descriptor.locality == "cluster" and not descriptor.single_writer
    assert "sql" not in descriptor.declared_capabilities
    assert {"records", "transactions", "leases", "outbox"} <= descriptor.declared_capabilities
    assert descriptor.requires_deployment_id
    settings = descriptor.settings_model.model_validate(_MONGO["settings"])
    target = descriptor.target_identity(settings)  # type: ignore[misc]
    assert "db.example" in str(target) and "password" not in str(target)
