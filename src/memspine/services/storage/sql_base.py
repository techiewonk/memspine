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
for files / ``create_all`` for ``:memory:``; Postgres runs ``create_all``) and
``manifest`` (the backend name). The event-log modes (D-45), zstd payload
compression (D-45/D-32), and M7 erasure all live here unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

import orjson
import zstandard
from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from memspine.config.constants import ZSTD_LEVEL
from memspine.core.erasure import redact_record
from memspine.core.events import EventKind, EventLogMode, MemoryEvent, canonical_payload
from memspine.core.records import MemoryRecord
from memspine.exceptions import StorageError
from memspine.services.base import CapabilityManifest, ServiceAdapter
from memspine.services.storage.sqlite.schema import (
    memory_events,
    memory_records,
    projector_offsets,
)

__all__ = ["SqlClient", "SqlStorage"]


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


class SqlClient(Protocol):
    """The one capability the storage base needs from its client (D-24)."""

    @property
    def engine(self) -> AsyncEngine: ...

    async def health(self) -> bool: ...


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
        self._ephemeral_offsets: dict[str, int] = {}
        self._cctx = zstandard.ZstdCompressor(level=ZSTD_LEVEL)
        self._dctx = zstandard.ZstdDecompressor()

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
        return self._started and await self._client.health()

    @property
    def mode(self) -> EventLogMode:
        return self._mode

    @property
    def can_rebuild(self) -> bool:
        """Ephemeral never rebuilds; rolling rebuilds while its window is intact
        (the window check happens at rebuild time, where the log can be read)."""
        return self._mode is not EventLogMode.EPHEMERAL

    # ── event log: the single write door ─────────────────────────────────────

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

        stmt = self._insert(memory_events).values(
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
        # Both dialects return the generated identity via RETURNING (Postgres
        # natively; SQLite via SQLAlchemy's rowid fetch) in inserted_primary_key.
        async with self._client.engine.begin() as conn:
            result = await conn.execute(stmt)
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
            "corroborations": record.corroborations,
            "skill_stage": record.skill_stage,
            "reflection_depth": record.reflection_depth,
        }
        stmt = self._insert(memory_records).values(record_id=record.record_id, **values)
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

    async def delete_all_records(self) -> None:
        """Projector rebuild support: drop the read model, keep the log."""
        async with self._client.engine.begin() as conn:
            await conn.execute(delete(memory_records))

    async def delete_record(self, record_id: str) -> None:
        """M7 hard-delete: remove one row from the read model (projector-driven
        by a FORGET(hard) event; the log side is ``redact_event_payloads``)."""
        async with self._client.engine.begin() as conn:
            await conn.execute(
                delete(memory_records).where(memory_records.c.record_id == record_id)
            )

    async def redact_event_payloads(self, record_id: str) -> list[int]:
        """M7 erasure: the ONE sanctioned mutation of the log.

        Rewrites every event payload carrying a snapshot of ``record_id`` so
        its ``content``/``content_zstd`` are emptied and a ``redacted`` marker
        is set — the event's seq, kind, and fingerprint (of the ORIGINAL
        payload) stay, so ordering and audit chains survive while the erased
        content is unrecoverable (GDPR erasure in an append-only design).
        Replay of a redacted WRITE materializes an empty-content row, which the
        subsequent FORGET(hard) event then removes — rebuild stays clean.
        """
        if self._mode is EventLogMode.EPHEMERAL:
            return []
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
                if not redact_record(payload, record_id):
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
            }
        )
