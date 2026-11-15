"""Dialect-neutral SQL storage base (D-36, Phase 6): the single write door (D0.1)
shared by the SQLite and Postgres adapters.

Everything here is portable SQLAlchemy Core over the injected client's async
engine; the only dialect-specific pieces are dispatched at runtime from the
bound engine's dialect:

- ``_insert(table)`` picks ``sqlalchemy.dialects.{sqlite,postgresql}.insert`` —
  both expose ``on_conflict_do_update`` with the same shape (D-36 upsert).
- ``_greatest(a, b)`` picks SQLite's scalar ``max(a, b)`` vs Postgres'
  ``greatest(a, b)`` for the advance-only offset high-water mark.

Subclasses supply only ``start()`` (schema bring-up differs: SQLite runs Alembic
for files / ``create_all`` for ``:memory:``; Postgres runs its fresh Alembic baseline) and
``manifest`` (the backend name). The event-log modes (D-45), zstd payload
compression (D-45/D-32), and M7 erasure all live here unchanged.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import orjson
import zstandard
from sqlalchemy import (
    ColumnElement,
    String,
    any_,
    bindparam,
    delete,
    func,
    inspect,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from memspine.config.constants import (
    READ_RECORD_MEMO_MAX,
    RECORD_BATCH_CHUNK,
    STORAGE_HEALTH_DEADLINE_S,
    ZSTD_LEVEL,
)
from memspine.exceptions import RebuildUnavailableError, StorageError
from memspine.services.base import CapabilityManifest, ServiceAdapter
from memspine.services.storage.entity_index_tables import (
    EntityIndexRow,
    SqlEntityIndexStore,
    entity_index_namespaces,
    entity_index_state,
    entity_index_table,
)

# Imported for its table too: ``provider_deployment_identity`` joins ``metadata`` before any
# ``create_all`` (``:memory:``) runs.
from memspine.services.storage.provider_metadata import (
    SqlProjectionBindingStore,
    sql_claim_deployment_id,
)
from memspine.services.storage.records_port import (
    EntityIndexAdvance,
    EntityIndexApplyBatch,
    EntityIndexApplyPurge,
    RecordsAdvance,
    RecordsProjectionView,
    shares_listed_records,
)
from memspine.services.storage.sqlite.schema import (
    memory_events,
    memory_records,
    projector_offsets,
    record_append_erasure_fence,
    record_append_identity,
    record_projection_visibility,
    records_projection_state,
)
from memspine.store.claim_cardinality import group_key, record_group_of
from memspine.store.events import EventKind, EventLogMode, MemoryEvent, canonical_payload
from memspine.store.record_event_ids import affected_record_ids, validate_record_event_shape
from memspine.store.records import MemoryRecord
from memspine.trust.erasure import redact_record, scrub_partition_nodes

if TYPE_CHECKING:
    from memspine.schemas.durable.common import OwnerScope
    from memspine.schemas.durable.lineage import LineageEdge
    from memspine.services.storage.intake_port import IntakeLedger
    from memspine.services.storage.records_port import (
        LeasePort,
        OutboxItem,
        ProjectionBindingPort,
    )

__all__ = ["SqlClient", "SqlStorage"]

# PostgreSQL sequences allocate before commit. Every event insert takes this
# transaction-scoped guard first, so no lower sequence can commit after a reader
# has certified a higher committed head. Rollback may leave a harmless gap.
_EVENT_APPEND_LOCK = "memory_events:append:global"
_ENTITY_RESET_GATE = "entity_index:reset_gate"
_ENTITY_GLOBAL_GATE = "entity_index:global_offset"
# Purge has no caller page budget; one fixed global page keeps the transaction
# bounded while allowing a newly certified pending marker to release retention.
_ENTITY_PURGE_PREFIX_PAGE = 1024
_RECORDS_WRITER_LOCK = "records:projection:writer"
_RECORD_EPHEMERAL_KINDS = frozenset(
    {
        EventKind.WRITE,
        EventKind.FORGET,
        EventKind.DECAY_TRANSITION,
        EventKind.RETRIEVE,
        EventKind.FEEDBACK,
        EventKind.SESSION,
    }
)


def _ephemeral_record_event(event: MemoryEvent) -> bool:
    return event.kind in _RECORD_EPHEMERAL_KINDS or (
        event.kind is EventKind.MARKER
        and event.payload.get("marker") in {"erasure_manifest", "erasure_release"}
    )


def _append_erased_ids(event: MemoryEvent) -> tuple[str, ...]:
    """Canonical ID-only tombstones installed beside an irreversible append."""
    if event.kind is EventKind.FORGET and bool(event.payload.get("hard")):
        record_id = event.payload.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            raise StorageError("hard FORGET requires a nonempty record_id")
        return (record_id,)
    if event.kind is not EventKind.MARKER or event.payload.get("marker") != "erasure_manifest":
        return ()
    manifest = event.payload.get("manifest")
    ids = manifest.get("ids") if isinstance(manifest, Mapping) else None
    raw = ids.get("record_ids") if isinstance(ids, Mapping) else None
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        raise StorageError("erasure manifest has invalid record ids")
    return tuple(dict.fromkeys(raw))


_ENTITY_KINDS = (
    EventKind.WRITE,
    EventKind.FORGET,
    EventKind.MARKER,
    EventKind.DECAY_TRANSITION,
    EventKind.REGISTRY_ERASURE_FENCE,
)


class _SqlEntityIndexView:
    """A projection callback's only SQL view, bound to its writer transaction."""

    def __init__(
        self, storage: SqlStorage, conn: AsyncConnection, namespace: str, index: SqlEntityIndexStore
    ) -> None:
        self._storage = storage
        self._conn = conn
        self._namespace = namespace
        self._index = index

    def _check(self, namespace: str) -> None:
        if namespace != self._namespace:
            raise StorageError("entity-index callback crossed its namespace")

    async def put(self, rows: Sequence[EntityIndexRow]) -> None:
        for row in rows:
            self._check(row.namespace)
        await self._index.put_on(self._conn, rows)

    async def select(
        self,
        namespace: str,
        kind: str,
        *,
        key: str | None = None,
        entity_id: str | None = None,
        ref: str | None = None,
        match: str | None = None,
    ) -> list[EntityIndexRow]:
        self._check(namespace)
        return await self._index.select_on(
            self._conn, namespace, kind, key=key, entity_id=entity_id, ref=ref, match=match
        )

    async def delete(
        self,
        namespace: str,
        kind: str,
        *,
        key: str | None = None,
        entity_id: str | None = None,
        ref: str | None = None,
    ) -> None:
        self._check(namespace)
        await self._index.delete_on(
            self._conn, namespace, kind, key=key, entity_id=entity_id, ref=ref
        )

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        row = (
            await self._conn.execute(
                select(memory_records).where(
                    memory_records.c.record_id == record_id,
                    memory_records.c.namespace == self._namespace,
                )
            )
        ).first()
        return self._storage._row_to_record(row._mapping) if row is not None else None

    async def pending_events(self, limit: int) -> list[MemoryEvent]:
        if limit <= 0:
            return []
        pending = (
            await self._conn.execute(
                select(entity_index_table.c.seq)
                .where(
                    entity_index_table.c.namespace == self._namespace,
                    entity_index_table.c.kind == "pending_positive",
                )
                .order_by(entity_index_table.c.seq, entity_index_table.c.key)
                .limit(limit)
            )
        ).all()
        if not pending:
            return []
        seqs = [int(row.seq) for row in pending]
        rows = (
            await self._conn.execute(
                select(memory_events)
                .where(
                    memory_events.c.seq.in_(seqs),
                    memory_events.c.namespace == self._namespace,
                    memory_events.c.kind == EventKind.MARKER.value,
                )
                .order_by(memory_events.c.seq)
            )
        ).all()
        by_seq = {int(row.seq): self._storage._row_to_event(row._mapping) for row in rows}
        if any(seq < 1 or seq not in by_seq for seq in seqs):
            raise StorageError("pending entity-index canonical event is missing")
        return [by_seq[seq] for seq in seqs]


class _SqlRecordsView:
    """Record reducer operations on the exact cursor/event transaction."""

    def __init__(self, storage: SqlStorage, conn: AsyncConnection) -> None:
        self._storage = storage
        self._conn = conn
        self._active = True
        self.record_presence: dict[str, bool] = {}

    def close(self) -> None:
        self._active = False

    def _check(self) -> None:
        if not self._active:
            raise StorageError("records projection view outlived its transaction")

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        self._check()
        row = (
            await self._conn.execute(
                select(memory_records).where(memory_records.c.record_id == record_id)
            )
        ).first()
        return self._storage._row_to_record(row._mapping) if row is not None else None

    async def put_record(self, record: MemoryRecord) -> None:
        self._check()
        await self._storage._put_record_on(self._conn, record)
        self.record_presence[record.record_id] = True

    async def delete_record(self, record_id: str) -> None:
        self._check()
        await self._storage._delete_record_on(self._conn, record_id)
        self.record_presence[record_id] = False

    async def group_page(
        self, namespace: str, group_key: str, *, after_id: str | None, limit: int
    ) -> list[MemoryRecord]:
        self._check()
        if limit <= 0:
            return []
        stmt = select(memory_records).where(
            memory_records.c.namespace == namespace,
            memory_records.c.claim_group_key == group_key,
        )
        if after_id is not None:
            stmt = stmt.where(memory_records.c.record_id > after_id)
        rows = (
            await self._conn.execute(stmt.order_by(memory_records.c.record_id).limit(limit))
        ).all()
        return [self._storage._row_to_record(row._mapping) for row in rows]

    async def erasure_state(self, namespace: str, record_id: str) -> tuple[bool, bool]:
        self._check()
        row = (
            await self._conn.execute(
                select(
                    record_projection_visibility.c.erased, record_projection_visibility.c.hidden
                ).where(
                    record_projection_visibility.c.namespace == namespace,
                    record_projection_visibility.c.record_id == record_id,
                )
            )
        ).first()
        fence = await self._conn.scalar(
            select(record_append_erasure_fence.c.record_id).where(
                record_append_erasure_fence.c.record_id == record_id
            )
        )
        erased = fence is not None or (bool(row.erased) if row is not None else False)
        hidden = bool(row.hidden) if row is not None else False
        return erased, hidden

    async def mark_erased(self, namespace: str, record_id: str) -> None:
        self._check()
        stmt = self._storage._insert(record_projection_visibility).values(
            namespace=namespace, record_id=record_id, erased=True, hidden=False
        )
        await self._conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["namespace", "record_id"], set_={"erased": True}
            )
        )

    async def mark_hidden(self, namespace: str, record_id: str) -> None:
        self._check()
        stmt = self._storage._insert(record_projection_visibility).values(
            namespace=namespace, record_id=record_id, erased=False, hidden=True
        )
        await self._conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["namespace", "record_id"], set_={"hidden": True}
            )
        )

    async def release_hidden(self, namespace: str, record_id: str) -> None:
        self._check()
        await self._conn.execute(
            record_projection_visibility.update()
            .where(
                record_projection_visibility.c.namespace == namespace,
                record_projection_visibility.c.record_id == record_id,
            )
            .values(hidden=False)
        )


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


