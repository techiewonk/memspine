"""The Engine facade (D-01): one clean API over the event-sourced substrate.

Startup follows plan §4 (Phase-0 scope — runners join in Phase 1):

1. bootstrap secrets, then resolve config (two-phase, D-22)
2. validate the memory combination (C1b dependency closure) + required services (D-10)
3. construct services; missing service hard-fails naming the extra (D-10)
4. open the write door (event log) and register projectors
6. catch-up: projectors replay from their high-water marks
7. ``describe()`` returns the effective world
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self, TypeVar

from memspine.clients.sqlite import SQLiteClient
from memspine.config.loader import ResolvedConfig, load_config
from memspine.core.events import EventKind, EventLogMode, MemoryEvent
from memspine.core.namespace import validate_namespace
from memspine.core.projector import Projector
from memspine.core.records import MemoryRecord, PiiTier, SourceInfo
from memspine.core.registry import SERVICE_EXTRAS, dependency_closure, missing_services
from memspine.core.replay import catch_up
from memspine.core.replay import rebuild as replay_rebuild
from memspine.exceptions import MemspineError, MissingServiceError
from memspine.observability.logging import EVENT_REBUILD, EVENT_RETRIEVE, EVENT_WRITE, get_logger
from memspine.services.secrets.env import EnvSecrets
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage

__all__ = ["Engine"]

_log = get_logger(__name__)

_T = TypeVar("_T")

#: Services the Phase-0 engine always constructs (core install, D-03).
_CORE_SERVICES = frozenset({"storage", "secrets"})


class Engine:
    """Async-first facade; thin sync wrappers on the public verbs (D-01)."""

    def __init__(
        self,
        template: str | None = None,
        user_config: str | Path | dict[str, Any] | None = None,
        dotenv_path: str | Path | None = ".env",
        **overrides: Any,
    ) -> None:
        self._template = template
        self._user_config = user_config
        self._dotenv_path = dotenv_path
        self._overrides = overrides
        self._resolved: ResolvedConfig | None = None
        self._enabled: set[str] = set()
        self._auto_enabled: list[str] = []
        self._client: SQLiteClient | None = None
        self._storage: SQLiteStorage | None = None
        self._projectors: list[Projector] = []
        self._started = False
        self._sync_loop: asyncio.AbstractEventLoop | None = None
        self._sync_thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> Self:
        if self._started:
            return self

        # 1. secrets, then config (D-22 two-phase).
        secrets = EnvSecrets(dotenv_path=self._dotenv_path)
        self._resolved = load_config(
            template=self._template,
            user_config=self._user_config,
            env=os.environ,
            overrides=self._overrides,
            secret_resolver=secrets.get,
        )
        config = self._resolved.config

        # 2. validate combination (C1b) + required services (D-10).
        self._enabled, self._auto_enabled = dependency_closure(config.enabled_memories())
        gaps = missing_services(self._enabled, set(_CORE_SERVICES))
        if gaps:
            mem_type, services = next(iter(sorted(gaps.items())))
            service = sorted(services)[0]
            if config.strict_services:
                raise MissingServiceError(service, SERVICE_EXTRAS.get(service))
            _log.warning(
                "service.missing_ignored",
                detail="strict_services=false: engine starts degraded (D-10)",
                gaps={k: sorted(v) for k, v in gaps.items()},
                first_missing=f"{mem_type} needs {service}",
            )

        # 3./4. construct services + open the write door.
        self._client = SQLiteClient(config.storage.path)
        await self._client.connect()
        self._storage = SQLiteStorage(
            self._client,
            mode=config.event_log.mode,
            compress=config.event_log.compress,
        )
        await self._storage.start()
        if config.event_log.mode is EventLogMode.EPHEMERAL:
            _log.warning(
                "event_log.ephemeral_mode_active",
                detail="events are not persisted: rebuild and audit taint unavailable (D-45)",
            )
        self._projectors = [RecordProjector(self._storage)]

        # 6. catch-up from high-water marks; rolling mode also prunes on boot
        # (continuous pruning joins the P3 sleep cycle).
        await catch_up(self._storage, list(self._projectors))
        if config.event_log.mode is EventLogMode.ROLLING:
            cutoff = datetime.now(UTC) - timedelta(days=config.event_log.retention_days)
            pruned = await self._storage.prune_events(older_than=cutoff)
            if pruned:
                _log.info("event_log.pruned", events=pruned, mode="rolling")
        self._started = True
        return self

    async def stop(self) -> None:
        if self._storage is not None:
            await self._storage.stop()
        if self._client is not None:
            await self._client.close()
        self._started = False

    # ── public verbs (P0: write / retrieve / rebuild / describe) ─────────────

    async def write(
        self,
        content: str,
        namespace: str = "default",
        memory_type: str = "semantic",
        source: SourceInfo | None = None,
        pii_tier: PiiTier = PiiTier.NONE,
        actor: str = "user",
    ) -> MemoryRecord:
        """Append a WRITE event through the single door; projection materializes it."""
        storage = self._require_started()
        ns = validate_namespace(namespace)
        record = MemoryRecord(
            namespace=ns,
            memory_type=memory_type,
            content=content,
            source=source or SourceInfo(role=actor),
            pii_tier=pii_tier,
        )
        event = MemoryEvent(
            kind=EventKind.WRITE,
            namespace=ns,
            actor=actor,
            payload={"record": record.model_dump(mode="json")},
        )
        appended = await storage.append_event(event)
        if appended.seq is None:  # pragma: no cover - write door always assigns seq
            raise MemspineError("write door returned an event without seq")
        # Inline-runner semantics (P0): the appended event is in hand — apply it
        # directly in every mode (startup catch-up already brought projectors to
        # the head; set_offset is advance-only, so races cannot regress marks).
        for projector in self._projectors:
            await projector.apply(appended)
            await storage.set_offset(projector.name, appended.seq)
        _log.info(EVENT_WRITE, namespace=ns, record_id=record.record_id, seq=appended.seq)
        return record

    async def retrieve(
        self, namespace: str = "default", memory_type: str | None = None
    ) -> list[MemoryRecord]:
        """P0 read path: relational listing. Hybrid retrieval lands in Phase 1."""
        storage = self._require_started()
        ns = validate_namespace(namespace)
        records = await storage.list_records(ns, memory_type)
        _log.info(EVENT_RETRIEVE, namespace=ns, memory_type=memory_type, count=len(records))
        return records

    async def rebuild(self) -> dict[str, int]:
        """Rebuild every projector from seq 0 (D0.1). Raises off-window (D-45)."""
        storage = self._require_started()
        counts = {
            projector.name: await replay_rebuild(storage, projector)
            for projector in self._projectors
        }
        _log.info(EVENT_REBUILD, counts=counts)
        return counts

    def describe(self) -> dict[str, Any]:
        """The effective world (§4 step 7). Only meaningful on a started engine."""
        storage = self._require_started()
        assert self._resolved is not None
        config = self._resolved.config
        return {
            "profile": config.profile,
            "memories": {
                "enabled": sorted(self._enabled),
                "auto_enabled": list(self._auto_enabled),
            },
            "event_log": {
                "mode": config.event_log.mode.value,
                "compress": config.event_log.compress,
                "retention_days": config.event_log.retention_days,
                "rebuildable": storage.can_rebuild,
            },
            "storage": {"backend": "sqlite", "path": config.storage.path},
            "projectors": [projector.name for projector in self._projectors],
            "strict_services": config.strict_services,
        }

    # ── thin sync wrappers (D-01) ────────────────────────────────────────────
    #
    # All sync verbs dispatch onto ONE long-lived background loop so aiosqlite
    # connections stay bound to a living loop across calls (a fresh
    # asyncio.run() per verb would strand pooled connections on dead loops).

    def start_sync(self) -> Self:
        return self._run_sync(self.start())

    def write_sync(self, content: str, **kwargs: Any) -> MemoryRecord:
        return self._run_sync(self.write(content, **kwargs))

    def retrieve_sync(self, **kwargs: Any) -> list[MemoryRecord]:
        return self._run_sync(self.retrieve(**kwargs))

    def stop_sync(self) -> None:
        self._run_sync(self.stop())
        self._close_sync_loop()

    def _run_sync(self, coro: Coroutine[Any, Any, _T]) -> _T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            coro.close()
            raise MemspineError(
                "sync wrapper called from inside a running event loop — "
                "use the async API (await engine.<verb>()) here"
            )
        if self._sync_loop is None or self._sync_loop.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name="memspine-sync", daemon=True)
            thread.start()
            self._sync_loop, self._sync_thread = loop, thread
        return asyncio.run_coroutine_threadsafe(coro, self._sync_loop).result()

    def _close_sync_loop(self) -> None:
        if self._sync_loop is not None and not self._sync_loop.is_closed():
            self._sync_loop.call_soon_threadsafe(self._sync_loop.stop)
            if self._sync_thread is not None:
                self._sync_thread.join(timeout=5)
            self._sync_loop.close()
        self._sync_loop = None
        self._sync_thread = None

    # ── internals ────────────────────────────────────────────────────────────

    def _require_started(self) -> SQLiteStorage:
        if not self._started or self._storage is None:
            raise MemspineError("Engine not started — call start() first")
        return self._storage
