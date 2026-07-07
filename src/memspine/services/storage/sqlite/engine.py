"""SQLite storage service: the single write door (D0.1) over SQLAlchemy Core.

Implements :class:`~memspine.services.storage.base.StorageService` with the
D-45 event-log modes:

- ``full``     — every event persisted forever (default),
- ``rolling``  — events prunable once every projector has applied them and they
                 age past the retention window,
- ``ephemeral``— events get a monotonic in-memory ``seq`` and flow to projectors
                 but are never written; rebuild is unavailable.

Payloads are canonical orjson (D-38), optionally zstd-compressed at rest (D-45,
reusing the D-32 dependency). The service never opens a connection: it consumes
an injected :class:`~memspine.clients.sqlite.SQLiteClient` (D-24).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import orjson
import zstandard
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from memspine.clients.sqlite import SQLiteClient
from memspine.config.constants import ZSTD_LEVEL
from memspine.core.events import EventKind, EventLogMode, MemoryEvent, canonical_payload
from memspine.core.records import MemoryRecord
from memspine.exceptions import StorageError
from memspine.services.base import CapabilityManifest, ServiceAdapter
from memspine.services.storage.sqlite.migrations import ensure_schema
from memspine.services.storage.sqlite.schema import (
    memory_events,
    memory_records,
    metadata,
    projector_offsets,
)

__all__ = ["SQLiteStorage"]


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


class SQLiteStorage(ServiceAdapter):
    """Event log + relational read model in one SQLite database."""

    def __init__(
        self,
        client: SQLiteClient,
        mode: EventLogMode = EventLogMode.FULL,
        compress: bool = False,
    ) -> None:
        self._client = client
        self._mode = mode
        self._compress = compress
        self._started = False
        # Ephemeral mode (D-45): monotonic seq + projector offsets live purely
        # in memory — persisting either would poison a later full/rolling run
        # on the same database file with high-water marks no event backs.
        self._ephemeral_seq = 0
        self._ephemeral_offsets: dict[str, int] = {}
        self._cctx = zstandard.ZstdCompressor(level=ZSTD_LEVEL)
        self._dctx = zstandard.ZstdDecompressor()

    @property
    def manifest(self) -> CapabilityManifest:
        return CapabilityManifest(
            name="sqlite",
            capability="storage",
            provides=("event_log", "records", "projector_offsets"),
        )

    @property
    def mode(self) -> EventLogMode:
        return self._mode

    @property
    def can_rebuild(self) -> bool:
        """Ephemeral never rebuilds; rolling rebuilds while its window is intact
        (the window check happens at rebuild time, where the log can be read)."""
        return self._mode is not EventLogMode.EPHEMERAL

    async def start(self) -> None:
        """Bring the schema to head: Alembic for file-backed databases (the real
        migration path), ``create_all`` only for ``:memory:`` engines, which a
        separate migration connection cannot reach."""
        if self._client.is_memory:
            async with self._client.engine.begin() as conn:
                await conn.run_sync(metadata.create_all)
        else:
            await asyncio.to_thread(ensure_schema, self._client.path)
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def health(self) -> bool:
        return self._started and await self._client.health()

    # ── event log: the single write door ────────────────────────────────────

    async def append_event(self, event: MemoryEvent) -> MemoryEvent:
        if event.seq is not None:
            raise StorageError("event already has a seq — events pass the write door once")

        if self._mode is EventLogMode.EPHEMERAL:
            self._ephemeral_seq += 1
            return event.model_copy(update={"seq": self._ephemeral_seq})

        payload = canonical_payload(event.payload)  # same encoding the fingerprint hashed
        compressed = self._compress
        if compressed:
            payload = self._cctx.compress(payload)

        async with self._client.engine.begin() as conn:
            result = await conn.execute(
                sqlite_insert(memory_events).values(
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
            )
        pk = result.inserted_primary_key
        if pk is None:
            raise StorageError("insert into memory_events returned no primary key")
        return event.model_copy(update={"seq": int(pk[0])})

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
        if self._mode is EventLogMode.EPHEMERAL:
            current = self._ephemeral_offsets.get(projector_name, 0)
            self._ephemeral_offsets[projector_name] = max(current, seq)
            return
        now = _iso(datetime.now(UTC))
        stmt = sqlite_insert(projector_offsets).values(
            projector_name=projector_name, last_seq=seq, updated_at=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["projector_name"],
            set_={
                "last_seq": func.max(stmt.excluded.last_seq, projector_offsets.c.last_seq),
                "updated_at": now,
            },
        )
        async with self._client.engine.begin() as conn:
            await conn.execute(stmt)

    async def reset_offset(self, projector_name: str) -> None:
        """Rebuild support: the one sanctioned way to move a high-water mark back."""
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
            min_applied = (
                await conn.execute(select(func.min(projector_offsets.c.last_seq)))
            ).scalar()
            if min_applied is None:
                return 0  # no projector has checkpointed yet — keep everything
            result = await conn.execute(
                delete(memory_events).where(
                    memory_events.c.seq <= int(min_applied),
                    memory_events.c.ts < _iso(older_than),
                )
            )
        return int(result.rowcount or 0)

    # ── relational read model ────────────────────────────────────────────────

    async def upsert_record(self, record: MemoryRecord) -> None:
        values = {
            "namespace": record.namespace,
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
        }
        stmt = sqlite_insert(memory_records).values(record_id=record.record_id, **values)
        stmt = stmt.on_conflict_do_update(index_elements=["record_id"], set_=values)
        async with self._client.engine.begin() as conn:
            await conn.execute(stmt)

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        stmt = select(memory_records).where(memory_records.c.record_id == record_id)
        async with self._client.engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        return self._row_to_record(row._mapping) if row is not None else None

    async def list_namespaces(self) -> list[str]:
        """Every namespace with at least one materialized record — the sweep
        surface for the lifecycle pipelines (M2/M3/M6)."""
        stmt = select(memory_records.c.namespace).distinct().order_by(memory_records.c.namespace)
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [str(row[0]) for row in rows]

    async def list_records(
        self, namespace: str, memory_type: str | None = None
    ) -> list[MemoryRecord]:
        stmt = select(memory_records).where(memory_records.c.namespace == namespace)
        if memory_type is not None:
            stmt = stmt.where(memory_records.c.memory_type == memory_type)
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [self._row_to_record(row._mapping) for row in rows]

    async def find_active_fact(
        self, namespace: str, entity: str, attribute: str
    ) -> MemoryRecord | None:
        """The currently-valid record for one (entity, attribute) key (M4):
        open validity (valid_to IS NULL), not deleted, latest recorded_at."""
        stmt = (
            select(memory_records)
            .where(
                memory_records.c.namespace == namespace,
                memory_records.c.entity == entity,
                memory_records.c.attribute == attribute,
                memory_records.c.valid_to.is_(None),
                memory_records.c.status != "deleted",
            )
            .order_by(memory_records.c.recorded_at.desc())
            .limit(1)
        )
        async with self._client.engine.connect() as conn:
            row = (await conn.execute(stmt)).first()
        return self._row_to_record(row._mapping) if row is not None else None

    async def delete_all_records(self) -> None:
        """Projector rebuild support: drop the read model, keep the log."""
        async with self._client.engine.begin() as conn:
            await conn.execute(delete(memory_records))

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
            }
        )
