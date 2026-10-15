"""Migration by replay of the retained event log (F-16b; M-01, M-26, G-23, ER-27 C, ER-33).

The v1 event log stays the truth. :class:`V1Upcaster` lifts each stored event to the current event
schema version with pure, registered upcasters (integer versions, the ``schemas.versioning`` registry).
Nothing at runtime reads an old shape: this module is the only place that sees a v1 event, and the source
store is never written. :class:`ReplayBuilder` writes the upcast log into a **fresh** store and replays
the projections that can be rebuilt from the log alone.

Limited rebuild (ER-27 C) is advertised, not hidden: :attr:`MigrationReport.not_rebuilt` names every
derived layer that replay does not regenerate (vectors, lexical index, graph, model-derived layers), and
a log that a rolling window already pruned, or an ephemeral one, is refused with a clear error.

Event versions: every kind is at v1 except ``memory.write`` (and the legacy full-snapshot
``memory.decay_transition``). Their v2 payload carries the record envelope fields (``layer``,
``type_tag``, ``provenance_class``) explicitly instead of leaving them to be re-derived; v3 (LF-E, MF-05)
moves a record written by a derived-layer producer back onto the layer its channel proves.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import orjson

from memspine.exceptions import RebuildUnavailableError
from memspine.observability import tap
from memspine.schemas.versioning import (
    declare_kind,
    latest_version,
    registered_versions,
    upcast,
    upcaster,
)
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sql_base import SqlStorage
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.layer_classify import LAYER_UPCAST_VERSION, corrected_layer
from memspine.store.layers import derive_layer, derive_provenance_class
from memspine.store.replay import catch_up

__all__ = [
    "CURRENT_EVENT_VERSION",
    "NOT_REBUILT_LAYERS",
    "MigrationReport",
    "ReplayBuilder",
    "V1Upcaster",
    "dump_store",
    "ensure_event_upcasters",
    "event_kind_id",
    "migrate_sqlite",
]

#: Layers a replay does not regenerate (limited rebuild advertised, ER-27 C / G-23).
NOT_REBUILT_LAYERS: dict[str, str] = {
    "vector index": "embeddings are re-created by the first start of an engine on the new store",
    "lexical index": "rebuilt by an engine start, not by this replay",
    "graph (L3)": "graph edges and communities are model- or rule-derived; re-derived on demand",
    "model-derived records (L2, L4, L5, unlogged L1)": (
        "only records present as logged writes are restored; a worker model re-derives the rest"
    ),
    "caches": "v1 caches are not migrated (G-23)",
}

#: Latest event schema version per kind; absent kinds are at 1.
_LATEST: dict[EventKind, int] = {EventKind.WRITE: 3, EventKind.DECAY_TRANSITION: 3}
CURRENT_EVENT_VERSION = max(_LATEST.values())


def event_kind_id(kind: EventKind) -> str:
    """Registry key of an event kind (kept apart from durable model kinds)."""
    return f"event:{kind.value}"


def _explicit_envelope(record: dict[str, Any]) -> dict[str, Any]:
    memory_type = str(record.get("memory_type", ""))
    source = record.get("source") or {}
    channel = str(source.get("channel", "internal"))
    role = str(source.get("role", "system"))
    out = dict(record)
    out.setdefault("layer", derive_layer(memory_type, channel))
    out.setdefault("type_tag", memory_type)
    out.setdefault(
        "provenance_class",
        derive_provenance_class(role, channel, bool(source.get("parents"))),
    )
    return out


def _upcast_snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if isinstance(out.get("record"), dict):
        out["record"] = _explicit_envelope(out["record"])
    return out


def _upcast_producer_layer(payload: dict[str, Any]) -> dict[str, Any]:
    """v2 -> v3 (LF-E, MF-05): put a producer's record back on the layer its channel proves.

    A record whose ``source.channel`` is a derived-layer producer (``layer_classify.PRODUCER_LAYER``:
    session_close, consolidation, multires -> L2; entity_summary, community_summary, reorganize -> L3;
    observation -> L4; standing -> L5) and whose stored layer is missing, L0 or L1 gets the producer's
    layer. Anything else, every other L0 record included, is untouched. Pure and deterministic, so a
    re-run on the same log gives the same result; each correction is reported on the ``migrate.layer_fix``
    tap with the version name :data:`LAYER_UPCAST_VERSION`.

    Rollback: the source log is never written (G-23), so the correction exists only in the migrated copy.
    To undo it, delete the migrated store and keep using the source, or re-migrate with the v2 -> v3
    registration removed (``_LATEST`` back to 2). The ``migrate.layer_fix`` events list every record that
    changed (``was`` and ``now``), so the change can also be reversed record by record."""
    record = payload.get("record")
    if not isinstance(record, dict):
        return dict(payload)
    source = record.get("source")
    channel = source.get("channel") if isinstance(source, dict) else None
    fixed = corrected_layer(record.get("layer"), channel)
    if fixed is None:
        return dict(payload)
    tap.event(
        "migrate.layer_fix",
        version=LAYER_UPCAST_VERSION,
        record_id=str(record.get("record_id", "")),
        channel=str(channel),
        was=str(record.get("layer")),
        now=fixed,
    )
    out = dict(payload)
    out["record"] = {**record, "layer": fixed}
    return out


def ensure_event_upcasters() -> None:
    """Register the event upcasters once (idempotent; survives a registry reset in tests)."""
    for kind in EventKind:
        declare_kind(event_kind_id(kind), _LATEST.get(kind, 1))
    for kind in (EventKind.WRITE, EventKind.DECAY_TRANSITION):
        registered = registered_versions(event_kind_id(kind))
        if 1 not in registered:
            upcaster(event_kind_id(kind), 1)(_upcast_snapshot_payload)
        if 2 not in registered:
            upcaster(event_kind_id(kind), 2)(_upcast_producer_layer)


class V1Upcaster:
    """Pure upcasting of stored events to the current version (migration time only, no runtime shim)."""

    def __init__(self) -> None:
        ensure_event_upcasters()

    @staticmethod
    def latest(kind: EventKind) -> int:
        return latest_version(event_kind_id(kind))

    def upcast_payload(
        self, kind: EventKind, version: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        stamped = dict(payload)
        stamped["schema_version"] = version
        out = upcast(event_kind_id(kind), stamped)
        out.pop("schema_version", None)
        return out

    def upcast_event(self, event: MemoryEvent) -> MemoryEvent:
        """The event at its kind's latest version; an already-current event is returned unchanged."""
        target = self.latest(event.kind)
        if event.schema_version == target:
            return event
        payload = self.upcast_payload(event.kind, event.schema_version, event.payload)
        return MemoryEvent(
            event_id=event.event_id,
            seq=None,
            kind=event.kind,
            namespace=event.namespace,
            ts=event.ts,
            actor=event.actor,
            schema_version=target,
            payload=payload,
        )


