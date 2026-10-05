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
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Protocol

from memspine.config import constants
from memspine.config.schema import MemspineConfig
from memspine.core.event_date import (
    cited_turns,
    happened_label,
    normalise_label,
    resolve_happened,
)
from memspine.core.events import EventKind, EventLogMode, MemoryEvent, fingerprint_payload
from memspine.core.firewall import Firewall, FirewallVerdict, instruction_shaped
from memspine.core.policies.community import CommunityOptions, CommunityPolicy
from memspine.core.policies.compression import CompressionPolicy
from memspine.core.policies.consolidation import (
    ConsolidationPolicy,
    ConsolidationTrigger,
    extractive_summary,
)
from memspine.core.policies.decay import DecayPolicy
from memspine.core.policies.retention import RetentionPolicy
from memspine.core.policies.trust import TrustPolicy
from memspine.core.records import MemoryRecord, RecordStatus, SourceInfo
from memspine.core.temporal_resolve import WeekMode
from memspine.exceptions import ConflictError
from memspine.memories.associative.communities import communities_available, detect_communities
from memspine.memories.associative.links import assert_within_budget, link_event
from memspine.memories.episodic.sessions import Session, detect_sessions, topic_segments
from memspine.memories.prospective.triggers import due_watches, invalidation_watches
from memspine.memories.semantic.write_pipeline import (
    MAX_PREVIOUS_EPISODES,
    EdgeContext,
    ExtractEdges,
    ScreenDerived,
    edge_fact_key,
)
from memspine.observability.logging import get_logger
from memspine.prompts.models import AnticipatedCue, ExtractedEdge, ExtractedFact
from memspine.services.graph.base import GraphStore

