"""Community-detection policy (D-40 + v0.2 A6, ADR-028, ADR-035): the background
reorganizer's community knobs, surfaced as config.

Pure options carrier — the clustering itself lives in
``memspine.memories.associative.communities`` (lazy graspologic-native import,
slim core D-03). The reorganize pipeline binds this from
``memories.associative.policies.community`` and passes the validated knobs
through, so a deployment can tune community granularity without a code change
while defaults preserve rebuild determinism (D0.1).
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import Field

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions

__all__ = ["CommunityOptions", "CommunityPolicy"]


class CommunityOptions(PolicyOptions):
    #: Communities smaller than this earn no summary parent (mirrors the
    #: consolidation min-session floor).
    min_size: int = constants.REORGANIZE_MIN_COMMUNITY_SIZE
    #: Leiden granularity: higher resolution => more, smaller communities.
    #: 2.0 is a candidate pending calibration on real memory graphs (#24).
    resolution: float = constants.LEIDEN_RESOLUTION
    #: Leiden refinement exploration (graspologic-native ``randomness``).
    randomness: float = constants.LEIDEN_RANDOMNESS
    #: Fixed by default so the same graph yields the same communities.
    random_seed: int = constants.LEIDEN_RANDOM_SEED
    #: Upper bound on a single community before it is split.
    max_cluster_size: int = constants.LEIDEN_MAX_CLUSTER_SIZE
    #: KB-12: ``auto`` = Leiden (+ LPA refinement) when ``[community]`` is
    #: installed, else reorganize is a no-op; ``leiden`` = the same, explicitly;
    #: ``lpa`` = built-in label propagation, no extra needed (collapse-guarded).
    algorithm: Literal["auto", "leiden", "lpa"] = constants.COMMUNITY_ALGORITHM
    #: LPA passes refining a Leiden result (0 = pure Leiden).
    refine_passes: int = Field(default=constants.COMMUNITY_REFINE_PASSES, ge=0)
    #: KB-12/#84: per sleep, place new nodes by neighbour majority plus at most
    #: ``incremental_passes`` LPA passes instead of a full rebuild; a warm full
    #: refresh runs on the triggers below. Off by default.
    incremental: bool = False
    incremental_passes: int = Field(default=constants.COMMUNITY_INCREMENTAL_PASSES, ge=0)
    #: Full refresh once incrementally placed nodes exceed this share of the graph.
    refresh_fraction: float = Field(default=constants.COMMUNITY_REFRESH_FRACTION, gt=0.0)
    #: ...or every this many sleeps.
    refresh_every: int = Field(default=constants.COMMUNITY_REFRESH_EVERY, ge=1)
    #: #84: keep a summary parent while its community's membership Jaccard vs the
    #: summarised member set stays at or above this (1.0 = off; 0.8 recommended).
    summary_keep_jaccard: float = Field(
        default=constants.COMMUNITY_SUMMARY_KEEP_JACCARD, gt=0.0, le=1.0
    )


class CommunityPolicy(BindablePolicy):
    name: ClassVar[str] = "community"
    Options: ClassVar[type[PolicyOptions]] = CommunityOptions

    def _options(self) -> CommunityOptions:
        options = self.options
        assert isinstance(options, CommunityOptions)
        return options
