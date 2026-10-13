"""Derive stages: session_lifecycle, decay_sweep, compress and the sleep_compute hook (F-15)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from memspine.compose.compression import CompressionPolicy
from memspine.core.policies.decay import DecayPolicy
from memspine.core.policies.retention import RetentionPolicy
from memspine.derive.pipeline.stages.context import PipelineContext, _policy_options, _sweep_stats
from memspine.memories.episodic.lifecycle import passive_after, plan_transitions
from memspine.observability.logging import get_logger
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import RecordStatus

_log = get_logger(__name__)


#: Memory types the lifecycle sweeps cover. Working memory is excluded — its
#: lifecycle is the paging window (M13.1); resource chunks decay like episodes.
_SWEEP_TYPES = ("episodic", "semantic", "resource")


async def session_lifecycle(ctx: PipelineContext) -> dict[str, object]:
    """#53: mark sessions idle past ``episodic.policies.sessions.passive_after``
    PASSIVE (and reopen any that saw a write since), one SESSION event each.

    Skipped unless the horizon is set. The decision is recorded in the log, so a
    rebuild replays the same passive set regardless of when it runs (ADR-037)."""
    mem = ctx.config.memories.get("episodic")
    horizon = passive_after(dict(mem.policies)) if mem is not None else None
    if horizon is None:
        return {"status": "skipped", "reason": "episodic.policies.sessions.passive_after unset"}
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    now = datetime.now(UTC)
    counts = {"passive": 0, "active": 0}
    errors: list[str] = []
    for namespace in await ctx.storage.list_namespaces():
        async with ctx.lock(namespace):  # a reopening write must not interleave
            records = await ctx.storage.list_records(namespace, "episodic")
            for change in plan_transitions(records, now, horizon):
                try:
                    await ctx.append_event(
                        MemoryEvent(
                            kind=EventKind.SESSION,
                            namespace=namespace,
                            actor="system",
                            payload={
                                "session_id": change.session_id,
                                "state": change.state,
                                "record_ids": change.record_ids,
                                "reason": "idle" if change.state == "passive" else "recent_write",
                            },
                        )
                    )
                    counts[change.state] += 1
                except Exception as exc:  # one session must not kill the sweep
                    errors.append(f"{change.session_id}: {exc}")
                    _log.warning(
                        "session_lifecycle.session_failed",
                        namespace=namespace,
                        session_id=change.session_id,
                        error=str(exc),
                    )
    stats = _sweep_stats("passivated", counts["passive"], errors)
    stats["reopened"] = counts["active"]
    return stats


async def decay_sweep(ctx: PipelineContext) -> dict[str, object]:
    """M3 decay: move idle records down the tier ladder via DECAY_TRANSITION.

    Only emits on a tier change (idempotent). Reinforcement needs no code here:
    RETRIEVE events advance ``last_accessed_at``, and the next sweep computes a
    hotter tier from it. Quarantined records are frozen in place (E1).

    Transitions are *delta* events ({record_id, set}) — never full snapshots.
    A snapshot taken before the append would overwrite whatever changed in
    between (empirically: a concurrent RETRIEVE's access stats were lost and a
    just-accessed record got demoted); a delta touches only the tier.
    """
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    now = datetime.now(UTC)
    transitions = 0
    errors: list[str] = []
    for namespace in await ctx.storage.list_namespaces():
        for memory_type in _SWEEP_TYPES:
            policy = DecayPolicy.bind(_policy_options(ctx, memory_type, "decay"))
            for record in await ctx.storage.list_records(namespace, memory_type):
                if record.status in (RecordStatus.DELETED, RecordStatus.QUARANTINED):
                    continue
                new_tier = policy.tier_for(record, now).value
                if new_tier == record.tier:
                    continue
                try:
                    await ctx.append_event(
                        MemoryEvent(
                            kind=EventKind.DECAY_TRANSITION,
                            namespace=namespace,
                            actor="system",
                            payload={
                                "record_id": record.record_id,
                                "set": {"tier": new_tier},
                                "transition": f"{record.tier}->{new_tier}",
                                "reason": "idle",
                            },
                        )
                    )
                    transitions += 1
                except Exception as exc:  # one bad record must not kill the sweep
                    errors.append(f"{record.record_id}: {exc}")
                    _log.warning(
                        "decay_sweep.record_failed",
                        namespace=namespace,
                        record_id=record.record_id,
                        error=str(exc),
                        exc_info=True,
                    )
    return _sweep_stats("transitions", transitions, errors)


async def compress(ctx: PipelineContext) -> dict[str, object]:
    """M6/D-32 cold-tier compression: zstd the content of records whose tier
    qualifies. Emits DECAY_TRANSITION with the compressed snapshot — at-rest
    encoding changes, meaning never does (views-not-replacements)."""
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    compressed = 0
    errors: list[str] = []
    for namespace in await ctx.storage.list_namespaces():
        for memory_type in _SWEEP_TYPES:
            policy = CompressionPolicy.bind(_policy_options(ctx, memory_type, "compression"))
            retention = RetentionPolicy.bind(_policy_options(ctx, memory_type, "retention"))
            for record in await ctx.storage.list_records(namespace, memory_type):
                if record.status in (RecordStatus.DELETED, RecordStatus.QUARANTINED):
                    continue
                if not policy.should_compress(record):
                    continue
                # Legal hold freezes representation too — auditors read originals.
                if retention.on_legal_hold(record):
                    continue
                try:
                    # zstd is CPU work — keep it off the event loop.
                    packed = (await asyncio.to_thread(policy.compress, record)).model_dump(
                        mode="json"
                    )
                    await ctx.append_event(
                        MemoryEvent(
                            kind=EventKind.DECAY_TRANSITION,
                            namespace=namespace,
                            actor="system",
                            payload={
                                "record_id": record.record_id,
                                # Delta, not snapshot (see decay_sweep): only the
                                # at-rest encoding of content changes.
                                "set": {
                                    "content": packed["content"],
                                    "content_zstd": packed["content_zstd"],
                                },
                                "transition": f"{record.tier}->{record.tier}",
                                "reason": "cold_tier_compress",
                            },
                        )
                    )
                    compressed += 1
                except Exception as exc:  # one bad record must not kill the sweep
                    errors.append(f"{record.record_id}: {exc}")
                    _log.warning(
                        "compress.record_failed",
                        namespace=namespace,
                        record_id=record.record_id,
                        error=str(exc),
                        exc_info=True,
                    )
    return _sweep_stats("compressed", compressed, errors)


async def sleep_compute(ctx: PipelineContext) -> dict[str, object]:
    """E7 anticipatory sleep-time compute hook (RG tier): no-op by default.

    Deployments override by registering their own pipeline under this name on
    the runner (pre-computed reflections, cache pre-warming, pre-assembled
    bundles). The slot runs after compress, before prune (plan §E7)."""
    return {"status": "noop", "hook": "E7"}
