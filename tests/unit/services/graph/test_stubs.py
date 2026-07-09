"""Stub adapters hard-fail with the D-10 error naming the right extra."""

from __future__ import annotations

import sys

import pytest

from memspine.clients.kuzu import KuzuClient
from memspine.exceptions import MissingServiceError


async def test_ladybug_client_without_package_names_the_graph_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # sys.modules["ladybug"] = None makes `import ladybug` raise ImportError even
    # in an environment that has ladybug installed — the missing-extra path is
    # exercised deterministically everywhere (mirrors the kuzu test below).
    from memspine.clients.ladybug import LadybugClient

    monkeypatch.setitem(sys.modules, "ladybug", None)
    with pytest.raises(MissingServiceError) as excinfo:
        await LadybugClient(":memory:").connect()
    assert excinfo.value.service == "graph:ladybug"
    assert excinfo.value.extra == "graph"
    assert "pip install memspine[graph]" in str(excinfo.value)


def test_neo4j_stub_names_the_neo4j_extra() -> None:
    from memspine.services.graph.neo4j import Neo4jGraphStore

    with pytest.raises(MissingServiceError) as excinfo:
        Neo4jGraphStore()
    assert excinfo.value.service == "graph:neo4j"
    assert excinfo.value.extra == "neo4j"
    assert "pip install memspine[neo4j]" in str(excinfo.value)


async def test_kuzu_client_without_package_names_the_kuzu_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # sys.modules["kuzu"] = None makes `import kuzu` raise ImportError even in
    # an environment that has kuzu installed — the missing-extra path is
    # exercised deterministically everywhere.
    monkeypatch.setitem(sys.modules, "kuzu", None)
    with pytest.raises(MissingServiceError) as excinfo:
        await KuzuClient(":memory:").connect()
    assert excinfo.value.service == "graph:kuzu"
    assert excinfo.value.extra == "kuzu"
    assert "pip install memspine[kuzu]" in str(excinfo.value)
