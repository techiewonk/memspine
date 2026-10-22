"""Stub adapters hard-fail with the D-10 error naming the right extra."""

from __future__ import annotations

import sys

import pytest

from memspine.exceptions import MissingServiceError


async def test_ladybug_client_without_package_names_the_graph_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # sys.modules["ladybug"] = None makes `import ladybug` raise ImportError even
    # in an environment that has ladybug installed — the missing-extra path is
    # exercised deterministically everywhere.
    from memspine.clients.ladybug import LadybugClient, LadybugSpec

    monkeypatch.setitem(sys.modules, "ladybug", None)
    with pytest.raises(MissingServiceError) as excinfo:
        await LadybugClient(LadybugSpec(":memory:")).open()
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
