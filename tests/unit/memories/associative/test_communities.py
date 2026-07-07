"""Community detection (D-40): a clean, logged no-op without ``[community]``."""

from __future__ import annotations

import pytest

from memspine.memories.associative import communities
from memspine.services.graph.base import GraphEdge


def _edges() -> list[GraphEdge]:
    return [
        GraphEdge(src="a", dst="b", rel_type="related", properties={"weight": 1.0}),
        GraphEdge(src="b", dst="c", rel_type="related", properties={"weight": 1.0}),
    ]


def test_noop_without_the_extra() -> None:
    if communities.communities_available():
        pytest.skip("graspologic installed — the no-op path is not reachable")
    assert communities.detect_communities(_edges()) == []


def test_absence_is_logged_at_info_exactly_once() -> None:
    if communities.communities_available():
        pytest.skip("graspologic installed — the no-op path is not reachable")
    communities._absence_logged = False
    assert communities.communities_available() is False
    assert communities._absence_logged is True  # first call logged
    assert communities.communities_available() is False  # second call silent


def test_detection_groups_connected_components_when_installed() -> None:
    if not communities.communities_available():
        pytest.skip("graspologic not installed ([community] extra)")
    clusters = communities.detect_communities(_edges(), min_size=3)
    assert clusters == [["a", "b", "c"]]
