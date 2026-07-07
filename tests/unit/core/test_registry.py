"""Dependency closure golden tests (§3, C1b/D-13)."""

from __future__ import annotations

import pytest

from memspine.core.registry import dependency_closure, validate_types
from memspine.exceptions import ConfigError


def test_independent_types_need_nothing() -> None:
    closure, auto = dependency_closure({"working", "episodic", "semantic"})
    assert closure == {"working", "episodic", "semantic"}
    assert auto == []


def test_reflective_pulls_episodic() -> None:
    closure, auto = dependency_closure({"reflective"})
    assert closure == {"reflective", "episodic"}
    assert auto == ["episodic"]


def test_prospective_and_associative_pull_semantic() -> None:
    closure, auto = dependency_closure({"prospective", "associative"})
    assert closure == {"prospective", "associative", "semantic"}
    assert auto == ["semantic"]


def test_shared_needs_any_of_semantic_or_episodic() -> None:
    closure, auto = dependency_closure({"shared"})
    assert closure == {"shared", "semantic"}  # deterministic default: first option
    assert auto == ["semantic"]

    closure2, auto2 = dependency_closure({"shared", "episodic"})
    assert closure2 == {"shared", "episodic"}
    assert auto2 == []


def test_unknown_type_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown memory type"):
        validate_types({"telepathic"})
    with pytest.raises(ConfigError):
        dependency_closure({"telepathic"})
