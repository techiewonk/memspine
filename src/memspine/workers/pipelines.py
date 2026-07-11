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
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from memspine.config import constants
from memspine.config.schema import MemspineConfig
from memspine.core.events import EventKind, EventLogMode, MemoryEvent, fingerprint_payload
from memspine.core.firewall import instruction_shaped
from memspine.core.policies.community import CommunityOptions, CommunityPolicy
from memspine.core.policies.compression import CompressionPolicy
from memspine.core.policies.consolidation import (
    ConsolidationPolicy,
    ConsolidationTrigger,
    extractive_summary,
)
from memspine.core.policies.decay import DecayPolicy
from memspine.core.policies.retention import RetentionPolicy
from memspine.core.records import MemoryRecord, RecordStatus, SourceInfo
from memspine.exceptions import ConflictError
from memspine.memories.associative.communities import communities_available, detect_communities
from memspine.memories.associative.links import assert_within_budget, link_event
from memspine.memories.episodic.sessions import Session, detect_sessions
from memspine.memories.prospective.triggers import due_watches, invalidation_watches
from memspine.observability.logging import get_logger
from memspine.prompts.models import ExtractedEdge
from memspine.services.graph.base import GraphStore

__all__ = [
    "PIPELINES",
    "ExtractEdges",
    "NamespaceLock",
    "Pipeline",
    "PipelineContext",
    "PipelineStorage",
    "check_watches",
    "compress",
    "consolidate",
    "decay_sweep",
    "event_log_prune",
    "extract_graph",
    "reorganize",
    "sleep_compute",
]

_log = get_logger(__name__)

#: SF-1/ADR-018: latch so the "invalidation watches never fire under
#: event_log.mode=ephemeral" warning from check_watches is logged at most once
#: per process (mirrors the engine-side latch on the watch-creation path).
_ephemeral_watch_warned = False

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

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]: ...


AppendEvent = Callable[[MemoryEvent], Awaitable[None]]
Summarize = Callable[[str], Awaitable[str]]
#: LLM edge extraction for the graphiti-style write path (C2). Takes source
#: text, returns the (reflexion-merged) relationship edges. None => the
#: extract_graph pipeline self-skips — no LLM role or the feature is off.
ExtractEdges = Callable[[str], Awaitable[list[ExtractedEdge]]]
#: Per-namespace write serialization: ``lock(namespace)`` yields the same
#: async context manager the engine's write verbs hold, so a pipeline's
#: read-then-write unit cannot interleave with a concurrent forget cascade.
NamespaceLock = Callable[[str], AbstractAsyncContextManager[None]]


def _no_lock(_namespace: str) -> AbstractAsyncContextManager[None]:
    """Default no-op lock: contexts built directly (tests, bare runs) get the
    single-writer behaviour they already have without engine plumbing."""
    return nullcontext()


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
    #: The association graph projection (D-26/P6). None => associative memory
    #: is disabled and the reorganizer reports "skipped".
    graph: GraphStore | None = None
    #: Per-namespace write lock (the engine passes its write-verb locks so
    #: reorganize cannot race forget's delete cascade — a forgotten node must
    #: not be resurrected by a late LINK projection). No-op by default.
    lock: NamespaceLock = _no_lock
    #: LLM edge extractor for the C2 graphiti-style pipeline. None => the
    #: extract_graph stage self-skips (feature off or no extract_edges LLM role).
    extract_edges: ExtractEdges | None = None


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
        # E1: a summary is DERIVED content — never more trusted than its
        # least-trusted member, and injection framing echoed into the summary
        # text (an LLM summarizing a poisoned episode) keeps the inert flag.
        trust=min(member.trust for member in members),
        instruction_flag=instruction_shaped(summary_text),
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


