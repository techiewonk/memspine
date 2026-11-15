"""MongoDB ``records`` store (RM-112, ADR-081): the :class:`RecordsStore` port without SQL.

The adapter receives an open :class:`~memspine.clients.mongodb.MongoConnector` and never opens
a connection itself (``docs/CLIENT_ADAPTER_FACTORY_STANDARD.md``). It needs a replica set or a
sharded cluster: every log append is a multi-document transaction (the connector refuses a
standalone server). Contract text: ``docs/RECORDS_STORE_CONTRACT.md``.

Collections (``<prefix><name>``; schema version :data:`SCHEMA_REVISION` in ``meta``):

* ``events`` - the append-only log. ``_id`` is the ``seq``, taken from the ``events`` counter
  document by ``findOneAndUpdate($inc)`` INSIDE the append transaction, so two appenders
  conflict on the counter and serialise: seqs are dense, strictly increasing in commit order
  and never reused (an aborted append rolls the counter back with it). The payload is the
  canonical JSON bytes the fingerprint hashed (optionally zstd), so it round-trips exactly.
* ``outbox`` - outbox items, offset per ``(scope, stage)`` from a counter document in the
  same transaction as the event; a ``$jsonSchema`` validator is the store-side constraint.
* ``offsets`` - projector high-water marks (``$max`` upsert: advance-only, race-free).
* ``leases`` - epoch-fenced leases, judged by the server clock (``$$NOW``), on a sync client.
* ``records`` - the read model: the record as its pydantic JSON plus the indexed key fields.
* ``lineage`` - (child, parent, role) edges, unique, ordered by a counter-assigned ``order``.
* ``counters`` / ``meta`` - sequence counters, the schema version document and the deployment
  identity document (``_id: "deployment"``).
* ``projector_bindings`` - the provider target each projector checkpoint belongs to (RM-112).

Event-log modes: ``full`` and ``rolling``. ``ephemeral`` is refused: a cluster store that
persists nothing has no reason to exist, and an in-process seq is not shared by processes.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import orjson
import zstandard

from memspine.clients.mongodb.driver import (
    ASCENDING,
    DESCENDING,
    BulkWriteError,
    CollectionInvalid,
    DuplicateKeyError,
    ObjectId,
    ReadConcern,
    ReadPreference,
    ReturnDocument,
    WriteConcern,
)
from memspine.config.constants import ZSTD_LEVEL
from memspine.exceptions import ConfigError, RebuildUnavailableError, StorageError
from memspine.observability.logging import get_logger
from memspine.schemas.durable.lineage import LineageEdge
from memspine.services.base import CapabilityManifest, ServiceAdapter
from memspine.services.storage.records_port import (
    EntityIndexAdvance,
    EntityIndexApplyBatch,
    EntityIndexApplyPurge,
    ProjectionTargetBinding,
    RecordsAdvance,
    RecordsProjectionView,
    binding_source_name,
    require_binding_match,
)
from memspine.store.events import EventKind, EventLogMode, MemoryEvent, canonical_payload
from memspine.store.leases import NamespaceLease
from memspine.store.record_event_ids import affected_record_ids, validate_record_event_shape
from memspine.store.records import MemoryRecord
from memspine.trust.erasure import redact_record, scrub_partition_nodes

if TYPE_CHECKING:
    from memspine.clients.mongodb import MongoConnector
    from memspine.clients.mongodb.driver import (
        AsyncClientSession,
        AsyncCollection,
        AsyncDatabase,
        Collection,
        Database,
    )
    from memspine.schemas.durable.common import OwnerScope
    from memspine.services.storage.entity_index_tables import EntityIndexRow
    from memspine.services.storage.records_port import OutboxItem

__all__ = [
    "SCHEMA_REVISION",
    "SCHEMA_VERSION",
    "MongoLeaseTable",
    "MongoProjectionBindingStore",
    "MongoRecordsStore",
]

_log = get_logger(__name__)

#: Record statuses that count as live in :meth:`MongoRecordsStore.layer_counts` (as in SQL).
_LIVE_STATUSES = ("activated", "resolving")

_ENTITY_INDEX_KINDS = (
    EventKind.WRITE.value,
    EventKind.FORGET.value,
    EventKind.MARKER.value,
    EventKind.DECAY_TRANSITION.value,
    EventKind.REGISTRY_ERASURE_FENCE.value,
)

# Purge has no caller page size; cap its checkpoint scan independently of backlog size.
_ENTITY_INDEX_PURGE_PREFIX_PAGE = 2048

#: Collection-setup version (bump with a migration step in :meth:`MongoRecordsStore.start`).
SCHEMA_VERSION = 1
SCHEMA_REVISION = f"mongodb-records-v{SCHEMA_VERSION}"

Doc = dict[str, Any]

_OUTBOX_VALIDATOR: Doc = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["tenant", "owner", "owner_kind", "stage", "offset", "payload", "created_at"],
        "properties": {
            "tenant": {"bsonType": "string", "minLength": 1},
            "owner": {"bsonType": "string", "minLength": 1},
            "owner_kind": {"bsonType": "string"},
            "stage": {"bsonType": "string", "minLength": 1},
            "offset": {"bsonType": ["int", "long"], "minimum": 0},
            "payload": {"bsonType": "string"},
            "created_at": {"bsonType": "string"},
        },
    }
}

#: (collection, keys, options) - every index the store uses, created idempotently.
_INDEXES: tuple[tuple[str, list[tuple[str, int]], Doc], ...] = (
    ("events", [("event_id", ASCENDING)], {"unique": True, "name": "event_id_unique"}),
    ("events", [("namespace", ASCENDING), ("_id", ASCENDING)], {"name": "namespace_seq"}),
    ("events", [("ts_at", ASCENDING)], {"name": "ts_at"}),
    (
        "outbox",
        [("tenant", 1), ("owner", 1), ("owner_kind", 1), ("stage", 1), ("offset", 1)],
        {"unique": True, "name": "scope_stage_offset_unique"},
    ),
    ("records", [("namespace", 1), ("memory_type", 1), ("order", 1)], {"name": "ns_type"}),
    ("records", [("namespace", 1), ("group_id", 1)], {"name": "ns_group"}),
    (
        "records",
        [("namespace", 1), ("entity", 1), ("attribute", 1), ("recorded_key", -1)],
        {"name": "ns_fact_key"},
    ),
    (
        "records",
        [
            ("namespace", 1),
            ("entity", 1),
            ("attribute", 1),
            ("claim_group_key", 1),
            ("recorded_key", -1),
        ],
        {"name": "ns_untyped_fact"},
    ),
    ("records", [("namespace", 1), ("session_key", 1)], {"name": "ns_session"}),
    (
        "records",
        [("namespace", 1), ("claim_group_key", 1), ("_id", 1)],
        {"name": "claim_group_page"},
    ),
    ("records", [("namespace", 1), ("parents", 1)], {"name": "ns_parents"}),
    (
        "lineage",
        [("child", 1), ("parent", 1), ("role", 1)],
        {"unique": True, "name": "edge_unique"},
    ),
    ("lineage", [("parent", 1), ("order", 1)], {"name": "parent_order"}),
    ("lineage", [("child", 1), ("order", 1)], {"name": "child_order"}),
    ("records", [("namespace", 1), ("quarantined", 1), ("order", 1)], {"name": "ns_quarantine"}),
)

#: The server's clock in epoch seconds (aggregation expression, MongoDB >= 4.2).
_NOW_S: Doc = {"$divide": [{"$toLong": "$$NOW"}, 1000]}


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def _sort_key(ts: datetime) -> str:
    """A lexically sortable UTC timestamp (fixed width, microseconds)."""
    aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class MongoLeaseTable:
    """The :class:`~memspine.services.storage.records_port.LeasePort` on MongoDB.

    Synchronous (lease holders run in threads and processes). Each operation is one atomic
    single-document update; expiry is judged by the server clock (``$$NOW``), never the
    host's, so clock skew between processes cannot hand a live lease to a second holder."""

    def __init__(self, collection: Collection[Doc]) -> None:
        self._leases = collection

    @staticmethod
    def _lease(doc: Doc) -> NamespaceLease:
        return NamespaceLease(
            ns=str(doc["_id"]),
            owner=str(doc["owner"]),
            epoch=int(doc["epoch"]),
            job_id=str(doc["job_id"]),
            lease_until=float(doc["lease_until"]),
            heartbeat_at=float(doc["heartbeat_at"]),
        )

    def claim(self, ns: str, owner: str, job_id: str, ttl: float) -> NamespaceLease | None:
        """Take the lease when absent or expired (epoch + 1); None while another holds it.

        One atomic upsert with a conditional pipeline (``$expr`` is not allowed in an upsert
        filter): the document takes this claim's token only when it had no live holder, and
        the claim won when the returned token is its own. Of concurrent claims exactly one
        token lands."""
        token = ObjectId()

        def take(value: Any, keep: Any) -> Doc:
            return {"$cond": ["$_free", value, keep]}

        pipeline: list[Doc] = [
            {
                "$set": {
                    "_free": {
                        "$or": [
                            {"$eq": [{"$type": "$lease_until"}, "missing"]},
                            {"$lte": ["$lease_until", _NOW_S]},
                        ]
                    }
                }
            },
            {
                "$set": {
                    "owner": take({"$literal": owner}, "$owner"),
                    "job_id": take({"$literal": job_id}, "$job_id"),
                    "epoch": take({"$add": [{"$ifNull": ["$epoch", 0]}, 1]}, "$epoch"),
                    "lease_until": take({"$add": [_NOW_S, float(ttl)]}, "$lease_until"),
                    "heartbeat_at": take(_NOW_S, "$heartbeat_at"),
                    "token": take(token, "$token"),
                }
            },
            {"$unset": "_free"},
        ]
        doc: Doc | None = None
        for attempt in range(2):
            try:
                doc = self._leases.find_one_and_update(
                    {"_id": ns}, pipeline, upsert=True, return_document=ReturnDocument.AFTER
                )
                break
            except DuplicateKeyError:  # two first claims raced the insert: re-run on the doc
                if attempt:
                    raise
        if doc is None or doc.get("token") != token:
            return None
        return self._lease(doc)

    def heartbeat(self, lease: NamespaceLease, ttl: float) -> NamespaceLease | None:
        """Extend a current lease; None when a takeover has fenced it."""
        doc = self._leases.find_one_and_update(
            {"_id": lease.ns, "epoch": lease.epoch},
            [{"$set": {"lease_until": {"$add": [_NOW_S, float(ttl)]}, "heartbeat_at": _NOW_S}}],
            return_document=ReturnDocument.AFTER,
        )
        return None if doc is None else self._lease(doc)

    def release(self, lease: NamespaceLease) -> bool:
        """Free a current lease (the epoch stays, so the next claim bumps it)."""
        result = self._leases.update_one(
            {"_id": lease.ns, "epoch": lease.epoch}, {"$set": {"lease_until": 0.0}}
        )
        return result.matched_count == 1

    def get_lease(self, ns: str) -> NamespaceLease | None:
        doc = self._leases.find_one({"_id": ns})
        return None if doc is None else self._lease(doc)

    def close(self) -> None:
        """Nothing to release: the connector owns the sync client."""


