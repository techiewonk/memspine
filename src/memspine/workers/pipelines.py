"""ANTI-LOCK-IN CORE (D-17): background pipelines as plain, idempotent,
async step functions. Runners decorate these; NO runner imports here, ever.

Each pipeline takes a :class:`PipelineContext` and returns a stats dict.
All mutations go through ``ctx.append_event`` — the write door — so a rebuild
replays exactly the lifecycle history the pipelines produced (D0.1). Every
pipeline is safe to re-run: consolidation checks for an existing summary,
decay only emits on a tier *change*, compression skips already-compressed rows.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from memspine.config.schema import MemspineConfig
from memspine.core.events import EventKind, EventLogMode, MemoryEvent
from memspine.core.policies.compression import CompressionPolicy
from memspine.core.policies.consolidation import ConsolidationPolicy, ConsolidationTrigger
from memspine.core.policies.decay import DecayPolicy
from memspine.core.policies.retention import RetentionPolicy
from memspine.core.records import MemoryRecord, RecordStatus, SourceInfo
from memspine.memories.episodic.sessions import Session, detect_sessions
from memspine.observability.logging import get_logger

__all__ = [
    "PIPELINES",
    "Pipeline",
    "PipelineContext",
    "PipelineStorage",
    "compress",
    "consolidate",
    "decay_sweep",
    "event_log_prune",
    "sleep_compute",
]

_log = get_logger(__name__)

#: Memory types the lifecycle sweeps cover. Working memory is excluded — its
#: lifecycle is the paging window (M13.1); resource chunks decay like episodes.
_SWEEP_TYPES = ("episodic", "semantic", "resource")


class PipelineStorage(Protocol):
    """The narrow storage slice pipelines may touch — keeps this module free of
    any concrete backend import (anti-lock-in, D-17). Reads only: every write
    goes through ``PipelineContext.append_event`` (the write door)."""

    async def prune_events(self, older_than: datetime) -> int: ...

    async def list_namespaces(self) -> list[str]: ...

    async def list_records(
        self, namespace: str, memory_type: str | None = None
    ) -> list[MemoryRecord]: ...


AppendEvent = Callable[[MemoryEvent], Awaitable[None]]
Summarize = Callable[[str], Awaitable[str]]


@dataclass
class PipelineContext:
    storage: PipelineStorage
    config: MemspineConfig
    #: The engine's append-through-the-write-door callable. None => read-only
    #: context (e.g. bare prune runs); mutating pipelines then report "skipped".
    append_event: AppendEvent | None = None
    #: Optional LLM summarizer (summarize role, D-43). None => deterministic
    #: extractive fallback (N6) — consolidation never *requires* an LLM.
    summarize: Summarize | None = None


Pipeline = Callable[[PipelineContext], Awaitable[dict[str, object]]]


def _policy_options(
    ctx: PipelineContext, memory_type: str, policy: str
) -> dict[str, object] | None:
    mem = ctx.config.memories.get(memory_type)
    if mem is None:
        return None
    raw = mem.policies.get(policy)
    return dict(raw) if isinstance(raw, dict) else None


def _sweep_stats(counter: str, count: int, errors: list[str]) -> dict[str, object]:
    """Partial progress is progress: report what succeeded AND what failed,
    never collapse a half-finished sweep into a bare error (D-18)."""
    if errors:
        return {"status": "partial", counter: count, "errors": errors}
    return {"status": "ok", counter: count}


async def event_log_prune(ctx: PipelineContext) -> dict[str, object]:
    """D-45 rolling-mode retention: prune applied events past the window."""
    if ctx.config.event_log.mode is not EventLogMode.ROLLING:
        return {"status": "skipped", "reason": "event_log.mode != rolling"}
    cutoff = datetime.now(UTC) - timedelta(days=ctx.config.event_log.retention_days)
    pruned = await ctx.storage.prune_events(older_than=cutoff)
    return {"status": "ok", "pruned": pruned}


def _consolidation_fires(policy: ConsolidationPolicy) -> bool:
    """Whether this sweep may consolidate. The sweep serves both SLEEP_CYCLE
    and SESSION_END semantics — it only ever summarizes *closed* sessions, so
    a session_end-triggered profile consolidates the same sessions, just at
    sweep cadence. HEAT needs a write-rate counter that lands with P4 telemetry;
    configuring it alone must be loud, not a silent no-op."""
    if policy.should_trigger(ConsolidationTrigger.SLEEP_CYCLE) or policy.should_trigger(
        ConsolidationTrigger.SESSION_END
    ):
        return True
    if ConsolidationTrigger.HEAT in policy.triggers:
        _log.warning(
            "consolidate.heat_trigger_unwired",
            detail="triggers=[heat] alone: heat tracking lands in P4 — "
            "consolidation will not run; add sleep_cycle or session_end",
        )
    return False


async def consolidate(ctx: PipelineContext) -> dict[str, object]:
    """M2 consolidation: closed episodic sessions → one semantic summary each.

    Deterministic-first (N6): session boundaries and membership come from
    boundary detection alone; the LLM (when a ``summarize`` role is bound) only
    words the summary.

    Idempotence + backfill (empirically hardened): the session key fingerprints
    the *full membership*, so an unchanged session is skipped, while a session
    whose membership drifted (a backfilled record joined it) gets a fresh
    summary and the stale one is archived with an ``evolve_to`` chain — never
    a silent exclusion, never duplicate active summaries.
    """
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    policy = ConsolidationPolicy.bind(_policy_options(ctx, "episodic", "consolidation"))
    if not _consolidation_fires(policy):
        return {"status": "skipped", "reason": "no active consolidation trigger"}
    inflate = CompressionPolicy.bind()
    gap = policy.session_gap
    now = datetime.now(UTC)
    summaries = 0
    superseded = 0
    errors: list[str] = []

    for namespace in await ctx.storage.list_namespaces():
        episodes = [
            record
            for record in await ctx.storage.list_records(namespace, "episodic")
            if record.status is RecordStatus.ACTIVATED and record.source.channel != "consolidation"
        ]
        if not episodes:
            continue
        by_id = {record.record_id: record for record in episodes}
        active_summaries = [
            record
            for record in await ctx.storage.list_records(namespace, "semantic")
            if record.source.channel == "consolidation" and record.status is RecordStatus.ACTIVATED
        ]
        existing_keys = {record.source.message_id for record in active_summaries}
        for session in detect_sessions(episodes, gap):
            if session.end >= now - gap:
                continue  # session still open — a new record may yet join it
            if session.session_key in existing_keys:
                continue  # membership unchanged since last summary (idempotence)
            try:
                summaries, superseded = await _consolidate_session(
                    ctx,
                    policy,
                    inflate,
                    namespace,
                    session,
                    by_id,
                    active_summaries,
                    now,
                    summaries,
                    superseded,
                )
            except Exception as exc:  # one bad session must not kill the sweep
                errors.append(f"{namespace}/{session.session_key}: {exc}")
                _log.warning(
                    "consolidate.session_failed",
                    namespace=namespace,
                    session_key=session.session_key,
                    error=str(exc),
                    exc_info=True,
                )
    stats: dict[str, object] = {"status": "ok", "summaries": summaries, "superseded": superseded}
    if errors:
        stats.update(status="partial", errors=errors)
    return stats


async def _consolidate_session(
    ctx: PipelineContext,
    policy: ConsolidationPolicy,
    inflate: CompressionPolicy,
    namespace: str,
    session: Session,
    by_id: dict[str, MemoryRecord],
    active_summaries: list[MemoryRecord],
    now: datetime,
    summaries: int,
    superseded: int,
) -> tuple[int, int]:
    # Cold-tier members must be inflated before anyone summarizes them.
    members = [inflate.inflate(by_id[record_id]) for record_id in session.record_ids]
    if not policy.worth_summarizing(members):
        return summaries, superseded
    summary_text = policy.fallback_summary(members)
    if ctx.summarize is not None:
        try:
            summary_text = await ctx.summarize("\n".join(record.content for record in members))
        except Exception as exc:  # LLM is an enhancer, never a gate (N6)
            _log.warning("consolidate.summarize_fallback", namespace=namespace, error=str(exc))
    # A summary of a closed session is bi-temporally a closed fact:
    # its validity is exactly the session window (M4 semantics).
    summary = MemoryRecord(
        namespace=namespace,
        memory_type="semantic",
        content=summary_text,
        valid_from=session.start,
        valid_to=session.end,
        source=SourceInfo(role="system", channel="consolidation", message_id=session.session_key),
    )
    # Membership drift: archive every prior summary whose window
    # overlaps this session — the fresh summary supersedes it (D-42).
    stale = [
        old
        for old in active_summaries
        if old.valid_to is not None
        and old.valid_from <= session.end
        and old.valid_to >= session.start
    ]
    assert ctx.append_event is not None
    # The WRITE event carries the consolidation provenance too, so even if a
    # crash tears the WRITE/CONSOLIDATE pair, member ids survive in the log.
    await ctx.append_event(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace=namespace,
            actor="system",
            payload={
                "record": summary.model_dump(mode="json"),
                "consolidation": {
                    "session_key": session.session_key,
                    "member_record_ids": session.record_ids,
                },
            },
        )
    )
    for old in stale:
        await ctx.append_event(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=namespace,
                actor="system",
                payload={
                    "record_id": old.record_id,
                    "set": {
                        "status": RecordStatus.ARCHIVED.value,
                        "superseded_at": now.isoformat(),
                        "evolve_to": summary.record_id,
                    },
                    "transition": "summary->superseded",
                    "reason": "reconsolidated",
                },
            )
        )
        superseded += 1
    await ctx.append_event(
        MemoryEvent(
            kind=EventKind.CONSOLIDATE,
            namespace=namespace,
            actor="system",
            payload={
                "session_key": session.session_key,
                "member_record_ids": session.record_ids,
                "summary_record_id": summary.record_id,
                "superseded_summary_ids": [old.record_id for old in stale],
                "summarizer": "llm" if ctx.summarize is not None else "extractive",
            },
        )
    )
    return summaries + 1, superseded


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


#: Name -> pipeline. Runners register from this table; the M11-adjacent names
#: are stable identifiers used in schedules and dead-letter reporting.
PIPELINES: dict[str, Pipeline] = {
    "consolidate": consolidate,
    "decay_sweep": decay_sweep,
    "compress": compress,
    "sleep_compute": sleep_compute,
    "event_log_prune": event_log_prune,
}
