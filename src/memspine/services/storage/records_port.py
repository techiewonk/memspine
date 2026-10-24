"""The SQL-independent ``records`` store port (ADR-081, RM-111).

``storage.records`` holds the authoritative state: the append-only event log and everything
that must commit with it or be read back exactly. This module states what the engine needs
from that store WITHOUT naming SQL, so a document database (MongoDB with a replica set,
RM-112) can implement it. SQLite and Postgres implement it today through
:class:`~memspine.services.storage.sql_base.SqlStorage`; the reusable conformance suite is
``tests/unit/services/storage/test_records_conformance.py`` (contract text:
``docs/RECORDS_STORE_CONTRACT.md``).

Facets (each a Protocol; :class:`RecordsStore` is their union):

* :class:`EventLogPort` - append-only log with a strictly increasing ``seq`` assigned at
  append (gaps allowed, reuse never; concurrent appenders get distinct, ordered seqs) and
  ordered replay by ``seq``.
* :class:`OutboxAppendPort` - one event plus its outbox items commit atomically: either the
  event and every item are durable, or none is (a failure on any item rolls the event back).
* :class:`OffsetPort` - per-projector high-water marks, advance-only except ``reset_offset``.
* :class:`LeasePort` (via :meth:`RecordsStore.leases`) - exclusive, expiring, epoch-fenced
  leases: one live holder per key; every takeover bumps the epoch; a stale holder's
  heartbeat or release is refused.
* :class:`RecordReadModelPort` - the materialized record read model.
* :class:`LineagePort` - the (child, parent, role) lineage DAG and its queries.
* :class:`ErasurePort` - the one sanctioned log mutation (M7 redaction keeps seq, kind and
  fingerprint and removes content), hard delete from the read model, and the children query
  erasure walks.
* :class:`SchemaPort` - versioned schema and index setup (``start`` is idempotent and brings
  the store to its current version; ``schema_revision`` names it), and ``stop``.
* :class:`ProviderMetadataPort` - the deployment identity and the projector target bindings
  (RM-112): what a projector checkpoint may be reused for lives with the checkpoint.

RM-112 widened the read-model and erasure facets with the helpers the engine calls
(``quarantined_ids``, ``recent_contents``, ``layer_counts``, ``count_quarantined``,
``list_quarantined``, ``scrub_marker_payloads``), so the engine needs nothing beyond this port
from ``storage.records``. SQL-only features (the outbox job book, the side tables, the intake
book, ``sql_adjacency``, pgvector) are refused for a records store without SQL by
:mod:`memspine.providers.records_features`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from memspine.exceptions import ConfigError
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.lineage import LineageEdge
from memspine.store.events import EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord

__all__ = [
    "ErasurePort",
    "EventLogPort",
    "LeaseLike",
    "LeasePort",
    "LineagePort",
    "OffsetPort",
    "OutboxAppendPort",
    "OutboxItem",
    "ProjectionBindingPort",
    "ProjectionTargetBinding",
    "ProviderMetadataPort",
    "RecordReadModelPort",
    "RecordsStore",
    "SchemaPort",
    "binding_source_name",
    "require_binding_match",
]


@dataclass(frozen=True, slots=True)
class OutboxItem:
    """One unit of derived work queued with an event: item ``offset`` of ``(scope, stage)``.

    Offsets are assigned by the store at commit (0, 1, 2, ... per ``(scope, stage)``), the
    same numbering the outbox job book (``runtime.outbox``) consumes."""

    scope: OwnerScope
    stage: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    at: datetime | None = None  # queue time; the event's ``ts`` when None


@runtime_checkable
class EventLogPort(Protocol):
    @property
    def can_rebuild(self) -> bool: ...

    async def append_event(self, event: MemoryEvent) -> MemoryEvent:
        """Assign the next ``seq`` and persist; ``seq`` is strictly increasing, never reused."""
        ...

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]:
        """Events with ``seq > after_seq`` in ascending ``seq`` order (replay order)."""
        ...

    async def prune_events(self, older_than: datetime) -> int: ...


@runtime_checkable
class OutboxAppendPort(Protocol):
    async def append_with_outbox(
        self, event: MemoryEvent, items: Sequence[OutboxItem]
    ) -> tuple[MemoryEvent, list[int]]:
        """Append ``event`` and queue ``items`` in ONE atomic commit; returns the event with
        its ``seq`` and each item's offset. Any failure leaves neither event nor items."""
        ...

    async def outbox_items(
        self, scope: OwnerScope, stage: str, after: int = -1
    ) -> list[tuple[int, dict[str, Any]]]:
        """Queued items of ``(scope, stage)`` with offset > ``after``, ascending."""
        ...


