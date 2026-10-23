"""ADR-081 / RM-111 WP6: ``deployment.locality`` rules, checked before any provider opens.

Pure: reads the validated config and the provider descriptors; never imports a driver, opens a
connection or touches a file. The container runs :func:`validate_deployment_locality` in
``check_capabilities``, before the records store (or any other slot) is built.

Rules (``docs/STORAGE_SLOTS_MIGRATION_PLAN.md`` section 3):

- Every data descriptor declares ``process`` | ``host`` | ``cluster``; ``resolve_locality``
  refines it from the settings and the records slot (``:memory:`` SQLite is ``process``;
  LanceDB/Tantivy/LadybugDB placed beside an in-memory store are ``process``; ``sql_adjacency``
  takes the records slot's locality: ``cluster`` on Postgres, ``host`` on a SQLite file).
  The declared trait is used, not the endpoint: a cluster provider reached through a loopback
  address (a sidecar proxy such as cloud-sql-proxy) stays ``cluster``.
- ``multi_node``: every data slot that is built must be ``cluster``. The one exception is a
  ``process`` cache (``memory``): it only memoises deterministic embeddings and extractions,
  so a per-node copy cannot diverge. Host-local features are rejected too: the Resonate step
  journal (``workers.runner: temporal``), the embedding pin file (``model.embedding.pin_preset``)
  and the asset directory (``ingest.asset_dir`` with ``ingest.assets: on``).
- ``multi_process``: a ``process`` records store (``:memory:``) is rejected (each process
  would hold a private database). A single-writer host-local store (Tantivy, LadybugDB,
  filesystem documents) is allowed only in the process with ``deployment.writer: true``;
  every other process fails here instead of opening it. Exactly one writer per host is the
  operator's responsibility (it cannot be proven from one process's config).
- Outside ``single_process`` a slot set to ``auto`` must be explicit: two processes or nodes
  with different extras installed would resolve it differently.
- ``deployment.data_dir`` is required only when a built slot places its files beside the records
  store (``needs_placement``) and the records store is not a file (Postgres), or when the
  embedding pin file is on in that situation.

Slots that are not built are not checked: ``storage.lexical`` only with ``read.hybrid``,
``storage.graph`` only with associative memory enabled. A provider that declares no locality
(a third-party plug-in) is not rejected.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from pydantic import BaseModel

from memspine.config.schema import MemspineConfig
from memspine.config.schema.storage import SlotConfig
from memspine.exceptions import ConfigError
from memspine.providers.contracts import ProjectionPlacement
from memspine.providers.registry import (
    Locality,
    ProviderDescriptor,
    get_registry,
    validate_settings,
)

__all__ = [
    "DeploymentLocalityError",
    "SlotLocality",
    "slot_localities",
    "validate_deployment_locality",
]

_SLOT_KIND: dict[str, str] = {
    "records": "storage_backends",
    "vector": "vector_stores",
    "lexical": "lexical_indexes",
    "graph": "graph_stores",
    "cache": "cache_backends",
    "documents": "document_stores",
}


class DeploymentLocalityError(ConfigError):
    """The selected storage slots or features do not fit ``deployment.locality``."""


@dataclass(frozen=True, slots=True)
class SlotLocality:
    """One built slot's resolved provider and effective locality (None: undeclared)."""

    slot: str
    provider: str
    locality: Locality | None
    single_writer: bool
    needs_placement: bool


def _descriptor(slot: str, name: str) -> ProviderDescriptor | None:
    registry = get_registry(_SLOT_KIND[slot])
    found = registry.find(name)
    return found if isinstance(found, ProviderDescriptor) else None


def _settings(descriptor: ProviderDescriptor, slot: SlotConfig) -> BaseModel:
    return validate_settings(descriptor, slot.settings)


def _built_slots(config: MemspineConfig, enabled: Collection[str] | None) -> list[str]:
    storage = config.storage
    slots = ["records", "vector", "cache"]
    if config.read.hybrid:
        slots.append("lexical")
    if enabled is None or "associative" in enabled:
        slots.append("graph")
    if storage.documents is not None:
        slots.append("documents")
    return slots


def slot_localities(
    config: MemspineConfig, *, enabled: Collection[str] | None = None
) -> list[SlotLocality]:
    """The effective locality of every slot this config builds (``auto`` slots are listed with
    ``provider='auto'`` and no locality). ``enabled`` is the enabled memory types (None = all
    slots that may be built)."""
    out: list[SlotLocality] = []
    records_slot = config.storage.records
    records_descriptor = _descriptor("records", records_slot.provider)
    records: Locality = "host"
    if records_descriptor is not None:
        resolved = records_descriptor.effective_locality(
            _settings(records_descriptor, records_slot), "host"
        )
        records = resolved if resolved is not None else "cluster"
    for slot in _built_slots(config, enabled):
        selected = getattr(config.storage, slot)
        assert isinstance(selected, SlotConfig)
        if selected.provider == "auto":
            out.append(SlotLocality(slot, "auto", None, False, False))
            continue
        descriptor = _descriptor(slot, selected.provider)
        if descriptor is None:
            continue  # unknown names are reported by the build with the valid list
        settings = _settings(descriptor, selected)
        out.append(
            SlotLocality(
                slot,
                selected.provider,
                descriptor.effective_locality(settings, records),
                descriptor.single_writer,
                descriptor.placement_needed(settings),
            )
        )
    return out


