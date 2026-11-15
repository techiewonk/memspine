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
* :class:`RecordsProjectionPort` - an ordered, atomic fold of canonical events into
  records and their scoped derived claims, with an internal durable cursor.
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
from ``storage.records``. SQL-only features (the outbox job book, the side tables,
``sql_adjacency``, pgvector) are refused for a records store without SQL by
:mod:`memspine.providers.records_features`. The intake book is its own capability port
(RM-137, :mod:`memspine.services.storage.intake_port`, capability ``intake_ledger``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from memspine.exceptions import ConfigError
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.lineage import LineageEdge
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord

if TYPE_CHECKING:
    from memspine.services.storage.entity_index_tables import EntityIndexRow

__all__ = [
    "EntityIndexAdvance",
    "EntityIndexEffects",
    "EntityIndexProjectionPort",
    "EntityIndexProjectionView",
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
    "RecordsAdvance",
    "RecordsProjectionPort",
    "RecordsProjectionView",
    "RecordsStore",
    "SchemaPort",
    "binding_source_name",
    "require_binding_match",
    "shares_listed_records",
    "sharing_listed_records",
]


#: RM-161: set for the duration of one read (``read.pipeline.timeview._snapshot_scoped``).
#: While it is set, a records store MAY return, for a record whose stored row is unchanged
#: since an earlier listing, the very object that listing built instead of validating the row
#: again. The returned records are then shared with other readers and must not be mutated in
#: place (the read path copies with ``model_copy``). A store that ignores it stays correct.
_SHARE_LISTED: ContextVar[bool] = ContextVar("memspine_share_listed_records", default=False)


@contextmanager
def sharing_listed_records() -> Iterator[None]:
    """RM-161: let the records store reuse unchanged listed records inside this block."""
    token = _SHARE_LISTED.set(True)
    try:
        yield
    finally:
        _SHARE_LISTED.reset(token)


def shares_listed_records() -> bool:
    """RM-161: whether the current context allows reusing unchanged listed records."""
    return _SHARE_LISTED.get()


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

    async def assert_full_event_history(self) -> None:
        """Raise when an actual prune removed any canonical event, even if log is now empty.

        Rollback gaps in allocated sequence numbers are not pruned history.
        """
        ...

    async def append_event(self, event: MemoryEvent) -> MemoryEvent:
        """Assign the next ``seq`` and persist; ``seq`` is strictly increasing, never reused."""
        ...

    async def append_registry_marker(self, event: MemoryEvent) -> MemoryEvent:
        """Append a registry marker; atomically validate pinned sources for copied text."""
        ...

    async def registry_erasure_fences(self, after_seq: int = 0) -> list[tuple[int, str, list[str]]]:
        """Return newer canonical source-erasure fences for warm registry invalidation."""
        ...

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]:
        """Events with ``seq > after_seq`` in ascending ``seq`` order (replay order)."""
        ...

    async def read_scoped_events(
        self, namespace: str, kinds: Sequence[EventKind], *, after_seq: int, limit: int
    ) -> tuple[list[MemoryEvent], int]:
        """At most ``limit`` matching events in seq order and a proven global head.

        The head is captured before the scoped query. Returned events satisfy
        ``after_seq < seq <= head``; callers may advance through the head only
        when fewer than ``limit`` matching events were returned.
        """
        ...

    async def prune_events(self, older_than: datetime) -> int: ...


@dataclass(frozen=True, slots=True)
class EntityIndexEffects:
    """ID-only notifications published after an ordered projection transaction commits."""

    changed: bool = False
    removed_entity_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EntityIndexAdvance:
    """Durable receipt for one bounded, serialized entity-index projection page."""

    epoch: int
    namespace_cursor: int
    namespace_mutation_revision: int
    global_offset: int
    complete: bool
    applied_count: int
    effects: EntityIndexEffects = field(default_factory=EntityIndexEffects)


class EntityIndexProjectionView(Protocol):
    """Index CRUD and record reads bound to the advance transaction, never a second writer."""

    async def put(self, rows: Sequence[EntityIndexRow]) -> None: ...

    async def select(
        self,
        namespace: str,
        kind: str,
        *,
        key: str | None = None,
        entity_id: str | None = None,
        ref: str | None = None,
        match: str | None = None,
    ) -> list[EntityIndexRow]: ...

    async def delete(
        self,
        namespace: str,
        kind: str,
        *,
        key: str | None = None,
        entity_id: str | None = None,
        ref: str | None = None,
    ) -> None: ...

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...

    async def pending_events(self, limit: int) -> list[MemoryEvent]:
        """Return retained canonical markers for the oldest ID-only pending rows.

        Results are namespace-bound and ordered by sequence. A missing canonical
        event is a history error; it must never silently certify the cursor.
        """
        ...


