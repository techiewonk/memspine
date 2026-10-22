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
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.lineage import LineageEdge
from memspine.store.events import MemoryEvent
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
    "RecordReadModelPort",
    "RecordsStore",
    "SchemaPort",
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


@runtime_checkable
class RecordsStore(
    EventLogPort,
    OutboxAppendPort,
    OffsetPort,
    RecordReadModelPort,
    LineagePort,
    ErasurePort,
    SchemaPort,
    Protocol,
):
    """Everything the engine needs from ``storage.records`` (ADR-081)."""

    def leases(self) -> LeasePort:
        """The store's lease table (shared by every process on a cluster store)."""
        ...
