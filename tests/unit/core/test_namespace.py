"""Namespace grammar + containment (M8.5) + grant enforcement (ADR-016)."""

from __future__ import annotations

import pytest

from memspine.core.namespace import grant_allows, is_within, parent, validate_namespace
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


# ── grant_allows: the ONE cross-namespace read decision (ADR-016) ─────────────


def test_own_namespace_is_always_readable() -> None:
    assert grant_allows("a", "a", "semantic", {})
    assert grant_allows("a", "a", "shared", {})  # own bookkeeping stays visible


def test_ungranted_namespaces_convey_nothing() -> None:
    assert not grant_allows("b", "a", "semantic", {})
    assert not grant_allows("b", "a", "semantic", {"c": None})


def test_unscoped_grant_covers_every_substantive_type() -> None:
    grants: dict[str, frozenset[str] | None] = {"a": None}
    assert grant_allows("b", "a", "semantic", grants)
    assert grant_allows("b", "a", "episodic", grants)


def test_scoped_grant_filters_by_memory_type() -> None:
    grants: dict[str, frozenset[str] | None] = {"a": frozenset({"episodic"})}
    assert grant_allows("b", "a", "episodic", grants)
    assert not grant_allows("b", "a", "semantic", grants)


def test_shared_bookkeeping_never_crosses_a_grant() -> None:
    # Even an unscoped grant must not let B enumerate A's other grants.
    assert not grant_allows("b", "a", "shared", {"a": None})


def test_hierarchy_conveys_nothing() -> None:
    # A grant to the parent does not cover the child namespace, and reading
    # a child of your own namespace still needs a grant (exact-match scoping).
    assert not grant_allows("org/team", "org", "semantic", {})
    assert not grant_allows("org", "org/team", "semantic", {})