async def check_watches(ctx: PipelineContext) -> dict[str, object]:
    """M13.8/ADR-016 prospective sweep: log which watches have fired.

    Read-only and trivially idempotent — delivery is pull-based in v0.1
    (``Engine.due()``), so this step only surfaces fired counts loudly in the
    sleep cycle; nothing is pushed and nothing mutates. Invalidation firing
    reads M4 CONFLICT events from the log (nothing readable in ephemeral
    mode, so only due-time watches can fire there — ADR-016).
    """
    global _ephemeral_watch_warned
    now = datetime.now(UTC)
    ephemeral = ctx.config.event_log.mode is EventLogMode.EPHEMERAL
    due_total = 0
    invalidated_total = 0
    fired_total = 0
    errors: list[str] = []
    conflicts_by_ns: dict[str, list[MemoryEvent]] | None = None  # lazy: one log scan
    for namespace in await ctx.storage.list_namespaces():
        try:
            watches = await ctx.storage.list_records(namespace, "prospective")
            if not watches:
                continue
            due = due_watches(watches, now)
            invalidated: list[MemoryRecord] = []
            has_target_watches = any(watch.entity is not None for watch in watches)
            # SF-1/ADR-018: target watches fire off M4 CONFLICT events; ephemeral
            # mode persists none, so they never fire. Surface it once at sweep
            # time (not just at creation) so long-running deployments see it.
            if ephemeral and has_target_watches and not _ephemeral_watch_warned:
                _ephemeral_watch_warned = True
                _log.warning(
                    "prospective.ephemeral_invalidation_never_fires",
                    namespace=namespace,
                    detail="event_log.mode=ephemeral persists no CONFLICT events — "
                    "invalidation (target) watches can never fire (ADR-016)",
                )
            if has_target_watches:
                if conflicts_by_ns is None:
                    conflicts_by_ns = await _conflicts_by_namespace(ctx)
                invalidated = invalidation_watches(watches, conflicts_by_ns.get(namespace, []))
            fired = {record.record_id for record in due} | {
                record.record_id for record in invalidated
            }
            if fired:
                _log.info(
                    "memory.watch_fired",
                    namespace=namespace,
                    fired=len(fired),
                    due=len(due),
                    invalidated=len(invalidated),
                )
            due_total += len(due)
            invalidated_total += len(invalidated)
            fired_total += len(fired)
        except Exception as exc:  # one bad namespace must not kill the sweep
            errors.append(f"{namespace}: {exc}")
            _log.warning(
                "check_watches.namespace_failed",
                namespace=namespace,
                error=str(exc),
                exc_info=True,
            )
    stats = _sweep_stats("fired", fired_total, errors)
    stats.update(due=due_total, invalidated=invalidated_total)
    return stats


async def _conflicts_by_namespace(ctx: PipelineContext) -> dict[str, list[MemoryEvent]]:
    """One batched scan of the log for CONFLICT events, bucketed by namespace."""
    buckets: dict[str, list[MemoryEvent]] = {}
    after = 0
    while True:
        batch = await ctx.storage.read_events(after_seq=after)
        if not batch:
            return buckets
        for event in batch:
            if event.kind is EventKind.CONFLICT:
                buckets.setdefault(event.namespace, []).append(event)
        last_seq = batch[-1].seq
        assert last_seq is not None  # events past the door always carry seq
        after = last_seq


async def sleep_compute(ctx: PipelineContext) -> dict[str, object]:
    """E7 anticipatory sleep-time compute hook (RG tier): no-op by default.

    Deployments override by registering their own pipeline under this name on
    the runner (pre-computed reflections, cache pre-warming, pre-assembled
    bundles). The slot runs after compress, before prune (plan §E7)."""
    return {"status": "noop", "hook": "E7"}