EntityIndexApplyBatch = Callable[
    [EntityIndexProjectionView, Sequence[MemoryEvent]], Awaitable[EntityIndexEffects]
]
EntityIndexApplyPurge = Callable[
    [EntityIndexProjectionView, Sequence[str]], Awaitable[EntityIndexEffects]
]


@runtime_checkable
class EntityIndexProjectionPort(Protocol):
    """The storage-owned ordered entity-index door.

    An advance takes the backend's shared reset gate and namespace writer lock BEFORE
    reading any cursor, event, record or index row. It captures a committed target no
    higher than ``target_seq``, reads at most ``limit + 1`` required canonical events
    for this namespace in seq order, and folds at most ``limit`` through ``apply_batch``.
    The callback is retry-pure: it uses only ``view`` and returns ID-only effects; it
    never appends events or opens another transaction. Rows, private namespace cursor,
    mutation revision, and the conservative global projector prefix commit atomically.
    A non-complete receipt never authorizes acknowledgement through ``target_seq``.

    The private scan cursor tracks events folded into ID-only prefix state. The
    certified namespace cursor is the scan cursor capped immediately before the
    earliest pending positive marker, whose canonical payload is retained by the
    global prune offset. Each callback may retry at most ``limit`` pending markers
    and fold at most ``limit`` new events. It runs even when the new page is empty.
    Only certified cursors contribute to the global prefix.

    The global prefix moves only across a bounded global page whose relevant events
    are covered by their own namespace cursors; an unseen foreign namespace stops it.
    Reset atomically increments epoch, clears index rows/cursors, and retains a global
    projector offset row at zero so rolling pruning cannot discard needed history.
    Purge takes the same gates, changes no event cursor, and increments the namespace
    revision when rows change. Notifications are published only after commit.
    """

    async def initialize_entity_index(self) -> None:
        """Register a durable zero offset before an enabled projector can write or prune."""
        ...

    async def advance_entity_index(
        self,
        namespace: str,
        target_seq: int,
        limit: int,
        apply_batch: EntityIndexApplyBatch,
    ) -> EntityIndexAdvance: ...

    async def purge_entity_index(
        self,
        namespace: str,
        record_ids: Sequence[str],
        apply_purge: EntityIndexApplyPurge,
    ) -> EntityIndexAdvance: ...

    async def reset_entity_index(self) -> EntityIndexAdvance: ...

    async def entity_index_revision(self, namespace: str) -> tuple[int, int]:
        """Return durable ``(reset_epoch, namespace_mutation_revision)`` for peer caches."""
        ...


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

    async def claim_group_page(
        self, namespace: str, group_key: str, *, after_id: str | None, limit: int
    ) -> list[MemoryRecord]:
        """Read a stable ID-ordered page from one physical typed claim group.

        A public reader must exhaust all pages before inferring absence of a
        visible rival. This is a read-only indexed view; no claim interpretation
        or consent decision is delegated to the adapter.
        """
        ...

    async def claim_group_snapshot(self, namespace: str, group_key: str) -> list[MemoryRecord]:
        """Return the complete indexed group from one consistent native read snapshot.

        A public conflict flag cannot be derived from independently committed
        pages: a peer mutation between pages can manufacture a false absence.
        Exclude IDs with current hidden/erased membership or a permanent append
        fence in that same snapshot. The adapter may page internally, but all
        pages and membership checks share one snapshot.
        """
        ...

    async def hidden_or_erased_record_ids(
        self, namespace: str, record_ids: Sequence[str]
    ) -> set[str]:
        """Return only IDs excluded by native current erasure state or append fence.

        This is a negative privacy filter for a complete historical candidate
        population. The adapter validates namespace and chunks bounded queries;
        a global append fence excludes an ID regardless of namespace.
        """
        ...

    async def list_records(
        self, namespace: str, memory_type: str | None = None, group_id: str | None = None
    ) -> list[MemoryRecord]: ...

    async def list_namespaces(self) -> list[str]: ...

    async def find_active_fact(
        self, namespace: str, entity: str, attribute: str
    ) -> MemoryRecord | None: ...

    async def find_active_untyped_fact(
        self, namespace: str, entity: str, attribute: str
    ) -> MemoryRecord | None:
        """Latest open semantic M4 fact with null typed claim-group key.

        Use the same indexed fact-key order as ``find_active_fact``; the typed
        projection field is a rebuildable selector, never caller authority.
        """
        ...

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


