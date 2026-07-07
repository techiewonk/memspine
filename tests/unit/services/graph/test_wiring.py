"""Engine wiring: graph construction gate + provider selection (D-26/D-10).

profile="simple" must stay green: no graph store exists unless associative
memory is enabled or the ``graph:`` config block is set explicitly.
"""

from __future__ import annotations

import pytest

from memspine import Engine
from memspine.exceptions import ConfigError, MissingServiceError


def _engine(**overrides: object) -> Engine:
    return Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        **overrides,
    )


async def test_simple_profile_constructs_no_graph_store() -> None:
    eng = _engine()
    await eng.start()
    try:
        assert eng.describe()["graph"] is None
    finally:
        await eng.stop()


async def test_associative_memory_enables_the_default_graph_store() -> None:
    eng = _engine(memories={"associative": {"enabled": True}})
    await eng.start()
    try:
        world = eng.describe()
        assert "associative" in world["memories"]["enabled"]
        assert world["graph"] == "SQLiteAdjacencyGraph"
    finally:
        await eng.stop()


async def test_explicit_graph_config_constructs_without_associative() -> None:
    eng = _engine(graph={"provider": "sqlite_adjacency"})
    await eng.start()
    try:
        world = eng.describe()
        assert "associative" not in world["memories"]["enabled"]
        assert world["graph"] == "SQLiteAdjacencyGraph"
    finally:
        await eng.stop()


async def test_ladybug_provider_fails_actionably_naming_the_extra() -> None:
    eng = _engine(graph={"provider": "ladybug"})
    with pytest.raises(MissingServiceError) as excinfo:
        await eng.start()
    assert excinfo.value.extra == "graph"


async def test_unknown_graph_provider_is_a_config_error() -> None:
    eng = _engine(graph={"provider": "carrier_pigeon"})
    with pytest.raises(ConfigError, match="carrier_pigeon"):
        await eng.start()