@runtime_checkable
class OffsetPort(Protocol):
    async def get_offset(self, projector_name: str) -> int: ...

    async def set_offset(self, projector_name: str, seq: int) -> None:
        """Advance-only checkpoint: never moves a mark backwards."""
        ...

    async def reset_offset(self, projector_name: str) -> None: ...


class LeaseLike(Protocol):
    """A held lease, as the lease port returns it."""

    @property
    def ns(self) -> str: ...

    @property
    def owner(self) -> str: ...

    @property
    def epoch(self) -> int: ...

    @property
    def lease_until(self) -> float: ...


@runtime_checkable
class LeasePort(Protocol):
    """Exclusive, expiring, epoch-fenced leases (synchronous: lease holders run in threads
    and processes as well as on the event loop)."""

    def claim(self, ns: str, owner: str, job_id: str, ttl: float) -> Any:
        """The lease when absent or expired (epoch + 1), else None."""
        ...

    def heartbeat(self, lease: Any, ttl: float) -> Any:
        """The extended lease, or None when ``lease`` is no longer current (fenced)."""
        ...

    def release(self, lease: Any) -> bool:
        """Free a current lease (its epoch stays); False when fenced."""
        ...

    def get_lease(self, ns: str) -> Any: ...

    def close(self) -> None: ...


@runtime_checkable
class RecordReadModelPort(Protocol):
    async def upsert_record(self, record: MemoryRecord) -> None: ...

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...

    async def get_records(self, record_ids: Sequence[str]) -> dict[str, MemoryRecord]:
        """RM-115: the existing records among ``record_ids`` keyed by id (missing ids
        absent), equal to one :meth:`get_record` per id, in one round trip per chunk."""
        ...

    async def list_records(
        self, namespace: str, memory_type: str | None = None, group_id: str | None = None
    ) -> list[MemoryRecord]: ...

    async def list_namespaces(self) -> list[str]: ...

    async def find_active_fact(
        self, namespace: str, entity: str, attribute: str
    ) -> MemoryRecord | None: ...

    async def list_session_records(
        self, namespace: str, session_key: str, roles: Sequence[str] | None = None
    ) -> list[MemoryRecord]: ...

    async def delete_all_records(self) -> None: ...

    async def quarantined_ids(self, record_ids: Sequence[str]) -> set[str]:
        """The ids among ``record_ids`` whose record exists and is quarantined."""
        ...

    async def recent_contents(self, namespace: str, limit: int) -> list[str]:
        """Contents of the ``limit`` most recently recorded non-quarantined records of
        ``namespace``, oldest first (ties keep :meth:`list_records` order)."""
        ...

    async def layer_counts(self, namespace: str) -> dict[str, int]:
        """Live (activated or resolving, not quarantined) records of ``namespace`` by layer."""
        ...

    async def count_quarantined(self, namespace: str) -> int: ...

    async def list_quarantined(self, namespace: str) -> list[MemoryRecord]:
        """:meth:`list_records` narrowed to quarantined records, in the same order."""
        ...


@runtime_checkable
class LineagePort(Protocol):
    async def add_lineage(
        self, edges: Sequence[LineageEdge], namespace: str | None = None
    ) -> int: ...

    async def lineage_parents(
        self, child_ids: Sequence[str], roles: Sequence[str] | None = None
    ) -> dict[str, list[LineageEdge]]: ...

    async def lineage_children(
        self, parent_ids: Sequence[str], roles: Sequence[str] | None = None
    ) -> dict[str, list[LineageEdge]]: ...

    async def delete_lineage(self, edges: Sequence[LineageEdge]) -> int: ...