@dataclass
class MigrationReport:
    """What a replay did and what it did not rebuild."""

    events_read: int = 0
    events_upcast: int = 0
    events_written: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    projected: dict[str, int] = field(default_factory=dict)
    not_rebuilt: dict[str, str] = field(default_factory=lambda: dict(NOT_REBUILT_LAYERS))
    limited_rebuild: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "events_read": self.events_read,
            "events_upcast": self.events_upcast,
            "events_written": self.events_written,
            "by_kind": dict(sorted(self.by_kind.items())),
            "projected": dict(sorted(self.projected.items())),
            "limited_rebuild": self.limited_rebuild,
            "not_rebuilt": dict(self.not_rebuilt),
        }


class ReplayBuilder:
    """Rebuild a fresh store from the retained log of ``source`` (G-23: the source is never written)."""

    def __init__(self, source: SqlStorage, target: SqlStorage, *, batch_size: int = 500) -> None:
        self._source = source
        self._target = target
        self._batch = batch_size
        self._upcaster = V1Upcaster()

    async def _check_source(self) -> None:
        if not self._source.can_rebuild:
            raise RebuildUnavailableError(
                "source event log is ephemeral (D-45): nothing was retained, migration impossible"
            )
        first = await self._source.read_events(after_seq=0, limit=1)
        if first and first[0].seq != 1:
            raise RebuildUnavailableError(
                f"source event log starts at seq {first[0].seq}, not 1: the rolling window pruned "
                "history (ER-27 C); migration by replay needs the full log"
            )

    async def run(self) -> MigrationReport:
        await self._check_source()
        if await self._target.read_events(after_seq=0, limit=1):
            raise RebuildUnavailableError("target store is not fresh: it already holds events")
        report = MigrationReport()
        counts: Counter[str] = Counter()
        tap.event("migrate.start", source_mode=getattr(self._source.mode, "value", ""))
        after = 0
        while True:
            batch = await self._source.read_events(after_seq=after, limit=self._batch)
            if not batch:
                break
            for event in batch:
                assert event.seq is not None
                after = event.seq
                report.events_read += 1
                try:
                    lifted = self._upcaster.upcast_event(event)
                except Exception as exc:
                    tap.event(
                        "migrate.upcast_failed",
                        seq=event.seq,
                        kind=event.kind.value,
                        error=type(exc).__name__,
                    )
                    raise
                if lifted is not event:
                    report.events_upcast += 1
                    tap.event(
                        "migrate.upcast",
                        seq=event.seq,
                        kind=event.kind.value,
                        from_version=event.schema_version,
                        to_version=lifted.schema_version,
                    )
                else:
                    lifted = event.model_copy(update={"seq": None})
                await self._target.append_event(lifted)
                report.events_written += 1
                counts[event.kind.value] += 1
        report.by_kind = dict(counts)
        report.projected = await catch_up(self._target, [RecordProjector(self._target)])
        tap.event(
            "migrate.done",
            events=report.events_written,
            upcast=report.events_upcast,
            projected=report.projected,
            not_rebuilt=sorted(report.not_rebuilt),
        )
        return report


