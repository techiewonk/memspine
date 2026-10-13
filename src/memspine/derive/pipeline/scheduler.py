"""Derive: the sleep verb, prompt-version sync and record inflation (F-15, from the engine)."""

from __future__ import annotations

import asyncio
from typing import Any

from memspine.exceptions import MemspineError, StorageError
from memspine.memories.procedural.prompt_registry import prompt_version_records
from memspine.observability.logging import EVENT_WRITE, get_logger
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.store.scope import validate_namespace
from memspine.workers.schedule import run_sleep_cycle

_log = get_logger(__name__)


async def sync_prompt_versions(eng: Any, namespace: str = "default") -> list[MemoryRecord]:
    """D-43 §4: record each resolved prompt version as a procedural record
    (reference only — the definition lives in prompts/). Idempotent."""
    eng._require_started()
    if eng._procedural is None or eng._prompts is None:
        raise MemspineError(
            "prompt-version sync needs procedural memory enabled and prompts loaded"
        )
    ns = validate_namespace(namespace)
    # Same one-writer-per-namespace unit as every other write verb: the
    # read-diff-append sequence below must not interleave with itself
    # (double-sync duplicating versions) or with corroboration's
    # read-modify-write. Firewall exemption: content is deterministic,
    # system-generated reference strings — see ADR-014.
    async with eng._write_locks.setdefault(ns, asyncio.Lock()):
        existing = await eng._procedural.list(ns, kind="prompt")
        fresh = prompt_version_records(ns, eng._prompts, existing)
        for record in fresh:
            await eng._append_and_project(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace=ns,
                    actor="system",
                    payload={"record": record.model_dump(mode="json")},
                )
            )
    if fresh:
        _log.info(EVENT_WRITE, namespace=ns, prompt_versions=len(fresh))
    return fresh


async def sleep(eng: Any) -> dict[str, dict[str, object]]:
    """Run the maintenance sleep cycle now (M2/E7): consolidate → decay →
    compress → prune. All steps are idempotent; P3 gives them teeth."""
    eng._require_started()
    assert eng._runner is not None
    if eng._windows is not None:
        # W1: call 1 first, so call 3 (extract_graph) sees this cycle's window edges. In
        # live (async) mode the open window is left to its idle timer; in batch mode
        # nothing else would ever close it.
        await eng._windows.flush(final=not eng._windows.asynchronous)
    stats = await run_sleep_cycle(eng._runner, eng._pipeline_ctx())
    eng._passive_sessions.clear()  # #53: the cycle may have passivated sessions
    degraded = {
        name: stage for name, stage in stats.items() if stage.get("status") in ("error", "partial")
    }
    if degraded:
        # Callers rarely inspect the stats dict — degradation must be loud.
        _log.warning("memory.sleep_degraded", failures=degraded)
    return stats


def _inflate_all(eng: Any, records: list[MemoryRecord], namespace: str) -> list[MemoryRecord]:
    """Inflate cold-tier content, skipping (loudly) any corrupt row rather
    than failing the entire read (blast-radius containment).

    Quarantined rows are NOT filtered here by design: ``retrieve(include_held=True)``
    is the operator listing/audit surface, so held content stays inspectable.
    Model-facing paths (``search``/``assemble``/timeline/sessions) apply
    the E1 quarantine gate themselves — never feed ``retrieve()`` output
    to a context window."""
    inflated: list[MemoryRecord] = []
    for record in records:
        if record.status is RecordStatus.DELETED:
            continue
        try:
            inflated.append(eng._inflate.inflate(record))
        except StorageError:
            _log.warning("memory.inflate_failed", namespace=namespace, record_id=record.record_id)
    return inflated
