"""significance.py: conversation-level permutation, cluster bootstrap guard, BH (M4)."""

from __future__ import annotations

import pytest
import significance as sig


def test_cluster_permutation_exact_all_positive_is_minimal_p() -> None:
    # 10 clusters, every one positive: only the all-plus and all-minus patterns reach
    # the observed |sum|, so the exact p is 2 / 2^10.
    clusters = {f"c{i}": [1.0, 0.0, 0.0] for i in range(10)}
    assert sig.cluster_permutation_p(clusters) == pytest.approx(2 / 1024)


def test_cluster_permutation_flips_whole_clusters() -> None:
    # One large positive cluster and one small negative one. Question-level flips
    # would make the result look strong; cluster-level flips cannot (2 clusters ->
    # 4 patterns, all of which reach |observed| = 45).
    clusters = {"a": [1.0] * 50, "b": [-1.0] * 5}
    assert sig.cluster_permutation_p(clusters) == pytest.approx(1.0)
    assert sig.permutation_p([1.0] * 50 + [-1.0] * 5) < 0.001


def test_cluster_permutation_null_and_empty() -> None:
    assert sig.cluster_permutation_p({"a": [0.0, 0.0], "b": [0.0]}) == 1.0
    assert sig.cluster_permutation_p({}) == 1.0


def test_cluster_permutation_random_branch_above_exact_limit() -> None:
    clusters = [[1.0] for _ in range(sig.EXACT_CLUSTER_LIMIT + 5)]
    p = sig.cluster_permutation_p(clusters, permutations=2000)
    assert 0 < p < 0.01


def test_cluster_bootstrap_refuses_fewer_than_30_clusters() -> None:
    with pytest.raises(ValueError, match="30"):
        sig.cluster_bootstrap_ci({f"c{i}": [1.0] for i in range(10)})


def test_cluster_bootstrap_ci_covers_the_pooled_mean() -> None:
    clusters = {f"u{i}": [1.0 if i % 3 else -1.0, 0.0] for i in range(40)}
    flat = [x for g in clusters.values() for x in g]
    mean = sum(flat) / len(flat)
    low, high = sig.cluster_bootstrap_ci(clusters, resamples=2000)
    assert low <= mean <= high
    assert sig.MIN_BOOTSTRAP_CLUSTERS == 30


def test_benjamini_hochberg_matches_reference() -> None:
    # Reference values from the standard step-up procedure.
    p = [0.01, 0.04, 0.03, 0.005]
    assert sig.benjamini_hochberg(p) == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert sig.benjamini_hochberg([]) == []
