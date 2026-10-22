"""Storage-owned placement policy without backend branches in consumers."""

from hashlib import sha256
from pathlib import Path

from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError
from memspine.providers.contracts import ProjectionPlacement
from memspine.providers.registry import ProviderDescriptor, get_registry, validate_settings


def storage_placement(config: MemspineConfig, owner_id: str) -> ProjectionPlacement:
    """Placement of host-local projections, owned by the ``storage.records`` provider.

    ``deployment.data_dir`` (ADR-081) is passed to the records descriptor. Unset, the base
    is the pre-ADR-081 one (the SQLite file path), so paths and fingerprints are unchanged."""
    registry = get_registry("storage_backends")
    records = config.storage.records
    descriptor: ProviderDescriptor = registry.get(records.provider)
    settings = validate_settings(descriptor, records.settings)
    if descriptor.placement is None:
        raise ConfigError("selected storage.records provider does not define projection placement")
    return descriptor.placement(settings, owner_id, config.deployment.data_dir)


def host_base_path(placement: ProjectionPlacement, slot: str, provider: str) -> Path:
    """The base path a host-local slot builds its files from (``<base>.lance`` etc.).

    Raises a ``ConfigError`` naming ``deployment.data_dir`` when the records store has no
    file to sit beside and no data directory is configured (ADR-081 rule 2)."""
    if placement.base_path is None:
        raise ConfigError(
            f"storage.{slot} provider {provider!r} is host-local and needs deployment.data_dir "
            "(the records store is not a file)"
        )
    return Path(placement.base_path)


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
