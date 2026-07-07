"""Namespace grammar + containment (M8.5)."""

from __future__ import annotations

import pytest

from memspine.core.namespace import is_within, parent, validate_namespace
from memspine.exceptions import NamespaceError


def test_normalizes_slashes() -> None:
    assert validate_namespace("/org/team/") == "org/team"


@pytest.mark.parametrize("bad", ["", "  ", "Org/Team", "a//b", "a/_x!", "-lead/x"])
def test_rejects_bad_paths(bad: str) -> None:
    with pytest.raises(NamespaceError):
        validate_namespace(bad)


def test_containment() -> None:
    assert is_within("org/team/agent", "org/team")
    assert is_within("org/team", "org/team")
    assert not is_within("org/team-b", "org/team")  # prefix, not a path boundary
    assert not is_within("org", "org/team")


def test_parent() -> None:
    assert parent("org/team/agent") == "org/team"
    assert parent("org") is None
