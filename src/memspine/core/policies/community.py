"""Community-detection policy (D-40 + v0.2 A6, ADR-028): the background
reorganizer's Leiden knobs, surfaced as config.

Pure options carrier — the clustering itself lives in
``memspine.memories.associative.communities.detect_communities`` (lazy
leidenalg import, slim core D-03). The reorganize pipeline binds this from
``memories.associative.policies.community`` and passes the validated knobs
through, so a deployment can tune community granularity without a code change
while defaults preserve today's behavior (rebuild determinism, D0.1).
"""

from __future__ import annotations

from typing import ClassVar

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions

__all__ = ["CommunityOptions", "CommunityPolicy"]


class CommunityOptions(PolicyOptions):
    #: Communities smaller than this earn no summary parent (mirrors the
    #: consolidation min-session floor).
    min_size: int = constants.REORGANIZE_MIN_COMMUNITY_SIZE
    #: Leiden granularity: higher resolution => more, smaller communities.
    resolution: float = constants.LEIDEN_RESOLUTION
    #: Refinement randomness (retained for compat; leidenalg seeds it internally
    #: via ``random_seed`` — see detect_communities, ADR-028).
    randomness: float = constants.LEIDEN_RANDOMNESS
    #: Fixed by default so the same graph yields the same communities.
    random_seed: int = constants.LEIDEN_RANDOM_SEED
    #: Upper bound on a single community before it is recursively split.
    max_cluster_size: int = constants.LEIDEN_MAX_CLUSTER_SIZE


class CommunityPolicy(BindablePolicy):
    name: ClassVar[str] = "community"
    Options: ClassVar[type[PolicyOptions]] = CommunityOptions

    def _options(self) -> CommunityOptions:
        options = self.options
        assert isinstance(options, CommunityOptions)
        return options