def _cluster_alternatives(slot: str) -> str:
    kind = _SLOT_KIND[slot]
    names = [
        name
        for name, descriptor in get_registry(kind).items()
        if isinstance(descriptor, ProviderDescriptor) and descriptor.locality == "cluster"
    ]
    if slot == "graph":
        names.append("sql_adjacency on a Postgres storage.records")
    if not names:
        planned = " (mongodb is planned, RM-112)" if slot == "documents" else ""
        return f"no cluster provider is available for this slot yet{planned}"
    return "choose a cluster provider: " + ", ".join(names)


def _placement(config: MemspineConfig) -> ProjectionPlacement | None:
    from memspine.providers.placement import storage_placement

    try:
        return storage_placement(config, "locality-check")
    except ConfigError:
        return None  # the records build reports an unusable records provider


def validate_deployment_locality(
    config: MemspineConfig, *, enabled: Collection[str] | None = None
) -> list[SlotLocality]:
    """Apply the ADR-081 ``deployment.locality`` and ``data_dir`` rules (module docstring).

    Raises :class:`DeploymentLocalityError` listing every violation with the slot and the fix;
    returns the resolved slots otherwise. Never opens anything."""
    deployment = config.deployment
    mode = deployment.locality
    slots = slot_localities(config, enabled=enabled)
    errors: list[str] = []
    prefix = f"deployment.locality={mode}: "
    for item in slots:
        where = f"storage.{item.slot} provider {item.provider!r}"
        if item.provider == "auto":
            if mode != "single_process":
                errors.append(
                    f"{prefix}{where} must be explicit (auto can resolve differently in each "
                    f"process or node); set storage.{item.slot}.provider "
                    "(for example sql_adjacency or neo4j)"
                )
            continue
        if item.locality is None:
            continue
        if mode == "multi_node":
            if item.locality == "process" and item.slot == "cache":
                continue
            if item.locality != "cluster":
                kind = "in-process" if item.locality == "process" else "host-local"
                errors.append(
                    f"{prefix}{where} is {kind} and cannot be shared across nodes; "
                    f"{_cluster_alternatives(item.slot)}"
                )
        elif mode == "multi_process":
            if item.slot != "cache" and item.locality == "process":
                errors.append(
                    f"{prefix}{where} is in-process (each process would hold a private copy); "
                    "use a database file or a cluster provider"
                )
            elif item.single_writer and item.locality == "host" and not deployment.writer:
                errors.append(
                    f"{prefix}{where} is a single-writer host-local store and this process is "
                    "not the designated writer; set deployment.writer: true in exactly one "
                    "process (env MEMSPINE_DEPLOYMENT__WRITER=true) or "
                    f"{_cluster_alternatives(item.slot)}"
                )
    if mode == "multi_node":
        if config.workers.runner == "temporal":
            errors.append(
                f"{prefix}workers.resonate.journal_path ({config.workers.resonate.journal_path!r}) "
                "is a host-local step journal; use workers.runner: inline or taskiq, or "
                "deployment.locality: single_process"
            )
        if config.model.embedding.pin_preset:
            errors.append(
                f"{prefix}model.embedding.pin_preset writes a host-local pin file; set "
                "model.embedding.pin_preset: false (every node must run the same embedding "
                "config)"
            )
        if config.ingest.assets == "on" and config.ingest.asset_dir:
            errors.append(
                f"{prefix}ingest.asset_dir is a host-local asset registry and blob cache; unset "
                "it (in-memory registry per process) until the asset registry moves to "
                "storage.records/documents (RM-113 item 18)"
            )
    needs_base = [
        item
        for item in slots
        if item.needs_placement and item.locality == "host" and item.provider != "auto"
    ]
    pin_file = config.model.embedding.pin_preset
    if needs_base or pin_file:
        placement = _placement(config)
        if placement is not None and not placement.in_memory and placement.base_path is None:
            for item in needs_base:
                errors.append(
                    f"storage.{item.slot} provider {item.provider!r} is host-local and needs "
                    "deployment.data_dir (the records store is not a file)"
                )
            if pin_file:
                errors.append(
                    "model.embedding.pin_preset writes a pin file and needs deployment.data_dir "
                    "(the records store is not a file)"
                )
    if errors:
        raise DeploymentLocalityError("; ".join(errors))
    return slots
