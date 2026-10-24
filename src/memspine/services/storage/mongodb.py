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
from memspine.exceptions import ConfigError, StorageError
from memspine.observability.logging import get_logger
from memspine.schemas.durable.lineage import LineageEdge
from memspine.services.base import CapabilityManifest, ServiceAdapter
from memspine.services.storage.records_port import (
    ProjectionTargetBinding,
    binding_source_name,
    require_binding_match,
)
from memspine.store.events import EventKind, EventLogMode, MemoryEvent, canonical_payload
from memspine.store.leases import NamespaceLease
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
    ("records", [("namespace", 1), ("session_key", 1)], {"name": "ns_session"}),
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
        seq, _ = await self._append(event, ())
        return event.model_copy(update={"seq": seq})

    async def append_with_outbox(
        self, event: MemoryEvent, items: Sequence[OutboxItem]
    ) -> tuple[MemoryEvent, list[int]]:
        seq, offsets = await self._append(event, items)
        return event.model_copy(update={"seq": seq}), offsets

    async def _append(
        self, event: MemoryEvent, items: Sequence[OutboxItem]
    ) -> tuple[int, list[int]]:
        self._fresh(event)
        events, outbox = self._c("events"), self._c("outbox")

        async def body(session: AsyncClientSession) -> tuple[int, list[int]]:
            seq = await self._next("events", session)
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

    async def prune_events(self, older_than: datetime) -> int:
        """Rolling mode only: delete events every projector applied and older than the
        cutoff; never past the slowest projector's mark. ``full`` keeps the log forever."""
        if self._mode is not EventLogMode.ROLLING:
            return 0
        slowest = await self._c("offsets").find_one({}, sort=[("last_seq", ASCENDING)])
        if slowest is None:
            return 0  # no projector has checkpointed yet - keep everything
        result = await self._c("events").delete_many(
            {"_id": {"$lte": int(slowest["last_seq"])}, "ts_at": {"$lt": older_than}}
        )
        return int(result.deleted_count)

    # ---------------------------------------------------------------- offsets
    async def get_offset(self, projector_name: str) -> int:
        doc = await self._c("offsets").find_one({"_id": projector_name})
        return int(doc["last_seq"]) if doc is not None else 0

    async def set_offset(self, projector_name: str, seq: int) -> None:
        """Advance-only (``$max``): a lower or concurrent lower checkpoint never wins."""
        update = {"$max": {"last_seq": int(seq)}, "$set": {"updated_at": _iso(datetime.now(UTC))}}
        try:
            await self._c("offsets").update_one({"_id": projector_name}, update, upsert=True)
        except DuplicateKeyError:  # two first checkpoints raced the insert: now it exists
            await self._c("offsets").update_one({"_id": projector_name}, update)

    async def reset_offset(self, projector_name: str) -> None:
        await self._c("offsets").delete_one({"_id": projector_name})

    # ---------------------------------------------------------------- read model
    @staticmethod
    def _record_fields(record: MemoryRecord) -> Doc:
        return {
            "namespace": record.namespace,
            "memory_type": record.memory_type,
            "group_id": record.group_id,
            "entity": record.entity,
            "attribute": record.attribute,
            "open": record.valid_to is None,
            "status": record.status.value,
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
        await self._c("records").update_one(
            {"_id": record.record_id},
            {"$set": self._record_fields(record), "$setOnInsert": {"order": ObjectId()}},
            upsert=True,
        )

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

    async def list_session_records(
        self, namespace: str, session_key: str, roles: Sequence[str] | None = None
    ) -> list[MemoryRecord]:
        query: Doc = {"namespace": namespace, "session_key": session_key}
        if roles is not None:
            query["source_role"] = {"$in": list(roles)}
        return await self._find_records(query)

    async def delete_all_records(self) -> None:
        await self._c("records").delete_many({})

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
    async def _rewrite_events(self, change: Callable[[Any], bool]) -> list[int]:
        """Rewrite every event whose decoded payload ``change`` mutates (adding ``redacted``).

        Each rewrite is a compare-and-set on the old payload bytes (single-document atomic),
        so the pass is safe beside appenders and idempotent when re-run after a crash; the
        seq, kind and original fingerprint never change."""
        events = self._c("events")
        rewritten: list[int] = []
        async for doc in events.find({}, sort=[("_id", ASCENDING)]):
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
        self, marker: str, scrub: Callable[[dict[str, Any]], bool]
    ) -> list[int]:
        """FU-CLOSE marker scrub, as :meth:`SqlStorage.scrub_marker_payloads`."""

        def change(payload: Any) -> bool:
            return isinstance(payload, dict) and payload.get("marker") == marker and scrub(payload)

        return await self._rewrite_events(change)

    async def delete_record(self, record_id: str) -> None:
        await self._c("records").delete_one({"_id": record_id})

    async def list_children(self, namespace: str, parent_ids: Sequence[str]) -> list[MemoryRecord]:
        if not parent_ids:
            return []
        return await self._find_records(
            {"namespace": namespace, "parents": {"$in": [str(p) for p in parent_ids]}}
        )
