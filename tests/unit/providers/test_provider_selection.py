from __future__ import annotations

from typing import Any

import pytest

from memspine.providers.contracts import BuildResult, DependencyRequirement, ProviderRef
from memspine.providers.registry import EMBEDDERS, ProviderDescriptor
from memspine.providers.selection import ProviderSelectionError, select_provider
from memspine.providers.settings import EmptySettings


class _Builder:
    async def build(self, settings: EmptySettings, ctx: Any) -> BuildResult[str]:
        return BuildResult("ok", ProviderRef("embedding", "selection_test"), (), frozenset())


def _descriptor(
    name: str,
    capabilities: frozenset[str],
    requirements: tuple[DependencyRequirement, ...] = (),
) -> ProviderDescriptor:
    return ProviderDescriptor(
        ProviderRef("embedding", name), EmptySettings, capabilities, requirements, _Builder()
    )


def test_explicit_selection_preserves_provider_and_checks_capabilities() -> None:
    selected = select_provider("embedding", "hash", required_capabilities=frozenset({"embedding"}))
    assert selected.descriptor.ref.name == "hash"
    assert selected.reason.startswith("explicit")
    with pytest.raises(ProviderSelectionError, match="lacks required capabilities"):
        select_provider("embedding", "hash", required_capabilities=frozenset({"gpu"}))


def test_explicit_provider_failure_does_not_substitute() -> None:
    name = "registry_test_missing_package_explicit"
    EMBEDDERS.register(
        name,
        _descriptor(
            name,
            frozenset({"embedding"}),
            (DependencyRequirement("package", "memspine_package_that_does_not_exist_xyz"),),
        ),
    )
    try:
        with pytest.raises(ProviderSelectionError, match="unavailable requirements"):
            select_provider("embedding", name)
    finally:
        EMBEDDERS.unregister(name)


def test_auto_uses_preference_then_eligible_compatible_candidates() -> None:
    name = "registry_test_auto"
    EMBEDDERS.register(name, _descriptor(name, frozenset({"embedding", "gpu"})))
    try:
        selected = select_provider(
            "embedding",
            "auto",
            required_capabilities=frozenset({"gpu"}),
            preferred_names=(name,),
            eligible_names=frozenset({name, "hash"}),
        )
        assert selected.descriptor.ref.name == name
        restricted = select_provider(
            "embedding",
            "auto",
            required_capabilities=frozenset({"embedding"}),
            preferred_names=(name,),
            eligible_names=frozenset({"hash"}),
        )
        assert restricted.descriptor.ref.name != name
    finally:
        EMBEDDERS.unregister(name)


def test_auto_skips_candidate_with_missing_package() -> None:
    name = "registry_test_missing_package"
    EMBEDDERS.register(
        name,
        _descriptor(
            name,
            frozenset({"embedding"}),
            (DependencyRequirement("package", "memspine_package_that_does_not_exist_xyz"),),
        ),
    )
    try:
        with pytest.raises(ProviderSelectionError, match="no compatible"):
            select_provider(
                "embedding",
                "auto",
                required_capabilities=frozenset({"embedding"}),
                preferred_names=(name,),
                eligible_names=frozenset({name}),
            )
    finally:
        EMBEDDERS.unregister(name)
