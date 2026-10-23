"""The Neo4j graph target identity is canonical and credential-free (no connection made).

It replaces the primary/mirror distinct-target check, which went with the graph mirror
(ADR-081 amendment 2026-10-23); the identity itself still keys the graph projection binding.
"""

from __future__ import annotations

from typing import Any

import pytest

from memspine.providers.registry import get_registry, provider_target_identity
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


def _target(prefix: str, *, database: str = "neo4j") -> Any:
    descriptor = get_registry("graph_stores").get("neo4j")
    identity = provider_target_identity(descriptor, _settings(prefix, database=database))
    assert identity is not None
    return identity.get("canonical_target", identity), identity


def test_one_target_has_one_identity_despite_credentials_and_url_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FIRST_URL", "neo4j://LOCALHOST")
    monkeypatch.setenv("FIRST_USER", "one")
    monkeypatch.setenv("FIRST_PASSWORD", "private-first-key")
    monkeypatch.setenv("SECOND_URL", "bolt://localhost:7687")
    monkeypatch.setenv("SECOND_USER", "two")
    monkeypatch.setenv("SECOND_PASSWORD", "private-second-key")
    first, first_full = _target("FIRST")
    second, second_full = _target("SECOND")
    assert first == second
    assert "private-" not in repr(first_full) + repr(second_full)


def test_distinct_databases_have_distinct_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIRST_URL", "bolt://localhost:7687")
    monkeypatch.setenv("SECOND_URL", "bolt://localhost:7687")
    assert _target("FIRST", database="one")[0] != _target("SECOND", database="two")[0]
