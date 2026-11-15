"""Replay the current event format into a fresh store without historical migration."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import orjson

from memspine.exceptions import RebuildUnavailableError
from memspine.observability import tap
from memspine.observability.logging import EVENT_REBUILD, get_logger
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sql_base import SqlStorage
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.registry_evidence import ENTITY_MARKERS, scrub_registry_marker
from memspine.store.replay import catch_up

_log = get_logger(__name__)

NOT_REBUILT_LAYERS = {
    "vector": "rebuild with the selected embedding provider",
    "graph": "rebuild with the selected graph provider",
    "lexical": "rebuild with the selected lexical provider",
    "model-derived records": "only logged writes are restored; workers regenerate unlogged derivations",
    "caches": "recreated on demand rather than copied from the source store",
}


async def _scrub_target_registry(target: SqlStorage, namespace: str, erased: set[str]) -> None:
    """Remove committed names copied before a concurrent source fence appeared."""

    def scrub(payload: dict[str, Any]) -> bool:
        return scrub_registry_marker(payload, erased)

    for marker in sorted(ENTITY_MARKERS):
        await target.scrub_marker_payloads(marker, scrub, namespace=namespace)


@dataclass
class ReplayReport:
    """What a replay did and what it did not rebuild."""

    events_read: int = 0
    events_written: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    projected: dict[str, int] = field(default_factory=dict)
    not_rebuilt: dict[str, str] = field(default_factory=lambda: dict(NOT_REBUILT_LAYERS))
    limited_rebuild: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "events_read": self.events_read,
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

    async def _check_source(self) -> None:
        if not self._source.can_rebuild:
            raise RebuildUnavailableError(
                "source event log is ephemeral (D-45): nothing was retained, replay impossible"
            )
        first = await self._source.read_events(after_seq=0, limit=1)
        if first and first[0].seq != 1:
            raise RebuildUnavailableError(
                f"source event log starts at seq {first[0].seq}, not 1: the rolling window pruned "
                "history (ER-27 C); replay by replay needs the full log"
            )

    async def run(self) -> ReplayReport:
        await self._check_source()
        if await self._target.read_events(after_seq=0, limit=1):
            raise RebuildUnavailableError("target store is not fresh: it already holds events")
        # An interrupted erasure may have committed its fence before the source's
        # historical marker scrub. Never copy that stale name into the target.
        erased_by_namespace: dict[str, set[str]] = {}
        for _, namespace, ids in await self._source.registry_erasure_fences():
            erased_by_namespace.setdefault(namespace, set()).update(ids)
        report = ReplayReport()
        counts: Counter[str] = Counter()
        copied_fence_seqs: set[int] = set()
        tap.event("replay.start", source_mode=getattr(self._source.mode, "value", ""))
        after = 0
        while True:
            batch = await self._source.read_events(after_seq=after, limit=self._batch)
            if not batch:
                break
            for event in batch:
                assert event.seq is not None
                after = event.seq
                report.events_read += 1
                if event.schema_version != 1:
                    raise ValueError("replay requires the current event schema version 1")
                if event.kind is EventKind.REGISTRY_ERASURE_FENCE:
                    fence_ids = event.payload.get("source_ids")
                    if not isinstance(fence_ids, list) or not all(
                        isinstance(rid, str) and rid for rid in fence_ids
                    ):
                        raise RebuildUnavailableError("malformed registry erasure fence in replay")
                    erased = set(fence_ids)
                    # A fence can commit while source batches are streaming.
                    # Sanitize already-copied target markers before the fence
                    # event itself is persisted, then sanitize later markers
                    # as each is copied.
                    await _scrub_target_registry(self._target, event.namespace, erased)
                    erased_by_namespace.setdefault(event.namespace, set()).update(erased)
                payload = dict(event.payload)
                current_erased = erased_by_namespace.get(event.namespace)
                if event.kind is EventKind.MARKER and current_erased:
                    scrub_registry_marker(payload, current_erased)
                lifted = event.model_copy(update={"seq": None, "payload": payload})
                await self._target._append_replayed_event(lifted)
                if event.kind is EventKind.REGISTRY_ERASURE_FENCE:
                    copied_fence_seqs.add(event.seq)
                report.events_written += 1
                counts[event.kind.value] += 1
        # PostgreSQL can expose a later ordinary sequence before an earlier
        # fence transaction commits. Catch every fence committed by this final
        # read, even if the ordered streaming cursor passed its source seq.
        for seq, namespace, ids in await self._source.registry_erasure_fences():
            if seq in copied_fence_seqs:
                continue
            erased = set(ids)
            await _scrub_target_registry(self._target, namespace, erased)
            source_event = await self._source.read_events(after_seq=seq - 1, limit=1)
            if not source_event or source_event[0].seq != seq:
                raise RebuildUnavailableError("registry erasure fence changed during replay")
            await self._target._append_replayed_event(
                source_event[0].model_copy(update={"seq": None})
            )
            copied_fence_seqs.add(seq)
            report.events_read += 1
            report.events_written += 1
            counts[EventKind.REGISTRY_ERASURE_FENCE.value] += 1
        report.by_kind = dict(counts)
        report.projected = await catch_up(self._target, [RecordProjector(self._target)])
        tap.event(
            "replay.done",
            events=report.events_written,
            projected=report.projected,
            not_rebuilt=sorted(report.not_rebuilt),
        )
        _log.info(
            EVENT_REBUILD,
            events_read=report.events_read,
            events_written=report.events_written,
            by_kind=dict(sorted(report.by_kind.items())),
            projected=dict(sorted(report.projected.items())),
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


#: Owner id of the replay's own provider lifecycle (no Engine, no deployment).
_REPLAY_OWNER = "memspine-replay"
#: Close deadline of the replay's provider lifecycle, in seconds.
_REPLAY_CLOSE_TIMEOUT_S = 30.0
#: SQLite busy timeout of both stores (the connector default, as before the descriptor build).
_REPLAY_BUSY_TIMEOUT_S = 5.0


async def replay_sqlite(source_path: str, target_path: str) -> ReplayReport:
    """Replay the log of the SQLite store at ``source_path`` into a fresh store at ``target_path``.

    Both stores are built through the ``sqlite`` storage descriptor (RM-113 item 7), so the
    connector, its registry sharing and its close come from the provider layer; this module
    never constructs a client."""
    from memspine.config.schema import EventLogConfig
    from memspine.providers.contexts import SharedBuildContext, StorageBuildContext
    from memspine.providers.lifecycle import ProviderLifecycleManager
    from memspine.providers.registry import get_registry, validate_settings

    if _exists(target_path):
        raise RebuildUnavailableError(f"target {target_path} exists: replay needs a fresh store")
    descriptor = get_registry("storage_backends").get("sqlite")
    resources = [ProviderLifecycleManager(close_timeout_s=_REPLAY_CLOSE_TIMEOUT_S)]
    resources.append(ProviderLifecycleManager(close_timeout_s=_REPLAY_CLOSE_TIMEOUT_S))
    try:
        stores: list[SqlStorage] = []
        for path, manager in zip((source_path, target_path), resources, strict=True):
            built = await descriptor.builder.build(
                validate_settings(
                    descriptor, {"path": path, "busy_timeout_s": _REPLAY_BUSY_TIMEOUT_S}
                ),
                StorageBuildContext(
                    SharedBuildContext(manager, _REPLAY_OWNER, _REPLAY_CLOSE_TIMEOUT_S),
                    EventLogConfig(),
                ),
            )
            stores.append(built.service)
        source, target = stores
        await source.start()
        await target.start()
        return await ReplayBuilder(source, target).run()
    finally:
        for manager in resources:
            await manager.close_all()
