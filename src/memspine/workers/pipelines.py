"""ANTI-LOCK-IN CORE (D-17): background pipelines as plain, idempotent,
async step functions. Runners decorate these; NO runner imports here, ever.

Each pipeline takes a :class:`PipelineContext` and returns a stats dict.
All mutations go through ``ctx.append_event`` — the write door — so a rebuild
replays exactly the lifecycle history the pipelines produced (D0.1). Every
pipeline is safe to re-run: consolidation checks for an existing summary,
decay only emits on a tier *change*, compression skips already-compressed rows.
"""

from __future__ import annotations

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
from memspine.memories.episodic.sessions import detect_sessions
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


async def event_log_prune(ctx: PipelineContext) -> dict[str, object]:
    """D-45 rolling-mode retention: prune applied events past the window."""
    if ctx.config.event_log.mode is not EventLogMode.ROLLING:
        return {"status": "skipped", "reason": "event_log.mode != rolling"}
    cutoff = datetime.now(UTC) - timedelta(days=ctx.config.event_log.retention_days)
    pruned = await ctx.storage.prune_events(older_than=cutoff)
    return {"status": "ok", "pruned": pruned}


async def consolidate(ctx: PipelineContext) -> dict[str, object]:
    """M2 consolidation: closed episodic sessions → one semantic summary each.

    Deterministic-first (N6): session boundaries and membership come from
    boundary detection alone; the LLM (when a ``summarize`` role is bound) only
    words the summary. Idempotent via the session-key check: a summary whose
    ``source.message_id`` matches the session key already exists ⇒ skip.
    """
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    policy = ConsolidationPolicy.bind(_policy_options(ctx, "episodic", "consolidation"))
    if not policy.should_trigger(ConsolidationTrigger.SLEEP_CYCLE):
        return {"status": "skipped", "reason": "sleep_cycle trigger disabled"}
    gap = policy.session_gap
    now = datetime.now(UTC)
    summaries = 0

    for namespace in await ctx.storage.list_namespaces():
        episodes = [
            record
            for record in await ctx.storage.list_records(namespace, "episodic")
            if record.status is RecordStatus.ACTIVATED and record.source.channel != "consolidation"
        ]
        if not episodes:
            continue
        by_id = {record.record_id: record for record in episodes}
        existing_keys = {
            record.source.message_id
            for record in await ctx.storage.list_records(namespace, "semantic")
            if record.source.channel == "consolidation"
        }
        for session in detect_sessions(episodes, gap):
            if session.end >= now - gap:
                continue  # session still open — a new record may yet join it
            if session.session_key in existing_keys:
                continue  # already consolidated (idempotence)
            members = [by_id[record_id] for record_id in session.record_ids]
            if not policy.worth_summarizing(members):
                continue
            summary_text = policy.fallback_summary(members)
            if ctx.summarize is not None:
                try:
                    summary_text = await ctx.summarize(
                        "\n".join(record.content for record in members)
                    )
                except Exception as exc:  # LLM is an enhancer, never a gate (N6)
                    _log.warning(
                        "consolidate.summarize_fallback", namespace=namespace, error=str(exc)
                    )
            summary = MemoryRecord(
                namespace=namespace,
                memory_type="semantic",
                content=summary_text,
                valid_from=session.start,
                source=SourceInfo(
                    role="system", channel="consolidation", message_id=session.session_key
                ),
            )
            await ctx.append_event(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace=namespace,
                    actor="system",
                    payload={"record": summary.model_dump(mode="json")},
                )
            )
            await ctx.append_event(
                MemoryEvent(
                    kind=EventKind.CONSOLIDATE,
                    namespace=namespace,
                    actor="system",
                    payload={
                        "session_key": session.session_key,
                        "member_record_ids": session.record_ids,
                        "summary_record_id": summary.record_id,
                        "summarizer": "llm" if ctx.summarize is not None else "extractive",
                    },
                )
            )
            summaries += 1
    return {"status": "ok", "summaries": summaries}


async def decay_sweep(ctx: PipelineContext) -> dict[str, object]:
    """M3 decay: move idle records down the tier ladder via DECAY_TRANSITION.

    Only emits on a tier change (idempotent). Reinforcement needs no code here:
    RETRIEVE events advance ``last_accessed_at``, and the next sweep computes a
    hotter tier from it. Quarantined records are frozen in place (E1).
    """
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    now = datetime.now(UTC)
    transitions = 0
    for namespace in await ctx.storage.list_namespaces():
        for memory_type in _SWEEP_TYPES:
            policy = DecayPolicy.bind(_policy_options(ctx, memory_type, "decay"))
            for record in await ctx.storage.list_records(namespace, memory_type):
                if record.status in (RecordStatus.DELETED, RecordStatus.QUARANTINED):
                    continue
                new_tier = policy.tier_for(record, now).value
                if new_tier == record.tier:
                    continue
                transitioned = record.model_copy(update={"tier": new_tier})
                await ctx.append_event(
                    MemoryEvent(
                        kind=EventKind.DECAY_TRANSITION,
                        namespace=namespace,
                        actor="system",
                        payload={
                            "record": transitioned.model_dump(mode="json"),
                            "transition": f"{record.tier}->{new_tier}",
                            "reason": "idle",
                        },
                    )
                )
                transitions += 1
    return {"status": "ok", "transitions": transitions}


async def compress(ctx: PipelineContext) -> dict[str, object]:
    """M6/D-32 cold-tier compression: zstd the content of records whose tier
    qualifies. Emits DECAY_TRANSITION with the compressed snapshot — at-rest
    encoding changes, meaning never does (views-not-replacements)."""
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    compressed = 0
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
                packed = policy.compress(record)
                await ctx.append_event(
                    MemoryEvent(
                        kind=EventKind.DECAY_TRANSITION,
                        namespace=namespace,
                        actor="system",
                        payload={
                            "record": packed.model_dump(mode="json"),
                            "transition": f"{record.tier}->{record.tier}",
                            "reason": "cold_tier_compress",
                        },
                    )
                )
                compressed += 1
    return {"status": "ok", "compressed": compressed}


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
