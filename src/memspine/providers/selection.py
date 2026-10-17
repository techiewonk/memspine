"""Capability-aware provider selection and settings validation."""

from __future__ import annotations

from dataclasses import dataclass

from memspine.exceptions import MissingServiceError
from memspine.providers.contracts import Category
from memspine.providers.registry import (
    KIND_TO_CATEGORY,
    ProviderDescriptor,
    ProviderRegistryError,
    get_registry,
    requirements_available,
)

__all__ = ["ProviderSelection", "ProviderSelectionError", "select_provider"]


class ProviderSelectionError(ProviderRegistryError):
    """No registered provider satisfies the explicit selection policy."""


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    descriptor: ProviderDescriptor
    configured_name: str
    reason: str


def select_provider(
    category: Category,
    configured_name: str,
    *,
    required_capabilities: frozenset[str] = frozenset(),
    preferred_names: tuple[str, ...] = (),
    eligible_names: frozenset[str] | None = None,
    available_services: frozenset[str] = frozenset(),
    available_capabilities: frozenset[str] = frozenset(),
) -> ProviderSelection:
    """Resolve a configured provider. Explicit selections never substitute silently.

    `auto` tries explicitly ordered preferences first, then registration order. A candidate is
    eligible only when its declared capabilities and installed package requirements satisfy the
    requested operation.
    """
    kind = next((kind for kind, current in KIND_TO_CATEGORY.items() if current == category), None)
    if kind is None:
        raise ProviderSelectionError(f"unknown provider category {category!r}")
    registry = get_registry(kind)
    if configured_name != "auto":
        selected = registry.find(configured_name)
        if not isinstance(selected, ProviderDescriptor):
            raise ProviderSelectionError(
                f"unknown {category} provider {configured_name!r} (valid: {', '.join(registry.names())})"
            )
        missing_capabilities = required_capabilities - selected.declared_capabilities
        available, missing_requirements = requirements_available(
            selected,
            available_services=available_services,
            available_capabilities=available_capabilities,
        )
        if missing_capabilities:
            raise ProviderSelectionError(
                f"{category} provider {configured_name!r} lacks required capabilities: "
                f"{', '.join(sorted(missing_capabilities))}"
            )
        if not available:
            missing_package = any(
                requirement.kind == "package"
                and not requirement.optional
                and requirement.name in missing_requirements
                for requirement in selected.requirements
            )
            if missing_package and selected.service_manifest and selected.service_manifest.extra:
                raise MissingServiceError(
                    f"{category} provider {configured_name!r}",
                    selected.service_manifest.extra,
                )
            raise ProviderSelectionError(
                f"{category} provider {configured_name!r} has unavailable requirements: "
                f"{', '.join(missing_requirements)}"
            )
        return ProviderSelection(selected, configured_name, "explicit provider is compatible")

    if eligible_names is None:
        raise ProviderSelectionError(
            f"{category} provider auto selection requires an explicit approved candidate set"
        )
    ordered = tuple(
        name
        for name in dict.fromkeys((*preferred_names, *registry.names()))
        if name in eligible_names
    )
    reasons: list[str] = []
    for name in ordered:
        if eligible_names is not None and name not in eligible_names:
            continue
        candidate = registry.find(name)
        if not isinstance(candidate, ProviderDescriptor):
            continue
        missing_capabilities = required_capabilities - candidate.declared_capabilities
        if missing_capabilities:
            reasons.append(f"{name}: missing capabilities {', '.join(sorted(missing_capabilities))}")
            continue
        available, missing_requirements = requirements_available(
            candidate,
            available_services=available_services,
            available_capabilities=available_capabilities,
        )
        if not available:
            reasons.append(f"{name}: missing requirements {', '.join(missing_requirements)}")
            continue
        return ProviderSelection(candidate, configured_name, "first compatible auto candidate")
    suffix = f" ({'; '.join(reasons)})" if reasons else ""
    raise ProviderSelectionError(
        f"no compatible {category} provider is registered for required capabilities "
        f"{sorted(required_capabilities)}{suffix}"
    )


__all__.append("validate_settings")