async def reorganize(ctx: PipelineContext) -> dict[str, object]:
    """D-40/D-42 background graph reorganizer: Leiden communities over the
    association graph → one consolidation-style summary parent per community
    of >= REORGANIZE_MIN_COMMUNITY_SIZE members, members linked to the parent
    via LINK events (ADR-015).

    No-op ("skipped") without a graph store (associative disabled) or without
    the ``[community]`` extra (D-40). Idempotent: the parent's provenance key
    fingerprints the full membership, so an unchanged community is skipped and
    a drifted one supersedes its stale parent (same pattern as consolidate).
    Parents mirror consolidation summaries: derived trust = min(member trust)
    (D-47 §5), instruction framing stays flagged, quarantined/non-active
    members never contribute.
    """
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    if ctx.graph is None:
        return {"status": "skipped", "reason": "no graph store (associative memory disabled)"}
    if not communities_available():
        return {
            "status": "skipped",
            "reason": "leidenalg not installed — `pip install memspine[community]` (D-40)",
        }
    inflate = CompressionPolicy.bind()
    community_opts = CommunityPolicy.bind(_policy_options(ctx, "associative", "community")).options
    assert isinstance(community_opts, CommunityOptions)
    edges = await ctx.graph.edge_list()
    # Leiden clustering is CPU work — keep it off the event loop (same
    # pattern as compress()'s zstd call). Knobs ride the associative policy
    # (v0.2 A6); defaults preserve rebuild determinism (D0.1).
    communities = await asyncio.to_thread(
        detect_communities,
        edges,
        min_size=community_opts.min_size,
        resolution=community_opts.resolution,
        randomness=community_opts.randomness,
        random_seed=community_opts.random_seed,
        max_cluster_size=community_opts.max_cluster_size,
    )
    parents = 0
    superseded = 0
    errors: list[str] = []
    fresh_keys: dict[str, set[str]] = {}  # namespace -> live community keys
    for community in communities:
        try:
            created, key, namespace = await _reorganize_community(ctx, inflate, community)
        except Exception as exc:  # one bad community must not kill the sweep
            errors.append(f"{community[0]}…: {exc}")
            _log.warning(
                "reorganize.community_failed",
                members=len(community),
                error=str(exc),
                exc_info=True,
            )
            continue
        if key is not None and namespace is not None:
            fresh_keys.setdefault(namespace, set()).add(key)
            parents += created
    # Membership drift: a stale parent whose community dissolved or changed
    # membership is archived — never a silent second active summary (D-42).
    for namespace in await ctx.storage.list_namespaces():
        try:
            # Same per-namespace unit as the engine's write verbs (M5): the
            # read-then-archive-then-tombstone pass must not interleave with
            # a concurrent forget cascade in this namespace.
            async with ctx.lock(namespace):
                superseded += await _supersede_stale_parents(
                    ctx, namespace, fresh_keys.get(namespace, set())
                )
        except Exception as exc:  # one bad namespace must not kill the sweep
            errors.append(f"{namespace}: {exc}")
            _log.warning(
                "reorganize.supersede_failed",
                namespace=namespace,
                error=str(exc),
                exc_info=True,
            )
    stats: dict[str, object] = {
        "status": "ok",
        "communities": len(communities),
        "parents": parents,
        "superseded": superseded,
    }
    if errors:
        stats.update(status="partial", errors=errors)
    return stats


async def _supersede_stale_parents(
    ctx: PipelineContext, namespace: str, live_keys: set[str]
) -> int:
    """Archive every stale community parent in ``namespace`` and tombstone its
    member→parent ``community`` edges (ADR-015 §2). Returns how many parents
    were superseded. Caller holds the namespace lock."""
    assert ctx.append_event is not None and ctx.graph is not None
    superseded = 0
    for old in await _reorganize_summaries(ctx, namespace):
        if old.source.message_id in live_keys:
            continue
        # The archived parent must also lose its graph reach: weight-0
        # tombstone LINK events retire each live member→parent community edge
        # replay-deterministically (same mechanism as budget pruning) — the
        # graph would otherwise accumulate stale membership edges forever.
        for edge in await ctx.graph.edges_of(old.record_id):
            if edge.rel_type != "community" or edge.weight <= 0.0:
                continue
            await ctx.append_event(
                link_event(
                    namespace,
                    edge.src,
                    edge.dst,
                    edge.rel_type,
                    weight=0.0,
                    reason="reorganize_supersede",
                    actor="system",
                )
            )
        await ctx.append_event(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=namespace,
                actor="system",
                payload={
                    "record_id": old.record_id,
                    "set": {
                        "status": RecordStatus.ARCHIVED.value,
                        "superseded_at": datetime.now(UTC).isoformat(),
                    },
                    "transition": "community_parent->superseded",
                    "reason": "reorganized",
                },
            )
        )
        superseded += 1
    return superseded


async def _reorganize_summaries(ctx: PipelineContext, namespace: str) -> list[MemoryRecord]:
    """Active summary parents this pipeline previously wrote in ``namespace``."""
    return [
        record
        for record in await ctx.storage.list_records(namespace, "semantic")
        if record.source.channel == "reorganize" and record.status is RecordStatus.ACTIVATED
    ]


