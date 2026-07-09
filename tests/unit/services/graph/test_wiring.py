"""Engine wiring: graph construction gate + provider selection (D-26/D-10).

profile="simple" must stay green: no graph store exists unless associative
memory is enabled; an explicit ``graph:`` block without associative memory is
a ConfigError (a dead store handle no projector maintains — M6, P6 review).
"""

from __future__ import annotations

import sys

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


async def test_explicit_graph_config_without_associative_is_a_config_error() -> None:
    """A graph store no projector ever writes is a dead handle — refuse the
    config loudly instead of constructing it (M6, P6 review)."""
    eng = _engine(graph={"provider": "sqlite_adjacency"})
    with pytest.raises(ConfigError, match=r"memories\.associative not enabled"):
        await eng.start()


async def test_ladybug_provider_without_package_fails_actionably_naming_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ladybug is now a real, installable adapter (D-26) — force the
    # missing-package path deterministically so this test holds whether or
    # not [graph] happens to be installed in the running environment (mirrors
    # the kuzu-client determinism trick in test_stubs.py).
    monkeypatch.setitem(sys.modules, "ladybug", None)
    eng = _engine(
        graph={"provider": "ladybug"},
        memories={"associative": {"enabled": True}},
    )
    with pytest.raises(MissingServiceError) as excinfo:
        await eng.start()
    assert excinfo.value.extra == "graph"


async def test_ladybug_provider_constructs_the_real_adapter() -> None:
    pytest.importorskip("ladybug")
    eng = _engine(
        graph={"provider": "ladybug"},
        memories={"associative": {"enabled": True}},
    )
    await eng.start()
    try:
        world = eng.describe()
        assert world["graph"] == "LadybugGraphStore"
    finally:
        await eng.stop()


async def test_unknown_graph_provider_is_a_config_error() -> None:
    eng = _engine(
        graph={"provider": "carrier_pigeon"},
        memories={"associative": {"enabled": True}},
    )
    with pytest.raises(ConfigError, match="carrier_pigeon"):
        await eng.start()
