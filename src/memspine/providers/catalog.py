"""Sanitized provider inventory derived from the authoritative registries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from memspine.providers.registry import (
    KIND_TO_CATEGORY,
    ProviderDescriptor,
    get_registry,
    provider_kinds,
    requirements_available,
)

__all__ = ["PluginIdentity", "ProviderCatalogEntry", "catalog", "catalog_dicts"]


@dataclass(frozen=True, slots=True)
class PluginIdentity:
    name: str
    version: str
    hash: str | None
    slot: str
    lifetime: str


@dataclass(frozen=True, slots=True)
class ProviderCatalogEntry:
    category: str
    name: str
    contract_version: int
    service_manifest: dict[str, Any] | None
    plugin_identity: PluginIdentity | None
    availability: str
    declared_capabilities: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    configured_provider: str | None = None
    resolved_provider: str | None = None
    selection_reason: str | None = None
    lifecycle_state: str | None = None
    health_state: str | None = None
    readiness_state: str | None = None
    projection_generation: str | None = None
    checkpoint_seq: int | None = None
    source_head_seq: int | None = None
    lag_events: int | None = None
    sanitized_error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return only catalog allowlisted values."""
        return {
            "category": self.category,
            "name": self.name,
            "contract_version": self.contract_version,
            "service_manifest": self.service_manifest,
            "plugin_identity": (
                {
                    "name": self.plugin_identity.name,
                    "version": self.plugin_identity.version,
                    "hash": self.plugin_identity.hash,
                    "slot": self.plugin_identity.slot,
                    "lifetime": self.plugin_identity.lifetime,
                }
                if self.plugin_identity
                else None
            ),
            "availability": self.availability,
            "declared_capabilities": list(self.declared_capabilities),
            "required_capabilities": list(self.required_capabilities),
            "configured_provider": self.configured_provider,
            "resolved_provider": self.resolved_provider,
            "selection_reason": self.selection_reason,
            "lifecycle_state": self.lifecycle_state,
            "health_state": self.health_state,
            "readiness_state": self.readiness_state,
            "projection_generation": self.projection_generation,
            "checkpoint_seq": self.checkpoint_seq,
            "source_head_seq": self.source_head_seq,
            "lag_events": self.lag_events,
            "sanitized_error_code": self.sanitized_error_code,
        }


def _plugin_identities() -> dict[str, PluginIdentity]:
    # Import on use so importing the catalog does not initialize entry-point discovery.
    from memspine.plugins import enabled_manifest

    return {
        row["slot"]: PluginIdentity(
            name=row["name"],
            version=row["version"],
            hash=row["hash"],
            slot=row["slot"],
            lifetime=row["lifetime"],
        )
        for row in enabled_manifest()
    }


def catalog(
    *,
    category: str | None = None,
    configured: dict[str, str] | None = None,
    resolved: dict[str, str] | None = None,
    selection_reasons: dict[str, str] | None = None,
) -> tuple[ProviderCatalogEntry, ...]:
    """Build inventory rows from descriptors; no settings, imports or connections are exposed."""
    if category is not None and category not in KIND_TO_CATEGORY.values():
        raise ValueError(f"unknown provider category {category!r}")
    plugins = _plugin_identities()
    configured = configured or {}
    resolved = resolved or {}
    selection_reasons = selection_reasons or {}
    entries: list[ProviderCatalogEntry] = []
    for kind in provider_kinds():
        current_category = KIND_TO_CATEGORY[kind]
        if category is not None and current_category != category:
            continue
        registry = get_registry(kind)
        for name, value in registry.items():
            if not isinstance(value, ProviderDescriptor):
                raise TypeError(f"registry invariant violated: {kind}:{name} lacks a descriptor")
            available, missing = requirements_available(value)
            manifest = value.service_manifest
            entries.append(
                ProviderCatalogEntry(
                    category=current_category,
                    name=name,
                    contract_version=value.ref.contract_version,
                    service_manifest=(
                        {
                            "name": manifest.name,
                            "capability": manifest.capability,
                            "extra": manifest.extra,
                            "provides": list(manifest.provides),
                        }
                        if manifest is not None
                        else None
                    ),
                    plugin_identity=plugins.get(f"{kind}:{name}"),
                    availability="available" if available else f"missing_dependency:{','.join(missing)}",
                    declared_capabilities=tuple(sorted(value.declared_capabilities)),
                    required_capabilities=(),
                    configured_provider=configured.get(current_category),
                    resolved_provider=resolved.get(current_category),
                    selection_reason=selection_reasons.get(current_category),
                )
            )
    return tuple(entries)


def catalog_dicts(**kwargs: Any) -> list[dict[str, Any]]:
    return [entry.to_dict() for entry in catalog(**kwargs)]