__all__ = [
    "PIPELINES",
    "ExtractEdges",
    "NamespaceLock",
    "Pipeline",
    "PipelineContext",
    "PipelineStorage",
    "SessionIndex",
    "anticipate",
    "check_watches",
    "compress",
    "consolidate",
    "decay_sweep",
    "event_log_prune",
    "extract_graph",
    "mine_facts",
    "reflect_profile",
    "reorganize",
    "sleep_compute",
    "stage_marker",
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
#: The engine's semantic write door for one extract_graph fact, called with the
#: namespace lock held: (unscreened record, parent trust cap) -> the stored record
#: (firewall screening, M5 dedup and the M4 conflict ladder all apply).
WriteDerivedFact = Callable[[MemoryRecord, list[float]], Awaitable[MemoryRecord]]
Summarize = Callable[[str], Awaitable[str]]
# ``ExtractEdges`` (re-exported): LLM edge extraction for the graphiti-style
# write path (C2). Takes source text plus an optional :class:`EdgeContext`,
# returns the (reflexion-merged) relationship edges. None on the context => the
# extract_graph pipeline self-skips — no LLM role or the feature is off.
#: H14: reflector (``reflect`` role): session turns -> (insight, evidence indices).
Reflect = Callable[[list[str]], Awaitable[list[tuple[str, list[int]]]]]
#: H14: engine-side reflection deposit (namespace, insight, evidence record ids, key).
DepositReflection = Callable[[str, str, list[str], str], Awaitable[object]]
#: H8: anticipator (``anticipate`` role): numbered session text -> cues.
Anticipate = Callable[[str], Awaitable[list[AnticipatedCue]]]
#: H8: engine-side cue deposit (namespace, target record id, cue texts, session key).
DepositCues = Callable[[str, str, list[str], str], Awaitable[object]]
#: C6': LLM fact miner (``extract`` role): session text -> atomic facts.
MineFacts = Callable[[str], Awaitable[list[ExtractedFact]]]
#: #29: (transcript, fact statements) -> {1-based fact index: date} for the facts the
#: model could date (one batched call).
DateFacts = Callable[[str, list[str]], Awaitable[dict[int, str]]]


class DepositFact(Protocol):
    """C6': engine-side deposit of one mined fact through the write door
    (namespace, text, entity, attribute, parent ids, event time, session key);
    ``kind`` (G1a) is ``state`` / ``event``, or None for an unclassified fact."""

    def __call__(
        self,
        namespace: str,
        text: str,
        entity: str | None,
        attribute: str | None,
        parents: list[str],
        valid_from: datetime,
        session_key: str,
        *,
        kind: str | None = None,
        happened: str | None = None,
    ) -> Awaitable[object]: ...


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
    #: C6' atomic-fact mining. Both None => the mine_facts stage self-skips.
    mine_facts: MineFacts | None = None
    #: #29: the batched LLM date fill (``consolidation.mine_event_dates_llm``).
    date_facts: DateFacts | None = None
    #: H8 anticipatory cues. Both None => the anticipate stage self-skips.
    anticipate: Anticipate | None = None
    #: H14 profile reflection. Both None => the reflect_profile stage self-skips.
    reflect: Reflect | None = None
    deposit_reflection: DepositReflection | None = None
    deposit_cues: DepositCues | None = None
    deposit_fact: DepositFact | None = None
    #: N2: the engine's write-door firewall for derived records this module
    #: appends itself (extract_graph facts). None (bare contexts): a local,
    #: context-free firewall from the config's trust policy screens instead.
    screen: ScreenDerived | None = None
    #: The engine's semantic write door for extract_graph facts (lock held), so a
    #: background ``state`` edge supersedes through the ladder like a write-time
    #: one (GP-1). None (bare contexts): the fact is screened and appended raw.
    write_fact: WriteDerivedFact | None = None
    #: One incremental log index shared by the derived stages of a cycle.
    session_index: SessionIndex = field(default_factory=lambda: SessionIndex())


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
        # B9 (F3): sanitise BEFORE compression. A flagged member reaches the
        # summariser wrapped as data, never as raw instructions; cleaning the
        # summary afterwards leaves the influence in (State Contamination).
        rendered = [
            constants.INSTRUCTION_FLAG_WRAP.format(content=record.content)
            if record.instruction_flag
            else record.content
            for record in members
        ]
        try:
            summary_text = await ctx.summarize("\n".join(rendered))
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
        # N2: derived (and, with a summarize role, LLM-authored) content is
        # never privileged — constants.DERIVED_ROLE states the rule.
        source=SourceInfo(
            role=constants.DERIVED_ROLE, channel="consolidation", message_id=session.session_key
        ),
        # E1: a summary is DERIVED content — never more trusted than its
        # least-trusted member, and injection framing echoed into the summary
        # text (an LLM summarizing a poisoned episode) keeps the inert flag.
        trust=min(member.trust for member in members),
        # B9: a summary inherits its members' flags (monotone, like trust);
        # re-detection on the summary text alone misses paraphrased framing.
        instruction_flag=instruction_shaped(summary_text)
        or any(member.instruction_flag for member in members),
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
    """D-40/D-42 background graph reorganizer: Leiden communities over each
    namespace's association graph (KB-9) → one consolidation-style summary parent per community
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
    # KB-9: Leiden runs per namespace, over that namespace's edges only, so no
    # community (and no summary parent) ever spans two tenants.
    communities: list[list[str]] = []
    for graph_namespace in await ctx.storage.list_namespaces():
        edges = await ctx.graph.edge_list(graph_namespace)
        # Leiden clustering is CPU work — keep it off the event loop (same
        # pattern as compress()'s zstd call). Knobs ride the associative policy
        # (v0.2 A6); defaults preserve rebuild determinism (D0.1).
        communities.extend(
            await asyncio.to_thread(
                detect_communities,
                edges,
                min_size=community_opts.min_size,
                resolution=community_opts.resolution,
                randomness=community_opts.randomness,
                random_seed=community_opts.random_seed,
                max_cluster_size=community_opts.max_cluster_size,
            )
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
            source=SourceInfo(role=constants.DERIVED_ROLE, channel="reorganize", message_id=key),
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


#: GP-8a: MARKER payload naming the sources one extract_graph sweep already sent
#: to the LLM, with each source's content fingerprint at the time. ``sources`` is
#: a list of ``{"record_id", "content_fingerprint"}`` dicts, the snapshot shape
#: the M7 erasure walker scrubs, so a hard forget also erases the fingerprint
#: (logs written before carry a ``{record_id: fingerprint}`` map, still read).
GRAPH_EXTRACTED_MARKER = "graph_extracted"


def _graph_marker_sources(raw: object) -> list[tuple[str, str]]:
    """``(record_id, fingerprint)`` pairs of a ``graph_extracted`` marker, either
    shape; a redacted entry (empty fingerprint) is no watermark."""
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, dict):  # pre-erasure-fix shape: {record_id: fingerprint}
        pairs = [(str(rid), str(fp)) for rid, fp in raw.items()]
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and entry.get("record_id"):
                fp = entry.get("content_fingerprint")
                pairs.append((str(entry["record_id"]), str(fp) if fp else ""))
    return [(rid, fp) for rid, fp in pairs if fp]

#: GR-4: at most this many known entity names ride along as extraction context.
EDGE_CONTEXT_MAX_ENTITIES = 50


def _one_line(text: str, limit: int = 400) -> str:
    """An episode squeezed onto one prompt line (GR-4 context, never a source)."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "..."


def _edge_contexts(sources: list[MemoryRecord], known: list[MemoryRecord]) -> list[EdgeContext]:
    """GR-4: per source, its event time, the episodes just before it (same
    group, at most ``MAX_PREVIOUS_EPISODES``) and the entity names already
    known in the namespace (most recent first)."""
    entities: list[str] = []
    for record in sorted(known, key=lambda r: r.recorded_at, reverse=True):
        if record.entity and record.entity not in entities:
            entities.append(record.entity)
    entities = entities[:EDGE_CONTEXT_MAX_ENTITIES]
    episodes = sorted(
        (r for r in sources if r.memory_type == "episodic"),
        key=lambda r: (r.valid_from, r.record_id),
    )
    contexts: list[EdgeContext] = []
    for record in sources:
        previous = [
            e
            for e in episodes
            if e.group_id == record.group_id
            and (e.valid_from, e.record_id) < (record.valid_from, record.record_id)
        ][-MAX_PREVIOUS_EPISODES:]
        contexts.append(
            EdgeContext(
                reference_time=record.valid_from,
                previous=[_one_line(e.content) for e in previous],
                entities=entities,
            )
        )
    return contexts


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
    saturated source keeps the fact record but skips the extra edge (logged).

    N2: a fact is LLM-authored, so it carries the non-privileged
    ``DERIVED_ROLE`` with its source as parent, and passes the write-door
    firewall (``ctx.screen``) before it is appended. A quarantined fact is
    recorded inert and gets no LINK; an instruction-flagged source is never
    sent to the extractor."""
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
    quarantined = 0
    provenance = 0
    errors: list[str] = []
    screen = ctx.screen or _local_screen(ctx)
    protected = ctx.config.firewall.protected_keys
    # GP-8a: a source already sent to the LLM (same content) is not sent again.
    index = ctx.session_index
    await index.refresh(ctx.storage)
    already = 0
    for namespace in await ctx.storage.list_namespaces():
        async with ctx.lock(namespace):
            # (src, rel, dst) key -> the fact record already written for it (GR-9).
            existing: dict[str, MemoryRecord] = {}
            sources: list[MemoryRecord] = []
            known: list[MemoryRecord] = []
            for mtype in ("episodic", "semantic"):
                for record in await ctx.storage.list_records(namespace, mtype):
                    if mtype == "semantic" and record.status is RecordStatus.ACTIVATED:
                        known.append(record)
                    if record.source.channel == "extract_graph":
                        key = record.source.message_id
                        if key and (
                            key not in existing
                            or record.status is RecordStatus.ACTIVATED
                            or existing[key].status is not RecordStatus.ACTIVATED
                        ):
                            existing[key] = record
                        continue  # never re-extract from our own output (no feedback loop)
                    if constants.CUE_TAG in record.tags:
                        continue  # a cue is a retrieval key, never a fact source (R2-1)
                    if (
                        record.status is RecordStatus.ACTIVATED
                        and not record.quarantined
                        # N2: injection framing must not be laundered into "facts".
                        and not record.instruction_flag
                    ):
                        sources.append(record)
            done: dict[str, str] = {}
            for record, edge_context in zip(sources, _edge_contexts(sources, known), strict=True):
                if (
                    index.graph_sources.get((namespace, record.record_id))
                    == record.content_fingerprint
                ):
                    already += 1
                    continue
                try:
                    edges = await ctx.extract_edges(record.content, edge_context)
                except Exception as exc:  # the LLM is an enhancer, never a gate (N6)
                    errors.append(f"{record.record_id}: {exc}")
                    _log.warning(
                        "extract_graph.extract_failed", record_id=record.record_id, error=str(exc)
                    )
                    continue  # no watermark: retried next sweep
                done[record.record_id] = record.content_fingerprint
                for edge in edges:
                    if edge.confidence < min_conf:
                        continue
                    key = _edge_key(namespace, edge)
                    if key in existing:
                        skipped += 1
                        # GR-9: a verbatim duplicate (same src, rel, dst and kind) adds
                        # its source episode to the fact's provenance; no LLM call.
                        if await _add_edge_provenance(ctx, existing[key], edge, record):
                            provenance += 1
                        continue
                    # GP-1: an event edge drops its attribute (add-only, never
                    # superseded); kind, rel and dst persist as tags.
                    attribute, tags = edge_fact_key(edge, edge.src_entity, protected)
                    fact = MemoryRecord(
                        namespace=namespace,
                        memory_type="semantic",
                        content=edge.fact,
                        entity=edge.src_entity,
                        attribute=attribute,
                        tags=tags,
                        valid_from=_edge_valid_from(edge, record.valid_from),
                        source=SourceInfo(
                            role=constants.DERIVED_ROLE,
                            channel="extract_graph",
                            message_id=key,
                            parents=[record.record_id],
                        ),
                    )
                    if ctx.write_fact is not None:
                        # The semantic door: firewall (N2, cap = the source's trust),
                        # M5 dedup and the M4 ladder, so a state edge supersedes the
                        # older value; event edges carry no attribute and are ADDed.
                        stored = await ctx.write_fact(fact, [record.trust])
                        existing[key] = stored
                        if stored.quarantined:
                            quarantined += 1  # held content gains no graph reach (E1)
                            continue
                        if stored.record_id != fact.record_id:
                            skipped += 1  # merged into / rejected by an existing fact
                            continue
                        fact = stored
                    else:
                        # Derived trust never exceeds the source (E1): the cap rides
                        # the same firewall screening as every other derived write.
                        fact, reasons = await screen(fact, [record.trust])
                        payload: dict[str, object] = {
                            "record": fact.model_dump(mode="json"),
                            "extract_graph": {"source_record_id": record.record_id},
                        }
                        if reasons:
                            payload["firewall"] = {"reasons": reasons}
                        await ctx.append_event(
                            MemoryEvent(
                                kind=EventKind.WRITE,
                                namespace=namespace,
                                actor="system",
                                payload=payload,
                            )
                        )
                        existing[key] = fact
                        if reasons:
                            quarantined += 1  # held content gains no graph reach (E1)
                            _log.warning(
                                "memory.quarantined",
                                namespace=namespace,
                                record_id=fact.record_id,
                                reasons=reasons,
                            )
                            continue
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
                            # GP-10: an edge is never stronger than its
                            # confidence or the trust of what it links.
                            weight=max(
                                0.0, min(1.0, edge.confidence, record.trust, fact.trust)
                            ),
                            reason="extract_graph",
                            actor="system",
                        )
                    )
                    linked += 1
            if done:
                # GP-8a: one watermark marker per namespace per sweep, appended
                # after the facts, so a crash mid-sweep re-extracts idempotently.
                await ctx.append_event(
                    MemoryEvent(
                        kind=EventKind.MARKER,
                        namespace=namespace,
                        actor="system",
                        payload={
                            "marker": GRAPH_EXTRACTED_MARKER,
                            "stage": "extract_graph",
                            "sources": [
                                {"record_id": rid, "content_fingerprint": fp}
                                for rid, fp in done.items()
                            ],
                        },
                    )
                )
                for record_id, fp in done.items():
                    index.graph_sources[(namespace, record_id)] = fp
    stats: dict[str, object] = {
        "status": "ok" if not errors else "partial",
        "edges_written": written,
        "links": linked,
        "skipped_existing": skipped,
        "skipped_sources": already,
        "quarantined": quarantined,
        "provenance_added": provenance,
    }
    if errors:
        stats["errors"] = errors
    return stats


async def _add_edge_provenance(
    ctx: PipelineContext, fact: MemoryRecord, edge: ExtractedEdge, source: MemoryRecord
) -> bool:
    """GR-9: record ``source`` as one more episode stating ``fact``'s edge.

    Only a verbatim match counts: the fact's ``kind:`` tag must equal the edge's
    kind (the (src, rel, dst) part is the key already matched). The episode is
    added as an ``edge_source:<id>`` tag through an add-only lifecycle delta, so
    the fact's trust, parents and erasure lineage are unchanged. Returns True when
    a tag was added (a source already counted adds nothing).
    """
    if ctx.append_event is None or f"kind:{edge.kind}" not in fact.tags:
        return False
    tag = f"{constants.EDGE_SOURCE_TAG_PREFIX}{source.record_id}"
    current = await ctx.storage.get_record(fact.record_id)
    if current is None or current.status is RecordStatus.DELETED:
        return False
    if source.record_id in current.source.parents or tag in current.tags:
        return False
    await ctx.append_event(
        MemoryEvent(
            kind=EventKind.DECAY_TRANSITION,
            namespace=current.namespace,
            actor="system",
            payload={
                "record_id": current.record_id,
                "set": {"tags_add": [tag]},
                "reason": "edge_provenance",
            },
        )
    )
    return True


def _local_screen(ctx: PipelineContext) -> ScreenDerived:
    """The context-free firewall for bare pipeline contexts (no engine).

    Same trust matrix, instruction check and quarantine rule as the engine's
    door, without the namespace anomaly context; with ``firewall.enabled`` off
    it scores trust only, like the engine's ablation arm. The parent cap is the
    plain minimum (integrity's derivation decay is the engine's concern).
    """
    firewall = Firewall(TrustPolicy.bind(_policy_options(ctx, "semantic", "trust")))
    enabled = ctx.config.firewall.enabled

    async def screen(
        record: MemoryRecord, trust_cap: list[float]
    ) -> tuple[MemoryRecord, list[str]]:
        verdict = (
            firewall.assess(record)
            if enabled
            else FirewallVerdict(trust=firewall.policy.trust_at_write(record.source))
        )
        stamped = verdict.apply(record)
        stamped = stamped.model_copy(update={"trust": min([stamped.trust, *trust_cap])})
        return stamped, (verdict.reasons if verdict.quarantine else [])

    return screen


def _fact_date(value: str | None, latest: datetime | None = None) -> datetime | None:
    """H2: YYYY-MM-DD / YYYY-MM / YYYY from a mined fact, as an aware datetime.

    R2-7: the date is an LLM output, so it is range-checked. A year before
    ``MINED_FACT_MIN_YEAR`` or a date past ``latest`` (the session's last turn,
    default now) plus ``MINED_FACT_FUTURE_SLACK_DAYS`` yields None, and the
    caller falls back to the session start. Unchecked, ``9999-12-31`` would be
    the newest statement on its key and win every later conflict.
    """
    if not value:
        return None
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            parsed = datetime.strptime(value.strip(), fmt).replace(tzinfo=UTC)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    ceiling = (latest or datetime.now(UTC)) + timedelta(days=constants.MINED_FACT_FUTURE_SLACK_DAYS)
    if parsed.year < constants.MINED_FACT_MIN_YEAR or parsed > ceiling:
        return None
    return parsed


#: The background stages that derive records from consolidated sessions and
#: keep a per-session done marker (R2-6/R5-2) in the event log.
DERIVED_STAGES = ("mine_facts", "anticipate", "reflect_profile")


def _members_fp(record_ids: list[str]) -> str:
    return fingerprint_payload({"members": sorted(record_ids)})


def stage_marker(
    namespace: str,
    stage: str,
    session_key: str,
    *,
    cleared: bool = False,
    members_fp: str | None = None,
) -> MemoryEvent:
    """A ``stage_done`` (or ``stage_cleared``) MARKER event for one session.

    The marker lives in the log, so it survives replay and rebuild like the
    CONSOLIDATE event it refers to. ``Engine.repair_taint`` appends the cleared
    form so a repaired session is processed again from its clean members.
    ``members_fp`` fingerprints the live members the stage actually read.
    """
    payload: dict[str, object] = {
        "marker": "stage_cleared" if cleared else "stage_done",
        "stage": stage,
        "session_key": session_key,
    }
    if members_fp is not None:
        payload["members_fp"] = members_fp
    return MemoryEvent(kind=EventKind.MARKER, namespace=namespace, actor="system", payload=payload)


@dataclass
class SessionIndex:
    """Consolidated sessions and stage markers, read from the log incrementally.

    One index lives on a :class:`PipelineContext`, so the three derived stages
    of a sleep cycle share one pass over the log (R2-12): each ``refresh`` only
    reads events past the last seq it saw. A session key seen twice (repair
    re-consolidates under the same key) keeps its latest membership.

    Done-ness is also tracked per live-membership fingerprint: after a repair,
    the cleared session and the fresh consolidation of the same clean turns are
    one unit of work, so the LLM sees those turns once, not twice.
    """

    after_seq: int = 0
    sessions: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    #: (stage, namespace, session_key) -> True (done) / False (cleared).
    markers: dict[tuple[str, str, str], bool] = field(default_factory=dict)
    #: (stage, namespace, session_key) -> membership fingerprint of its done marker.
    key_fp: dict[tuple[str, str, str], str] = field(default_factory=dict)
    #: (stage, namespace, fingerprint) -> session keys done with that membership.
    done_fp: dict[tuple[str, str, str], set[str]] = field(default_factory=dict)
    #: GP-8a: (namespace, source record id) -> content fingerprint the
    #: extract_graph stage already sent to the LLM (``graph_extracted`` markers).
    graph_sources: dict[tuple[str, str], str] = field(default_factory=dict)

    async def refresh(self, storage: PipelineStorage) -> None:
        while True:
            events = await storage.read_events(after_seq=self.after_seq, limit=1000)
            if not events:
                return
            for event in events:
                self.observe(event)
            self.after_seq = max([self.after_seq, *(e.seq for e in events if e.seq is not None)])

    def observe(self, event: MemoryEvent) -> None:
        payload = event.payload or {}
        if event.kind is EventKind.FORGET:
            # M7: a forgotten source keeps no watermark (its fingerprint is erased).
            self.graph_sources.pop((event.namespace, str(payload.get("record_id", ""))), None)
        elif event.kind is EventKind.CONSOLIDATE:
            key = str(payload.get("session_key", ""))
            if key:
                members = [str(m) for m in payload.get("member_record_ids", [])]
                self.sessions[(event.namespace, key)] = members
        elif event.kind is EventKind.MARKER:
            marker = payload.get("marker")
            if marker == GRAPH_EXTRACTED_MARKER:
                for record_id, fp in _graph_marker_sources(payload.get("sources")):
                    self.graph_sources[(event.namespace, record_id)] = fp
            elif marker in ("stage_done", "stage_cleared"):
                fp = payload.get("members_fp")
                self.mark(
                    str(payload.get("stage", "")),
                    event.namespace,
                    str(payload.get("session_key", "")),
                    done=marker == "stage_done",
                    members_fp=str(fp) if fp else None,
                )

    def mark(
        self, stage: str, namespace: str, key: str, *, done: bool, members_fp: str | None
    ) -> None:
        slot = (stage, namespace, key)
        self.markers[slot] = done
        old = self.key_fp.pop(slot, None)
        if old is not None:
            self.done_fp.get((stage, namespace, old), set()).discard(key)
        if done and members_fp is not None:
            self.key_fp[slot] = members_fp
            self.done_fp.setdefault((stage, namespace, members_fp), set()).add(key)

    def membership_done(self, stage: str, namespace: str, members_fp: str) -> bool:
        return bool(self.done_fp.get((stage, namespace, members_fp)))

    def state(self, stage: str, namespace: str, key: str) -> bool | None:
        """True = done, False = cleared, None = no marker (a pre-marker log)."""
        return self.markers.get((stage, namespace, key))


async def _live_members(ctx: PipelineContext, member_ids: list[str]) -> list[MemoryRecord]:
    """R2-2/R2-8: the session members a derived stage may read.

    Only ACTIVATED, unquarantined records: a forgotten (DELETED) or rolled-back
    (ARCHIVED) turn must never be re-mined into new memories. Cold-tier members
    are inflated, otherwise the stage would see compressed (blank) text.
    """
    inflate = CompressionPolicy.bind()
    members: list[MemoryRecord] = []
    for record_id in member_ids:
        record = await ctx.storage.get_record(record_id)
        if record is None or record.status is not RecordStatus.ACTIVATED or record.quarantined:
            continue
        members.append(inflate.inflate(record))
    members.sort(key=lambda m: (m.valid_from, m.record_id))
    return members


#: work(namespace, session_key, members) -> (records produced, deposit errors).
#: Raising means the LLM call failed: the session is retried next cycle.
_StageWork = Callable[[str, str, list[MemoryRecord]], Awaitable[tuple[int, list[str]]]]
#: legacy_done(namespace, session_key): done-ness for logs written before markers.
_LegacyDone = Callable[[str, str], Awaitable[bool]]


async def _run_session_stage(
    ctx: PipelineContext,
    stage: str,
    counter: str,
    legacy_done: _LegacyDone,
    work: _StageWork,
) -> dict[str, object]:
    """Shared loop of the derived stages: each consolidated session once.

    A session is marked done once its LLM call succeeded, whatever the call
    produced (nothing, merges, rejects, or some failed deposits), so it is never
    re-sent to the LLM and a merge never re-inflates importance (R2-6/R5-2).
    A deposit that raises is reported and the rest still land.
    """
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    if ctx.config.event_log.mode is EventLogMode.EPHEMERAL:
        # R2-12: CONSOLIDATE events are not persisted, so there is nothing to
        # read sessions from; "ok, 0" would hide that the stage never runs.
        return {
            "status": "skipped",
            "reason": "event_log.mode=ephemeral: consolidated sessions are not in the log",
        }
    index = ctx.session_index
    await index.refresh(ctx.storage)
    processed = 0
    produced = 0
    errors: list[str] = []
    for (namespace, key), member_ids in list(index.sessions.items()):
        state = index.state(stage, namespace, key)
        if state is True:
            continue
        if state is None and await legacy_done(namespace, key):
            continue
        members = await _live_members(ctx, member_ids)
        fp = _members_fp([m.record_id for m in members])
        if members and index.membership_done(stage, namespace, fp):
            members = []  # the same turns were already processed under another key
        if members:
            try:
                count, deposit_errors = await work(namespace, key, members)
            except Exception as exc:  # LLM is an enhancer, never a gate: retry next cycle
                errors.append(f"{namespace}:{key}: {exc}")
                continue
            produced += count
            errors.extend(deposit_errors)
            processed += 1
        await ctx.append_event(stage_marker(namespace, stage, key, members_fp=fp))
        index.mark(stage, namespace, key, done=True, members_fp=fp)
    return {
        "status": "ok" if not errors else "partial",
        "sessions": processed,
        counter: produced,
        "errors": errors,
    }


def _legacy_tag_check(
    ctx: PipelineContext, memory_type: str, matches: Callable[[MemoryRecord, str], bool]
) -> _LegacyDone:
    """Pre-marker idempotence: a derived record already names the session.

    Lists each namespace once per stage run, not once per session (R5-2).
    """
    cache: dict[str, list[MemoryRecord]] = {}

    async def done(namespace: str, key: str) -> bool:
        if namespace not in cache:
            cache[namespace] = await ctx.storage.list_records(namespace, memory_type)
        return any(matches(record, key) for record in cache[namespace])

    return done


def _mining_transcript(segment: list[MemoryRecord], numbered: bool) -> str:
    """The miner's transcript: ``[YYYY-MM-DD] turn`` lines, numbered ``[n]`` (1-based)
    when the miner may cite its evidence turns (#29)."""
    lines = [f"[{m.valid_from:%Y-%m-%d}] {m.content}" for m in segment]
    if not numbered:
        return "\n".join(lines)
    return "\n".join(f"[{n}] {line}" for n, line in enumerate(lines, 1))


@dataclass
class _Happened:
    """#29: one mined fact's happened date: the label, and the event time a
    deterministic resolution gives (None: keep the H2 event time)."""

    label: str | None = None
    start: datetime | None = None


def _happened(
    fact: ExtractedFact, segment: list[MemoryRecord], numbered: bool, week: WeekMode
) -> _Happened:
    """#29: deterministic first. A relative phrase in the fact's statement (resolved
    against its first cited turn, or the segment's day when it has only one), else in
    its cited turns (each against its own date); else the miner's own ``date``."""
    cited = cited_turns(segment, getattr(fact, "turns", [])) if numbered else []
    days = {m.valid_from.date() for m in segment}
    anchor = cited[0].valid_from if cited else (segment[0].valid_from if len(days) == 1 else None)
    span = resolve_happened([(fact.value, anchor)], week=week) if anchor is not None else None
    if span is None and cited:
        span = resolve_happened(((m.content, m.valid_from) for m in cited), week=week)
    if span is not None:
        return _Happened(happened_label(*span), datetime.combine(span[0], time(), tzinfo=UTC))
    label = normalise_label(getattr(fact, "date", None))
    if label is not None and _fact_date(label.split("..", 1)[0], segment[-1].valid_from) is None:
        label = None  # R2-7: out of range, as for the event time
    return _Happened(label)


async def _fill_dates(
    dater: DateFacts, transcript: str, mined: list[ExtractedFact], happened: list[_Happened]
) -> None:
    """#29: one call dates the facts no rule or miner did; a failure leaves them undated."""
    missing = [i for i, h in enumerate(happened) if h.label is None]
    if not missing:
        return
    try:
        dates = await dater(transcript, [mined[i].value for i in missing])
    except Exception as exc:  # an enhancer, never a gate
        _log.warning("mine_facts.date_fill_failed", error=str(exc))
        return
    for position, index in enumerate(missing, 1):
        label = normalise_label(dates.get(position))
        if label is not None and _fact_date(label.split("..", 1)[0]) is not None:
            happened[index].label = label


async def mine_facts(ctx: PipelineContext) -> dict[str, object]:
    """C6': mine atomic, dated facts from each consolidated session, once.

    Idempotent through a per-session ``stage_done`` marker in the log. The
    transcript given to the miner carries each turn's event date, so relative
    dates ("yesterday") can be resolved to absolute ones. Raw turns are kept:
    facts are an index over them, not a replacement (M6 views).
    """
    policy = ConsolidationPolicy.bind(_policy_options(ctx, "episodic", "consolidation"))
    options = policy.options
    if not getattr(options, "mine_facts", False):
        return {"status": "skipped", "reason": "consolidation.mine_facts is off"}
    if ctx.mine_facts is None or ctx.deposit_fact is None:
        return {"status": "skipped", "reason": "no extract LLM role bound"}
    miner, deposit = ctx.mine_facts, ctx.deposit_fact
    by_topic = bool(getattr(options, "mine_by_topic", False))
    numbered = bool(getattr(options, "mine_evidence_turns", False))
    event_dates = bool(getattr(options, "mine_event_dates", False))
    dater = ctx.date_facts if getattr(options, "mine_event_dates_llm", False) else None
    week = ctx.config.read.relative_week

    async def work(namespace: str, key: str, members: list[MemoryRecord]) -> tuple[int, list[str]]:
        # H15: one call per topic segment. Every call runs before any deposit, so a
        # failed call retries the whole session next cycle without duplicates.
        segments = topic_segments(members) if by_topic else [members]
        batches = []
        for segment in segments:
            transcript = _mining_transcript(segment, numbered)
            mined = await miner(transcript)
            happened: list[_Happened] = []
            if event_dates:
                happened = [_happened(fact, segment, numbered, week) for fact in mined]
                if dater is not None:  # #29: one batched call fills the undated facts
                    await _fill_dates(dater, transcript, mined, happened)
            batches.append((segment, mined, happened))
        written = 0
        errors: list[str] = []
        for segment, mined, happened in batches:
            start, latest = segment[0].valid_from, segment[-1].valid_from
            for index, fact in enumerate(mined):
                text = f"{fact.entity} {fact.attribute}: {fact.value}"
                when = _fact_date(getattr(fact, "date", None), latest) or start
                # #29: a cited fact's parents are its evidence turns, not the segment.
                cited = cited_turns(segment, getattr(fact, "turns", [])) if numbered else []
                parents = [m.record_id for m in (cited or segment)]
                # G1a: only a state is keyed by (entity, attribute) and may supersede;
                # the deposit drops an event's attribute, so the ladder ADDs it.
                kind = getattr(fact, "kind", "event") or "event"
                extra: dict[str, str] = {}
                if happened and happened[index].label:
                    extra["happened"] = str(happened[index].label)
                    when = happened[index].start or when
                try:
                    await deposit(
                        namespace,
                        text,
                        fact.entity or None,
                        fact.attribute or None,
                        parents,
                        when,
                        key,
                        kind=kind,
                        **extra,
                    )
                except Exception as exc:  # one bad fact must not lose the rest
                    errors.append(f"{namespace}:{key}: deposit failed: {exc}")
                    continue
                written += 1
        return written, errors

    legacy = _legacy_tag_check(ctx, "semantic", lambda r, key: f"mined:{key}" in r.tags)
    return await _run_session_stage(ctx, "mine_facts", "facts", legacy, work)


async def anticipate(ctx: PipelineContext) -> dict[str, object]:
    """H8: store likely future questions as cues on the turns that answer them.

    One call per consolidated session, idempotent per session (done marker). Cues
    go through ``Engine.add_cues``: firewall-screened, trust capped at the target
    turn's, and retrieval keys only (never assembled as content).
    """
    policy = ConsolidationPolicy.bind(_policy_options(ctx, "episodic", "consolidation"))
    if not getattr(policy.options, "anticipate", False):
        return {"status": "skipped", "reason": "consolidation.anticipate is off"}
    if ctx.anticipate is None or ctx.deposit_cues is None:
        return {"status": "skipped", "reason": "no anticipate/extract LLM role bound"}
    anticipator, deposit = ctx.anticipate, ctx.deposit_cues

    async def work(namespace: str, key: str, members: list[MemoryRecord]) -> tuple[int, list[str]]:
        transcript = "\n".join(
            f"[{n}] [{m.valid_from:%Y-%m-%d}] {m.content}" for n, m in enumerate(members, 1)
        )
        proposed = await anticipator(transcript)
        by_line: dict[int, list[str]] = {}
        for item in proposed:
            if 1 <= item.line <= len(members) and item.cue.strip():
                by_line.setdefault(item.line, []).append(item.cue.strip())
        written = 0
        errors: list[str] = []
        for line, texts in by_line.items():
            try:
                await deposit(namespace, members[line - 1].record_id, texts, key)
            except Exception as exc:
                errors.append(f"{namespace}:{key}: cue deposit failed: {exc}")
                continue
            written += len(texts)
        return written, errors

    legacy = _legacy_tag_check(ctx, "semantic", lambda r, key: f"anticipated:{key}" in r.tags)
    return await _run_session_stage(ctx, "anticipate", "cues", legacy, work)


async def reflect_profile(ctx: PipelineContext) -> dict[str, object]:
    """H14: profile insights per consolidated session, once (done marker)."""
    policy = ConsolidationPolicy.bind(_policy_options(ctx, "episodic", "consolidation"))
    if not getattr(policy.options, "reflect_profile", False):
        return {"status": "skipped", "reason": "consolidation.reflect_profile is off"}
    if ctx.reflect is None or ctx.deposit_reflection is None:
        return {"status": "skipped", "reason": "no reflect LLM role / reflective memory"}
    reflector, deposit = ctx.reflect, ctx.deposit_reflection

    async def work(namespace: str, key: str, members: list[MemoryRecord]) -> tuple[int, list[str]]:
        proposed = await reflector([f"[{m.valid_from:%Y-%m-%d}] {m.content}" for m in members])
        written = 0
        errors: list[str] = []
        for text, evidence in proposed:
            ids = [members[i].record_id for i in evidence if 0 <= i < len(members)]
            if not (text.strip() and ids):
                continue
            try:
                await deposit(namespace, text.strip(), ids, key)
            except Exception as exc:  # e.g. evidence forgotten mid-cycle (R2-2)
                errors.append(f"{namespace}:{key}: reflection deposit failed: {exc}")
                continue
            written += 1
        return written, errors

    legacy = _legacy_tag_check(
        ctx, "reflective", lambda r, key: r.source.message_id == f"reflected:{key}"
    )
    return await _run_session_stage(ctx, "reflect_profile", "insights", legacy, work)


#: Name -> pipeline. Runners register from this table; the M11-adjacent names
#: are stable identifiers used in schedules and dead-letter reporting.
PIPELINES: dict[str, Pipeline] = {
    "consolidate": consolidate,
    "reorganize": reorganize,
    "extract_graph": extract_graph,
    "mine_facts": mine_facts,
    "anticipate": anticipate,
    "reflect_profile": reflect_profile,
    "check_watches": check_watches,
    "decay_sweep": decay_sweep,
    "compress": compress,
    "sleep_compute": sleep_compute,
    "event_log_prune": event_log_prune,
}