async def dump_store(storage: SqlStorage) -> bytes:
    """Deterministic dump of the log and the record read model (for byte-stability checks)."""
    events: list[MemoryEvent] = []
    after = 0
    while True:
        batch = await storage.read_events(after_seq=after, limit=500)
        if not batch:
            break
        after = batch[-1].seq or after
        events.extend(batch)
    records: list[dict[str, Any]] = []
    for ns in await storage.list_namespaces():
        records.extend(rec.model_dump(mode="json") for rec in await storage.list_records(ns))
    records.sort(key=lambda r: (r["namespace"], r["record_id"]))
    return orjson.dumps(
        {"events": [e.model_dump(mode="json") for e in events], "records": records},
        option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2,
    )


def _exists(path: str) -> bool:
    from pathlib import Path

    return Path(path).exists()


async def migrate_sqlite(source_path: str, target_path: str) -> MigrationReport:
    """Replay the log of the SQLite store at ``source_path`` into a fresh store at ``target_path``."""
    from memspine.clients.sqlite import SQLiteClient
    from memspine.services.storage.sqlite.engine import SQLiteStorage

    if _exists(target_path):
        raise RebuildUnavailableError(f"target {target_path} exists: migration needs a fresh store")
    src_client = SQLiteClient(source_path)
    dst_client = SQLiteClient(target_path)
    await src_client.connect()
    await dst_client.connect()
    try:
        source = SQLiteStorage(src_client)
        target = SQLiteStorage(dst_client)
        await source.start()
        await target.start()
        return await ReplayBuilder(source, target).run()
    finally:
        await src_client.close()
        await dst_client.close()
