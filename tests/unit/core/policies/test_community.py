"""Community-detection policy (v0.2 A6): defaults mirror the constants and
unknown keys are rejected at bind time (extra="forbid")."""

from __future__ import annotations

import pytest

from memspine.config import constants
from memspine.core.policies.community import CommunityOptions, CommunityPolicy
from memspine.exceptions import ConfigError


def test_defaults_track_the_constants() -> None:
    opts = CommunityPolicy.bind().options
    assert isinstance(opts, CommunityOptions)
    assert opts.min_size == constants.REORGANIZE_MIN_COMMUNITY_SIZE
    assert opts.resolution == constants.LEIDEN_RESOLUTION
    assert opts.randomness == constants.LEIDEN_RANDOMNESS
    assert opts.random_seed == constants.LEIDEN_RANDOM_SEED
    assert opts.max_cluster_size == constants.LEIDEN_MAX_CLUSTER_SIZE


def test_overrides_bind() -> None:
    opts = CommunityPolicy.bind({"min_size": 5, "resolution": 2.0, "random_seed": 7}).options
    assert isinstance(opts, CommunityOptions)
    assert (opts.min_size, opts.resolution, opts.random_seed) == (5, 2.0, 7)


def test_unknown_key_is_a_config_error() -> None:
    with pytest.raises(ConfigError):
        CommunityPolicy.bind({"bogus": 1})
