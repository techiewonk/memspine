"""Storage-owned placement policy without backend branches in consumers."""
from hashlib import sha256

from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError
from memspine.providers.contracts import ProjectionPlacement
from memspine.providers.registry import ProviderDescriptor, get_registry, validate_settings


def storage_placement(config: MemspineConfig, owner_id: str) -> ProjectionPlacement:
    registry = get_registry("storage_backends")
    descriptor: ProviderDescriptor = registry.get(config.storage.provider)
    settings = validate_settings(descriptor, config.storage.settings)
    if descriptor.placement is None:
        raise ConfigError("selected storage provider does not define projection placement")
    return descriptor.placement(settings, owner_id)


def generation_component(generation_id: str) -> str:
    """Return a filesystem-safe, stable scope for an arbitrary generation ID."""
    if generation_id == "active":
        return generation_id
    return sha256(generation_id.encode("utf-8")).hexdigest()[:24]


def physical_generation_id(logical_service_id: str, generation_id: str) -> str:
    """Map a logical generation to an isolated physical scope.

    The initial ``active`` route deliberately preserves the provider's existing
    physical location. Rebuild generations include the logical service in the
    scope so equal generation labels in two categories cannot share a target.
    """
    if not logical_service_id or not generation_id:
        raise ValueError("logical service and generation identities are required")
    if generation_id == "active":
        return "active"
    return f"{logical_service_id}:{generation_id}"