@dataclass(frozen=True, slots=True)
class RecordsAdvance:
    """One committed page of the canonical records projection."""

    cursor: int
    applied_count: int
    complete: bool


class RecordsProjectionView(Protocol):
    """Transaction-bound record and ID-only erasure operations for one event fold.

    The view is invalid after ``advance_records`` returns. Its operations must use
    the transaction that read the cursor and events; none may append events or
    acquire a second connection. Group pages use the physical claim-group index
    and a stable record-id cursor. The caller must exhaust pages before deciding
    that a group has no conflicting value.
    """

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...

    async def put_record(self, record: MemoryRecord) -> None:
        """Project a row and its pure, rebuildable claim-group index key."""
        ...

    async def delete_record(self, record_id: str) -> None: ...

    async def group_page(
        self, namespace: str, group_key: str, *, after_id: str | None, limit: int
    ) -> list[MemoryRecord]: ...

    async def erasure_state(self, namespace: str, record_id: str) -> tuple[bool, bool]:
        """Return permanent erased and currently hidden membership, respectively."""
        ...

    async def mark_erased(self, namespace: str, record_id: str) -> None: ...

    async def mark_hidden(self, namespace: str, record_id: str) -> None: ...

    async def release_hidden(self, namespace: str, record_id: str) -> None:
        """Release hidden membership only; permanent erased membership survives."""
        ...


@runtime_checkable
class RecordsProjectionPort(Protocol):
    """Storage-owned ordered record fold, sharing a native transaction and cursor.

    Native lock order preserves the existing reset/prune gate and append guard;
    the records writer guard is acquired after either, before cursor/row reads.
    ``apply_batch`` is retry-pure: it may use only the supplied view and cannot
    append events, mutate process state, or cause an external effect. A failed
    callback rolls back rows, erasure membership and cursor together. Sequence
    numbers may have gaps, but committed events are folded in order. No caller
    may advance the generic ``records`` offset beyond this certified cursor.
    """

    async def initialize_records_projection(self) -> None:
        """Pin a zero cursor before enabled writes or event pruning."""
        ...

    async def advance_records(
        self,
        target_seq: int,
        limit: int,
        apply_batch: Callable[[RecordsProjectionView, Sequence[MemoryEvent]], Awaitable[None]],
    ) -> RecordsAdvance:
        """Fold at most ``limit`` next events in one transaction through ``target_seq``.

        ``complete`` means the target was applied or the durable cursor already
        reached it. A short page alone does not certify a missing target event.
        """
        ...

    async def append_ephemeral_records(
        self,
        event: MemoryEvent,
        apply_batch: Callable[[RecordsProjectionView, Sequence[MemoryEvent]], Awaitable[None]],
    ) -> MemoryEvent:
        """Allocate an ephemeral seq and fold that event in one native writer tx.

        Only an unsequenced event in ephemeral mode is accepted. The event is
        not persisted, so a separate append-then-project sequence cannot recover
        from cancellation. Generic ephemeral append must refuse record-affecting
        kinds and erasure markers; they use this door. Other projectors and
        observers run after this transaction commits.
        """
        ...

    async def reset_records_projection(self) -> None:
        """Atomically clear rows and membership and set both cursor and offset to zero.

        Refuse after a true canonical prune; serialize with append/prune and
        ``advance_records`` under the existing reset gate and records guard.
        Retain the registered checkpoint row at zero so pruning sees it.
        """
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
    async def begin_registry_erasure(self, namespace: str, source_ids: Sequence[str]) -> None:
        """Commit an ID-only erasure barrier after authorization and before deletion."""
        ...

    async def redact_event_payloads(
        self, record_id: str | Sequence[str], node_renames: Mapping[str, str] | None = None
    ) -> list[int]:
        """Scrub every snapshot of the records from the log; seq, kind, fingerprint stay."""
        ...

    async def scrub_marker_payloads(
        self, marker: str, scrub: Callable[[dict[str, Any]], bool], *, namespace: str
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
    EntityIndexProjectionPort,
    RecordsProjectionPort,
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