@runtime_checkable
class ErasurePort(Protocol):
    async def redact_event_payloads(
        self, record_id: str | Sequence[str], node_renames: Mapping[str, str] | None = None
    ) -> list[int]:
        """Scrub every snapshot of the records from the log; seq, kind, fingerprint stay."""
        ...

    async def scrub_marker_payloads(
        self, marker: str, scrub: Callable[[dict[str, Any]], bool]
    ) -> list[int]:
        """The redaction pass for MARKER events named ``marker``: ``scrub`` mutates a decoded
        payload and returns True when it changed it; changed payloads get ``redacted: true``
        and keep seq and kind. Returns the rewritten seqs."""
        ...

    async def delete_record(self, record_id: str) -> None: ...

    async def list_children(
        self, namespace: str, parent_ids: Sequence[str]
    ) -> list[MemoryRecord]: ...


@runtime_checkable
class SchemaPort(Protocol):
    async def start(self) -> None:
        """Bring collections/tables and indexes to the current version (idempotent)."""
        ...

    async def schema_revision(self) -> str:
        """The applied schema version (an Alembic revision, a collection-setup version)."""
        ...

    async def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ProjectionTargetBinding:
    """The provider target a projector checkpoint belongs to (and, for a verified switch,
    the source binding it replaces)."""

    deployment_id: str
    projector_name: str
    provider: str  # "<category>:<name>:v<contract>"
    target_fingerprint: str
    generation_id: str = "active"
    source_provider: str | None = None
    source_target_fingerprint: str | None = None
    source_generation_id: str | None = None
    verified_checkpoint_seq: int | None = None


def binding_source_name(provider: str) -> str:
    """The provider NAME of a binding identity (``graph:ladybug:v1`` -> ``ladybug``)."""
    parts = provider.split(":")
    return parts[1] if len(parts) > 1 else provider


def require_binding_match(
    target: ProjectionTargetBinding, saved_provider: str, saved_fingerprint: str
) -> None:
    """Fail closed when the stored binding names another provider or target."""
    if saved_provider != target.provider or saved_fingerprint != target.target_fingerprint:
        raise ConfigError(
            f"projector {target.projector_name!r} is bound to provider "
            f"{saved_provider!r}, but configuration selects {target.provider!r} or a "
            "different target; startup is blocked until a verified projection switch "
            "is completed"
        )


@runtime_checkable
class ProjectionBindingPort(Protocol):
    """Projector target bindings, stored beside the projector offsets they gate.

    ``ensure`` creates a binding once (refusing a non-zero offset with no binding: the target
    may have changed under the same projector name), accepts the same target again, refuses a
    different provider, target or generation, and replaces the binding only through a verified
    switch: the stored binding equals the switch source and the offset equals the verified
    checkpoint (a compare-and-swap)."""

    async def bound_provider(self, deployment_id: str, projector_name: str) -> str | None: ...

    async def adopt_renamed_provider(
        self, target: ProjectionTargetBinding, old_provider: str
    ) -> bool: ...

    async def ensure(self, target: ProjectionTargetBinding) -> str: ...


@runtime_checkable
class ProviderMetadataPort(Protocol):
    async def claim_deployment_id(self, candidate: str) -> str:
        """Store ``candidate`` as the deployment identity unless one is stored (first writer
        wins, concurrent callers agree); return the stored identity."""
        ...

    def projection_bindings(self) -> ProjectionBindingPort: ...


@runtime_checkable
class RecordsStore(
    EventLogPort,
    OutboxAppendPort,
    OffsetPort,
    RecordReadModelPort,
    LineagePort,
    ErasurePort,
    SchemaPort,
    ProviderMetadataPort,
    Protocol,
):
    """Everything the engine needs from ``storage.records`` (ADR-081)."""

    @property
    def mode(self) -> EventLogMode:
        """The event-log mode the store runs in."""
        ...

    async def health(self) -> bool: ...

    def leases(self) -> LeasePort:
        """The store's lease table (shared by every process on a cluster store)."""
        ...