class MongoEntityIndexStore:
    """RM-130: the entity name index (:mod:`~memspine.services.storage.entity_index_tables`) as one
    MongoDB collection, one document per row keyed by the row's primary key."""

    _COLS = ("namespace", "kind", "key", "entity_id", "ref")

    def __init__(self, rows: AsyncCollection[Doc]) -> None:
        self._rows = rows

    async def put(self, rows: Sequence[Any]) -> None:
        for row in rows:
            doc: Doc = {c: getattr(row, c) for c in self._COLS}
            if row.kind == "merge":
                await self._put_merge(row, doc)
                continue
            await self._rows.replace_one(
                {"_id": dict(doc)},
                {**doc, "match": row.match, "data": dict(row.data), "seq": int(row.seq)},
                upsert=True,
            )

    async def _put_merge(self, row: Any, doc: Doc) -> None:
        """Normalize all link candidates for one absorbed entity in one transaction."""
        guard: Doc = {
            "namespace": row.namespace,
            "kind": "merge_guard",
            "key": row.ref,
            "entity_id": "",
            "ref": "",
        }
        guard_id = dict(guard)
        with contextlib.suppress(DuplicateKeyError):
            await self._rows.update_one(
                {"_id": guard_id},
                {"$setOnInsert": {**guard, "match": "", "data": {}, "seq": 0, "version": 0}},
                upsert=True,
            )

        async def body(session: AsyncClientSession) -> None:
            await self._rows.update_one(
                {"_id": guard_id}, {"$inc": {"version": 1}}, session=session
            )
            key = {"_id": dict(doc)}
            if row.data.get("link"):
                try:
                    await self._rows.update_one(
                        key,
                        {
                            "$min": {"seq": int(row.seq)},
                            "$setOnInsert": {**doc, "match": row.match, "data": dict(row.data)},
                        },
                        upsert=True,
                        session=session,
                    )
                except DuplicateKeyError:
                    await self._rows.update_one(
                        key, {"$min": {"seq": int(row.seq)}}, session=session
                    )
            else:
                await self._rows.replace_one(
                    key,
                    {**doc, "match": row.match, "data": dict(row.data), "seq": int(row.seq)},
                    upsert=True,
                    session=session,
                )
            related = [
                item
                async for item in self._rows.find(
                    {"namespace": row.namespace, "kind": "merge", "ref": row.ref},
                    session=session,
                )
            ]
            earliest = min(related, key=lambda item: (int(item["seq"]), str(item["key"])))
            for item in related:
                if item.get("data", {}).get("link") and item["_id"] != earliest["_id"]:
                    await self._rows.delete_one({"_id": item["_id"]}, session=session)

        async with self._rows.database.client.start_session() as session:
            await session.with_transaction(
                body,
                read_concern=ReadConcern("snapshot"),
                write_concern=WriteConcern("majority"),
                read_preference=ReadPreference.PRIMARY,
            )

    @staticmethod
    def _filter(namespace: str, kind: str, **cols: str | None) -> Doc:
        out: Doc = {"namespace": namespace, "kind": kind}
        out.update({k: v for k, v in cols.items() if v is not None})
        return out

    async def select(
        self,
        namespace: str,
        kind: str,
        *,
        key: str | None = None,
        entity_id: str | None = None,
        ref: str | None = None,
        match: str | None = None,
    ) -> list[Any]:
        from memspine.services.storage.entity_index_tables import EntityIndexRow

        found = self._rows.find(
            self._filter(namespace, kind, key=key, entity_id=entity_id, ref=ref, match=match)
        )
        rows = [
            EntityIndexRow(
                namespace=str(d["namespace"]),
                kind=str(d["kind"]),
                key=str(d["key"]),
                entity_id=str(d["entity_id"]),
                ref=str(d["ref"]),
                match=str(d.get("match", "")),
                data=dict(d.get("data") or {}),
                seq=int(d.get("seq", 0)),
            )
            async for d in found
        ]
        return sorted(rows, key=lambda r: r.pk)

    async def delete(
        self,
        namespace: str,
        kind: str,
        *,
        key: str | None = None,
        entity_id: str | None = None,
        ref: str | None = None,
    ) -> None:
        await self._rows.delete_many(
            self._filter(namespace, kind, key=key, entity_id=entity_id, ref=ref)
        )

    async def clear(self) -> None:
        await self._rows.delete_many({})

    async def select_after(
        self, namespace: str, kind: str, *, after: tuple[int, str, str, str] | None, limit: int
    ) -> list[Any]:
        """DS-12: one keyset page in ``(seq, key, entity_id, ref)`` order."""
        from memspine.services.storage.entity_index_tables import EntityIndexRow

        if limit <= 0:
            return []
        query: Doc = {"namespace": namespace, "kind": kind}
        if after is not None:
            seq, key, entity_id, ref = after
            query["$or"] = [
                {"seq": {"$gt": seq}},
                {"seq": seq, "key": {"$gt": key}},
                {"seq": seq, "key": key, "entity_id": {"$gt": entity_id}},
                {"seq": seq, "key": key, "entity_id": entity_id, "ref": {"$gt": ref}},
            ]
        found = (
            self._rows.find(query)
            .sort([("seq", 1), ("key", 1), ("entity_id", 1), ("ref", 1)])
            .limit(limit)
        )
        return [
            EntityIndexRow(
                namespace=str(d["namespace"]),
                kind=str(d["kind"]),
                key=str(d["key"]),
                entity_id=str(d["entity_id"]),
                ref=str(d["ref"]),
                match=str(d.get("match", "")),
                data=dict(d.get("data") or {}),
                seq=int(d.get("seq", 0)),
            )
            async for d in found
        ]


class _MongoEntityIndexTxView:
    """Index and record operations on the caller's one snapshot transaction."""

    def __init__(
        self, store: MongoRecordsStore, session: AsyncClientSession, namespace: str
    ) -> None:
        self._store = store
        self._session = session
        self._namespace = namespace
        self._rows = store._c("entity_index")

    def _require_namespace(self, namespace: str) -> None:
        if namespace != self._namespace:
            raise StorageError("entity index transaction cannot cross namespaces")

    async def put(self, rows: Sequence[EntityIndexRow]) -> None:
        for row in rows:
            self._require_namespace(row.namespace)
            doc: Doc = {c: getattr(row, c) for c in MongoEntityIndexStore._COLS}
            await self._rows.replace_one(
                {"_id": dict(doc)},
                {**doc, "match": row.match, "data": dict(row.data), "seq": int(row.seq)},
                upsert=True,
                session=self._session,
            )

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
        from memspine.services.storage.entity_index_tables import EntityIndexRow

        self._require_namespace(namespace)
        query = MongoEntityIndexStore._filter(
            namespace, kind, key=key, entity_id=entity_id, ref=ref, match=match
        )
        found = self._rows.find(query, session=self._session)
        rows = [
            EntityIndexRow(
                namespace=str(d["namespace"]),
                kind=str(d["kind"]),
                key=str(d["key"]),
                entity_id=str(d["entity_id"]),
                ref=str(d["ref"]),
                match=str(d.get("match", "")),
                data=dict(d.get("data") or {}),
                seq=int(d.get("seq", 0)),
            )
            async for d in found
        ]
        return sorted(rows, key=lambda row: row.pk)

    async def delete(
        self,
        namespace: str,
        kind: str,
        *,
        key: str | None = None,
        entity_id: str | None = None,
        ref: str | None = None,
    ) -> None:
        self._require_namespace(namespace)
        await self._rows.delete_many(
            MongoEntityIndexStore._filter(namespace, kind, key=key, entity_id=entity_id, ref=ref),
            session=self._session,
        )

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        doc = await self._store._c("records").find_one(
            {"_id": record_id, "namespace": self._namespace},
            {"doc": 1},
            session=self._session,
        )
        return self._store._doc_to_record(doc) if doc is not None else None

    async def pending_events(self, limit: int) -> list[MemoryEvent]:
        if limit <= 0:
            return []
        rows = self._rows.find(
            {"namespace": self._namespace, "kind": "pending_positive"},
            {"seq": 1},
            sort=[("seq", ASCENDING)],
            limit=limit,
            session=self._session,
        )
        events: list[MemoryEvent] = []
        async for row in rows:
            seq = int(row["seq"])
            if seq < 1:
                raise StorageError("entity index pending canonical event is missing")
            doc = await self._store._c("events").find_one(
                {
                    "_id": seq,
                    "namespace": self._namespace,
                    "kind": EventKind.MARKER.value,
                },
                session=self._session,
            )
            if doc is None:
                raise StorageError("entity index pending canonical event is missing")
            events.append(self._store._doc_to_event(doc))
        return events


