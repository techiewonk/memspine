"""Per-process warm pools for separate worker processes (ADR-079 decision 4, RM-76).

A worker process (the taskiq outbox worker, each uvicorn worker of the REST app, the MCP
stdio subprocess) builds and starts its Engines *in that process*, after fork or spawn,
from its start-up hook. Starting an Engine acquires its clients and model weights from the
process :class:`~memspine.providers.shared.ResourceRegistry` and runs the warm-up selected
by ``runtime.shared_resources`` (``warmup``, ``warmup_timeout_s``, ``warmup_concurrency``,
``warmup_include`` / ``warmup_exclude``). Every handler in the process that asks for the
same :class:`EngineSpec` gets the same started Engine (refcounted; the last release stops
it); Engines of different specs are separate but share the process registry.

Fork safety: the registry forgets its entries in a forked child (``os.register_at_fork``),
and so does this pool: an Engine that was starting or started in the parent is dropped in
the child, never stopped there and never reused, because its handles belong to the parent.
An Engine object that was only *constructed* in the parent (an app built before a
pre-forking server forks) holds no handles and is started in the child. No resource is
shared across processes; a cross-process model server is RM-79.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from memspine.api.engine import Engine
from memspine.observability.logging import get_logger
from memspine.providers.shared import shared_registry

__all__ = [
    "EngineSpec",
    "ProcessEngines",
    "process_engine",
    "process_engines",
    "worker_readiness",
]

_log = get_logger(__name__)
EntryState = Literal["idle", "starting", "started"]


@dataclass(frozen=True, slots=True)
class EngineSpec:
    """How a worker process builds its Engine: template, user config file and overrides
    (``Engine(template, user_config, **overrides)``). Equal specs share one Engine."""

    template: str | None = None
    config: str | None = None
    overrides: Mapping[str, Any] = field(default_factory=dict)
    dotenv_path: str | None = ".env"

    def key(self) -> str:
        """A digest of the spec (never logged raw: overrides may carry settings)."""
        config = None if self.config is None else str(Path(self.config).expanduser().resolve())
        raw = json.dumps(
            {
                "template": self.template,
                "config": config,
                "overrides": dict(self.overrides),
                "dotenv": self.dotenv_path,
            },
            sort_keys=True,
            default=repr,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def build(self) -> Engine:
        return Engine(
            template=self.template,
            user_config=self.config,
            dotenv_path=self.dotenv_path,
            **dict(self.overrides),
        )


@dataclass(slots=True)
class _Entry:
    spec: EngineSpec
    engine: Engine
    pid: int
    state: EntryState = "idle"
    refs: int = 0
    roles: dict[str, int] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loop: asyncio.AbstractEventLoop | None = None
    error: str | None = None


class ProcessEngines:
    """The Engines of one OS process, keyed by :class:`EngineSpec` (see module docstring)."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def _entry(self, spec: EngineSpec) -> _Entry:
        key = spec.key()
        entry = self._entries.get(key)
        if entry is not None and entry.pid != os.getpid():
            # Inherited without the fork hook having run: only an idle Engine is reusable.
            if entry.state != "idle":
                entry = None
            else:
                entry.pid = os.getpid()
        if entry is None:
            entry = _Entry(spec, spec.build(), os.getpid())
            self._entries[key] = entry
        return entry

    def engine(self, spec: EngineSpec) -> Engine:
        """This process's Engine for ``spec`` (built if needed, not started). An app that
        needs the object before its start-up hook runs (``create_app``) takes it here."""
        return self._entry(spec).engine

    async def acquire(self, spec: EngineSpec, *, role: str) -> Engine:
        """Start (once) and hold this process's Engine for ``spec``. Concurrent first
        acquisitions coalesce into one start; ``warmup: blocking`` failures raise here."""
        loop = asyncio.get_running_loop()
        while True:
            entry = self._entry(spec)
            async with entry.lock:
                if self._entries.get(spec.key()) is not entry:
                    continue  # stopped by its last holder while we waited: build anew
                await self._hold(entry, spec, role, loop)
                return entry.engine

    @staticmethod
    async def _hold(
        entry: _Entry, spec: EngineSpec, role: str, loop: asyncio.AbstractEventLoop
    ) -> None:
        """Start the Engine if needed and count one holder (caller holds ``entry.lock``)."""
        if entry.state == "started":
            if entry.loop is not loop:
                raise RuntimeError(
                    "this process's Engine was started on another event loop; "
                    "one Engine serves one loop"
                )
        else:
            entry.state = "starting"
            entry.error = None
            try:
                await entry.engine.start()  # warm-up per runtime.shared_resources
            except BaseException as exc:
                entry.state = "idle"
                entry.error = type(exc).__name__
                raise
            entry.state, entry.loop = "started", loop
            _log.info(
                "runtime.worker_engine_started",
                role=role,
                pid=entry.pid,
                engine=spec.key(),
                warmup=entry.engine.config.runtime.shared_resources.warmup,
            )
        entry.refs += 1
        entry.roles[role] = entry.roles.get(role, 0) + 1

    async def release(self, spec: EngineSpec, *, role: str) -> None:
        """Drop one hold; the last one stops the Engine (shared leases are released, so
        the registry closes what no other Engine in the process still holds)."""
        entry = self._entries.get(spec.key())
        if entry is None or entry.pid != os.getpid() or entry.refs <= 0:
            return
        async with entry.lock:
            entry.refs -= 1
            left = entry.roles.get(role, 0) - 1
            if left > 0:
                entry.roles[role] = left
            else:
                entry.roles.pop(role, None)
            if entry.refs > 0 or entry.state != "started":
                return
            try:
                await entry.engine.stop()
            finally:
                if self._entries.get(spec.key()) is entry:
                    del self._entries[spec.key()]
                _log.info("runtime.worker_engine_stopped", role=role, pid=entry.pid)

    def readiness(self) -> dict[str, Any]:
        """Per-process readiness: every Engine's start and warm state, and the registry.

        ``ready`` is true when at least one Engine is started and none is starting or
        failed to start. Warm state is reported but never gates readiness (RM-74)."""
        pid = os.getpid()
        engines = []
        for key, entry in self._entries.items():
            if entry.pid != pid:
                continue
            warm = entry.engine.warm_state() if entry.state == "started" else {}
            engines.append(
                {
                    "engine": key,
                    "state": entry.state,
                    "roles": sorted(entry.roles),
                    "holders": entry.refs,
                    "start_error": entry.error,
                    "warm_done": all(s.status != "warming" for s in warm.values()),
                    "warm": {
                        name: {"status": s.status, "error_type": s.error_type}
                        for name, s in sorted(warm.items())
                    },
                }
            )
        stats = shared_registry().stats()
        started = [e for e in engines if e["state"] == "started"]
        return {
            "pid": pid,
            "ready": bool(started) and len(started) == len(engines),
            "engines": engines,
            "shared_resources": len(stats),
            # Always 0: an entry keyed to another pid would be a handle across a fork.
            "inherited_resources": sum(1 for s in stats if s.key.pid != pid),
        }

    def reset_after_fork(self) -> None:
        """In a forked child: forget Engines that were starting or started in the parent
        (their handles are the parent's; never stopped or reused here). Constructed-only
        Engines hold no handles and stay available to start in the child."""
        pid = os.getpid()
        kept: dict[str, _Entry] = {}
        for key, entry in self._entries.items():
            if entry.state == "idle" and entry.refs == 0:
                entry.pid = pid
                entry.lock = asyncio.Lock()
                entry.loop = None
                kept[key] = entry
        self._entries = kept


_ENGINES = ProcessEngines()


def process_engines() -> ProcessEngines:
    """This process's pool (one per process; reset in a forked child)."""
    return _ENGINES


@asynccontextmanager
async def process_engine(spec: EngineSpec, *, role: str) -> AsyncIterator[Engine]:
    """Start-up hook body for a worker process: hold the started, warming Engine for
    ``spec`` for the life of the block (``role`` labels the holder in readiness)."""
    pool = process_engines()
    engine = await pool.acquire(spec, role=role)
    try:
        yield engine
    finally:
        await pool.release(spec, role=role)


def worker_readiness() -> dict[str, Any]:
    """Readiness of this worker process (see :meth:`ProcessEngines.readiness`)."""
    return process_engines().readiness()


if hasattr(os, "register_at_fork"):  # POSIX only; Windows spawns fresh processes
    os.register_at_fork(after_in_child=_ENGINES.reset_after_fork)