async def _reorganize_community(
    ctx: PipelineContext, inflate: CompressionPolicy, community: list[str]
) -> tuple[int, str | None, str | None]:
    """Write one summary parent for ``community``. Returns
    ``(parents_created, community_key, namespace)`` — key/namespace are None
    when the community does not qualify."""
    assert ctx.append_event is not None
    members: list[MemoryRecord] = []
    for record_id in community:
        record = await ctx.storage.get_record(record_id)
        # Only live namespace truth feeds a summary (E1): quarantined or
        # non-active members are held/derived content, not community evidence.
        if (
            record is not None
            and record.status is RecordStatus.ACTIVATED
            and not record.quarantined
        ):
            members.append(inflate.inflate(record))
    if len(members) < constants.REORGANIZE_MIN_COMMUNITY_SIZE:
        return 0, None, None
    namespaces = {member.namespace for member in members}
    if len(namespaces) > 1:
        # Links never cross namespaces (ADR-015), so a mixed community means
        # corrupted state — refuse loudly rather than pick a tenant.
        raise ValueError(f"community spans namespaces {sorted(namespaces)} — refusing to summarize")
    namespace = members[0].namespace
    member_ids = sorted(member.record_id for member in members)
    key = fingerprint_payload({"community": member_ids})
    # Same per-namespace unit as the engine's write verbs (M5): the
    # idempotency read, the summary WRITE and the membership LINKs must not
    # interleave with a concurrent forget cascade in this namespace.
    async with ctx.lock(namespace):
        if any(old.source.message_id == key for old in await _reorganize_summaries(ctx, namespace)):
            return 0, key, namespace  # unchanged membership (idempotence)
        ordered = sorted(members, key=lambda member: (member.valid_from, member.record_id))
        summary_text = extractive_summary(
            [member.content for member in ordered], constants.CONSOLIDATION_SUMMARY_MAX_CHARS
        )
        if ctx.summarize is not None:
            try:
                summary_text = await ctx.summarize("\n".join(member.content for member in ordered))
            except Exception as exc:  # LLM is an enhancer, never a gate (N6)
                _log.warning("reorganize.summarize_fallback", namespace=namespace, error=str(exc))
        summary = MemoryRecord(
            namespace=namespace,
            memory_type="semantic",
            content=summary_text,
            source=SourceInfo(role="system", channel="reorganize", message_id=key),
            # E1/D-47 §5: derived content is never more trusted than its
            # least-trusted member, and echoed injection framing stays flagged.
            trust=min(member.trust for member in members),
            instruction_flag=instruction_shaped(summary_text),
        )
        # The WRITE carries consolidation-shaped provenance, so audit taint and
        # the graph projector's derived_from edges ride existing machinery.
        await ctx.append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace=namespace,
                actor="system",
                payload={
                    "record": summary.model_dump(mode="json"),
                    "consolidation": {"session_key": key, "member_record_ids": member_ids},
                },
            )
        )
        # Member -> parent LINK events (ADR-015). System-written membership
        # links are budget-exempt by design: budget enforcement lives at the
        # memory-layer creation surface, and a 20-member community keeps all
        # 20 links. The rel is RESERVED (H1): callers cannot forge it.
        for member_id in member_ids:
            await ctx.append_event(
                link_event(
                    namespace,
                    member_id,
                    summary.record_id,
                    "community",
                    weight=1.0,
                    reason="reorganize",
                    actor="system",
                )
            )
    return 1, key, namespace


def _edge_key(namespace: str, edge: ExtractedEdge) -> str:
    """Idempotency key for an extracted edge: same (src, rel, dst) in a
    namespace fingerprints to the same record, so a re-run never duplicates."""
    return fingerprint_payload(
        {"extract_graph": [namespace, edge.src_entity, edge.rel, edge.dst_entity]}
    )


def _edge_valid_from(edge: ExtractedEdge, fallback: datetime) -> datetime:
    """The edge's stated ISO ``valid_from`` if parseable, else the source
    record's time — a malformed date never fails the sweep."""
    if edge.valid_from:
        try:
            parsed = datetime.fromisoformat(edge.valid_from)
        except ValueError:
            return fallback
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return fallback