class MongoProjectionBindingStore:
    """The :class:`~memspine.services.storage.records_port.ProjectionBindingPort` on MongoDB.

    One document per ``(deployment, projector)``; creation is an insert that loses to a
    concurrent first writer (the winner's binding is then checked), and a verified switch is
    a compare-and-swap on the old provider, target and generation. The offset it checks is the
    store's own ``offsets`` document, as on SQL."""

    def __init__(self, bindings: AsyncCollection[Doc], offsets: AsyncCollection[Doc]) -> None:
        self._bindings = bindings
        self._offsets = offsets

    @staticmethod
    def _key(deployment_id: str, projector_name: str) -> Doc:
        return {"_id": {"deployment": deployment_id, "projector": projector_name}}

    async def _offset(self, projector_name: str) -> int:
        doc = await self._offsets.find_one({"_id": projector_name})
        return int(doc["last_seq"]) if doc is not None else 0

    async def bound_provider(self, deployment_id: str, projector_name: str) -> str | None:
        doc = await self._bindings.find_one(self._key(deployment_id, projector_name))
        return None if doc is None else binding_source_name(str(doc["provider"]))

    async def ensure(self, target: ProjectionTargetBinding) -> str:
        """As :meth:`SqlProjectionBindingStore.ensure` (same refusals, same messages)."""
        key = self._key(target.deployment_id, target.projector_name)
        row = await self._bindings.find_one(key)
        if row is not None:
            provider, fingerprint, generation = (
                str(row["provider"]),
                str(row["target_fingerprint"]),
                str(row["generation_id"]),
            )
            if (provider, fingerprint, generation) == (
                target.provider,
                target.target_fingerprint,
                target.generation_id,
            ):
                return generation
            if target.source_provider is None:
                require_binding_match(target, provider, fingerprint)
                raise ConfigError(
                    f"projector {target.projector_name!r} is bound to generation "
                    f"{generation!r}, but the selected generation is "
                    f"{target.generation_id!r}; a verified projection switch is required"
                )
            source_matches = (
                binding_source_name(provider) == target.source_provider
                and fingerprint == target.source_target_fingerprint
                and generation == target.source_generation_id
            )
            if not source_matches:
                raise ConfigError(
                    f"projector {target.projector_name!r} no longer matches the verified "
                    "switch source; refusing to reuse or replace its offset"
                )
            if await self._offset(target.projector_name) != target.verified_checkpoint_seq:
                raise ConfigError(
                    f"projector {target.projector_name!r} offset does not match the "
                    "verified target checkpoint; startup is blocked"
                )
            result = await self._bindings.update_one(
                {
                    **key,
                    "provider": provider,
                    "target_fingerprint": fingerprint,
                    "generation_id": generation,
                },
                {
                    "$set": {
                        "provider": target.provider,
                        "target_fingerprint": target.target_fingerprint,
                        "generation_id": target.generation_id,
                        "updated_at": _iso(datetime.now(UTC)),
                    }
                },
            )
            if result.modified_count != 1:
                raise ConfigError("projector binding changed during verified activation")
            return target.generation_id

        if await self._offset(target.projector_name) > 0:
            raise ConfigError(
                f"projector {target.projector_name!r} has a non-zero unbound offset "
                "without a provider target binding; a verified projection rebuild or "
                "switch is required before startup"
            )
        if target.source_provider is not None:
            raise ConfigError(
                f"projector {target.projector_name!r} has no source binding for the verified "
                "switch; refusing to create a target binding"
            )
        with contextlib.suppress(DuplicateKeyError):  # a concurrent first binder won
            await self._bindings.insert_one(
                {
                    **key,
                    "provider": target.provider,
                    "target_fingerprint": target.target_fingerprint,
                    "generation_id": target.generation_id,
                    "updated_at": _iso(datetime.now(UTC)),
                }
            )
        saved = await self._bindings.find_one(key)
        assert saved is not None  # inserted above or by the concurrent winner
        require_binding_match(target, str(saved["provider"]), str(saved["target_fingerprint"]))
        return str(saved["generation_id"])


class _MongoRecordsTxView:
    """The records projector's one-session view, invalid after its callback returns."""

    def __init__(self, store: MongoRecordsStore, session: AsyncClientSession) -> None:
        self._store = store
        self._session = session
        self._active = True

    def _check(self) -> None:
        if not self._active:
            raise StorageError("records projection transaction view has expired")

    def close(self) -> None:
        self._active = False

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        self._check()
        doc = await self._store._c("records").find_one(
            {"_id": record_id}, {"doc": 1}, session=self._session
        )
        return self._store._doc_to_record(doc) if doc is not None else None

    async def put_record(self, record: MemoryRecord) -> None:
        self._check()
        await self._store._c("records").update_one(
            {"_id": record.record_id},
            {
                "$set": self._store._record_fields(record),
                "$setOnInsert": {"order": ObjectId()},
            },
            upsert=True,
            session=self._session,
        )

    async def delete_record(self, record_id: str) -> None:
        self._check()
        await self._store._c("records").delete_one({"_id": record_id}, session=self._session)

    async def group_page(
        self, namespace: str, group_key: str, *, after_id: str | None, limit: int
    ) -> list[MemoryRecord]:
        self._check()
        if limit <= 0:
            return []
        query: Doc = {"namespace": namespace, "claim_group_key": group_key}
        if after_id is not None:
            query["_id"] = {"$gt": after_id}
        found = self._store._c("records").find(
            query,
            {"doc": 1},
            sort=[("_id", ASCENDING)],
            limit=limit,
            session=self._session,
        )
        return [self._store._doc_to_record(doc) async for doc in found]

    async def erasure_state(self, namespace: str, record_id: str) -> tuple[bool, bool]:
        self._check()
        fenced = await self._store._c("records_append_fence").find_one(
            {"_id": record_id}, {"_id": 1}, session=self._session
        )
        found = self._store._c("records_projection_membership").find(
            {"namespace": namespace, "record_id": record_id},
            {"kind": 1},
            session=self._session,
        )
        kinds = {str(doc["kind"]) async for doc in found}
        return fenced is not None or "erased" in kinds, "hidden" in kinds

    async def _mark(self, namespace: str, record_id: str, kind: str) -> None:
        self._check()
        await self._store._c("records_projection_membership").update_one(
            {"_id": {"namespace": namespace, "record_id": record_id, "kind": kind}},
            {"$setOnInsert": {"namespace": namespace, "record_id": record_id, "kind": kind}},
            upsert=True,
            session=self._session,
        )

    async def mark_erased(self, namespace: str, record_id: str) -> None:
        await self._mark(namespace, record_id, "erased")

    async def mark_hidden(self, namespace: str, record_id: str) -> None:
        await self._mark(namespace, record_id, "hidden")

    async def release_hidden(self, namespace: str, record_id: str) -> None:
        self._check()
        await self._store._c("records_projection_membership").delete_one(
            {"_id": {"namespace": namespace, "record_id": record_id, "kind": "hidden"}},
            session=self._session,
        )


