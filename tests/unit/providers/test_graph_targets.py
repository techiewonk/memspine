from __future__ import annotations

import pytest

from memspine.exceptions import ConfigError
from memspine.providers.graph_targets import require_distinct_graph_targets
from memspine.providers.registry import get_registry
from memspine.providers.settings import Neo4jSettings


def _settings(prefix: str, *, database: str = "neo4j") -> Neo4jSettings:
    return Neo4jSettings(
        url_env=f"{prefix}_URL",
        user_env=f"{prefix}_USER",
        password_env=f"{prefix}_PASSWORD",
        database=database,
        connect_timeout_s=1.0,
        request_timeout_s=2.0,
    )


def test_duplicate_graph_target_rejected_despite_credentials_and_url_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = get_registry("graph_stores").get("neo4j")
    monkeypatch.setenv("PRIMARY_URL", "neo4j://LOCALHOST")
    monkeypatch.setenv("PRIMARY_USER", "one")
    monkeypatch.setenv("PRIMARY_PASSWORD", "private-primary-key")
    monkeypatch.setenv("MIRROR_URL", "bolt://localhost:7687")
    monkeypatch.setenv("MIRROR_USER", "two")
    monkeypatch.setenv("MIRROR_PASSWORD", "private-mirror-key")
    with pytest.raises(ConfigError, match="distinct canonical targets") as error:
        require_distinct_graph_targets(
            descriptor, _settings("PRIMARY"), descriptor, _settings("MIRROR")
        )
    assert "private-" not in str(error.value)


def test_distinct_graph_databases_allowed_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = get_registry("graph_stores").get("neo4j")
    monkeypatch.setenv("PRIMARY_URL", "bolt://localhost:7687")
    monkeypatch.setenv("MIRROR_URL", "bolt://localhost:7687")
    require_distinct_graph_targets(
        descriptor,
        _settings("PRIMARY", database="primary"),
        descriptor,
        _settings("MIRROR", database="mirror"),
    )
