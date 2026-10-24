"""Replay the current event format into a fresh store without historical migration."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import orjson

from memspine.exceptions import RebuildUnavailableError
from memspine.observability import tap
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sql_base import SqlStorage
from memspine.store.events import MemoryEvent
from memspine.store.replay import catch_up

NOT_REBUILT_LAYERS = {
    "vector": "rebuild with the selected embedding provider",
    "graph": "rebuild with the selected graph provider",
    "lexical": "rebuild with the selected lexical provider",
    "model-derived records": "only logged writes are restored; workers regenerate unlogged derivations",
    "caches": "recreated on demand rather than copied from the source store",
}


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
        report = ReplayReport()
        counts: Counter[str] = Counter()
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
                lifted = event.model_copy(update={"seq": None})
                await self._target.append_event(lifted)
                report.events_written += 1
                counts[event.kind.value] += 1
        report.by_kind = dict(counts)
        report.projected = await catch_up(self._target, [RecordProjector(self._target)])
        tap.event(
            "replay.done",
            events=report.events_written,
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