class MongoRecordsStore(ServiceAdapter):
    """Event log, outbox, offsets, leases, read model and lineage in one MongoDB database."""

    def __init__(
        self,
        connector: MongoConnector,
        mode: EventLogMode = EventLogMode.FULL,
        compress: bool = False,
        collection_prefix: str = "",
    ) -> None:
        if mode is EventLogMode.EPHEMERAL:
            raise StorageError(
                "the MongoDB records store persists the log: use event_log.mode full or rolling"
            )
        self._connector = connector
        self._mode = mode
        self._compress = compress
        self._prefix = collection_prefix
        self._started = False
        self._leases: MongoLeaseTable | None = None
        self._cctx = zstandard.ZstdCompressor(level=ZSTD_LEVEL)
        self._dctx = zstandard.ZstdDecompressor()

    # ---------------------------------------------------------------- plumbing
    @property
    def _db(self) -> AsyncDatabase[Doc]:
        return self._connector.database

    def _c(self, name: str) -> AsyncCollection[Doc]:
        return self._db[self._prefix + name]

    async def _transaction[T](self, body: Callable[[AsyncClientSession], Awaitable[T]]) -> T:
        """Run ``body`` in one snapshot transaction, retried on transient conflicts by the
        driver (``body`` must rebuild its local state on every attempt)."""
        async with self._connector.handle.start_session() as session:
            return await session.with_transaction(
                body,
                read_concern=ReadConcern("snapshot"),
                write_concern=WriteConcern("majority"),
                read_preference=ReadPreference.PRIMARY,
            )

    async def _next(
        self, counter: Any, session: AsyncClientSession | None = None, by: int = 1
    ) -> int:
        """Advance a counter document and return its new value."""
        doc = await self._c("counters").find_one_and_update(
            {"_id": counter},
            {"$inc": {"value": by}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        assert doc is not None  # upsert + AFTER always returns the document
        return int(doc["value"])

    # ---------------------------------------------------------------- lifecycle
    @property
    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="mongodb",
            capability="storage",
            extra="mongodb",
            provides=("event_log", "records", "projector_offsets", "outbox", "leases"),
        )

    @property
    def mode(self) -> EventLogMode:
        return self._mode

    @property
    def can_rebuild(self) -> bool:
        return True  # full or rolling (ephemeral is refused at construction)

    async def start(self) -> None:
        """Create collections, the outbox validator, indexes and counters; record the version.

        Idempotent. A database set up by a NEWER store version is refused, never downgraded."""
        meta = self._c("meta")
        current = await meta.find_one({"_id": "schema"})
        if current is not None and int(current.get("version", 0)) > SCHEMA_VERSION:
            raise StorageError(
                f"MongoDB records schema is version {current['version']}, newer than this "
                f"memspine ({SCHEMA_VERSION}); upgrade memspine"
            )
        existing = set(await self._db.list_collection_names())
        for name in (
            "events",
            "outbox",
            "offsets",
            "leases",
            "records",
            "lineage",
            "counters",
            "projector_bindings",
            "entity_index",
            "entity_index_state",
            "records_projection_state",
            "records_projection_membership",
            "records_append_fence",
            "records_append_identity",
        ):
            full = self._prefix + name
            options: Doc = {"validator": _OUTBOX_VALIDATOR} if name == "outbox" else {}
            if full not in existing:
                with contextlib.suppress(CollectionInvalid):  # another process was first
                    await self._db.create_collection(full, **options)
            if name == "outbox":
                await self._db.command("collMod", full, validator=_OUTBOX_VALIDATOR)
        for name, keys, opts in _INDEXES:
            await self._c(name).create_index(keys, **opts)
        for counter in ("events", "lineage"):  # pre-created: transactions never insert them
            with contextlib.suppress(DuplicateKeyError):
                await self._c("counters").update_one(
                    {"_id": counter}, {"$setOnInsert": {"value": 0}}, upsert=True
                )
        with contextlib.suppress(DuplicateKeyError):
            await self._c("entity_index_state").update_one(
                {"_id": "reset"},
                {"$setOnInsert": {"epoch": 0, "guard": 0}},
                upsert=True,
            )
        with contextlib.suppress(DuplicateKeyError):
            await self._c("records_projection_state").update_one(
                {"_id": "writer"},
                {"$setOnInsert": {"cursor": 0, "guard": 0}},
                upsert=True,
            )
        await meta.update_one(
            {"_id": "schema"},
            {
                "$set": {"version": SCHEMA_VERSION, "revision": SCHEMA_REVISION},
                "$setOnInsert": {"created_at": _iso(datetime.now(UTC))},
            },
            upsert=True,
        )
        self._started = True

    async def schema_revision(self) -> str:
        doc = await self._c("meta").find_one({"_id": "schema"})
        if doc is None:
            raise StorageError("MongoDB records store is not set up: call start() first")
        return str(doc["revision"])

    async def stop(self) -> None:
        if self._leases is not None:
            self._leases.close()
            self._leases = None
        self._started = False

    async def health(self) -> bool:
        return self._started and await self._connector.health()

    def leases(self) -> MongoLeaseTable:
        if self._leases is None:
            sync_db: Database[Doc] = self._connector.sync_database()
            self._leases = MongoLeaseTable(sync_db[self._prefix + "leases"])
        return self._leases

    # ---------------------------------------------------------------- provider metadata
    async def claim_deployment_id(self, candidate: str) -> str:
        """First writer wins: ``$setOnInsert`` on the ``meta`` deployment document."""
        meta = self._c("meta")
        with contextlib.suppress(DuplicateKeyError):  # two first claims raced the insert
            await meta.update_one(
                {"_id": "deployment"},
                {
                    "$setOnInsert": {
                        "deployment_id": candidate,
                        "created_at": _iso(datetime.now(UTC)),
                    }
                },
                upsert=True,
            )
        doc = await meta.find_one({"_id": "deployment"})
        assert doc is not None  # upserted above or by the concurrent winner
        return str(doc["deployment_id"])

    def projection_bindings(self) -> MongoProjectionBindingStore:
        return MongoProjectionBindingStore(self._c("projector_bindings"), self._c("offsets"))

    def entity_index_store(self) -> MongoEntityIndexStore:
        """RM-130: the entity name index projection (``entity_index`` collection)."""
        store = self.__dict__.get("_entity_index_store")
        if store is None:
            store = self.__dict__["_entity_index_store"] = MongoEntityIndexStore(
                self._c("entity_index")
            )
        return store

    async def initialize_entity_index(self) -> None:
        """Pin rolling history when the entity projector is enabled, before its first read."""

        async def body(session: AsyncClientSession) -> None:
            reset = await self._c("entity_index_state").find_one_and_update(
                {"_id": "reset"},
                {"$inc": {"guard": 1}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if reset is None:
                raise StorageError("entity index state is not set up: call start() first")
            await self._entity_index_register(session, reset)

        await self._transaction(body)

    async def assert_full_event_history(self) -> None:
        """Refuse full replay once retention has actually removed canonical events."""

        async def body(session: AsyncClientSession) -> None:
            reset = await self._c("entity_index_state").find_one_and_update(
                {"_id": "reset"},
                {"$inc": {"guard": 1}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if reset is None:
                raise StorageError("entity index state is not set up: call start() first")
            if int(reset.get("pruned_through", 0)) > 0:
                raise RebuildUnavailableError(
                    "event history was pruned (D-45); full replay is unavailable"
                )

        await self._transaction(body)

    async def _entity_index_register(self, session: AsyncClientSession, reset: Doc) -> None:
        """Refuse first use after actual pruning, then retain the zero checkpoint."""
        existing = await self._c("offsets").find_one(
            {"_id": "entity_index"}, {"_id": 1}, session=session
        )
        if existing is not None:
            return
        if int(reset.get("pruned_through", 0)) > 0:
            raise RebuildUnavailableError(
                "entity index cannot initialize after event history was pruned (D-45)"
            )
        await self._c("offsets").update_one(
            {"_id": "entity_index"},
            {"$setOnInsert": {"last_seq": 0}},
            upsert=True,
            session=session,
        )

    async def _entity_index_guard(
        self, session: AsyncClientSession, namespace: str
    ) -> tuple[int, int, int, int]:
        """Write both guards before any snapshot read of projection state or events."""
        state = self._c("entity_index_state")
        reset = await state.find_one_and_update(
            {"_id": "reset"},
            {"$inc": {"guard": 1}},
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if reset is None:
            raise StorageError("entity index state is not set up: call start() first")
        await self._entity_index_register(session, reset)
        ns = await state.find_one_and_update(
            {"_id": f"ns:{namespace}"},
            {
                "$inc": {"guard": 1},
                "$setOnInsert": {"scan_cursor": 0, "cursor": 0, "revision": 0},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        assert ns is not None
        return (
            int(reset["epoch"]),
            int(ns["scan_cursor"] if "scan_cursor" in ns else ns["cursor"]),
            int(ns["cursor"]),
            int(ns["revision"]),
        )

    async def _entity_index_offset(self, session: AsyncClientSession) -> int:
        doc = await self._c("offsets").find_one(
            {"_id": "entity_index"}, {"last_seq": 1}, session=session
        )
        return int(doc["last_seq"]) if doc is not None else 0

    async def _entity_index_prefix(
        self,
        session: AsyncClientSession,
        target: int,
        own_namespace: str,
        own_cursor: int,
        limit: int,
    ) -> int:
        """Advance a bounded global prefix only through covered relevant events."""
        prior = await self._entity_index_offset(session)
        if target <= prior:
            return prior
        docs = [
            doc
            async for doc in self._c("events").find(
                {"_id": {"$gt": prior, "$lte": target}},
                {"_id": 1, "kind": 1, "namespace": 1},
                sort=[("_id", ASCENDING)],
                limit=limit + 1,
                session=session,
            )
        ]
        page = docs[:limit]
        cursors: dict[str, int] = {own_namespace: own_cursor}
        prefix = prior
        for doc in page:
            seq = int(doc["_id"])
            if doc["kind"] in _ENTITY_INDEX_KINDS:
                ns = str(doc["namespace"])
                if ns not in cursors:
                    row = await self._c("entity_index_state").find_one(
                        {"_id": f"ns:{ns}"}, {"cursor": 1}, session=session
                    )
                    cursors[ns] = int(row["cursor"]) if row is not None else 0
                if cursors[ns] < seq:
                    prefix = seq - 1
                    break
            prefix = seq
        else:
            if len(docs) <= limit:
                prefix = target
        if prefix > prior:
            await self._c("offsets").update_one(
                {"_id": "entity_index"},
                {"$set": {"last_seq": prefix, "updated_at": _iso(datetime.now(UTC))}},
                upsert=True,
                session=session,
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
            raise ValueError("entity index limit must be positive and target nonnegative")

        async def body(session: AsyncClientSession) -> EntityIndexAdvance:
            epoch, scan_cursor, cursor, revision = await self._entity_index_guard(
                session, namespace
            )
            head = await self._c("events").find_one(
                {}, {"_id": 1}, sort=[("_id", DESCENDING)], session=session
            )
            target = max(
                scan_cursor,
                min(max(0, int(target_seq)), int(head["_id"]) if head else 0),
            )
            events = [
                self._doc_to_event(doc)
                async for doc in self._c("events").find(
                    {
                        "_id": {"$gt": scan_cursor, "$lte": target},
                        "namespace": namespace,
                        "kind": {"$in": _ENTITY_INDEX_KINDS},
                    },
                    sort=[("_id", ASCENDING)],
                    limit=limit + 1,
                    session=session,
                )
            ]
            batch = events[:limit]
            effects = await apply_batch(_MongoEntityIndexTxView(self, session, namespace), batch)
            next_scan = max(
                scan_cursor, target if len(events) <= limit else int(batch[-1].seq or 0)
            )
            earliest = await self._c("entity_index").find_one(
                {"namespace": namespace, "kind": "pending_positive"},
                {"seq": 1},
                sort=[("seq", ASCENDING)],
                session=session,
            )
            next_cursor = min(next_scan, int(earliest["seq"]) - 1) if earliest else next_scan
            if next_cursor < cursor:
                raise StorageError("entity index pending marker regressed its certified cursor")
            complete = next_scan >= target and next_cursor >= target
            next_revision = revision + int(effects.changed)
            if next_scan != scan_cursor or next_cursor != cursor or next_revision != revision:
                await self._c("entity_index_state").update_one(
                    {"_id": f"ns:{namespace}"},
                    {
                        "$set": {
                            "scan_cursor": next_scan,
                            "cursor": next_cursor,
                            "revision": next_revision,
                        }
                    },
                    session=session,
                )
            global_offset = await self._entity_index_prefix(
                session, target, namespace, next_cursor, limit
            )
            return EntityIndexAdvance(
                epoch,
                next_cursor,
                next_revision,
                global_offset,
                complete,
                len(batch),
                effects,
            )

        return await self._transaction(body)

    async def purge_entity_index(
        self,
        namespace: str,
        record_ids: Sequence[str],
        apply_purge: EntityIndexApplyPurge,
    ) -> EntityIndexAdvance:
        ids = tuple(dict.fromkeys(str(rid) for rid in record_ids if rid))

        async def body(session: AsyncClientSession) -> EntityIndexAdvance:
            epoch, scan, cursor, revision = await self._entity_index_guard(session, namespace)
            effects = await apply_purge(_MongoEntityIndexTxView(self, session, namespace), ids)
            earliest = await self._c("entity_index").find_one(
                {"namespace": namespace, "kind": "pending_positive"},
                {"seq": 1},
                sort=[("seq", ASCENDING)],
                session=session,
            )
            next_cursor = min(scan, int(earliest["seq"]) - 1) if earliest else scan
            if next_cursor < cursor:
                raise StorageError("entity index purge regressed its certified cursor")
            if effects.changed:
                revision += 1
            if effects.changed or next_cursor != cursor:
                await self._c("entity_index_state").update_one(
                    {"_id": f"ns:{namespace}"},
                    {"$set": {"cursor": next_cursor, "revision": revision}},
                    session=session,
                )
            head = await self._c("events").find_one(
                {}, {"_id": 1}, sort=[("_id", DESCENDING)], session=session
            )
            global_offset = await self._entity_index_prefix(
                session,
                int(head["_id"]) if head is not None else 0,
                namespace,
                next_cursor,
                _ENTITY_INDEX_PURGE_PREFIX_PAGE,
            )
            return EntityIndexAdvance(
                epoch,
                next_cursor,
                revision,
                global_offset,
                next_cursor >= scan,
                0,
                effects,
            )

        return await self._transaction(body)

    async def reset_entity_index(self) -> EntityIndexAdvance:
        async def body(session: AsyncClientSession) -> EntityIndexAdvance:
            state = self._c("entity_index_state")
            reset = await state.find_one_and_update(
                {"_id": "reset"},
                {"$inc": {"epoch": 1, "guard": 1}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if reset is None:
                raise StorageError("entity index state is not set up: call start() first")
            if int(reset.get("pruned_through", 0)) > 0:
                raise RebuildUnavailableError(
                    "entity index cannot reset after event history was pruned (D-45)"
                )
            await self._c("entity_index").delete_many({}, session=session)
            await state.delete_many({"_id": {"$ne": "reset"}}, session=session)
            await self._c("offsets").update_one(
                {"_id": "entity_index"},
                {"$set": {"last_seq": 0, "updated_at": _iso(datetime.now(UTC))}},
                upsert=True,
                session=session,
            )
            return EntityIndexAdvance(int(reset["epoch"]), 0, 0, 0, True, 0)

        return await self._transaction(body)

    async def entity_index_revision(self, namespace: str) -> tuple[int, int]:
        async def body(session: AsyncClientSession) -> tuple[int, int]:
            state = self._c("entity_index_state")
            reset = await state.find_one({"_id": "reset"}, {"epoch": 1}, session=session)
            ns = await state.find_one({"_id": f"ns:{namespace}"}, {"revision": 1}, session=session)
            return (int(reset["epoch"]) if reset else 0, int(ns["revision"]) if ns else 0)

        return await self._transaction(body)

    async def _records_guard(self, session: AsyncClientSession) -> tuple[Doc, int]:
        """Write the prune/reset gate, then the records writer, before reading state."""
        reset = await self._c("entity_index_state").find_one_and_update(
            {"_id": "reset"},
            {"$inc": {"guard": 1}},
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if reset is None:
            raise StorageError("records projection state is not set up: call start() first")
        writer = await self._c("records_projection_state").find_one_and_update(
            {"_id": "writer"},
            {"$inc": {"guard": 1}},
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if writer is None:
            raise StorageError("records projection state is not set up: call start() first")
        return reset, int(writer["cursor"])

    async def _records_register(self, session: AsyncClientSession, reset: Doc, cursor: int) -> None:
        offset = await self._c("offsets").find_one(
            {"_id": "records"}, {"last_seq": 1}, session=session
        )
        if offset is not None:
            if int(offset["last_seq"]) != cursor:
                raise StorageError("records checkpoint differs from its certified cursor")
            return
        if int(reset.get("pruned_through", 0)) > 0 or cursor > 0:
            raise RebuildUnavailableError(
                "records projection cannot initialize after event history was pruned (D-45)"
            )
        await self._c("offsets").update_one(
            {"_id": "records"},
            {"$setOnInsert": {"last_seq": 0}},
            upsert=True,
            session=session,
        )

    async def initialize_records_projection(self) -> None:
        async def body(session: AsyncClientSession) -> None:
            reset, cursor = await self._records_guard(session)
            await self._records_register(session, reset, cursor)

        await self._transaction(body)

    async def advance_records(
        self,
        target_seq: int,
        limit: int,
        apply_batch: Callable[[RecordsProjectionView, Sequence[MemoryEvent]], Awaitable[None]],
    ) -> RecordsAdvance:
        if target_seq < 0 or limit <= 0:
            raise ValueError("records projection target must be nonnegative and limit positive")

        async def body(session: AsyncClientSession) -> RecordsAdvance:
            reset, cursor = await self._records_guard(session)
            await self._records_register(session, reset, cursor)
            docs = [
                doc
                async for doc in self._c("events").find(
                    {"_id": {"$gt": cursor, "$lte": target_seq}},
                    sort=[("_id", ASCENDING)],
                    limit=limit + 1,
                    session=session,
                )
            ]
            events = [self._doc_to_event(doc) for doc in docs[:limit]]
            view = _MongoRecordsTxView(self, session)
            try:
                await apply_batch(view, events)
            finally:
                view.close()
            next_cursor = int(events[-1].seq or cursor) if events else cursor
            if next_cursor > cursor:
                await self._c("records_projection_state").update_one(
                    {"_id": "writer"}, {"$set": {"cursor": next_cursor}}, session=session
                )
                await self._c("offsets").update_one(
                    {"_id": "records"}, {"$set": {"last_seq": next_cursor}}, session=session
                )
            return RecordsAdvance(next_cursor, len(events), next_cursor >= target_seq)

        return await self._transaction(body)

    async def append_ephemeral_records(
        self,
        event: MemoryEvent,
        apply_batch: Callable[[RecordsProjectionView, Sequence[MemoryEvent]], Awaitable[None]],
    ) -> MemoryEvent:
        raise StorageError("MongoDB records store does not support ephemeral event logs")

    async def reset_records_projection(self) -> None:
        async def body(session: AsyncClientSession) -> None:
            reset, cursor = await self._records_guard(session)
            if int(reset.get("pruned_through", 0)) > 0:
                raise RebuildUnavailableError(
                    "records projection cannot reset after event history was pruned (D-45)"
                )
            await self._records_register(session, reset, cursor)
            await self._c("records").delete_many({}, session=session)
            await self._c("records_projection_membership").delete_many({}, session=session)
            await self._c("records_projection_state").update_one(
                {"_id": "writer"}, {"$set": {"cursor": 0}}, session=session
            )
            await self._c("offsets").update_one(
                {"_id": "records"},
                {"$set": {"last_seq": 0, "updated_at": _iso(datetime.now(UTC))}},
                session=session,
            )

        await self._transaction(body)

    # ---------------------------------------------------------------- event log
    def _event_doc(self, event: MemoryEvent, seq: int) -> Doc:
        payload = canonical_payload(event.payload)  # the bytes the fingerprint hashed
        if self._compress:
            payload = self._cctx.compress(payload)
        return {
            "_id": seq,
            "event_id": event.event_id,
            "kind": event.kind.value,
            "namespace": event.namespace,
            "ts": _iso(event.ts),
            "ts_at": event.ts,
            "actor": event.actor,
            "schema_version": event.schema_version,
            "payload": payload,
            "compressed": self._compress,
            "fingerprint": event.fingerprint,
        }

    def _decode_payload(self, doc: Doc) -> Any:
        raw = bytes(doc["payload"])
        if doc["compressed"]:
            raw = self._dctx.decompress(raw)
        return orjson.loads(raw)

    def _doc_to_event(self, doc: Doc) -> MemoryEvent:
        return MemoryEvent(
            event_id=doc["event_id"],
            seq=int(doc["_id"]),
            kind=EventKind(doc["kind"]),
            namespace=doc["namespace"],
            ts=datetime.fromisoformat(doc["ts"]),
            actor=doc["actor"],
            schema_version=doc["schema_version"],
            payload=self._decode_payload(doc),
            fingerprint=doc["fingerprint"],
        )

    @staticmethod
    def _fresh(event: MemoryEvent) -> None:
        if event.seq is not None:
            raise StorageError("event already has a seq — events pass the write door once")

    async def append_event(self, event: MemoryEvent) -> MemoryEvent:
        self._reject_unfenced_registry_event(event)
        seq, _ = await self._append(event, ())
        return event.model_copy(update={"seq": seq})

    @staticmethod
    def _reject_unfenced_registry_event(event: MemoryEvent) -> None:
        from memspine.store.registry_evidence import registry_control_marker

        if event.kind is EventKind.REGISTRY_ERASURE_FENCE or registry_control_marker(
            event.kind, event.payload
        ):
            raise StorageError("registry control events require their guarded append door")

    async def _records_event_fence(self, event: MemoryEvent, session: AsyncClientSession) -> None:
        """Validate owners and terminal fences under the shared event-counter write."""
        try:
            validate_record_event_shape(event)
        except ValueError as exc:
            raise StorageError(str(exc)) from exc
        fences = self._c("records_append_fence")
        identities = self._c("records_append_identity")
        record_id: str | None = None
        if event.kind is EventKind.WRITE:
            raw = event.payload.get("record")
            if not isinstance(raw, Mapping):
                raise StorageError("WRITE requires a canonical record snapshot")
            record_id = raw.get("record_id")
            if (
                not isinstance(record_id, str)
                or not record_id
                or raw.get("namespace") != event.namespace
            ):
                raise StorageError("WRITE record identity or namespace mismatches its event")
        erased: list[str] = []
        if event.kind is EventKind.FORGET and bool(event.payload.get("hard")):
            rid = event.payload.get("record_id")
            if not isinstance(rid, str) or not rid:
                raise StorageError("hard FORGET requires a nonempty record_id")
            erased.append(rid)
        elif event.kind is EventKind.MARKER and event.payload.get("marker") == "erasure_manifest":
            manifest = event.payload.get("manifest")
            if manifest is not None and not isinstance(manifest, Mapping):
                raise StorageError("erasure manifest has invalid record ids")
            raw_ids = manifest.get("ids") if isinstance(manifest, Mapping) else None
            if raw_ids is not None and not isinstance(raw_ids, Mapping):
                raise StorageError("erasure manifest has invalid record ids")
            record_ids = raw_ids.get("record_ids") if isinstance(raw_ids, Mapping) else None
            if record_ids is not None:
                if not isinstance(record_ids, list) or any(
                    not isinstance(rid, str) or not rid for rid in record_ids
                ):
                    raise StorageError("erasure manifest has invalid record ids")
                erased.extend(record_ids)
        ids = tuple(dict.fromkeys((*affected_record_ids(event), *erased)))
        if ids:
            guard = await self._c("records_projection_state").update_one(
                {"_id": "writer"}, {"$inc": {"guard": 1}}, session=session
            )
            if guard.matched_count != 1:
                raise StorageError("records projection state is not set up: call start() first")
        for rid in ids:
            identity = await identities.find_one({"_id": rid}, {"namespace": 1}, session=session)
            if identity is not None and identity["namespace"] != event.namespace:
                raise StorageError("record id belongs to another namespace")
            existing = await fences.find_one({"_id": rid}, {"namespace": 1}, session=session)
            if existing is not None and existing["namespace"] != event.namespace:
                raise StorageError("record id belongs to another namespace")
            record = await self._c("records").find_one(
                {"_id": rid}, {"namespace": 1}, session=session
            )
            if record is not None and record["namespace"] != event.namespace:
                raise StorageError("record id belongs to another namespace")
        if record_id is not None:
            if await fences.find_one({"_id": record_id}, {"_id": 1}, session=session):
                raise StorageError("WRITE record identity was permanently erased")
            await identities.update_one(
                {"_id": record_id},
                {"$setOnInsert": {"namespace": event.namespace}},
                upsert=True,
                session=session,
            )
        for rid in dict.fromkeys(erased):
            await fences.update_one(
                {"_id": rid},
                {"$setOnInsert": {"namespace": event.namespace}},
                upsert=True,
                session=session,
            )

    async def begin_registry_erasure(self, namespace: str, source_ids: Sequence[str]) -> None:
        """Commit an ID-only canonical barrier before source deletion and log scrub."""
        ids = sorted(set(source_ids))
        if ids:
            await self._append(
                MemoryEvent(
                    kind=EventKind.REGISTRY_ERASURE_FENCE,
                    namespace=namespace,
                    actor="registry_erasure",
                    payload={"source_ids": ids},
                ),
                (),
            )

    async def registry_erasure_fences(self, after_seq: int = 0) -> list[tuple[int, str, list[str]]]:
        cursor = self._c("events").find(
            {"kind": EventKind.REGISTRY_ERASURE_FENCE.value, "_id": {"$gt": after_seq}},
            sort=[("_id", ASCENDING)],
        )
        fences: list[tuple[int, str, list[str]]] = []
        async for doc in cursor:
            payload = self._decode_payload(doc)
            ids = payload.get("source_ids") if isinstance(payload, dict) else None
            if not isinstance(ids, list) or not all(isinstance(rid, str) and rid for rid in ids):
                raise StorageError("malformed registry erasure fence")
            fences.append((int(doc["_id"]), str(doc["namespace"]), ids))
        return fences

    async def append_registry_marker(self, event: MemoryEvent) -> MemoryEvent:
        """Check all sources and append in one MongoDB transaction.

        The event counter write conflicts with an erasure barrier transaction;
        conditional writes to every source document conflict with concurrent
        version, status or quarantine updates.
        """
        from memspine.store.registry_evidence import (
            ENTITY_MARKERS,
            registry_marker_has_text,
            registry_marker_has_unattributed_text,
            valid_source_refs,
        )

        self._fresh(event)
        marker = event.payload.get("marker")
        if event.kind is not EventKind.MARKER or marker not in ENTITY_MARKERS:
            raise StorageError("registry append requires an entity marker")
        if registry_marker_has_unattributed_text(event.payload):
            raise StorageError("registry marker cannot carry unpinned text")
        if (
            marker in {"entity.create", "entity.alias"}
            and registry_marker_has_text(event.payload)
            and not valid_source_refs(event.payload)
        ):
            raise StorageError("registry append requires pinned raw source citations")
        refs = (
            dict(zip(event.payload["source_ids"], event.payload["source_versions"], strict=True))
            if marker in {"entity.create", "entity.alias"}
            and registry_marker_has_text(event.payload)
            else {}
        )

        async def body(session: AsyncClientSession) -> int:
            seq = await self._next("events", session)
            await self._records_event_fence(event, session)
            predicates = [
                {
                    "_id": rid,
                    "namespace": event.namespace,
                    "memory_type": {"$in": ["episodic", "resource"]},
                    "version": version,
                    "status": {"$in": ["activated", "archived"]},
                    "quarantined": False,
                }
                for rid, version in refs.items()
            ]
            if predicates:
                result = await self._c("records").update_many(
                    {"$or": predicates}, {"$inc": {"registry_guard": 1}}, session=session
                )
                if result.modified_count != len(refs):
                    raise StorageError("registry source changed before marker append")
            cursor = self._c("events").find(
                {"namespace": event.namespace, "kind": EventKind.REGISTRY_ERASURE_FENCE.value},
                session=session,
            )
            async for doc in cursor:
                payload = self._decode_payload(doc)
                fence_ids = payload.get("source_ids") if isinstance(payload, dict) else None
                if (
                    not isinstance(fence_ids, list)
                    or not all(isinstance(rid, str) and rid for rid in fence_ids)
                    or set(refs) & set(fence_ids)
                ):
                    raise StorageError("registry source is being erased")
            await self._c("events").insert_one(self._event_doc(event, seq), session=session)
            return seq

        seq = await self._transaction(body)
        return event.model_copy(update={"seq": seq})

    async def append_with_outbox(
        self, event: MemoryEvent, items: Sequence[OutboxItem]
    ) -> tuple[MemoryEvent, list[int]]:
        self._reject_unfenced_registry_event(event)
        seq, offsets = await self._append(event, items)
        return event.model_copy(update={"seq": seq}), offsets

    async def _append(
        self, event: MemoryEvent, items: Sequence[OutboxItem]
    ) -> tuple[int, list[int]]:
        self._fresh(event)
        events, outbox = self._c("events"), self._c("outbox")

        async def body(session: AsyncClientSession) -> tuple[int, list[int]]:
            seq = await self._next("events", session)
            await self._records_event_fence(event, session)
            await events.insert_one(self._event_doc(event, seq), session=session)
            offsets: list[int] = []
            for item in items:
                # Encoded inside the transaction: an unencodable item aborts the event too.
                encoded = json.dumps(dict(item.payload), default=str)
                scope = item.scope
                key = {
                    "outbox": True,
                    "tenant": scope.tenant,
                    "owner": scope.owner,
                    "owner_kind": scope.owner_kind,
                    "stage": item.stage,
                }
                offset = await self._next(key, session) - 1
                await outbox.insert_one(
                    {
                        "tenant": scope.tenant,
                        "owner": scope.owner,
                        "owner_kind": scope.owner_kind,
                        "stage": item.stage,
                        "offset": offset,
                        "payload": encoded,
                        "created_at": _iso(item.at or event.ts),
                    },
                    session=session,
                )
                offsets.append(offset)
            return seq, offsets

        return await self._transaction(body)

    async def outbox_items(
        self, scope: OwnerScope, stage: str, after: int = -1
    ) -> list[tuple[int, dict[str, Any]]]:
        cursor = self._c("outbox").find(
            {
                "tenant": scope.tenant,
                "owner": scope.owner,
                "owner_kind": scope.owner_kind,
                "stage": stage,
                "offset": {"$gt": after},
            },
            {"offset": 1, "payload": 1},
            sort=[("offset", ASCENDING)],
        )
        return [(int(d["offset"]), dict(json.loads(d["payload"]))) async for d in cursor]

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]:
        cursor = self._c("events").find(
            {"_id": {"$gt": after_seq}}, sort=[("_id", ASCENDING)], limit=limit
        )
        return [self._doc_to_event(doc) async for doc in cursor]

    async def read_scoped_events(
        self, namespace: str, kinds: Sequence[EventKind], *, after_seq: int, limit: int
    ) -> tuple[list[MemoryEvent], int]:
        """Bounded namespace/kind catch-up through a captured global event head."""
        latest = await self._c("events").find_one({}, {"_id": 1}, sort=[("_id", DESCENDING)])
        head = max(after_seq, int(latest["_id"]) if latest is not None else 0)
        if not kinds or limit <= 0 or head == after_seq:
            return [], head
        cursor = self._c("events").find(
            {
                "_id": {"$gt": after_seq, "$lte": head},
                "namespace": namespace,
                "kind": {"$in": [kind.value for kind in kinds]},
            },
            sort=[("_id", ASCENDING)],
            limit=limit,
        )
        return [self._doc_to_event(doc) async for doc in cursor], head

    async def prune_events(self, older_than: datetime) -> int:
        """Rolling mode only: delete events every projector applied and older than the
        cutoff; never past the slowest projector's mark. ``full`` keeps the log forever."""
        if self._mode is not EventLogMode.ROLLING:
            return 0

        async def body(session: AsyncClientSession) -> int:
            # Reset and pruning conflict before the checkpoint read. Otherwise a prune
            # could use an old positive offset after reset has rewound it to zero.
            guard = await self._c("entity_index_state").update_one(
                {"_id": "reset"}, {"$inc": {"guard": 1}}, session=session
            )
            if guard.matched_count != 1:
                raise StorageError("entity index state is not set up: call start() first")
            slowest = await self._c("offsets").find_one(
                {}, sort=[("last_seq", ASCENDING)], session=session
            )
            if slowest is None:
                return 0
            query: Doc = {
                "_id": {"$lte": int(slowest["last_seq"])},
                "ts_at": {"$lt": older_than},
                "kind": {"$ne": EventKind.REGISTRY_ERASURE_FENCE.value},
            }
            highest = await self._c("events").find_one(
                query, {"_id": 1}, sort=[("_id", DESCENDING)], session=session
            )
            result = await self._c("events").delete_many(query, session=session)
            if highest is not None and result.deleted_count:
                await self._c("entity_index_state").update_one(
                    {"_id": "reset"},
                    {"$max": {"pruned_through": int(highest["_id"])}},
                    session=session,
                )
            return int(result.deleted_count)

        return await self._transaction(body)

    # ---------------------------------------------------------------- offsets
    async def get_offset(self, projector_name: str) -> int:
        doc = await self._c("offsets").find_one({"_id": projector_name})
        return int(doc["last_seq"]) if doc is not None else 0

    async def set_offset(self, projector_name: str, seq: int) -> None:
        """Advance-only (``$max``): a lower or concurrent lower checkpoint never wins."""
        if projector_name == "entity_index":
            raise StorageError("entity index checkpoint requires the ordered projection door")
        if projector_name == "records":

            async def body(session: AsyncClientSession) -> None:
                reset, cursor = await self._records_guard(session)
                await self._records_register(session, reset, cursor)
                if seq > cursor:
                    raise StorageError("records checkpoint requires the ordered projection door")
                await self._c("offsets").update_one(
                    {"_id": "records"}, {"$max": {"last_seq": int(seq)}}, session=session
                )

            await self._transaction(body)
            return
        update = {"$max": {"last_seq": int(seq)}, "$set": {"updated_at": _iso(datetime.now(UTC))}}
        try:
            await self._c("offsets").update_one({"_id": projector_name}, update, upsert=True)
        except DuplicateKeyError:  # two first checkpoints raced the insert: now it exists
            await self._c("offsets").update_one({"_id": projector_name}, update)

    async def reset_offset(self, projector_name: str) -> None:
        if projector_name == "entity_index":
            await self.reset_entity_index()
            return
        if projector_name == "records":
            await self.reset_records_projection()
            return
        await self._c("offsets").delete_one({"_id": projector_name})

    # ---------------------------------------------------------------- read model
    @staticmethod
    def _record_fields(record: MemoryRecord) -> Doc:
        from memspine.store.claim_cardinality import group_key, record_group_of

        group = record_group_of(record)
        return {
            "namespace": record.namespace,
            "claim_group_key": group_key(group) if group is not None else None,
            "memory_type": record.memory_type,
            "group_id": record.group_id,
            "entity": record.entity,
            "attribute": record.attribute,
            "open": record.valid_to is None,
            "status": record.status.value,
            "version": record.version,
            "quarantined": bool(record.quarantined),
            "layer": record.layer,
            "recorded_key": _sort_key(record.recorded_at),
            "session_key": record.source.message_id,
            "source_role": record.source.role,
            "parents": [str(p) for p in (record.source.parents or [])],
            # The whole record as its own JSON (bytes as base64, big ints exact): lossless.
            "doc": record.model_dump_json(),
        }

    @staticmethod
    def _doc_to_record(doc: Doc) -> MemoryRecord:
        return MemoryRecord.model_validate_json(doc["doc"])

    async def upsert_record(self, record: MemoryRecord) -> None:
        async def body(session: AsyncClientSession) -> None:
            await self._records_guard(session)
            if await self._c("records_append_fence").find_one(
                {"_id": record.record_id}, {"_id": 1}, session=session
            ):
                raise StorageError("record id is permanently erased")
            owner = await self._c("records_append_identity").find_one(
                {"_id": record.record_id}, {"namespace": 1}, session=session
            )
            if owner is not None and owner["namespace"] != record.namespace:
                raise StorageError("record id belongs to another namespace")
            existing = await self._c("records").find_one(
                {"_id": record.record_id}, {"namespace": 1}, session=session
            )
            if existing is not None and existing["namespace"] != record.namespace:
                raise StorageError("record id belongs to another namespace")
            await self._c("records").update_one(
                {"_id": record.record_id},
                {"$set": self._record_fields(record), "$setOnInsert": {"order": ObjectId()}},
                upsert=True,
                session=session,
            )

        await self._transaction(body)

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        doc = await self._c("records").find_one({"_id": record_id}, {"doc": 1})
        return self._doc_to_record(doc) if doc is not None else None

    async def get_records(self, record_ids: Sequence[str]) -> dict[str, MemoryRecord]:
        """RM-115: one ``$in`` query (missing ids absent; duplicates read once)."""
        ids = list(dict.fromkeys(str(r) for r in record_ids if r))
        if not ids:
            return {}
        cursor = self._c("records").find({"_id": {"$in": ids}}, {"doc": 1})
        return {str(doc["_id"]): self._doc_to_record(doc) async for doc in cursor}

    async def claim_group_page(
        self, namespace: str, group_key: str, *, after_id: str | None, limit: int
    ) -> list[MemoryRecord]:
        """One indexed current-claim page for the public visibility overlay."""
        if limit <= 0:
            return []
        query: Doc = {"namespace": namespace, "claim_group_key": group_key}
        if after_id is not None:
            query["_id"] = {"$gt": after_id}
        found = self._c("records").find(query, {"doc": 1}, sort=[("_id", ASCENDING)], limit=limit)
        return [self._doc_to_record(doc) async for doc in found]

    async def claim_group_snapshot(self, namespace: str, group_key: str) -> list[MemoryRecord]:
        """Read one complete indexed, currently visible group from one snapshot."""

        async def body(session: AsyncClientSession) -> list[MemoryRecord]:
            found = self._c("records").find(
                {"namespace": namespace, "claim_group_key": group_key},
                {"_id": 1, "doc": 1},
                sort=[("_id", ASCENDING)],
                session=session,
            )
            rows = [doc async for doc in found]
            if not rows:
                return []
            ids = [str(doc["_id"]) for doc in rows]
            excluded = await self._hidden_or_erased_ids(session, namespace, ids)
            return [self._doc_to_record(doc) for doc in rows if str(doc["_id"]) not in excluded]

        return await self._transaction(body)

    async def _hidden_or_erased_ids(
        self, session: AsyncClientSession, namespace: str, record_ids: Sequence[str]
    ) -> set[str]:
        found: set[str] = set()
        unique = list(dict.fromkeys(str(rid) for rid in record_ids if rid))
        for start in range(0, len(unique), 512):
            ids = unique[start : start + 512]
            membership = self._c("records_projection_membership").find(
                {
                    "namespace": namespace,
                    "record_id": {"$in": ids},
                    "kind": {"$in": ["erased", "hidden"]},
                },
                {"record_id": 1},
                session=session,
            )
            async for doc in membership:
                found.add(str(doc["record_id"]))
            fences = self._c("records_append_fence").find(
                {"_id": {"$in": ids}}, {"_id": 1}, session=session
            )
            async for doc in fences:
                found.add(str(doc["_id"]))
        return found

    async def hidden_or_erased_record_ids(
        self, namespace: str, record_ids: Sequence[str]
    ) -> set[str]:
        """ID-only negative membership for a historical population in one snapshot."""
        if not namespace:
            raise ValueError("record visibility namespace must be nonempty")

        async def body(session: AsyncClientSession) -> set[str]:
            return await self._hidden_or_erased_ids(session, namespace, record_ids)

        return await self._transaction(body)

    async def _find_records(self, query: Doc) -> list[MemoryRecord]:
        cursor = self._c("records").find(query, {"doc": 1}, sort=[("order", ASCENDING)])
        return [self._doc_to_record(doc) async for doc in cursor]

    async def list_records(
        self, namespace: str, memory_type: str | None = None, group_id: str | None = None
    ) -> list[MemoryRecord]:
        query: Doc = {"namespace": namespace}
        if memory_type is not None:
            query["memory_type"] = memory_type
        if group_id is not None:
            query["group_id"] = group_id
        return await self._find_records(query)

    async def list_namespaces(self) -> list[str]:
        return sorted(str(ns) for ns in await self._c("records").distinct("namespace"))

    async def find_active_fact(
        self, namespace: str, entity: str, attribute: str
    ) -> MemoryRecord | None:
        """Semantic only, open validity, not deleted, not quarantined, latest recorded."""
        doc = await self._c("records").find_one(
            {
                "namespace": namespace,
                "memory_type": "semantic",
                "entity": entity,
                "attribute": attribute,
                "open": True,
                "status": {"$ne": "deleted"},
                "quarantined": False,
            },
            {"doc": 1},
            sort=[("recorded_key", DESCENDING)],
        )
        return self._doc_to_record(doc) if doc is not None else None

    async def find_active_untyped_fact(
        self, namespace: str, entity: str, attribute: str
    ) -> MemoryRecord | None:
        """The latest keyed semantic incumbent outside a comparable claim group."""
        doc = await self._c("records").find_one(
            {
                "namespace": namespace,
                "memory_type": "semantic",
                "entity": entity,
                "attribute": attribute,
                "claim_group_key": None,  # Mongo matches both null and missing legacy rows.
                "open": True,
                "status": {"$ne": "deleted"},
                "quarantined": False,
            },
            {"doc": 1},
            sort=[("recorded_key", DESCENDING)],
        )
        return self._doc_to_record(doc) if doc is not None else None

    async def list_session_records(
        self, namespace: str, session_key: str, roles: Sequence[str] | None = None
    ) -> list[MemoryRecord]:
        query: Doc = {"namespace": namespace, "session_key": session_key}
        if roles is not None:
            query["source_role"] = {"$in": list(roles)}
        return await self._find_records(query)

    async def delete_all_records(self) -> None:
        async def body(session: AsyncClientSession) -> None:
            await self._records_guard(session)
            await self._c("records").delete_many({}, session=session)

        await self._transaction(body)

    async def quarantined_ids(self, record_ids: Sequence[str]) -> set[str]:
        ids = list(dict.fromkeys(str(r) for r in record_ids if r))
        if not ids:
            return set()
        cursor = self._c("records").find({"_id": {"$in": ids}, "quarantined": True}, {"_id": 1})
        return {str(doc["_id"]) async for doc in cursor}

    async def recent_contents(self, namespace: str, limit: int) -> list[str]:
        """The newest ``limit`` live contents, oldest first (ties in insertion order)."""
        if limit <= 0:
            return []
        cursor = self._c("records").find(
            {"namespace": namespace, "quarantined": False},
            {"doc": 1},
            sort=[("recorded_key", DESCENDING), ("order", DESCENDING)],
            limit=limit,
        )
        newest = [str(orjson.loads(doc["doc"])["content"]) async for doc in cursor]
        return newest[::-1]

    async def layer_counts(self, namespace: str) -> dict[str, int]:
        live: Doc = {
            "namespace": namespace,
            "status": {"$in": list(_LIVE_STATUSES)},
            "quarantined": False,
        }
        records = self._c("records")
        cursor = await records.aggregate(
            [
                {"$match": {**live, "layer": {"$type": "string"}}},
                {"$group": {"_id": "$layer", "n": {"$sum": 1}}},
            ]
        )
        counts = {str(doc["_id"]): int(doc["n"]) async for doc in cursor}
        # A document written before the layer field existed counts on the layer it hydrates to.
        async for doc in records.find({**live, "layer": {"$exists": False}}, {"doc": 1}):
            layer = self._doc_to_record(doc).layer
            counts[layer] = counts.get(layer, 0) + 1
        return counts

    async def count_quarantined(self, namespace: str) -> int:
        return int(
            await self._c("records").count_documents({"namespace": namespace, "quarantined": True})
        )

    async def list_quarantined(self, namespace: str) -> list[MemoryRecord]:
        return await self._find_records({"namespace": namespace, "quarantined": True})

    # ---------------------------------------------------------------- lineage
    async def add_lineage(self, edges: Sequence[LineageEdge], namespace: str | None = None) -> int:
        """Insert edges; an existing (child, parent, role) is left alone. Returns edges added."""
        if not edges:
            return 0
        top = await self._next("lineage", by=len(edges))  # reserve an insertion-order block
        docs = [
            {
                "child": e.child,
                "parent": e.parent,
                "role": e.role,
                "namespace": namespace,
                "order": top - len(edges) + i + 1,
                "edge": e.model_dump_json(),
            }
            for i, e in enumerate(edges)
        ]
        try:
            result = await self._c("lineage").insert_many(docs, ordered=False)
        except BulkWriteError as exc:
            errors = exc.details.get("writeErrors", [])
            if any(err.get("code") != 11000 for err in errors):
                raise
            return int(exc.details.get("nInserted", 0))
        return len(result.inserted_ids)

    async def _edges(
        self, field: str, ids: Sequence[str], roles: Sequence[str] | None
    ) -> dict[str, list[LineageEdge]]:
        query: Doc = {field: {"$in": list(ids)}}
        if roles is not None:
            query["role"] = {"$in": list(roles)}
        out: dict[str, list[LineageEdge]] = {}
        cursor = self._c("lineage").find(query, {"edge": 1, field: 1}, sort=[("order", 1)])
        async for doc in cursor:
            out.setdefault(str(doc[field]), []).append(LineageEdge.model_validate_json(doc["edge"]))
        return out

    async def lineage_parents(
        self, child_ids: Sequence[str], roles: Sequence[str] | None = None
    ) -> dict[str, list[LineageEdge]]:
        return await self._edges("child", child_ids, roles)

    async def lineage_children(
        self, parent_ids: Sequence[str], roles: Sequence[str] | None = None
    ) -> dict[str, list[LineageEdge]]:
        return await self._edges("parent", parent_ids, roles)

    async def delete_lineage(self, edges: Sequence[LineageEdge]) -> int:
        if not edges:
            return 0
        result = await self._c("lineage").delete_many(
            {"$or": [{"child": e.child, "parent": e.parent, "role": e.role} for e in edges]}
        )
        return int(result.deleted_count)

    # ---------------------------------------------------------------- erasure
    async def _rewrite_events(
        self, change: Callable[[Any], bool], *, namespace: str | None = None
    ) -> list[int]:
        """Rewrite every event whose decoded payload ``change`` mutates (adding ``redacted``).

        Each rewrite is a compare-and-set on the old payload bytes (single-document atomic),
        so the pass is safe beside appenders and idempotent when re-run after a crash; the
        seq, kind and original fingerprint never change."""
        events = self._c("events")
        rewritten: list[int] = []
        query = {"namespace": namespace} if namespace is not None else {}
        async for doc in events.find(query, sort=[("_id", ASCENDING)]):
            payload = self._decode_payload(doc)
            if not change(payload):
                continue
            payload["redacted"] = True
            encoded = canonical_payload(payload)
            if doc["compressed"]:
                encoded = self._cctx.compress(encoded)
            result = await events.update_one(
                {"_id": doc["_id"], "payload": doc["payload"]}, {"$set": {"payload": encoded}}
            )
            if result.modified_count:
                rewritten.append(int(doc["_id"]))
        return rewritten

    async def redact_event_payloads(
        self, record_id: str | Sequence[str], node_renames: Mapping[str, str] | None = None
    ) -> list[int]:
        """M7 erasure, the one sanctioned log mutation (see ``SqlStorage``)."""
        record_ids = [record_id] if isinstance(record_id, str) else list(record_id)

        def change(payload: Any) -> bool:
            changed = False
            for rid in record_ids:
                changed |= redact_record(payload, rid)
            if node_renames:
                changed |= scrub_partition_nodes(payload, node_renames)
            return changed

        return await self._rewrite_events(change)

    async def scrub_marker_payloads(
        self, marker: str, scrub: Callable[[dict[str, Any]], bool], *, namespace: str
    ) -> list[int]:
        """FU-CLOSE marker scrub, as :meth:`SqlStorage.scrub_marker_payloads`."""
        events = self._c("events")
        rewritten: list[int] = []
        query = {"namespace": namespace, "kind": EventKind.MARKER.value}
        async for doc in events.find(query, sort=[("_id", ASCENDING)]):
            payload = self._decode_payload(doc)
            if (
                not isinstance(payload, dict)
                or payload.get("marker") != marker
                or not scrub(payload)
            ):
                continue
            payload["redacted"] = True
            encoded = canonical_payload(payload)
            if doc["compressed"]:
                encoded = self._cctx.compress(encoded)
            result = await events.update_one(
                {"_id": doc["_id"], "payload": doc["payload"]}, {"$set": {"payload": encoded}}
            )
            if result.modified_count:
                rewritten.append(int(doc["_id"]))
        return rewritten

    async def delete_record(self, record_id: str) -> None:
        async def body(session: AsyncClientSession) -> None:
            await self._records_guard(session)
            await self._c("records").delete_one({"_id": record_id}, session=session)

        await self._transaction(body)

    async def list_children(self, namespace: str, parent_ids: Sequence[str]) -> list[MemoryRecord]:
        if not parent_ids:
            return []
        return await self._find_records(
            {"namespace": namespace, "parents": {"$in": [str(p) for p in parent_ids]}}
        )
