"""Pre-connect graph role collision checks using provider-declared identity."""

from __future__ import annotations

from pydantic import BaseModel

from memspine.exceptions import ConfigError
from memspine.providers.registry import ProviderDescriptor, provider_target_identity


def require_distinct_graph_targets(
    primary: ProviderDescriptor,
    primary_settings: BaseModel,
    mirror: ProviderDescriptor,
    mirror_settings: BaseModel,
) -> None:
    """Reject duplicate targets before either role opens a native connection.

    Providers may include ``canonical_target`` in their safe target identity to
    separate physical destination from authentication/revision fingerprints.
    Identity callbacks resolve configuration only; they must not connect.
    """
    left = provider_target_identity(primary, primary_settings)
    right = provider_target_identity(mirror, mirror_settings)
    if left is None or right is None:
        if primary.ref == mirror.ref:
            raise ConfigError(
                "primary/mirror target isolation requires provider target identity"
            )
        return
    left_target = left.get("canonical_target", left)
    right_target = right.get("canonical_target", right)
    if left_target == right_target:
        raise ConfigError("primary graph and graph mirror must use distinct canonical targets")


__all__ = ["require_distinct_graph_targets"]