async def extract_graph(ctx: PipelineContext) -> dict[str, object]:
    """C2 graphiti-style write path: LLM edge extraction over recent records →
    semantic fact records + ``asserted`` association LINKs.

    For each source record (episodic/semantic, active, not itself extract_graph
    output) the ``extract_edges`` callable returns reflexion-merged relationship
    edges; each edge becomes a semantic record keyed ``(entity=src, attribute=
    rel)`` with the fact as content, and an ``asserted`` LINK from the source to
    the new fact so the D-42 reorganizer forms communities over the LLM edges.
    Slotted after ``consolidate`` and before ``reorganize`` for exactly that.

    No-op ("skipped") without a write door or an ``extract_edges`` callable
    (feature off / no LLM role). Idempotent: an edge's (src, rel, dst) keys its
    record, so a re-run skips already-written edges; LINK upserts are replay
    -deterministic. Derived trust never exceeds the source's (E1/D-47 §5), and
    the ``asserted`` rel is non-reserved so the M13.6 link budget applies — a
    saturated source keeps the fact record but skips the extra edge (logged)."""
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    if ctx.extract_edges is None:
        return {"status": "skipped", "reason": "extract_graph disabled or no extract_edges role"}
    opts = _policy_options(ctx, "semantic", "extract_graph") or {}
    raw_conf = opts.get("min_confidence", 0.0)
    min_conf = float(raw_conf) if isinstance(raw_conf, (int, float, str)) else 0.0
    written = 0
    linked = 0
    skipped = 0
    errors: list[str] = []
    for namespace in await ctx.storage.list_namespaces():
        async with ctx.lock(namespace):
            existing: set[str] = set()
            sources: list[MemoryRecord] = []
            for mtype in ("episodic", "semantic"):
                for record in await ctx.storage.list_records(namespace, mtype):
                    if record.source.channel == "extract_graph":
                        if record.source.message_id:
                            existing.add(record.source.message_id)
                        continue  # never re-extract from our own output (no feedback loop)
                    if record.status is RecordStatus.ACTIVATED and not record.quarantined:
                        sources.append(record)
            for record in sources:
                try:
                    edges = await ctx.extract_edges(record.content)
                except Exception as exc:  # the LLM is an enhancer, never a gate (N6)
                    errors.append(f"{record.record_id}: {exc}")
                    _log.warning(
                        "extract_graph.extract_failed", record_id=record.record_id, error=str(exc)
                    )
                    continue
                for edge in edges:
                    if edge.confidence < min_conf:
                        continue
                    key = _edge_key(namespace, edge)
                    if key in existing:
                        skipped += 1
                        continue
                    existing.add(key)
                    fact = MemoryRecord(
                        namespace=namespace,
                        memory_type="semantic",
                        content=edge.fact,
                        entity=edge.src_entity,
                        attribute=edge.rel,
                        valid_from=_edge_valid_from(edge, record.valid_from),
                        source=SourceInfo(role="system", channel="extract_graph", message_id=key),
                        trust=record.trust,  # derived trust never exceeds the source (E1)
                        instruction_flag=instruction_shaped(edge.fact),
                    )
                    await ctx.append_event(
                        MemoryEvent(
                            kind=EventKind.WRITE,
                            namespace=namespace,
                            actor="system",
                            payload={
                                "record": fact.model_dump(mode="json"),
                                "extract_graph": {"source_record_id": record.record_id},
                            },
                        )
                    )
                    written += 1
                    # Associate the fact with its source (non-reserved rel: budget
                    # applies). A saturated source keeps the record, skips the link.
                    if ctx.graph is not None:
                        try:
                            await assert_within_budget(ctx.graph, record.record_id)
                        except ConflictError:
                            _log.warning(
                                "extract_graph.link_budget_full", record_id=record.record_id
                            )
                            continue
                    await ctx.append_event(
                        link_event(
                            namespace,
                            record.record_id,
                            fact.record_id,
                            "asserted",
                            weight=max(0.0, min(1.0, edge.confidence)),
                            reason="extract_graph",
                            actor="system",
                        )
                    )
                    linked += 1
    stats: dict[str, object] = {
        "status": "ok" if not errors else "partial",
        "edges_written": written,
        "links": linked,
        "skipped_existing": skipped,
    }
    if errors:
        stats["errors"] = errors
    return stats


#: Name -> pipeline. Runners register from this table; the M11-adjacent names
#: are stable identifiers used in schedules and dead-letter reporting.
PIPELINES: dict[str, Pipeline] = {
    "consolidate": consolidate,
    "reorganize": reorganize,
    "extract_graph": extract_graph,
    "check_watches": check_watches,
    "decay_sweep": decay_sweep,
    "compress": compress,
    "sleep_compute": sleep_compute,
    "event_log_prune": event_log_prune,
}