class SqlClient(Protocol):
    """The one capability the storage base needs from its client (D-24)."""

    @property
    def engine(self) -> AsyncEngine: ...

    async def health(self, deadline_s: float) -> bool: ...


class SqlStorage(ServiceAdapter):
    """Event log + relational read model over any SQLAlchemy-supported dialect."""

    def __init__(
        self,
        client: SqlClient,
        mode: EventLogMode = EventLogMode.FULL,
        compress: bool = False,
    ) -> None:
        self._client = client
        self._mode = mode
        self._compress = compress
        self._started = False
        # Ephemeral mode (D-45): monotonic seq + projector offsets live purely
        # in memory — persisting either would poison a later full/rolling run
        # on the same database with high-water marks no event backs.
        self._ephemeral_seq = 0
        self._ephemeral_records_lock = asyncio.Lock()
        self._ephemeral_offsets: dict[str, int] = {}
        self._cctx = zstandard.ZstdCompressor(level=ZSTD_LEVEL)
        self._dctx = zstandard.ZstdDecompressor()
        # RM-161: per namespace, record id -> (raw row, the record built from it), for listings
        # made while ``sharing_listed_records`` is on. An entry is reused only for an identical
        # row, so it can never serve stale data; writes and deletes here drop theirs at once.
        self._listed: dict[str, dict[str, tuple[tuple[Any, ...], MemoryRecord]]] = {}
        #: ADR-095 (in-process copies): ids hard-deleted from the read model. A listing that was
        #: already in flight when the delete ran never memoises them, so no erased record stays
        #: in this memory; the in-flight reader still gets the row it read before the erase.
        self._deleted_ids: set[str] = set()

    # ── dialect dispatch ─────────────────────────────────────────────────────

    @property
    def _is_postgres(self) -> bool:
        return self._client.engine.dialect.name == "postgresql"

    def _insert(self, table: Any) -> Any:
        """The upsert-capable INSERT for the bound dialect (D-36). Both the
        sqlite and postgresql constructs expose ``on_conflict_do_update``."""
        return pg_insert(table) if self._is_postgres else sqlite_insert(table)

    def _greatest(self, left: Any, right: Any) -> ColumnElement[Any]:
        """Two-arg maximum: Postgres ``greatest(a, b)`` vs SQLite scalar
        ``max(a, b)`` (SQLite's ``max`` is scalar with 2+ args; Postgres' is an
        aggregate, so the advance-only offset needs ``greatest`` there)."""
        return func.greatest(left, right) if self._is_postgres else func.max(left, right)

    # ── lifecycle (subclass-supplied) ────────────────────────────────────────

    async def start(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def manifest(self) -> CapabilityManifest:  # pragma: no cover - overridden
        raise NotImplementedError

    async def stop(self) -> None:
        self._started = False

    async def health(self) -> bool:
        # RM-113: a real ``SELECT 1`` round trip through the connector.
        return self._started and await self._client.health(STORAGE_HEALTH_DEADLINE_S)

    @property
    def mode(self) -> EventLogMode:
        return self._mode

    @property
    def can_rebuild(self) -> bool:
        """Ephemeral never rebuilds; rolling rebuilds while its window is intact
        (the window check happens at rebuild time, where the log can be read)."""
        return self._mode is not EventLogMode.EPHEMERAL

    # ── event log: the single write door ─────────────────────────────────────

    def _event_insert(self, event: MemoryEvent) -> Any:
        """The ``memory_events`` INSERT of one event (canonical, optionally zstd payload)."""
        payload = canonical_payload(event.payload)  # same encoding the fingerprint hashed
        compressed = self._compress
        if compressed:
            payload = self._cctx.compress(payload)
        return self._insert(memory_events).values(
            event_id=event.event_id,
            kind=event.kind.value,
            namespace=event.namespace,
            ts=_iso(event.ts),
            actor=event.actor,
            schema_version=event.schema_version,
            payload=payload,
            compressed=compressed,
            fingerprint=event.fingerprint,
        )

    async def _guard_event_append(self, conn: AsyncConnection) -> None:
        """Serialize PostgreSQL sequence allocation through transaction completion."""
        if self._is_postgres:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": _EVENT_APPEND_LOCK},
            )

    async def _record_append_fence_on(
        self, conn: AsyncConnection, event: MemoryEvent, *, trusted_replay: bool = False
    ) -> None:
        try:
            validate_record_event_shape(event)
        except ValueError as exc:
            raise StorageError(str(exc)) from exc
        erased = _append_erased_ids(event)
        snapshot = event.payload.get("record") if event.kind is EventKind.WRITE else None
        if event.kind is EventKind.WRITE:
            if not isinstance(snapshot, Mapping):
                raise StorageError("WRITE requires a canonical record snapshot")
            record_id = snapshot.get("record_id")
            if (
                not isinstance(record_id, str)
                or not record_id
                or snapshot.get("namespace") != event.namespace
            ):
                raise StorageError("WRITE record identity or namespace mismatches its event")
        else:
            record_id = None
        ids = list(dict.fromkeys((*affected_record_ids(event), *erased)))
        if not ids:
            return
        await self._records_writer_lock(conn)
        owned = (
            await conn.execute(
                select(
                    record_append_identity.c.record_id, record_append_identity.c.namespace
                ).where(record_append_identity.c.record_id.in_(ids))
            )
        ).all()
        if any(row.namespace != event.namespace for row in owned):
            raise StorageError("record id belongs to another namespace")
        owners = (
            await conn.execute(
                select(memory_records.c.record_id, memory_records.c.namespace).where(
                    memory_records.c.record_id.in_(ids)
                )
            )
        ).all()
        if any(row.namespace != event.namespace for row in owners):
            raise StorageError("record id belongs to another namespace")
        if record_id is not None and not trusted_replay:
            fenced = await conn.scalar(
                select(record_append_erasure_fence.c.record_id).where(
                    record_append_erasure_fence.c.record_id == record_id
                )
            )
            if fenced is not None:
                raise StorageError("record id is permanently erased")
        identity_ids = [*erased]
        if record_id is not None:
            identity_ids.insert(0, record_id)
        for owned_id in dict.fromkeys(identity_ids):
            stmt = self._insert(record_append_identity).values(
                record_id=owned_id, namespace=event.namespace
            )
            await conn.execute(stmt.on_conflict_do_nothing(index_elements=["record_id"]))
        for erased_id in erased:
            stmt = self._insert(record_append_erasure_fence).values(record_id=erased_id)
            await conn.execute(stmt.on_conflict_do_nothing(index_elements=["record_id"]))

    async def append_event(self, event: MemoryEvent) -> MemoryEvent:
        from memspine.store.registry_evidence import registry_control_marker

        if event.kind is EventKind.REGISTRY_ERASURE_FENCE or registry_control_marker(
            event.kind, event.payload
        ):
            raise StorageError("registry control events require their guarded append door")
        if event.seq is not None:
            raise StorageError("event already has a seq — events pass the write door once")

        if self._mode is EventLogMode.EPHEMERAL:
            if _ephemeral_record_event(event):
                raise StorageError(
                    "record-affecting ephemeral events require append_ephemeral_records"
                )
            async with self._ephemeral_records_lock:
                self._ephemeral_seq += 1
                return event.model_copy(update={"seq": self._ephemeral_seq})

        stmt = self._event_insert(event)
        # Both dialects return the generated identity via RETURNING (Postgres
        # natively; SQLite via SQLAlchemy's rowid fetch) in inserted_primary_key.
        async with self._client.engine.begin() as conn:
            await self._entity_lock(conn, None)
            await self._guard_event_append(conn)
            await self._record_append_fence_on(conn, event)
            result = await conn.execute(stmt)
        pk = result.inserted_primary_key
        if pk is None:
            raise StorageError("insert into memory_events returned no primary key")
        return event.model_copy(update={"seq": int(pk[0])})

    async def begin_registry_erasure(self, namespace: str, source_ids: Sequence[str]) -> None:
        """Commit an ID-only erasure barrier before deleting or scrubbing source copies.

        A completed barrier is retained even in rolling mode. A failed later erase is
        safely retryable; its cited source can no longer authorize fresh registry text.
        """
        if self._mode is EventLogMode.EPHEMERAL:
            return  # no durable registry marker or cross-process event log exists
        ids = sorted(set(source_ids))
        if not ids:
            return
        barrier = MemoryEvent(
            kind=EventKind.REGISTRY_ERASURE_FENCE,
            namespace=namespace,
            actor="registry_erasure",
            payload={"source_ids": ids},
        )
        async with self._client.engine.begin() as conn:
            await self._guard_event_append(conn)
            if self._is_postgres:
                # PostgreSQL sequences allocate before commit. Fence polling uses
                # an increasing seq cursor across *all* namespaces, so fence
                # transactions must commit in allocation order globally.
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": "registry:erasure-fence:global"},
                )
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": f"registry:{namespace}"},
                )
            await conn.execute(self._event_insert(barrier))

    async def registry_erasure_fences(self, after_seq: int = 0) -> list[tuple[int, str, list[str]]]:
        if self._mode is EventLogMode.EPHEMERAL:
            return []
        stmt = (
            select(
                memory_events.c.seq,
                memory_events.c.namespace,
                memory_events.c.payload,
                memory_events.c.compressed,
            )
            .where(
                memory_events.c.kind == EventKind.REGISTRY_ERASURE_FENCE.value,
                memory_events.c.seq > after_seq,
            )
            .order_by(memory_events.c.seq)
        )
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        fences: list[tuple[int, str, list[str]]] = []
        for seq, namespace, raw, compressed in rows:
            payload = orjson.loads(self._dctx.decompress(raw) if compressed else raw)
            ids = payload.get("source_ids") if isinstance(payload, dict) else None
            if not isinstance(ids, list) or not all(isinstance(rid, str) and rid for rid in ids):
                raise StorageError("malformed registry erasure fence")
            fences.append((int(seq), str(namespace), ids))
        return fences

    async def append_registry_marker(self, event: MemoryEvent) -> MemoryEvent:
        """Append a name-bearing resolver contribution under a native source fence."""
        from memspine.store.registry_evidence import (
            ENTITY_MARKERS,
            registry_marker_has_text,
            registry_marker_has_unattributed_text,
            valid_source_refs,
        )

        if event.seq is not None or event.kind is not EventKind.MARKER:
            raise StorageError("registry append requires a fresh marker")
        marker = event.payload.get("marker")
        if marker not in ENTITY_MARKERS:
            raise StorageError("registry append requires an entity marker")
        if registry_marker_has_unattributed_text(event.payload):
            raise StorageError("registry marker cannot carry unpinned text")
        if (
            marker in {"entity.create", "entity.alias"}
            and registry_marker_has_text(event.payload)
            and not valid_source_refs(event.payload)
        ):
            raise StorageError("registry append requires pinned raw source citations")
        if self._mode is EventLogMode.EPHEMERAL:
            raise StorageError("durable registry markers require a persisted event log")
        refs = (
            dict(zip(event.payload["source_ids"], event.payload["source_versions"], strict=True))
            if marker in {"entity.create", "entity.alias"}
            and registry_marker_has_text(event.payload)
            else {}
        )
        async with self._client.engine.begin() as conn:
            await self._guard_event_append(conn)
            if self._is_postgres:
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": f"registry:{event.namespace}"},
                )
            # SQLite's first write takes its single-writer lock. In Postgres the
            # advisory lock orders this insert against begin_registry_erasure.
            result = await conn.execute(self._event_insert(event))
            rows = (
                (
                    await conn.execute(
                        select(memory_records)
                        .where(memory_records.c.record_id.in_(sorted(refs)))
                        .with_for_update()
                    )
                ).all()
                if refs
                else []
            )
            if len(rows) != len(refs) or any(
                row._mapping["namespace"] != event.namespace
                or row._mapping["memory_type"] not in {"episodic", "resource"}
                or row._mapping["version"] != refs[row._mapping["record_id"]]
                or row._mapping["status"] not in {"activated", "archived"}
                or row._mapping["quarantined"]
                for row in rows
            ):
                raise StorageError("registry source changed before marker append")
            fences = (
                await conn.execute(
                    select(memory_events.c.payload, memory_events.c.compressed).where(
                        memory_events.c.namespace == event.namespace,
                        memory_events.c.kind == EventKind.REGISTRY_ERASURE_FENCE.value,
                    )
                )
            ).all()
            for raw, compressed in fences:
                payload = orjson.loads(self._dctx.decompress(raw) if compressed else raw)
                fence_ids = payload.get("source_ids") if isinstance(payload, dict) else None
                if (
                    not isinstance(fence_ids, list)
                    or not all(isinstance(rid, str) and rid for rid in fence_ids)
                    or set(refs) & set(fence_ids)
                ):
                    raise StorageError("registry source is being erased")
        pk = result.inserted_primary_key
        if pk is None:
            raise StorageError("insert into memory_events returned no primary key")
        return event.model_copy(update={"seq": int(pk[0])})

    async def _append_replayed_event(self, event: MemoryEvent) -> MemoryEvent:
        """Private fresh-target door for ordered canonical log transfer by ReplayBuilder.

        Replay copies validated historical events before projecting any source rows,
        so a new-contribution source check cannot run here. Generic append remains
        closed to name-bearing markers and erasure fences.
        """
        if event.seq is not None or self._mode is EventLogMode.EPHEMERAL:
            raise StorageError("replay requires a fresh event and persisted target log")
        async with self._client.engine.begin() as conn:
            await self._entity_lock(conn, None)
            await self._guard_event_append(conn)
            await self._record_append_fence_on(conn, event, trusted_replay=True)
            if self._is_postgres and event.kind is EventKind.REGISTRY_ERASURE_FENCE:
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": "registry:erasure-fence:global"},
                )
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": f"registry:{event.namespace}"},
                )
            result = await conn.execute(self._event_insert(event))
        pk = result.inserted_primary_key
        if pk is None:
            raise StorageError("replay insert returned no primary key")
        return event.model_copy(update={"seq": int(pk[0])})

    # ── records port (ADR-081): atomic append-plus-outbox ────────────────────

    async def append_with_outbox(
        self, event: MemoryEvent, items: Sequence[OutboxItem]
    ) -> tuple[MemoryEvent, list[int]]:
        """Append ``event`` and queue ``items`` (``outbox_item`` rows, the table the outbox
        job book reads) in one transaction: a failure on any item rolls the event back.

        Item offsets continue each ``(scope, stage)`` log. On Postgres a transaction-scoped
        advisory lock per ``(scope, stage)`` serialises concurrent appenders (SQLite has one
        writer); a conflicting offset can therefore only fail the whole commit, never
        half-apply it."""
        from memspine.services.storage.outbox_tables import outbox_item

        if event.seq is not None:
            raise StorageError("event already has a seq — events pass the write door once")
        from memspine.store.registry_evidence import registry_control_marker

        if event.kind is EventKind.REGISTRY_ERASURE_FENCE or registry_control_marker(
            event.kind, event.payload
        ):
            raise StorageError("registry control events require their guarded append door")
        if self._mode is EventLogMode.EPHEMERAL:
            raise StorageError("append-plus-outbox needs a persisted event log (not ephemeral)")
        stmt = self._event_insert(event)
        offsets: list[int] = []
        async with self._client.engine.begin() as conn:
            await self._entity_lock(conn, None)
            await self._guard_event_append(conn)
            await self._record_append_fence_on(conn, event)
            result = await conn.execute(stmt)
            pk = result.inserted_primary_key
            if pk is None:
                raise StorageError("insert into memory_events returned no primary key")
            for item in items:
                # Encoded inside the transaction: an unencodable item rolls the event back.
                encoded = json.dumps(dict(item.payload), default=str)
                scope = item.scope
                if self._is_postgres:
                    key = (
                        f"outbox_item:{scope.tenant}:{scope.owner}:{scope.owner_kind}:{item.stage}"
                    )
                    await conn.execute(
                        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key}
                    )
                where = (
                    outbox_item.c.tenant == scope.tenant,
                    outbox_item.c.owner == scope.owner,
                    outbox_item.c.owner_kind == scope.owner_kind,
                    outbox_item.c.stage == item.stage,
                )
                top = await conn.scalar(select(func.max(outbox_item.c.item_offset)).where(*where))
                offset = 0 if top is None else int(top) + 1
                await conn.execute(
                    outbox_item.insert().values(
                        tenant=scope.tenant,
                        owner=scope.owner,
                        owner_kind=scope.owner_kind,
                        stage=item.stage,
                        item_offset=offset,
                        payload=encoded,
                        created_at=_iso(item.at or event.ts),
                    )
                )
                offsets.append(offset)
        return event.model_copy(update={"seq": int(pk[0])}), offsets

    async def outbox_items(
        self, scope: OwnerScope, stage: str, after: int = -1
    ) -> list[tuple[int, dict[str, Any]]]:
        """Queued items of ``(scope, stage)`` after offset ``after`` (ascending)."""
        from memspine.services.storage.outbox_tables import outbox_item

        stmt = (
            select(outbox_item.c.item_offset, outbox_item.c.payload)
            .where(
                outbox_item.c.tenant == scope.tenant,
                outbox_item.c.owner == scope.owner,
                outbox_item.c.owner_kind == scope.owner_kind,
                outbox_item.c.stage == stage,
                outbox_item.c.item_offset > after,
            )
            .order_by(outbox_item.c.item_offset)
        )
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [(int(row[0]), dict(json.loads(row[1]))) for row in rows]

    # ── records port (ADR-081): schema version, leases, lineage ─────────────

    async def schema_revision(self) -> str:
        """The applied Alembic revision, or ``metadata`` for a ``create_all`` schema."""

        def _revision(sync_conn: Any) -> str:
            if not inspect(sync_conn).has_table("alembic_version"):
                return "metadata"
            value = sync_conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            return str(value) if value else "metadata"

        async with self._client.engine.connect() as conn:
            return await conn.run_sync(_revision)

    def leases(self) -> LeasePort:  # pragma: no cover - overridden
        """The lease table of this store (subclass-supplied: it needs a sync connection)."""
        raise NotImplementedError

    # ── records port (RM-112): provider metadata ─────────────────────────────

    async def claim_deployment_id(self, candidate: str) -> str:
        return await sql_claim_deployment_id(self._client.engine, candidate)

    def entity_index_store(self) -> Any:
        """RM-130: the entity name index projection (the ``entity_index`` table)."""
        from memspine.services.storage.entity_index_tables import SqlEntityIndexStore

        store = self.__dict__.get("_entity_index_store")
        if store is None:
            store = self.__dict__["_entity_index_store"] = SqlEntityIndexStore(self._client.engine)
        return store

    async def _entity_lock(
        self, conn: AsyncConnection, namespace: str | None, *, exclusive: bool = False
    ) -> None:
        """Lock order: reset gate, namespace gate, then global-prefix gate."""
        if self._is_postgres:
            await conn.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtext(:key))"
                    if exclusive
                    else "SELECT pg_advisory_xact_lock_shared(hashtext(:key))"
                ),
                {"key": _ENTITY_RESET_GATE},
            )
            if namespace is not None:
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": f"entity_index:namespace:{namespace}"},
                )
        else:
            await conn.exec_driver_sql("BEGIN IMMEDIATE")

    async def _records_writer_lock(self, conn: AsyncConnection) -> None:
        """Called after the reset gate (and append guard for ordered folds)."""
        if self._is_postgres:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": _RECORDS_WRITER_LOCK},
            )

    async def _entity_state(self, conn: AsyncConnection) -> int:
        stmt = self._insert(entity_index_state).values(id=1, epoch=0, pruned_seq=0)
        await conn.execute(stmt.on_conflict_do_nothing(index_elements=["id"]))
        epoch = await conn.scalar(
            select(entity_index_state.c.epoch).where(entity_index_state.c.id == 1)
        )
        assert epoch is not None
        return int(epoch)

    async def _entity_register(self, conn: AsyncConnection) -> int:
        epoch = await self._entity_state(conn)
        existing = await conn.scalar(
            select(projector_offsets.c.last_seq).where(
                projector_offsets.c.projector_name == "entity_index"
            )
        )
        if existing is None:
            pruned = await conn.scalar(
                select(entity_index_state.c.pruned_seq).where(entity_index_state.c.id == 1)
            )
            if int(pruned or 0) > 0:
                raise RebuildUnavailableError(
                    "entity index cannot initialize after event history was pruned (D-45)"
                )
            stmt = self._insert(projector_offsets).values(
                projector_name="entity_index", last_seq=0, updated_at=_iso(datetime.now(UTC))
            )
            await conn.execute(stmt.on_conflict_do_nothing(index_elements=["projector_name"]))
        return epoch

    async def initialize_entity_index(self) -> None:
        """Register an enabled entity projector before writes or rolling retention run."""
        if self._mode is EventLogMode.EPHEMERAL:
            return
        async with self._client.engine.begin() as conn:
            await self._entity_lock(conn, None, exclusive=True)
            await self._entity_register(conn)

    async def assert_full_event_history(self) -> None:
        """Refuse replay when an actual retention delete removed canonical history."""
        if self._mode is EventLogMode.EPHEMERAL:
            raise RebuildUnavailableError("ephemeral event log has no replayable history")
        async with self._client.engine.begin() as conn:
            await self._entity_lock(conn, None)
            pruned = await conn.scalar(
                select(entity_index_state.c.pruned_seq).where(entity_index_state.c.id == 1)
            )
            if int(pruned or 0) > 0:
                raise RebuildUnavailableError(
                    "event history was pruned (D-45); full replay is unavailable"
                )

    async def _entity_namespace(
        self, conn: AsyncConnection, namespace: str
    ) -> tuple[int, int, int]:
        row = (
            await conn.execute(
                select(
                    entity_index_namespaces.c.scan_cursor,
                    entity_index_namespaces.c.cursor,
                    entity_index_namespaces.c.revision,
                ).where(entity_index_namespaces.c.namespace == namespace)
            )
        ).first()
        return (
            (int(row.scan_cursor), int(row.cursor), int(row.revision))
            if row is not None
            else (0, 0, 0)
        )

    async def _entity_global_prefix(self, conn: AsyncConnection, limit: int) -> int:
        if self._is_postgres:
            await conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": _ENTITY_GLOBAL_GATE},
            )
        offset = await conn.scalar(
            select(projector_offsets.c.last_seq).where(
                projector_offsets.c.projector_name == "entity_index"
            )
        )
        prefix = int(offset or 0)
        head = await conn.scalar(select(func.max(memory_events.c.seq)))
        if head is None or int(head) <= prefix:
            return prefix
        relevant = {kind.value for kind in _ENTITY_KINDS}
        rows = (
            await conn.execute(
                select(memory_events.c.seq, memory_events.c.namespace, memory_events.c.kind)
                .where(memory_events.c.seq > prefix, memory_events.c.seq <= int(head))
                .order_by(memory_events.c.seq)
                .limit(max(limit, 1))
            )
        ).all()
        cursors: dict[str, int] = {}
        for row in rows:
            if row.kind in relevant:
                if row.namespace not in cursors:
                    _, cursor, _ = await self._entity_namespace(conn, row.namespace)
                    cursors[row.namespace] = cursor
                if row.seq > cursors[row.namespace]:
                    break
            prefix = int(row.seq)
        if rows and len(rows) < max(limit, 1) and prefix == int(rows[-1].seq):
            prefix = int(head)  # committed sequence gaps have no events to project
        if prefix > int(offset or 0):
            await conn.execute(
                projector_offsets.update()
                .where(projector_offsets.c.projector_name == "entity_index")
                .values(last_seq=prefix, updated_at=_iso(datetime.now(UTC)))
            )
        return prefix

    async def advance_entity_index(
        self,
        namespace: str,
        target_seq: int,
        limit: int,
        apply_batch: EntityIndexApplyBatch,
    ) -> EntityIndexAdvance:
        if limit <= 0 or target_seq < 0:
            raise ValueError("entity-index limit must be positive and target nonnegative")
        async with self._client.engine.begin() as conn:
            await self._entity_lock(conn, namespace)
            epoch = await self._entity_register(conn)
            scan_cursor, cursor, revision = await self._entity_namespace(conn, namespace)
            head = await conn.scalar(select(func.max(memory_events.c.seq)))
            captured = max(scan_cursor, min(target_seq, int(head or 0)))
            rows = (
                await conn.execute(
                    select(memory_events)
                    .where(
                        memory_events.c.namespace == namespace,
                        memory_events.c.kind.in_([kind.value for kind in _ENTITY_KINDS]),
                        memory_events.c.seq > scan_cursor,
                        memory_events.c.seq <= captured,
                    )
                    .order_by(memory_events.c.seq)
                    .limit(limit + 1)
                )
            ).all()
            page_complete = len(rows) <= limit
            batch = [self._row_to_event(row._mapping) for row in rows[:limit]]
            view = _SqlEntityIndexView(self, conn, namespace, self.entity_index_store())
            # The callback retries at most `limit` old pending markers through
            # view.pending_events(), then folds this at-most-limit new page.
            effects = await apply_batch(view, batch)
            new_scan = captured if page_complete else int(batch[-1].seq or scan_cursor)
            earliest_pending = await conn.scalar(
                select(func.min(entity_index_table.c.seq)).where(
                    entity_index_table.c.namespace == namespace,
                    entity_index_table.c.kind == "pending_positive",
                )
            )
            new_cursor = (
                min(new_scan, int(earliest_pending) - 1)
                if earliest_pending is not None
                else new_scan
            )
            if new_cursor < cursor:
                raise StorageError("entity-index pending marker regressed its certified cursor")
            complete = new_scan >= captured and new_cursor >= captured
            if effects.changed:
                revision += 1
            if new_scan != scan_cursor or new_cursor != cursor or effects.changed:
                stmt = self._insert(entity_index_namespaces).values(
                    namespace=namespace,
                    scan_cursor=new_scan,
                    cursor=new_cursor,
                    revision=revision,
                )
                await conn.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["namespace"],
                        set_={
                            "scan_cursor": new_scan,
                            "cursor": new_cursor,
                            "revision": revision,
                        },
                    )
                )
            global_offset = await self._entity_global_prefix(conn, limit + 1)
            receipt = EntityIndexAdvance(
                epoch, new_cursor, revision, global_offset, complete, len(batch), effects
            )
        return receipt

    async def purge_entity_index(
        self,
        namespace: str,
        record_ids: Sequence[str],
        apply_purge: EntityIndexApplyPurge,
    ) -> EntityIndexAdvance:
        async with self._client.engine.begin() as conn:
            await self._entity_lock(conn, namespace)
            epoch = await self._entity_register(conn)
            scan_cursor, cursor, revision = await self._entity_namespace(conn, namespace)
            effects = await apply_purge(
                _SqlEntityIndexView(self, conn, namespace, self.entity_index_store()), record_ids
            )
            earliest_pending = await conn.scalar(
                select(func.min(entity_index_table.c.seq)).where(
                    entity_index_table.c.namespace == namespace,
                    entity_index_table.c.kind == "pending_positive",
                )
            )
            new_cursor = (
                min(scan_cursor, int(earliest_pending) - 1)
                if earliest_pending is not None
                else scan_cursor
            )
            if new_cursor < cursor:
                raise StorageError("entity-index purge regressed its certified cursor")
            if effects.changed:
                revision += 1
            if effects.changed or new_cursor != cursor:
                stmt = self._insert(entity_index_namespaces).values(
                    namespace=namespace,
                    scan_cursor=scan_cursor,
                    cursor=new_cursor,
                    revision=revision,
                )
                await conn.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["namespace"],
                        set_={"cursor": new_cursor, "revision": revision},
                    )
                )
            offset = await self._entity_global_prefix(conn, _ENTITY_PURGE_PREFIX_PAGE)
            receipt = EntityIndexAdvance(
                epoch, new_cursor, revision, offset, new_cursor >= scan_cursor, 0, effects
            )
        return receipt

    async def reset_entity_index(self) -> EntityIndexAdvance:
        if self._mode is EventLogMode.EPHEMERAL:
            raise RebuildUnavailableError("ephemeral event log has no replayable history")
        async with self._client.engine.begin() as conn:
            if self._is_postgres:
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": _ENTITY_RESET_GATE},
                )
                await conn.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": _ENTITY_GLOBAL_GATE},
                )
            else:
                await conn.exec_driver_sql("BEGIN IMMEDIATE")
            epoch = await self._entity_register(conn) + 1
            pruned = await conn.scalar(
                select(entity_index_state.c.pruned_seq).where(entity_index_state.c.id == 1)
            )
            if int(pruned or 0) > 0:
                raise RebuildUnavailableError(
                    "entity index cannot reset after event history was pruned (D-45)"
                )
            await conn.execute(delete(entity_index_table))
            await conn.execute(delete(entity_index_namespaces))
            await conn.execute(
                entity_index_state.update().where(entity_index_state.c.id == 1).values(epoch=epoch)
            )
            await conn.execute(
                projector_offsets.update()
                .where(projector_offsets.c.projector_name == "entity_index")
                .values(last_seq=0, updated_at=_iso(datetime.now(UTC)))
            )
            receipt = EntityIndexAdvance(epoch, 0, 0, 0, True, 0)
        return receipt

    async def entity_index_revision(self, namespace: str) -> tuple[int, int]:
        async with self._client.engine.begin() as conn:
            await self._entity_lock(conn, namespace)
            epoch = await self._entity_register(conn)
            _, _, revision = await self._entity_namespace(conn, namespace)
        return epoch, revision

    async def _records_register(self, conn: AsyncConnection) -> int:
        stmt = self._insert(records_projection_state).values(id=1, cursor=0)
        await conn.execute(stmt.on_conflict_do_nothing(index_elements=["id"]))
        cursor = await conn.scalar(
            select(records_projection_state.c.cursor).where(records_projection_state.c.id == 1)
        )
        assert cursor is not None
        offset = await conn.scalar(
            select(projector_offsets.c.last_seq).where(
                projector_offsets.c.projector_name == "records"
            )
        )
        if offset is None:
            pruned = await conn.scalar(
                select(entity_index_state.c.pruned_seq).where(entity_index_state.c.id == 1)
            )
            if int(pruned or 0) > 0 or int(cursor) > 0:
                raise RebuildUnavailableError(
                    "records projection cannot initialize after event history was pruned (D-45)"
                )
            offset_stmt = self._insert(projector_offsets).values(
                projector_name="records", last_seq=0, updated_at=_iso(datetime.now(UTC))
            )
            await conn.execute(
                offset_stmt.on_conflict_do_nothing(index_elements=["projector_name"])
            )
        elif int(offset) != int(cursor):
            raise StorageError("records checkpoint differs from its certified cursor")
        return int(cursor)

    async def initialize_records_projection(self) -> None:
        if self._mode is EventLogMode.EPHEMERAL:
            return
        async with self._client.engine.begin() as conn:
            await self._entity_lock(conn, None, exclusive=True)
            await self._records_writer_lock(conn)
            await self._records_register(conn)

    def _records_committed(self, view: _SqlRecordsView) -> None:
        for record_id, present in view.record_presence.items():
            self._forget_listed(record_id)
            if present:
                self._deleted_ids.discard(record_id)
            else:
                self._deleted_ids.add(record_id)

    async def advance_records(
        self,
        target_seq: int,
        limit: int,
        apply_batch: Callable[[RecordsProjectionView, Sequence[MemoryEvent]], Awaitable[None]],
    ) -> RecordsAdvance:
        if self._mode is EventLogMode.EPHEMERAL:
            raise StorageError("ephemeral records use append_ephemeral_records")
        if target_seq < 0 or limit <= 0:
            raise ValueError("records projection target must be nonnegative and limit positive")
        async with self._client.engine.begin() as conn:
            await self._entity_lock(conn, None)
            await self._records_writer_lock(conn)
            cursor = await self._records_register(conn)
            rows = (
                await conn.execute(
                    select(memory_events)
                    .where(memory_events.c.seq > cursor, memory_events.c.seq <= target_seq)
                    .order_by(memory_events.c.seq)
                    .limit(limit + 1)
                )
            ).all()
            batch = [self._row_to_event(row._mapping) for row in rows[:limit]]
            view = _SqlRecordsView(self, conn)
            try:
                await apply_batch(view, batch)
            finally:
                view.close()
            next_cursor = int(batch[-1].seq or cursor) if batch else cursor
            if next_cursor > cursor:
                await conn.execute(
                    records_projection_state.update()
                    .where(records_projection_state.c.id == 1)
                    .values(cursor=next_cursor)
                )
                await conn.execute(
                    projector_offsets.update()
                    .where(projector_offsets.c.projector_name == "records")
                    .values(last_seq=next_cursor, updated_at=_iso(datetime.now(UTC)))
                )
            receipt = RecordsAdvance(next_cursor, len(batch), next_cursor >= target_seq)
        self._records_committed(view)
        return receipt

    async def append_ephemeral_records(
        self,
        event: MemoryEvent,
        apply_batch: Callable[[RecordsProjectionView, Sequence[MemoryEvent]], Awaitable[None]],
    ) -> MemoryEvent:
        if self._mode is not EventLogMode.EPHEMERAL or event.seq is not None:
            raise StorageError("ephemeral records door requires a fresh event in ephemeral mode")
        async with self._ephemeral_records_lock:
            projected = event.model_copy(update={"seq": self._ephemeral_seq + 1})
            async with self._client.engine.begin() as conn:
                await self._entity_lock(conn, None)
                await self._guard_event_append(conn)
                await self._record_append_fence_on(conn, projected)
                await self._records_writer_lock(conn)
                view = _SqlRecordsView(self, conn)
                try:
                    await apply_batch(view, [projected])
                finally:
                    view.close()
            self._ephemeral_seq += 1
            self._records_committed(view)
            return projected

    async def reset_records_projection(self) -> None:
        if self._mode is EventLogMode.EPHEMERAL:
            raise RebuildUnavailableError("ephemeral event log has no replayable history")
        async with self._client.engine.begin() as conn:
            await self._entity_lock(conn, None, exclusive=True)
            await self._records_writer_lock(conn)
            pruned = await conn.scalar(
                select(entity_index_state.c.pruned_seq).where(entity_index_state.c.id == 1)
            )
            if int(pruned or 0) > 0:
                raise RebuildUnavailableError(
                    "records projection cannot reset after event history was pruned (D-45)"
                )
            await self._records_register(conn)
            await conn.execute(delete(memory_records))
            await conn.execute(delete(record_projection_visibility))
            await conn.execute(
                records_projection_state.update()
                .where(records_projection_state.c.id == 1)
                .values(cursor=0)
            )
            await conn.execute(
                projector_offsets.update()
                .where(projector_offsets.c.projector_name == "records")
                .values(last_seq=0, updated_at=_iso(datetime.now(UTC)))
            )
        self._listed.clear()
        self._deleted_ids.clear()

    def intake_ledger(self) -> IntakeLedger:
        """RM-137: the intake ledger, message identity index and derivation discovery
        (:class:`~memspine.services.storage.intake_port.IntakeLedger`) of this database."""
        from memspine.services.storage.intake_sql import SqlIntakeLedger

        ledger: IntakeLedger | None = self.__dict__.get("_intake_ledger")
        if ledger is None:
            ledger = self.__dict__["_intake_ledger"] = SqlIntakeLedger(self._client.engine)
        return ledger

    def projection_bindings(self) -> ProjectionBindingPort:
        return SqlProjectionBindingStore(self._client.engine)

    async def add_lineage(self, edges: Sequence[LineageEdge], namespace: str | None = None) -> int:
        from memspine.store.lineage import add_edges

        return await add_edges(self._client.engine, edges, namespace)

    async def lineage_parents(
        self, child_ids: Sequence[str], roles: Sequence[str] | None = None
    ) -> dict[str, list[LineageEdge]]:
        from memspine.store.lineage import parents_of

        return await parents_of(self._client.engine, child_ids, roles)

    async def lineage_children(
        self, parent_ids: Sequence[str], roles: Sequence[str] | None = None
    ) -> dict[str, list[LineageEdge]]:
        from memspine.store.lineage import children_of

        return await children_of(self._client.engine, parent_ids, roles)

    async def delete_lineage(self, edges: Sequence[LineageEdge]) -> int:
        from memspine.store.lineage import delete_edges

        return await delete_edges(self._client.engine, edges)

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]:
        if self._mode is EventLogMode.EPHEMERAL:
            return []
        stmt = (
            select(memory_events)
            .where(memory_events.c.seq > after_seq)
            .order_by(memory_events.c.seq)
            .limit(limit)
        )
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [self._row_to_event(row._mapping) for row in rows]

    async def read_scoped_events(
        self, namespace: str, kinds: Sequence[EventKind], *, after_seq: int, limit: int
    ) -> tuple[list[MemoryEvent], int]:
        """Bounded namespace/kind catch-up with the global head captured first."""
        if self._mode is EventLogMode.EPHEMERAL:
            return [], after_seq
        async with self._client.engine.connect() as conn:
            latest = await conn.scalar(select(func.max(memory_events.c.seq)))
            head = max(after_seq, int(latest or 0))
            if not kinds or limit <= 0 or head == after_seq:
                return [], head
            stmt = (
                select(memory_events)
                .where(
                    memory_events.c.namespace == namespace,
                    memory_events.c.kind.in_([kind.value for kind in kinds]),
                    memory_events.c.seq > after_seq,
                    memory_events.c.seq <= head,
                )
                .order_by(memory_events.c.seq)
                .limit(limit)
            )
            rows = (await conn.execute(stmt)).all()
        return [self._row_to_event(row._mapping) for row in rows], head

    def _row_to_event(self, row: Any) -> MemoryEvent:
        raw: bytes = row["payload"]
        if row["compressed"]:
            raw = self._dctx.decompress(raw)
        return MemoryEvent(
            event_id=row["event_id"],
            seq=row["seq"],
            kind=EventKind(row["kind"]),
            namespace=row["namespace"],
            ts=_parse_ts(row["ts"]),
            actor=row["actor"],
            schema_version=row["schema_version"],
            payload=orjson.loads(raw),
            fingerprint=row["fingerprint"],
        )

    # ── projector offsets ────────────────────────────────────────────────────

    async def get_offset(self, projector_name: str) -> int:
        if self._mode is EventLogMode.EPHEMERAL:
            return self._ephemeral_offsets.get(projector_name, 0)
        stmt = select(projector_offsets.c.last_seq).where(
            projector_offsets.c.projector_name == projector_name
        )
        async with self._client.engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        return int(row[0]) if row is not None else 0

    async def set_offset(self, projector_name: str, seq: int) -> None:
        """Advance-only, atomic upsert: concurrent checkpoints can neither race
        the insert nor move a high-water mark backwards."""
        if projector_name in {"entity_index", "records"}:
            raise StorageError(f"{projector_name} owns its ordered checkpoint")
        if self._mode is EventLogMode.EPHEMERAL:
            current = self._ephemeral_offsets.get(projector_name, 0)
            self._ephemeral_offsets[projector_name] = max(current, seq)
            return
        now = _iso(datetime.now(UTC))
        stmt = self._insert(projector_offsets).values(
            projector_name=projector_name, last_seq=seq, updated_at=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["projector_name"],
            set_={
                "last_seq": self._greatest(stmt.excluded.last_seq, projector_offsets.c.last_seq),
                "updated_at": now,
            },
        )
        async with self._client.engine.begin() as conn:
            await conn.execute(stmt)

    async def reset_offset(self, projector_name: str) -> None:
        """Rebuild support: the one sanctioned way to move a high-water mark back."""
        if projector_name == "entity_index":
            await self.reset_entity_index()
            return
        if projector_name == "records":
            await self.reset_records_projection()
            return
        if self._mode is EventLogMode.EPHEMERAL:
            self._ephemeral_offsets.pop(projector_name, None)
            return
        async with self._client.engine.begin() as conn:
            await conn.execute(
                delete(projector_offsets).where(
                    projector_offsets.c.projector_name == projector_name
                )
            )

    # ── retention (D-45) ─────────────────────────────────────────────────────

    async def prune_events(self, older_than: datetime) -> int:
        """Delete events all projectors have applied and older than the cutoff.

        Only meaningful in ``rolling`` mode; a no-op in ``full`` (audit log is
        sacred) and ``ephemeral`` (nothing persisted). Never deletes past the
        slowest projector's high-water mark, so catch-up can always finish.
        """
        if self._mode is not EventLogMode.ROLLING:
            return 0
        async with self._client.engine.begin() as conn:
            # A reset moves the entity-index offset back to zero.  Retention must
            # observe that reset under the same gate before it chooses a cutoff.
            await self._entity_lock(conn, None, exclusive=True)
            # Sequence allocation and retention's deletion set must be ordered;
            # the durable watermark records rows actually removed, not gaps from
            # PostgreSQL transactions that rolled back after allocating a seq.
            await self._guard_event_append(conn)
            min_applied = (
                await conn.execute(select(func.min(projector_offsets.c.last_seq)))
            ).scalar()
            if min_applied is None:
                return 0  # no projector has checkpointed yet — keep everything
            eligible = (
                memory_events.c.seq <= int(min_applied),
                memory_events.c.ts < _iso(older_than),
                memory_events.c.kind != EventKind.REGISTRY_ERASURE_FENCE.value,
            )
            highest = await conn.scalar(select(func.max(memory_events.c.seq)).where(*eligible))
            result = await conn.execute(delete(memory_events).where(*eligible))
            if result.rowcount and highest is not None:
                await self._entity_state(conn)
                await conn.execute(
                    entity_index_state.update()
                    .where(entity_index_state.c.id == 1)
                    .values(
                        pruned_seq=self._greatest(entity_index_state.c.pruned_seq, int(highest))
                    )
                )
        return int(result.rowcount or 0)

    # ── relational read model ────────────────────────────────────────────────

    async def _put_record_on(self, conn: AsyncConnection, record: MemoryRecord) -> None:
        group = record_group_of(record)
        values = {
            "namespace": record.namespace,
            "claim_group_key": group_key(group) if group is not None else None,
            "memory_type": record.memory_type,
            "content": record.content,
            "content_fingerprint": record.content_fingerprint,
            "entity": record.entity,
            "attribute": record.attribute,
            "valid_from": _iso(record.valid_from),
            "valid_to": _iso(record.valid_to) if record.valid_to else None,
            "recorded_at": _iso(record.recorded_at),
            "superseded_at": _iso(record.superseded_at) if record.superseded_at else None,
            "source": orjson.dumps(record.source.model_dump()),
            "status": record.status.value,
            "version": record.version,
            "history": orjson.dumps([v.model_dump(mode="json") for v in record.history]),
            "evolve_to": record.evolve_to,
            "pii_tier": record.pii_tier.value,
            "consent_tags": orjson.dumps(record.consent_tags),
            "scoring": orjson.dumps(record.scoring.model_dump(mode="json")),
            "trust": record.trust,
            "quarantined": record.quarantined,
            "instruction_flag": record.instruction_flag,
            "simhash": record.simhash,
            "minhash_sig": record.minhash_sig,
            "tier": record.tier,
            "content_zstd": record.content_zstd,
            "corroborations": record.corroborations,
            "skill_stage": record.skill_stage,
            "reflection_depth": record.reflection_depth,
            "group_id": record.group_id,
            "tags": orjson.dumps(record.tags),
            "invalid_at": _iso(record.invalid_at) if record.invalid_at else None,
            "session_key": record.source.message_id,  # I6
            "source_role": record.source.role,  # I6
            "layer": record.layer,  # F-02a envelope
            "type_tag": record.type_tag,
            "provenance_class": record.provenance_class,
            "spk": record.axes.speaker,
            "addr": record.axes.addressee,
            "ask": record.axes.asker,
            "sub": orjson.dumps(record.axes.subject),
            "fingerprint": orjson.dumps(record.fingerprint.model_dump(mode="json")),
            "schema_version": record.schema_version,
            "source_at": _iso(record.source_at) if record.source_at else None,  # F-02b
            "source_precision": record.source_precision,
            "event_precision": record.event_precision,
            "event_approximate": 1 if record.event_approximate else None,
            "event_basis": record.event_basis,
            "source_tz": record.source_tz,  # W2-02
            "participants": orjson.dumps(record.axes.participants)
            if record.axes.participants
            else None,
        }
        stmt = self._insert(memory_records).values(record_id=record.record_id, **values)
        stmt = stmt.on_conflict_do_update(index_elements=["record_id"], set_=values)
        await conn.execute(stmt)

    async def upsert_record(self, record: MemoryRecord) -> None:
        self._forget_listed(record.record_id, record.namespace)
        async with self._client.engine.begin() as conn:
            await self._entity_lock(conn, None)
            await self._records_writer_lock(conn)
            fenced = await conn.scalar(
                select(record_append_erasure_fence.c.record_id).where(
                    record_append_erasure_fence.c.record_id == record.record_id
                )
            )
            if fenced is not None:
                raise StorageError("record id is permanently erased")
            owner = await conn.scalar(
                select(memory_records.c.namespace).where(
                    memory_records.c.record_id == record.record_id
                )
            )
            if owner is not None and owner != record.namespace:
                raise StorageError("record id belongs to another namespace")
            append_owner = await conn.scalar(
                select(record_append_identity.c.namespace).where(
                    record_append_identity.c.record_id == record.record_id
                )
            )
            if append_owner is not None and append_owner != record.namespace:
                raise StorageError("record id belongs to another namespace")
            await self._put_record_on(conn, record)

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        stmt = select(memory_records).where(memory_records.c.record_id == record_id)
        async with self._client.engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        return self._row_to_record(row._mapping) if row is not None else None

    async def claim_group_page(
        self, namespace: str, group_key: str, *, after_id: str | None, limit: int
    ) -> list[MemoryRecord]:
        """Peer read of one physical claim-group page; callers apply policy."""
        if limit <= 0:
            return []
        stmt = select(memory_records).where(
            memory_records.c.namespace == namespace,
            memory_records.c.claim_group_key == group_key,
        )
        if after_id is not None:
            stmt = stmt.where(memory_records.c.record_id > after_id)
        async with self._client.engine.connect() as conn:
            rows = (
                await conn.execute(stmt.order_by(memory_records.c.record_id).limit(limit))
            ).all()
        return [self._row_to_record(row._mapping) for row in rows]

    async def claim_group_snapshot(self, namespace: str, group_key: str) -> list[MemoryRecord]:
        """Read all currently visible group peers from one statement snapshot."""
        hidden = (
            select(record_projection_visibility.c.record_id)
            .where(
                record_projection_visibility.c.namespace == namespace,
                record_projection_visibility.c.record_id == memory_records.c.record_id,
                or_(
                    record_projection_visibility.c.erased.is_(True),
                    record_projection_visibility.c.hidden.is_(True),
                ),
            )
            .exists()
        )
        fenced = (
            select(record_append_erasure_fence.c.record_id)
            .where(record_append_erasure_fence.c.record_id == memory_records.c.record_id)
            .exists()
        )
        stmt = (
            select(memory_records)
            .where(
                memory_records.c.namespace == namespace,
                memory_records.c.claim_group_key == group_key,
                ~hidden,
                ~fenced,
            )
            .order_by(memory_records.c.record_id)
        )
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [self._row_to_record(row._mapping) for row in rows]

    async def hidden_or_erased_record_ids(
        self, namespace: str, record_ids: Sequence[str]
    ) -> set[str]:
        """Read current negative visibility for historical candidates in one snapshot."""
        if not namespace:
            raise StorageError("record visibility requires a namespace")
        if any(not isinstance(record_id, str) or not record_id for record_id in record_ids):
            raise StorageError("record visibility requires nonempty record IDs")
        ids = list(dict.fromkeys(record_ids))
        if not ids:
            return set()
        chunks = [ids[i : i + RECORD_BATCH_CHUNK] for i in range(0, len(ids), RECORD_BATCH_CHUNK)]
        excluded: set[str] = set()
        async with self._client.engine.connect() as conn:
            if self._is_postgres:
                await conn.execution_options(isolation_level="REPEATABLE READ")
            async with conn.begin():
                for chunk in chunks:
                    hidden = await conn.execute(
                        select(record_projection_visibility.c.record_id).where(
                            record_projection_visibility.c.namespace == namespace,
                            record_projection_visibility.c.record_id.in_(chunk),
                            or_(
                                record_projection_visibility.c.erased.is_(True),
                                record_projection_visibility.c.hidden.is_(True),
                            ),
                        )
                    )
                    fenced = await conn.execute(
                        select(record_append_erasure_fence.c.record_id).where(
                            record_append_erasure_fence.c.record_id.in_(chunk)
                        )
                    )
                    excluded.update(str(row.record_id) for row in hidden)
                    excluded.update(str(row.record_id) for row in fenced)
        return excluded

    async def get_records(self, record_ids: Sequence[str]) -> dict[str, MemoryRecord]:
        """The records among ``record_ids`` that exist, keyed by id (missing ids are
        absent); duplicates are read once. Equal to one :meth:`get_record` per id, in
        one query per chunk: Postgres binds the ids as one array (``= ANY``), SQLite
        as ``IN`` lists of at most :data:`RECORD_BATCH_CHUNK` ids (its bind limit)."""
        ids = list(dict.fromkeys(str(r) for r in record_ids if r))
        if not ids:
            return {}
        if self._is_postgres:
            chunks = [ids]
        else:
            chunks = [
                ids[i : i + RECORD_BATCH_CHUNK] for i in range(0, len(ids), RECORD_BATCH_CHUNK)
            ]
        out: dict[str, MemoryRecord] = {}
        async with self._client.engine.connect() as conn:
            for chunk in chunks:
                cond = (
                    memory_records.c.record_id
                    == any_(bindparam("record_ids", chunk, type_=ARRAY(String)))
                    if self._is_postgres
                    else memory_records.c.record_id.in_(chunk)
                )
                for row in (await conn.execute(select(memory_records).where(cond))).all():
                    record = self._row_to_record(row._mapping)
                    out[record.record_id] = record
        return out

    async def list_namespaces(self) -> list[str]:
        """Every namespace with at least one materialized record — the sweep
        surface for the lifecycle pipelines (M2/M3/M6)."""
        stmt = select(memory_records.c.namespace).distinct().order_by(memory_records.c.namespace)
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [str(row[0]) for row in rows]

    async def list_records(
        self,
        namespace: str,
        memory_type: str | None = None,
        group_id: str | None = None,
    ) -> list[MemoryRecord]:
        stmt = select(memory_records).where(memory_records.c.namespace == namespace)
        if memory_type is not None:
            stmt = stmt.where(memory_records.c.memory_type == memory_type)
        if group_id is not None:  # D2 sub-scoping facet
            stmt = stmt.where(memory_records.c.group_id == group_id)
        deadline = asyncio.get_running_loop().time() + float(
            getattr(self._client, "busy_timeout_s", 5.0)
        )
        delay = 0.005
        while True:
            try:
                async with self._client.engine.connect() as conn:
                    rows = (await conn.execute(stmt)).all()
                break
            except OperationalError as exc:
                # SQLite shared-cache :memory: databases report SQLITE_LOCKED
                # ("database table is locked"), which PRAGMA busy_timeout does
                # not retry. A concurrent grant/write is short-lived; retry the
                # read until the configured client deadline so callers observe
                # either the state before or after the transaction, never an
                # infrastructure exception or a torn partial result.
                if (
                    self._is_postgres
                    or "locked" not in str(exc).lower()
                    or asyncio.get_running_loop().time() >= deadline
                ):
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.05)
        if shares_listed_records():
            return self._shared_records(namespace, rows, whole=memory_type is None and not group_id)
        return [self._row_to_record(row._mapping) for row in rows]

    def _shared_records(
        self, namespace: str, rows: Sequence[Any], *, whole: bool
    ) -> list[MemoryRecord]:
        """RM-161: the records of ``rows``, reusing the record built for an identical row by an
        earlier shared listing (no validation). A whole-namespace listing replaces the
        namespace's entries, so rows deleted elsewhere do not linger in memory."""
        memo = self._listed.pop(namespace, {})
        kept: dict[str, tuple[tuple[Any, ...], MemoryRecord]] = {} if whole else memo
        out: list[MemoryRecord] = []
        for row in rows:
            values = tuple(row)
            mapping = row._mapping
            record_id = mapping["record_id"]
            hit = memo.get(record_id)
            if hit is not None and hit[0] == values:
                record = hit[1]
            else:
                record = self._row_to_record(mapping)
            if record_id not in self._deleted_ids:
                kept[record_id] = (values, record)
            out.append(record)
        self._listed[namespace] = kept  # most recently listed last
        total = sum(len(entries) for entries in self._listed.values())
        while total > READ_RECORD_MEMO_MAX and self._listed:
            oldest = next(iter(self._listed))
            total -= len(self._listed.pop(oldest))
        return out

    def _forget_listed(self, record_id: str, namespace: str | None = None) -> None:
        """RM-161: drop the shared listing entry of one record (it is being written or
        deleted), in ``namespace`` or, unknown, in every namespace."""
        if namespace is not None:
            entries = self._listed.get(namespace)
            if entries is not None:
                entries.pop(record_id, None)
            return
        for entries in self._listed.values():
            entries.pop(record_id, None)

    async def list_session_records(
        self, namespace: str, session_key: str, roles: Sequence[str] | None = None
    ) -> list[MemoryRecord]:
        """I6: one conversation of one namespace, through ``ix_memory_records_ns_session``
        (optionally only some speaker roles)."""
        stmt = select(memory_records).where(
            memory_records.c.namespace == namespace,
            memory_records.c.session_key == session_key,
        )
        if roles is not None:
            stmt = stmt.where(memory_records.c.source_role.in_(list(roles)))
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [self._row_to_record(row._mapping) for row in rows]

    async def quarantined_ids(self, record_ids: Sequence[str]) -> set[str]:
        """The ids among ``record_ids`` whose record exists and is quarantined.

        One query in place of a ``get_record`` per id when only the quarantine
        flag is needed (the firewall's neighbour check)."""
        if not record_ids:
            return set()
        stmt = select(memory_records.c.record_id, memory_records.c.quarantined).where(
            memory_records.c.record_id.in_(list(dict.fromkeys(record_ids)))
        )
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return {str(row[0]) for row in rows if row[1]}

    async def recent_contents(self, namespace: str, limit: int) -> list[str]:
        """Contents of the ``limit`` most recently recorded non-quarantined
        records of a namespace, oldest first.

        Equal to filtering :meth:`list_records` on ``not quarantined``, stable-
        sorting by ``recorded_at`` and keeping the last ``limit``, without
        building a :class:`MemoryRecord` per row. The rows come from the same
        unordered namespace select as :meth:`list_records`, so ties on
        ``recorded_at`` keep the same relative order."""
        stmt = select(
            memory_records.c.content,
            memory_records.c.recorded_at,
            memory_records.c.quarantined,
        ).where(memory_records.c.namespace == namespace)
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        live = [(_parse_ts(row[1]), str(row[0])) for row in rows if not row[2]]
        live.sort(key=lambda pair: pair[0])
        return [content for _, content in live[-limit:]] if limit > 0 else []

    async def layer_counts(self, namespace: str) -> dict[str, int]:
        """Count live current-format records by their persisted layer."""
        statement = (
            select(memory_records.c.layer, func.count())
            .where(
                memory_records.c.namespace == namespace,
                memory_records.c.status.in_(("activated", "resolving")),
                memory_records.c.quarantined.is_(False),
            )
            .group_by(memory_records.c.layer)
        )
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(statement)).all()
        return {str(row[0]): int(row[1]) for row in rows}

    async def count_quarantined(self, namespace: str) -> int:
        """How many rows of ``namespace`` carry the quarantine flag (#63: the
        firewall oversamples its neighbour query by this much)."""
        stmt = (
            select(func.count())
            .select_from(memory_records)
            .where(
                memory_records.c.namespace == namespace,
                memory_records.c.quarantined.is_(True),
            )
        )
        async with self._client.engine.connect() as conn:
            return int((await conn.execute(stmt)).scalar_one())

    async def list_quarantined(self, namespace: str) -> list[MemoryRecord]:
        """:meth:`list_records` narrowed to rows whose quarantine flag is set,
        in the same relative order (the corroboration scan only visits those)."""
        stmt = select(memory_records).where(
            memory_records.c.namespace == namespace,
            memory_records.c.quarantined.is_(True),
        )
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [self._row_to_record(row._mapping) for row in rows]

    async def find_active_fact(
        self, namespace: str, entity: str, attribute: str
    ) -> MemoryRecord | None:
        """The currently-valid record for one (entity, attribute) key (M4):
        open validity (valid_to IS NULL), not deleted, NOT quarantined (E1: a
        quarantined claim must never act as the incumbent fact the conflict
        ladder defends), latest recorded_at. SEMANTIC records only (ADR-016):
        other types reuse the key columns for non-fact identities — a skill's
        name, a prospective watch's watched target — and none of those may
        become the incumbent the M4 ladder supersedes (a semantic write would
        otherwise archive the watch instead of firing it)."""
        stmt = (
            select(memory_records)
            .where(
                memory_records.c.namespace == namespace,
                memory_records.c.memory_type == "semantic",
                memory_records.c.entity == entity,
                memory_records.c.attribute == attribute,
                memory_records.c.valid_to.is_(None),
                memory_records.c.status != "deleted",
                memory_records.c.quarantined.is_(False),
            )
            .order_by(memory_records.c.recorded_at.desc())
            .limit(1)
        )
        async with self._client.engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        return self._row_to_record(row._mapping) if row is not None else None

    async def find_active_untyped_fact(
        self, namespace: str, entity: str, attribute: str
    ) -> MemoryRecord | None:
        """Latest eligible M4 incumbent without a typed claim-group key."""
        stmt = (
            select(memory_records)
            .where(
                memory_records.c.namespace == namespace,
                memory_records.c.memory_type == "semantic",
                memory_records.c.entity == entity,
                memory_records.c.attribute == attribute,
                memory_records.c.valid_to.is_(None),
                memory_records.c.status != "deleted",
                memory_records.c.quarantined.is_(False),
                memory_records.c.claim_group_key.is_(None),
            )
            .order_by(memory_records.c.recorded_at.desc())
            .limit(1)
        )
        async with self._client.engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        return self._row_to_record(row._mapping) if row is not None else None

    async def delete_all_records(self) -> None:
        """Projector rebuild support: drop the read model, keep the log."""
        self._listed.clear()
        self._deleted_ids.clear()  # the rebuild re-derives the read model from the redacted log
        async with self._client.engine.begin() as conn:
            await self._entity_lock(conn, None)
            await self._records_writer_lock(conn)
            await conn.execute(delete(memory_records))

    async def _delete_record_on(self, conn: AsyncConnection, record_id: str) -> None:
        await conn.execute(delete(memory_records).where(memory_records.c.record_id == record_id))

    async def delete_record(self, record_id: str) -> None:
        """M7 hard-delete: remove one row from the read model (projector-driven
        by a FORGET(hard) event; the log side is ``redact_event_payloads``)."""
        self._forget_listed(record_id)
        self._deleted_ids.add(record_id)
        async with self._client.engine.begin() as conn:
            await self._entity_lock(conn, None)
            await self._records_writer_lock(conn)
            await self._delete_record_on(conn, record_id)

    async def list_children(self, namespace: str, parent_ids: Sequence[str]) -> list[MemoryRecord]:
        """#43: records of ``namespace`` whose ``source.parents`` name any of
        ``parent_ids`` (derived records: mined facts, cues, reflections, edges).

        Scans the namespace's ``source`` column only, and loads full rows for
        the matches (an erasure-time call, not a hot path)."""
        wanted = set(parent_ids)
        if not wanted:
            return []
        stmt = select(memory_records.c.record_id, memory_records.c.source).where(
            memory_records.c.namespace == namespace
        )
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        hits: list[str] = []
        for record_id, raw in rows:
            parents = orjson.loads(raw).get("parents") or []
            if wanted & set(map(str, parents)):
                hits.append(str(record_id))
        records = [await self.get_record(record_id) for record_id in hits]
        return [record for record in records if record is not None]

    async def redact_event_payloads(
        self, record_id: str | Sequence[str], node_renames: Mapping[str, str] | None = None
    ) -> list[int]:
        """M7 erasure: the ONE sanctioned mutation of the log.

        Rewrites every event payload carrying a snapshot of ``record_id`` (one
        id or several, in one pass over the log) so its identifying fields
        (content, fingerprint, fact key, tags, history; see
        :mod:`memspine.trust.erasure`) are emptied and a ``redacted`` marker
        is set — the event's seq, kind, and fingerprint (of the ORIGINAL
        payload) stay, so ordering and audit chains survive while the erased
        content is unrecoverable (GDPR erasure in an append-only design).
        Replay of a redacted WRITE materializes an empty-content row, which the
        subsequent FORGET(hard) event then removes — rebuild stays clean.

        ``node_renames`` (old entity node id -> opaque id) also renames those
        node ids in ``community_partition`` markers in the same pass
        (:func:`~memspine.trust.erasure.scrub_partition_nodes`).
        """
        if self._mode is EventLogMode.EPHEMERAL:
            return []
        record_ids = [record_id] if isinstance(record_id, str) else list(record_id)
        redacted: list[int] = []
        async with self._client.engine.begin() as conn:
            rows = (await conn.execute(select(memory_events))).all()
            for row in rows:
                mapping = row._mapping
                raw: bytes = mapping["payload"]
                if mapping["compressed"]:
                    raw = self._dctx.decompress(raw)
                payload = orjson.loads(raw)
                # Scrub EVERY snapshot/delta of this record, wherever it hides
                # (record / incoming_record / dropped_record / compress delta).
                changed = False
                for rid in record_ids:
                    changed |= redact_record(payload, rid)
                if node_renames:
                    changed |= scrub_partition_nodes(payload, node_renames)
                if not changed:
                    continue
                payload["redacted"] = True
                encoded = canonical_payload(payload)
                if mapping["compressed"]:
                    encoded = self._cctx.compress(encoded)
                await conn.execute(
                    memory_events.update()
                    .where(memory_events.c.seq == mapping["seq"])
                    .values(payload=encoded)
                )
                redacted.append(int(mapping["seq"]))
        return redacted

    async def scrub_marker_payloads(
        self, marker: str, scrub: Callable[[dict[str, Any]], bool], *, namespace: str
    ) -> list[int]:
        """FU-CLOSE: the same log-redaction pass as :meth:`redact_event_payloads`, for MARKER events
        named ``marker`` (e.g. the ``profile_card`` card, which carries entry text).

        ``scrub`` mutates a decoded payload in place and returns True when it changed anything;
        a changed payload gets the ``redacted`` flag and is rewritten, keeping seq and kind.
        Returns the rewritten seqs."""
        if self._mode is EventLogMode.EPHEMERAL:
            return []
        rewritten: list[int] = []
        async with self._client.engine.begin() as conn:
            statement = select(memory_events).where(
                memory_events.c.namespace == namespace,
                memory_events.c.kind == EventKind.MARKER.value,
            )
            rows = (await conn.execute(statement)).all()
            for row in rows:
                mapping = row._mapping
                raw: bytes = mapping["payload"]
                if mapping["compressed"]:
                    raw = self._dctx.decompress(raw)
                payload = orjson.loads(raw)
                if not isinstance(payload, dict) or payload.get("marker") != marker:
                    continue
                if not scrub(payload):
                    continue
                payload["redacted"] = True
                encoded = canonical_payload(payload)
                if mapping["compressed"]:
                    encoded = self._cctx.compress(encoded)
                await conn.execute(
                    memory_events.update()
                    .where(memory_events.c.seq == mapping["seq"])
                    .values(payload=encoded)
                )
                rewritten.append(int(mapping["seq"]))
        return rewritten

    def _row_to_record(self, row: Any) -> MemoryRecord:
        return MemoryRecord.model_validate(
            {
                "record_id": row["record_id"],
                "namespace": row["namespace"],
                "memory_type": row["memory_type"],
                "content": row["content"],
                "content_fingerprint": row["content_fingerprint"],
                "entity": row["entity"],
                "attribute": row["attribute"],
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "recorded_at": row["recorded_at"],
                "superseded_at": row["superseded_at"],
                "source": orjson.loads(row["source"]),
                "status": row["status"],
                "version": row["version"],
                "history": orjson.loads(row["history"]),
                "evolve_to": row["evolve_to"],
                "pii_tier": row["pii_tier"],
                "consent_tags": orjson.loads(row["consent_tags"]),
                "scoring": orjson.loads(row["scoring"]),
                "trust": row["trust"],
                "quarantined": row["quarantined"],
                "instruction_flag": row["instruction_flag"],
                "simhash": row["simhash"],
                "minhash_sig": row["minhash_sig"],
                "tier": row["tier"],
                "content_zstd": row["content_zstd"],
                "corroborations": row["corroborations"],
                "skill_stage": row["skill_stage"],
                "reflection_depth": row["reflection_depth"],
                "group_id": row["group_id"],
                "tags": orjson.loads(row["tags"]),
                "invalid_at": row["invalid_at"],
                **self._envelope_fields(row),
            }
        )

    @staticmethod
    def _envelope_fields(row: Any) -> dict[str, Any]:
        """Read explicit current envelope columns without old-row inference."""
        out: dict[str, Any] = {
            "schema_version": row["schema_version"],
            "type_tag": row["type_tag"],
            "provenance_class": row["provenance_class"],
            "layer": row["layer"],
        }
        axes = {"speaker": row["spk"], "addressee": row["addr"], "asker": row["ask"]}
        if row["sub"] is not None or any(v is not None for v in axes.values()):
            axes["subject"] = orjson.loads(row["sub"]) if row["sub"] is not None else []
            if row["participants"] is not None:  # W2-02
                axes["participants"] = orjson.loads(row["participants"])
            out["axes"] = axes
        out["fingerprint"] = orjson.loads(row["fingerprint"])
        for key in ("source_at", "source_precision", "event_precision", "event_basis", "source_tz"):
            if row[key] is not None:
                out[key] = row[key]
        if row["event_approximate"]:
            out["event_approximate"] = True
        return out
