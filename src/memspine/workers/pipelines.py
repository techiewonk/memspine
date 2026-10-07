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
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from typing import Any, Protocol

from memspine.config import constants
from memspine.config.schema import MemspineConfig
from memspine.core.escaping import escape_markers
from memspine.core.event_date import (
    anchor_turn,
    cited_turns,
    happened_label,
    happened_of,
    label_start,
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
from memspine.core.query_shape import content_words
from memspine.core.records import MemoryRecord, RecordStatus, SourceInfo, chrono_key
from memspine.core.temporal_resolve import WeekMode
from memspine.exceptions import ConfigError, ConflictError
from memspine.memories.associative.communities import (
    PartitionResult,
    communities_available,
    partition_graph,
)
from memspine.memories.associative.entities import (
    MENTIONS_REL,
    EntityPolicy,
    canonical_entity,
    entity_node_id,
    parse_entity_node,
)
from memspine.memories.associative.links import assert_within_budget, link_event
from memspine.memories.associative.resolution import (
    MERGE_METHODS,
    Embed,
    EntityResolver,
    KnownEntity,
    ResolveBatch,
)
from memspine.memories.episodic.lifecycle import passive_after, plan_transitions
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
from memspine.services.graph.base import GraphEdge, GraphStore
from memspine.workers.list_cards import (
    LIST_CLASSES_MARKER,
    DepositListCard,
    LabelClasses,
    derive_list_cards,
)

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
    "entity_summary_name",
    "entity_summary_node",
    "event_log_prune",
    "extract_graph",
    "mine_facts",
    "predict_calibrate",
    "reflect_profile",
    "reorganize",
    "resolve_mode",
    "session_lifecycle",
    "sleep_compute",
    "stage_marker",
    "summarize_entities",
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
#: #20: text -> the entity names a decision provider (GLiNER2) finds in it.
FindEntities = Callable[[str], Awaitable[list[str]]]
#: #56: (previous summary, new turns) -> the updated summary (``summarize@incremental``).
SummarizeIncremental = Callable[[str, str], Awaitable[str]]
#: #62: (opening cue, session date, known statements) -> predicted facts, one per line.
PredictEpisode = Callable[[str, str, list[str]], Awaitable[str]]
#: #62: (prediction, dated transcript, known statements) -> the facts the transcript
#: states that the known statements do not (``calibrate`` v2, ADR-049).
CalibrateEpisode = Callable[[str, str, list[str]], Awaitable[list[ExtractedFact]]]
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
#: #48: run the engine's retention expiry; returns the stage stats.
ExpireRetention = Callable[[], Awaitable[dict[str, object]]]
#: GP-6 (#17): ``[(entity name, dated fact lines)]`` -> {1-based index: summary}
#: (one batched LLM call, at most ``ENTITY_SUMMARY_BATCH`` entities).
SummarizeEntities = Callable[[list[tuple[str, list[str]]]], Awaitable[dict[int, str]]]


class DepositFact(Protocol):
    """C6': engine-side deposit of one mined fact through the write door
    (namespace, text, entity, attribute, parent ids, event time, session key);
    ``kind`` (G1a) is ``state`` / ``event``, or None for an unclassified fact;
    ``happened`` / ``said`` (#29) the fact's happened date and the day it was said;
    ``persons`` / ``location`` / ``topic`` (#28) its multi-view fields."""

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
        said: str | None = None,
        persons: list[str] | None = None,
        location: str | None = None,
        topic: str | None = None,
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
    #: #56: the incremental summary update (``session_summary.incremental``). None
    #: => every summary is written from all its turns (``summarize`` or extractive).
    summarize_incremental: SummarizeIncremental | None = None
    #: #62: the predict and calibrate calls (``consolidation.predict_calibrate``).
    #: Any of the three None => the predict_calibrate stage self-skips.
    predict_episode: PredictEpisode | None = None
    calibrate: CalibrateEpisode | None = None
    deposit_surprise: DepositFact | None = None
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
    #: #20: the session-level extractor (``extract_edges@session``: numbered turns
    #: in, edges with ``episode_indices`` out). None => record-level extraction.
    extract_session_edges: ExtractEdges | None = None
    #: #20: the decision provider's entity finder (GLiNER2): text -> entity names,
    #: the allowed-entity list of a session extraction. None => no list.
    find_entities: FindEntities | None = None
    #: C6' atomic-fact mining. Both None => the mine_facts stage self-skips.
    mine_facts: MineFacts | None = None
    #: #29: the batched LLM date fill (``consolidation.mine_event_dates_llm``).
    date_facts: DateFacts | None = None
    #: #30: the list-card deposit and the per-person class labeller
    #: (``consolidation.list_cards``). No deposit => the step self-skips.
    deposit_list_card: DepositListCard | None = None
    label_classes: LabelClasses | None = None
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
    #: #48: the engine's retention expiry (hard forget through its forget path).
    #: None => the retention_expire stage reports "skipped".
    expire_retention: ExpireRetention | None = None
    #: GP-6 (#17): the batched entity summariser (``summarize_entity`` role).
    #: None => entities over the free length get an extractive summary.
    summarize_entities: SummarizeEntities | None = None
    #: GP-7 (#18): the batched entity resolver (``resolve_entity@batch``) and the
    #: embedder that ranks candidates. None => ``resolve: llm`` acts as ``rules``.
    resolve_entities: ResolveBatch | None = None
    embed: Embed | None = None


Pipeline = Callable[[PipelineContext], Awaitable[dict[str, object]]]


async def retention_expire(ctx: PipelineContext) -> dict[str, object]:
    """#48: expire records past their ``retention.classes`` TTL (hard forget)."""
    if not ctx.config.retention.classes:
        return {"status": "skipped", "reason": "no retention.classes"}
    if ctx.expire_retention is None:
        return {"status": "skipped", "reason": "no engine forget path in this context"}
    return await ctx.expire_retention()


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
    incremental = policy.session_summary.incremental
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
            still_open = session.end >= now - gap
            if still_open and not incremental:
                continue  # session still open — a new record may yet join it
            if session.session_key in existing_keys:
                if not still_open:
                    # #56: a summary written while the session was open is closed
                    # without a call, so the derived stages now see the session;
                    # whatever ``incremental`` is now (it may have been switched off).
                    closed = await _close_open_summary(
                        ctx, namespace, session, active_summaries, now
                    )
                    summaries += closed
                    superseded += closed
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
                    still_open=still_open,
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
    *,
    still_open: bool = False,
) -> tuple[int, int]:
    # Cold-tier members must be inflated before anyone summarizes them.
    members = [inflate.inflate(by_id[record_id]) for record_id in session.record_ids]
    if not policy.worth_summarizing(members):
        return summaries, superseded
    # Membership drift: archive every prior summary whose window
    # overlaps this session — the fresh summary supersedes it (D-42).
    stale = [
        old
        for old in active_summaries
        if old.valid_to is not None
        and old.valid_from <= session.end
        and old.valid_to >= session.start
    ]
    summary_text = policy.fallback_summary(members)
    tags: list[str] = []
    mode: str | None = None
    if policy.session_summary.incremental:
        # #56 (ADR-048): incremental update or periodic full rebuild.
        summary_text, mode, since = await _incremental_summary(
            ctx, policy, namespace, members, stale, summary_text
        )
        tags.append(f"{constants.SUMMARY_SINCE_REBUILD_PREFIX}{since}")
        if still_open:
            tags.append(constants.SUMMARY_OPEN_TAG)
    elif ctx.summarize is not None:
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
    summary = _summary_record(namespace, session, members, summary_text, tags)
    extra: dict[str, object] = {}
    if mode is not None:
        extra = {"summary_mode": mode, "open_session": still_open}
    await _append_summary(ctx, namespace, session, summary, stale, now, extra)
    return summaries + 1, superseded + len(stale)


def _summary_record(
    namespace: str,
    session: Session,
    members: list[MemoryRecord],
    summary_text: str,
    tags: list[str],
) -> MemoryRecord:
    # A summary of a closed session is bi-temporally a closed fact:
    # its validity is exactly the session window (M4 semantics).
    return MemoryRecord(
        namespace=namespace,
        memory_type="semantic",
        content=summary_text,
        valid_from=session.start,
        valid_to=session.end,
        # N2: derived (and, with a summarize role, LLM-authored) content is
        # never privileged — constants.DERIVED_ROLE states the rule.
        # The members are the summary's parents: erasure of a member cascades to
        # the summary (forget-by-cascade), and the MTI walk sees the lineage.
        source=SourceInfo(
            role=constants.DERIVED_ROLE,
            channel="consolidation",
            message_id=session.session_key,
            parents=list(session.record_ids),
        ),
        # E1: a summary is DERIVED content — never more trusted than its
        # least-trusted member, and injection framing echoed into the summary
        # text (an LLM summarizing a poisoned episode) keeps the inert flag.
        trust=min(member.trust for member in members),
        # B9: a summary inherits its members' flags (monotone, like trust);
        # re-detection on the summary text alone misses paraphrased framing.
        instruction_flag=instruction_shaped(summary_text)
        or any(member.instruction_flag for member in members),
        tags=tags,
    )


async def _append_summary(
    ctx: PipelineContext,
    namespace: str,
    session: Session,
    summary: MemoryRecord,
    stale: list[MemoryRecord],
    now: datetime,
    extra: dict[str, object],
) -> None:
    """WRITE the summary, archive the summaries it supersedes, then CONSOLIDATE."""
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
                **extra,
            },
        )
    )


def _since_rebuild(summary: MemoryRecord) -> int | None:
    """#56: turns folded into ``summary`` since its last full rebuild (None: untagged)."""
    for tag in summary.tags:
        if tag.startswith(constants.SUMMARY_SINCE_REBUILD_PREFIX):
            value = tag[len(constants.SUMMARY_SINCE_REBUILD_PREFIX) :]
            return int(value) if value.isdigit() else None
    return None


def _render_members(members: list[MemoryRecord]) -> str:
    # B9 (F3): a flagged member reaches the summariser wrapped as data.
    return "\n".join(
        constants.INSTRUCTION_FLAG_WRAP.format(content=record.content)
        if record.instruction_flag
        else record.content
        for record in members
    )


async def _incremental_summary(
    ctx: PipelineContext,
    policy: ConsolidationPolicy,
    namespace: str,
    members: list[MemoryRecord],
    stale: list[MemoryRecord],
    fallback: str,
) -> tuple[str, str, int]:
    """#56: (summary text, mode, turns folded in since the last full rebuild).

    The previous version is the newest superseded summary whose parents are all
    still members of the session. Its new turns are folded in with one
    ``summarize@incremental`` call while fewer than ``rebuild_every`` turns were
    folded in since the last full rebuild; otherwise (or with no previous version,
    or when a member left the session) the summary is rebuilt from every turn.
    Without an LLM the deterministic extractive summary is used (it cannot drift).
    """
    if ctx.summarize is None:
        return fallback, "extractive", 0
    member_ids = {m.record_id for m in members}
    previous = [
        old
        for old in stale
        if old.source.parents
        and set(old.source.parents) <= member_ids
        and _since_rebuild(old) is not None
    ]
    previous.sort(key=lambda r: (r.recorded_at, r.record_id))
    if previous and ctx.summarize_incremental is not None:
        prev = previous[-1]
        covered = set(prev.source.parents)
        new = [m for m in members if m.record_id not in covered]
        since = (_since_rebuild(prev) or 0) + len(new)
        if new and since < policy.session_summary.rebuild_every:
            text = (
                constants.INSTRUCTION_FLAG_WRAP.format(content=prev.content)
                if prev.instruction_flag
                else prev.content
            )
            try:
                return (
                    await ctx.summarize_incremental(text, _render_members(new)),
                    "incremental",
                    since,
                )
            except Exception as exc:  # LLM is an enhancer, never a gate (N6)
                _log.warning(
                    "consolidate.incremental_fallback", namespace=namespace, error=str(exc)
                )
    try:
        return await ctx.summarize(_render_members(members)), "rebuild", 0
    except Exception as exc:  # LLM is an enhancer, never a gate (N6)
        _log.warning("consolidate.summarize_fallback", namespace=namespace, error=str(exc))
        return fallback, "extractive", 0


async def _close_open_summary(
    ctx: PipelineContext,
    namespace: str,
    session: Session,
    active_summaries: list[MemoryRecord],
    now: datetime,
) -> int:
    """#56: re-stamp the open-session summary of a now-closed session, no call.

    Same text, parents, trust and flags; the open tag is dropped and the copy
    supersedes the open version. Returns 1 when a summary was closed, else 0."""
    open_summary = next(
        (
            r
            for r in active_summaries
            if r.source.message_id == session.session_key and constants.SUMMARY_OPEN_TAG in r.tags
        ),
        None,
    )
    if open_summary is None:
        return 0
    closed = MemoryRecord(
        namespace=namespace,
        memory_type="semantic",
        content=open_summary.content,
        valid_from=session.start,
        valid_to=session.end,
        source=open_summary.source.model_copy(),
        trust=open_summary.trust,
        instruction_flag=open_summary.instruction_flag,
        tags=[t for t in open_summary.tags if t != constants.SUMMARY_OPEN_TAG],
    )
    await _append_summary(
        ctx,
        namespace,
        session,
        closed,
        [open_summary],
        now,
        {"summary_mode": "close", "open_session": False},
    )
    return 1


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


#: KB-12: MARKER payload carrying one namespace's community partition as a
#: delta (``set``: node -> anchor, ``drop``: nodes gone) plus the incremental
#: refresh counters. Written only with ``community.incremental``.
COMMUNITY_PARTITION_MARKER = "community_partition"


def _community_algorithm(options: CommunityOptions) -> str | None:
    """KB-12: the algorithm this sweep runs, or None for the D-40 no-op.

    ``auto`` and ``leiden`` need the ``[community]`` extra; without it the
    reorganizer stays the logged no-op it has always been. Only an explicit
    ``lpa`` runs without the extra.
    """
    if options.algorithm == "lpa":
        return "lpa"
    return "leiden" if communities_available() else None


def _anchors(labels: dict[str, int]) -> dict[str, str]:
    """A partition as node -> smallest member of its community: a numbering-free
    form, so a delta between two sweeps only lists nodes that really moved."""
    first: dict[int, str] = {}
    for node in sorted(labels):
        first.setdefault(labels[node], node)
    return {node: first[label] for node, label in labels.items()}


def _from_anchors(anchors: dict[str, str]) -> dict[str, int]:
    order = {anchor: i for i, anchor in enumerate(sorted(set(anchors.values())))}
    return {node: order[anchor] for node, anchor in anchors.items()}


async def _reorganize_records(ctx: PipelineContext, namespace: str) -> list[MemoryRecord]:
    """Every summary parent this pipeline wrote in ``namespace``, any status."""
    return [
        record
        for record in await ctx.storage.list_records(namespace, "semantic")
        if record.source.channel == "reorganize"
    ]


async def _community_links(ctx: PipelineContext, parent_id: str) -> set[str]:
    """The live member -> parent ``community`` links of one summary parent."""
    assert ctx.graph is not None
    return {
        edge.src
        for edge in await ctx.graph.edges_of(parent_id)
        if edge.rel_type == "community" and edge.dst == parent_id and edge.weight > 0.0
    }


async def _summarised_set(ctx: PipelineContext, parent_id: str) -> frozenset[str]:
    """The members a summary parent summarised: its ``derived_from`` targets
    (projected from the WRITE's provenance, never tombstoned)."""
    assert ctx.graph is not None
    return frozenset(
        edge.dst
        for edge in await ctx.graph.edges_of(parent_id)
        if edge.rel_type == "derived_from" and edge.src == parent_id
    )


async def _parent_partition(ctx: PipelineContext, parents: list[MemoryRecord]) -> dict[str, int]:
    """The previous partition as the live summary parents record it (one label
    per parent, in key order). Derived from the event log through the graph
    projection, so a rebuild reproduces it (D0.1)."""
    labels: dict[str, int] = {}
    for label, parent in enumerate(sorted(parents, key=lambda p: p.source.message_id or "")):
        for member in sorted(await _community_links(ctx, parent.record_id)):
            labels.setdefault(member, label)
    return labels


@dataclass
class _SummaryKeeper:
    """#84: match a drifted community to the live parent that summarised
    nearly the same members, so it keeps its summary instead of a rewrite."""

    threshold: float
    #: parent record id -> (parent, the member set it summarised).
    summarised: dict[str, tuple[MemoryRecord, frozenset[str]]]
    claimed: set[str] = field(default_factory=set)

    def match(self, member_ids: set[str]) -> MemoryRecord | None:
        best: MemoryRecord | None = None
        best_score = self.threshold
        for parent_id in sorted(self.summarised):
            if parent_id in self.claimed:
                continue
            parent, summarised = self.summarised[parent_id]
            union = member_ids | summarised
            score = len(member_ids & summarised) / len(union) if union else 0.0
            if score >= best_score and (best is None or score > best_score):
                best, best_score = parent, score
        return best


@dataclass
class _Community:
    """One detected community resolved to its live, summarisable members."""

    members: list[MemoryRecord]
    member_ids: list[str]
    key: str
    namespace: str


async def reorganize(ctx: PipelineContext) -> dict[str, object]:
    """D-40/D-42 background graph reorganizer: communities over each
    namespace's association graph (KB-9) → one consolidation-style summary
    parent per community of >= REORGANIZE_MIN_COMMUNITY_SIZE members, members
    linked to the parent via LINK events (ADR-015).

    The partition is the KB-12 hybrid (ADR-043): Leiden warm-started from the
    previous partition, then LPA refinement; with ``community.incremental`` a
    sleep only places new nodes until a refresh trigger fires. Summary parents
    and their ``community`` links are not partition input, so a summary never
    joins its own community.

    No-op ("skipped") without a graph store (associative disabled) or, unless
    ``algorithm: lpa``, without the ``[community]`` extra (D-40). Idempotent:
    the parent's provenance key fingerprints the full membership, so an
    unchanged community is skipped and a drifted one supersedes its stale
    parent (same pattern as consolidate), unless ``summary_keep_jaccard`` keeps
    it (#84). Parents mirror consolidation summaries: derived trust = min(member
    trust) (D-47 §5), instruction framing stays flagged, quarantined/non-active
    members never contribute.
    """
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    if ctx.graph is None:
        return {"status": "skipped", "reason": "no graph store (associative memory disabled)"}
    community_opts = CommunityPolicy.bind(_policy_options(ctx, "associative", "community")).options
    assert isinstance(community_opts, CommunityOptions)
    algorithm = _community_algorithm(community_opts)
    if algorithm is None:
        return {
            "status": "skipped",
            "reason": "graspologic-native not installed — `pip install memspine[community]` "
            "or set community.algorithm: lpa (D-40)",
        }
    inflate = CompressionPolicy.bind()
    keep_on = community_opts.summary_keep_jaccard < 1.0
    if community_opts.incremental:
        await ctx.session_index.refresh(ctx.storage)
    parents = 0
    superseded = 0
    kept = 0
    detected = 0
    modes: dict[str, str] = {}
    errors: list[str] = []
    fresh_keys: dict[str, set[str]] = {}  # namespace -> live community keys
    # KB-9: one partition per namespace, over that namespace's edges only, so
    # no community (and no summary parent) ever spans two tenants.
    for graph_namespace in await ctx.storage.list_namespaces():
        reorg = await _reorganize_records(ctx, graph_namespace)
        parent_ids = {record.record_id for record in reorg}
        active = [record for record in reorg if record.status is RecordStatus.ACTIVATED]
        edges = [
            edge
            for edge in await ctx.graph.edge_list(graph_namespace)
            # GP-2: entity ``mentions`` edges are not associations either.
            if edge.rel_type not in ("community", "mentions")
            and edge.src not in parent_ids
            and edge.dst not in parent_ids
        ]
        try:
            result: PartitionResult | None = await _partition_namespace(
                ctx, graph_namespace, edges, active, algorithm, community_opts
            )
        except Exception as exc:  # one bad namespace must not kill the sweep
            errors.append(f"{graph_namespace}: {exc}")
            _log.warning(
                "reorganize.partition_failed",
                namespace=graph_namespace,
                error=str(exc),
                exc_info=True,
            )
            result = None
        if result is None or result.collapsed:
            # No change: the namespace keeps every live parent it had.
            fresh_keys.setdefault(graph_namespace, set()).update(
                str(record.source.message_id) for record in active
            )
            continue
        if community_opts.incremental:
            modes[graph_namespace] = result.mode
        keeper: _SummaryKeeper | None = None
        if keep_on:
            keeper = _SummaryKeeper(
                community_opts.summary_keep_jaccard,
                {
                    parent.record_id: (parent, await _summarised_set(ctx, parent.record_id))
                    for parent in active
                },
            )
        resolved: list[_Community] = []
        for community in result.communities(community_opts.min_size):
            detected += 1
            try:
                found = await _resolve_community(ctx, inflate, community)
            except Exception as exc:  # one bad community must not kill the sweep
                errors.append(f"{community[0]}…: {exc}")
                _log.warning(
                    "reorganize.community_failed",
                    members=len(community),
                    error=str(exc),
                    exc_info=True,
                )
                continue
            if found is not None:
                resolved.append(found)
        if keeper is not None:
            # An exact match is claimed first, so a near match never takes it.
            live = {record.source.message_id: record.record_id for record in active}
            keeper.claimed.update(live[c.key] for c in resolved if c.key in live)
        for found in resolved:
            try:
                created, live_key = await _reorganize_community(ctx, found, keeper)
            except Exception as exc:  # one bad community must not kill the sweep
                errors.append(f"{found.member_ids[0]}…: {exc}")
                _log.warning(
                    "reorganize.community_failed",
                    members=len(found.member_ids),
                    error=str(exc),
                    exc_info=True,
                )
                continue
            fresh_keys.setdefault(found.namespace, set()).add(live_key)
            parents += created
            kept += int(live_key != found.key)
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
        "communities": detected,
        "parents": parents,
        "superseded": superseded,
    }
    if keep_on:
        stats["kept"] = kept
    if modes:
        stats["modes"] = modes
    if errors:
        stats.update(status="partial", errors=errors)
    return stats


async def _partition_namespace(
    ctx: PipelineContext,
    namespace: str,
    edges: list[GraphEdge],
    active: list[MemoryRecord],
    algorithm: str,
    options: CommunityOptions,
) -> PartitionResult:
    """One namespace's partition (KB-12): full (warm-started from the previous
    partition) or, with ``incremental``, new-node placement until a refresh
    trigger fires. Incremental state rides MARKER events, so a rebuild from
    the log reproduces it (D0.1)."""
    state = ctx.session_index.communities.get(namespace) if options.incremental else None
    if state is not None:
        previous = _from_anchors(state.anchors)
    else:
        previous = await _parent_partition(ctx, active)
    run = partial(
        partition_graph,
        edges,
        algorithm="lpa" if algorithm == "lpa" else "leiden",
        previous=previous or None,
        resolution=options.resolution,
        randomness=options.randomness,
        random_seed=options.random_seed,
        max_cluster_size=options.max_cluster_size,
        refine_passes=options.refine_passes,
        incremental_passes=options.incremental_passes,
    )
    # Partitioning is CPU work — keep it off the event loop (same pattern as
    # compress()'s zstd call).
    result: PartitionResult | None = None
    if state is not None and previous and state.sleeps + 1 < options.refresh_every:
        result = await asyncio.to_thread(run, mode="incremental")
        drifted = state.placed + result.placed > options.refresh_fraction * len(result.labels)
        if drifted or result.collapsed:
            # Refresh trigger: too much was placed incrementally, or the
            # incremental step collapsed — a full run decides instead (a
            # collapsed incremental run must never lock the namespace).
            result = None
    if result is None:
        result = await asyncio.to_thread(run, mode="full")
    if options.incremental and (state is not None or not result.collapsed):
        # A collapse is recorded too (no membership change, sleeps + 1), so the
        # sleep counter keeps advancing and ``refresh_every`` still fires.
        await _record_partition(ctx, namespace, state, result)
    return result


async def _record_partition(
    ctx: PipelineContext,
    namespace: str,
    state: CommunityState | None,
    result: PartitionResult,
) -> None:
    """Append the partition delta + refresh counters as a MARKER event."""
    assert ctx.append_event is not None
    before = state.anchors if state is not None else {}
    # A collapsed run changes no membership: its marker only advances counters.
    anchors = dict(before) if result.collapsed else _anchors(result.labels)
    if result.collapsed and state is not None:
        sleeps, placed = state.sleeps + 1, state.placed
    elif result.mode == "incremental" and state is not None:
        sleeps, placed = state.sleeps + 1, state.placed + result.placed
    else:
        sleeps, placed = 0, 0
    await ctx.append_event(
        MemoryEvent(
            kind=EventKind.MARKER,
            namespace=namespace,
            actor="system",
            payload={
                "marker": COMMUNITY_PARTITION_MARKER,
                "stage": "reorganize",
                "mode": result.mode,
                "set": {n: a for n, a in sorted(anchors.items()) if before.get(n) != a},
                "drop": sorted(n for n in before if n not in anchors),
                "sleeps_since_refresh": sleeps,
                "placed_since_refresh": placed,
            },
        )
    )


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


async def _resolve_community(
    ctx: PipelineContext, inflate: CompressionPolicy, community: list[str]
) -> _Community | None:
    """The live, summarisable members of ``community`` and its membership key;
    None when too few qualify."""
    members: list[MemoryRecord] = []
    for record_id in community:
        record = await ctx.storage.get_record(record_id)
        # Only live namespace truth feeds a summary (E1): quarantined or
        # non-active members are held/derived content, not community evidence,
        # and a summary parent is never a member of a community (no
        # summary-of-summary feedback loop).
        if (
            record is not None
            and record.status is RecordStatus.ACTIVATED
            and not record.quarantined
            and record.source.channel != "reorganize"
        ):
            members.append(inflate.inflate(record))
    if len(members) < constants.REORGANIZE_MIN_COMMUNITY_SIZE:
        return None
    namespaces = {member.namespace for member in members}
    if len(namespaces) > 1:
        # Links never cross namespaces (ADR-015), so a mixed community means
        # corrupted state — refuse loudly rather than pick a tenant.
        raise ValueError(f"community spans namespaces {sorted(namespaces)} — refusing to summarize")
    member_ids = sorted(member.record_id for member in members)
    return _Community(
        members=members,
        member_ids=member_ids,
        key=fingerprint_payload({"community": member_ids}),
        namespace=members[0].namespace,
    )


async def _keep_summary(ctx: PipelineContext, community: _Community, parent: MemoryRecord) -> None:
    """#84: re-point ``parent``'s membership links at ``community`` (new members
    linked, departed ones tombstoned); its summary text is left as it is."""
    assert ctx.append_event is not None
    linked = await _community_links(ctx, parent.record_id)
    wanted = set(community.member_ids)
    for member_id, weight in [
        *((m, 1.0) for m in sorted(wanted - linked)),
        *((m, 0.0) for m in sorted(linked - wanted)),
    ]:
        await ctx.append_event(
            link_event(
                community.namespace,
                member_id,
                parent.record_id,
                "community",
                weight=weight,
                reason="reorganize_keep",
                actor="system",
            )
        )


async def _reorganize_community(
    ctx: PipelineContext, community: _Community, keeper: _SummaryKeeper | None = None
) -> tuple[int, str]:
    """Write (or keep) the summary parent of one community. Returns
    ``(parents_created, live_key)``: the key of the parent that now stands for
    the community (a kept parent's own key under #84)."""
    assert ctx.append_event is not None
    namespace, members, member_ids, key = (
        community.namespace,
        community.members,
        community.member_ids,
        community.key,
    )
    # Same per-namespace unit as the engine's write verbs (M5): the
    # idempotency read, the summary WRITE and the membership LINKs must not
    # interleave with a concurrent forget cascade in this namespace.
    async with ctx.lock(namespace):
        live = {old.source.message_id: old for old in await _reorganize_summaries(ctx, namespace)}
        if key in live:
            if keeper is not None:
                # A parent kept under #84 may have drifted links; a community
                # back at its summarised set re-points them (no-op otherwise).
                await _keep_summary(ctx, community, live[key])
            return 0, key  # unchanged membership (idempotence)
        match = keeper.match(set(member_ids)) if keeper is not None else None
        if (
            match is not None
            and match.source.message_id in live
            # A kept summary may never claim more trust than a member it now
            # stands for (D-47 §5): a less-trusted newcomer forces a rewrite.
            and min(member.trust for member in members) >= match.trust
        ):
            assert keeper is not None
            keeper.claimed.add(match.record_id)
            await _keep_summary(ctx, community, match)
            return 0, str(match.source.message_id)
        ordered = sorted(members, key=chrono_key)
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
            source=SourceInfo(
                role=constants.DERIVED_ROLE,
                channel="reorganize",
                message_id=key,
                parents=member_ids,
            ),
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
    return 1, key


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


#: GP-7 (#18): MARKER payload carrying one extract_graph sweep's entity-resolution
#: decisions. ``decisions`` is a list of ``{"record_id", "namespace", "entity",
#: "attribute", "method"}`` dicts: the name (``entity``), the entity it resolved to
#: or, for ``contested``, the one it was kept apart from (``attribute``), keyed by
#: the source record that named it. These are the record-snapshot keys the M7
#: erasure walker scrubs, so erasing the source erases the decision too. A
#: session-level fact cites several turns (#20): its decision is one entry per
#: cited turn, sharing a ``group`` index, and the alias holds only while every
#: entry of the group is intact — erasing ANY cited turn erases the decision.
ENTITY_RESOLVED_MARKER = "entity_resolved"
#: GP-6 (#17): MARKER payload naming the entities one summarize_entities sweep
#: summarised. ``entities`` is a list of ``{"record_id", "namespace", "entity",
#: "content_fingerprint"}`` dicts: the summary record, the entity node and the
#: membership fingerprint it was written from. Erasing the summary (a member's
#: hard forget cascades to it) scrubs the entry, so the entity is summarised again.
ENTITY_SUMMARIZED_MARKER = "entity_summarized"


def _erasable_entries(raw: object) -> list[dict[str, Any]]:
    return [entry for entry in raw if isinstance(entry, dict)] if isinstance(raw, list) else []


_RESOLVE_MODES = ("off", "rules", "llm")


def resolve_mode(opts: dict[str, object]) -> str:
    mode = str(opts.get("resolve", "off") or "off")
    if mode not in _RESOLVE_MODES:
        raise ConfigError(
            f"memories.semantic.policies.extract_graph.resolve must be one of "
            f"{list(_RESOLVE_MODES)}, got {mode!r}"
        )
    return mode


def _record_names(record: MemoryRecord) -> list[str]:
    """The raw entity names a record carries: its ``entity`` and ``dst:`` tags."""
    names = [record.entity] if record.entity else []
    names.extend(tag[4:] for tag in record.tags if tag.startswith("dst:"))
    return [name for name in names if name.strip()]


def _known_entities(records: list[MemoryRecord]) -> dict[str, KnownEntity]:
    """GP-7: the entities live, unquarantined records name: canonical -> its most
    frequent spelling and the trust of the most trusted record naming it."""
    policy = EntityPolicy(blocklist=constants.ENTITY_NODE_BLOCKLIST)
    spellings: dict[str, Counter[str]] = {}
    trust: dict[str, float] = {}
    for record in records:
        if record.status is not RecordStatus.ACTIVATED or record.quarantined:
            continue
        for name in _record_names(record):
            canonical = canonical_entity(name)
            if not policy.accepts(canonical):
                continue
            spellings.setdefault(canonical, Counter())[name.strip()] += 1
            trust[canonical] = max(trust.get(canonical, 0.0), record.trust)
    return {
        c: KnownEntity(c, min(counts, key=lambda n: (-counts[n], n)), trust[c])
        for c, counts in spellings.items()
    }


async def _resolve_edges(
    ctx: PipelineContext,
    namespace: str,
    mode: str,
    known: list[MemoryRecord],
    extracted: list[tuple[MemoryRecord, list[ExtractedEdge]]],
    parent_sets: list[list[MemoryRecord]] | None = None,
) -> tuple[list[tuple[MemoryRecord, list[ExtractedEdge]]], dict[str, int]]:
    """GP-7: resolve every edge's names, rewrite merged ones to the known entity's
    spelling, and append the decisions as one ``entity_resolved`` MARKER.

    ``parent_sets`` (parallel to ``extracted``) are the records each entry's
    facts cite; every decision is keyed by all of them (erasure of any one
    erases it), not only by the owner the trust guard judges."""
    assert ctx.append_event is not None
    requests: list[tuple[str, float]] = []
    owners: list[str] = []
    keyed_by: list[tuple[str, ...]] = []
    for i, (record, edges) in enumerate(extracted):
        cited = parent_sets[i] if parent_sets is not None else [record]
        parent_ids = tuple(sorted({p.record_id for p in cited} | {record.record_id}))
        for edge in edges:
            for name in (edge.src_entity, edge.dst_entity):
                requests.append((name, record.trust))
                owners.append(record.record_id)
                keyed_by.append(parent_ids)
    resolver = EntityResolver(
        _known_entities(known),
        dict(ctx.session_index.entity_aliases.get(namespace, {})),
        embed=ctx.embed,
        llm=ctx.resolve_entities if mode == "llm" else None,
    )
    try:
        outcomes = await resolver.resolve(requests)
    except Exception as exc:  # the LLM is an enhancer, never a gate (N6)
        _log.warning("extract_graph.resolve_failed", namespace=namespace, error=str(exc))
        return extracted, {"resolve_errors": 1}
    rewrite: dict[tuple[str, str], str] = {}
    decisions: list[dict[str, object]] = []
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    counts: dict[str, int] = {"resolved": 0, "contested": 0}
    group = 0
    for (name, _trust), owner, parent_ids, outcome in zip(
        requests, owners, keyed_by, outcomes, strict=True
    ):
        if outcome.target is not None:
            rewrite[(owner, name)] = outcome.target
        if outcome.method not in (*MERGE_METHODS, "contested"):
            continue
        other = outcome.target if outcome.target is not None else outcome.candidate
        key = (canonical_entity(name), outcome.method, parent_ids)
        if key in seen:
            continue
        seen.add(key)
        counts["resolved" if outcome.target is not None else "contested"] += 1
        for parent_id in parent_ids:
            decisions.append(
                {
                    "record_id": parent_id,
                    "namespace": namespace,
                    "entity": name,
                    "attribute": other,
                    "method": outcome.method,
                    "group": group,
                }
            )
        group += 1
    counts["resolve_llm_calls"] = resolver.llm_calls
    if decisions:
        event = MemoryEvent(
            kind=EventKind.MARKER,
            namespace=namespace,
            actor="system",
            payload={
                "marker": ENTITY_RESOLVED_MARKER,
                "stage": "extract_graph",
                "decisions": decisions,
            },
        )
        await ctx.append_event(event)
        ctx.session_index.observe(event)
    resolved: list[tuple[MemoryRecord, list[ExtractedEdge]]] = []
    for record, edges in extracted:
        out = []
        for edge in edges:
            src = rewrite.get((record.record_id, edge.src_entity), edge.src_entity)
            dst = rewrite.get((record.record_id, edge.dst_entity), edge.dst_entity)
            if (src, dst) != (edge.src_entity, edge.dst_entity):
                edge = edge.model_copy(update={"src_entity": src, "dst_entity": dst})
            out.append(edge)
        resolved.append((record, out))
    return resolved, counts


#: GR-4: at most this many known entity names ride along as extraction context.
EDGE_CONTEXT_MAX_ENTITIES = 50


def _one_line(text: str, limit: int = 400) -> str:
    """An episode squeezed onto one prompt line (GR-4 context, never a source)."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "..."


def _context_entities(known: list[MemoryRecord]) -> list[str]:
    """GR-4: the entity names already known in the namespace, most recent first."""
    entities: list[str] = []
    for record in sorted(known, key=lambda r: r.recorded_at, reverse=True):
        if record.entity and record.entity not in entities:
            entities.append(record.entity)
    return entities[:EDGE_CONTEXT_MAX_ENTITIES]


def _session_transcript(members: list[MemoryRecord]) -> str:
    """#20: the session as ``[n] [YYYY-MM-DD] turn`` lines (1-based), the numbers
    the extractor cites in ``episode_indices``."""
    return "\n".join(
        f"[{n}] [{m.valid_from:%Y-%m-%d}] {_one_line(m.content, 2000)}"
        for n, m in enumerate(members, 1)
    )


def _cited(members: list[MemoryRecord], indices: list[int]) -> list[MemoryRecord]:
    """#20: the session turns an edge cites (valid 1-based indices, transcript
    order); the whole session when it cites none, so a fact always has parents."""
    cited = [members[i - 1] for i in sorted(set(indices)) if 1 <= i <= len(members)]
    return cited or list(members)


#: #20: the stage name of the per-session ``stage_done`` markers extract_graph
#: appends under ``granularity: session`` (the session watermark).
EXTRACT_GRAPH_SESSION_STAGE = "extract_graph"


def _edge_contexts(sources: list[MemoryRecord], known: list[MemoryRecord]) -> list[EdgeContext]:
    """GR-4: per source, its event time, the episodes just before it (same
    group, at most ``MAX_PREVIOUS_EPISODES``) and the entity names already
    known in the namespace (most recent first)."""
    entities = _context_entities(known)
    episodes = sorted(
        (r for r in sources if r.memory_type == "episodic"),
        key=chrono_key,
    )
    contexts: list[EdgeContext] = []
    for record in sources:
        previous = [
            e
            for e in episodes
            if e.group_id == record.group_id and chrono_key(e) < chrono_key(record)
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
    # #20: ``granularity: session`` sends each consolidated session in one call
    # (needs the session extractor the engine builds for it); record = per source.
    session_extract = (
        ctx.extract_session_edges if opts.get("granularity", "record") == "session" else None
    )
    resolving = resolve_mode(opts)
    resolution: dict[str, int] = {}
    written = 0
    linked = 0
    skipped = 0
    quarantined = 0
    provenance = 0
    errors: list[str] = []
    screen = ctx.screen or _local_screen(ctx)
    protected = ctx.config.firewall.protected_keys
    append = ctx.append_event
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
                    if record.source.channel == constants.ENTITY_SUMMARY_CHANNEL:
                        continue  # GP-6: a summary of facts is not a new fact source
                    if (
                        record.status is RecordStatus.ACTIVATED
                        and not record.quarantined
                        # N2: injection framing must not be laundered into "facts".
                        and not record.instruction_flag
                    ):
                        sources.append(record)
            done: dict[str, str] = {}
            # Extraction first, writes after (GP-7 resolves every name in one pass).
            # Each entry: (owner, edges, parents). The owner is the record the
            # resolver judges trust by and keys its decision on; the parents are
            # what the fact cites: the source record, or the session turns (#20).
            extracted: list[tuple[MemoryRecord, list[ExtractedEdge]]] = []
            parent_sets: list[list[MemoryRecord]] = []
            sessions_done: list[tuple[str, str]] = []
            if session_extract is not None:
                # #20: one call per consolidated session (numbered turns); the
                # session's members leave the per-record pass.
                member_of: dict[str, str] = {}
                for (session_ns, session_key), member_ids in index.sessions.items():
                    if session_ns == namespace:
                        for member_id in member_ids:
                            member_of.setdefault(member_id, session_key)
                grouped: dict[str, list[MemoryRecord]] = {}
                loose: list[MemoryRecord] = []
                for record in sources:
                    owner_key = member_of.get(record.record_id)
                    if owner_key is None:
                        loose.append(record)
                    else:
                        grouped.setdefault(owner_key, []).append(record)
                names = _context_entities(known)
                for session_key, members in grouped.items():
                    members.sort(key=chrono_key)
                    members_fp = _members_fp([m.record_id for m in members])
                    if index.membership_done(
                        EXTRACT_GRAPH_SESSION_STAGE, namespace, members_fp
                    ) or all(
                        index.graph_sources.get((namespace, m.record_id)) == m.content_fingerprint
                        for m in members
                    ):
                        already += len(members)
                        continue
                    allowed: list[str] = []
                    if ctx.find_entities is not None:
                        try:
                            allowed = list(
                                await ctx.find_entities("\n".join(m.content for m in members))
                            )
                        except Exception as exc:  # an enhancer, never a gate
                            _log.warning("extract_graph.entities_failed", error=str(exc))
                    context = EdgeContext(
                        reference_time=members[-1].valid_from,
                        entities=names,
                        allowed_entities=allowed,
                    )
                    try:
                        edges = await session_extract(_session_transcript(members), context)
                    except Exception as exc:  # the LLM is an enhancer, never a gate (N6)
                        errors.append(f"{namespace}:{session_key}: {exc}")
                        _log.warning(
                            "extract_graph.extract_failed", session_key=session_key, error=str(exc)
                        )
                        continue  # no watermark: retried next sweep
                    for member in members:
                        done[member.record_id] = member.content_fingerprint
                    sessions_done.append((session_key, members_fp))
                    for edge in edges:
                        if edge.confidence < min_conf:
                            continue
                        cited = _cited(members, edge.episode_indices)
                        # The resolver's trust guard judges the least trusted turn.
                        owner = min(cited, key=lambda m: (m.trust, chrono_key(m)))
                        extracted.append((owner, [edge]))
                        parent_sets.append(cited)
                sources = loose
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
                extracted.append((record, [e for e in edges if e.confidence >= min_conf]))
                parent_sets.append([record])
            if resolving != "off" and any(edges for _, edges in extracted):
                # GP-7: one resolution pass (at most one batched LLM call) per sweep.
                extracted, counts = await _resolve_edges(
                    ctx, namespace, resolving, [*known, *sources], extracted, parent_sets
                )
                for name, count in counts.items():
                    resolution[name] = resolution.get(name, 0) + count

            async def emit(
                edge: ExtractedEdge,
                parents: list[MemoryRecord],
                namespace: str = namespace,
                existing: dict[str, MemoryRecord] = existing,
            ) -> None:
                """One extracted edge -> a fact record (+ ``asserted`` LINKs) whose
                parents are ``parents``: the source record, or the session turns
                the edge cites (#20)."""
                nonlocal written, linked, skipped, quarantined, provenance
                if edge.confidence < min_conf:
                    return
                key = _edge_key(namespace, edge)
                if key in existing:
                    skipped += 1
                    # GR-9: a verbatim duplicate (same src, rel, dst and kind) adds
                    # its source episode to the fact's provenance; no LLM call.
                    for parent in parents:
                        if await _add_edge_provenance(ctx, existing[key], edge, parent):
                            provenance += 1
                    return
                trusts = [p.trust for p in parents]
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
                    valid_from=_edge_valid_from(edge, parents[-1].valid_from),
                    source=SourceInfo(
                        role=constants.DERIVED_ROLE,
                        channel="extract_graph",
                        message_id=key,
                        parents=[p.record_id for p in parents],
                    ),
                )
                if ctx.write_fact is not None:
                    # The semantic door: firewall (N2, cap = the sources' trust),
                    # M5 dedup and the M4 ladder, so a state edge supersedes the
                    # older value; event edges carry no attribute and are ADDed.
                    stored = await ctx.write_fact(fact, trusts)
                    existing[key] = stored
                    if stored.quarantined:
                        quarantined += 1  # held content gains no graph reach (E1)
                        return
                    if stored.record_id != fact.record_id:
                        skipped += 1  # merged into / rejected by an existing fact
                        return
                    fact = stored
                else:
                    # Derived trust never exceeds the source (E1): the cap rides
                    # the same firewall screening as every other derived write.
                    fact, reasons = await screen(fact, trusts)
                    payload: dict[str, object] = {
                        "record": fact.model_dump(mode="json"),
                        "extract_graph": {"source_record_id": parents[0].record_id},
                    }
                    if reasons:
                        payload["firewall"] = {"reasons": reasons}
                    await append(
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
                        return
                written += 1
                for parent in parents:
                    # Associate the fact with its source (non-reserved rel: budget
                    # applies). A saturated source keeps the record, skips the link.
                    if ctx.graph is not None:
                        try:
                            await assert_within_budget(ctx.graph, parent.record_id)
                        except ConflictError:
                            _log.warning(
                                "extract_graph.link_budget_full", record_id=parent.record_id
                            )
                            continue
                    await append(
                        link_event(
                            namespace,
                            parent.record_id,
                            fact.record_id,
                            "asserted",
                            # GP-10: an edge is never stronger than its
                            # confidence or the trust of what it links.
                            weight=max(0.0, min(1.0, edge.confidence, parent.trust, fact.trust)),
                            reason="extract_graph",
                            actor="system",
                        )
                    )
                    linked += 1

            for (_owner, edges), parents in zip(extracted, parent_sets, strict=True):
                for edge in edges:
                    await emit(edge, parents)
            if done:
                # GP-8a: one watermark marker per namespace per sweep, appended
                # after the facts, so a crash mid-sweep re-extracts idempotently.
                await append(
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
            for session_key, members_fp in sessions_done:
                # #20: the session watermark (a re-consolidated session with the
                # same live turns is not sent again).
                await append(
                    stage_marker(
                        namespace, EXTRACT_GRAPH_SESSION_STAGE, session_key, members_fp=members_fp
                    )
                )
                index.mark(
                    EXTRACT_GRAPH_SESSION_STAGE,
                    namespace,
                    session_key,
                    done=True,
                    members_fp=members_fp,
                )
    stats: dict[str, object] = {
        "status": "ok" if not errors else "partial",
        "edges_written": written,
        "links": linked,
        "skipped_existing": skipped,
        "skipped_sources": already,
        "quarantined": quarantined,
        "provenance_added": provenance,
    }
    if resolving != "off":
        stats.update(resolution)
    if errors:
        stats["errors"] = errors
    return stats


def _entity_summary_options(ctx: PipelineContext) -> dict[str, object] | None:
    """``memories.associative.policies.entity_summaries``: None when off, else
    its options (``true`` = defaults)."""
    mem = ctx.config.memories.get("associative")
    raw = mem.policies.get("entity_summaries") if mem is not None else None
    if not raw:
        return None
    return dict(raw) if isinstance(raw, dict) else {}


def _int_option(opts: dict[str, object], key: str, default: int) -> int:
    raw = opts.get(key, default)
    value = int(raw) if isinstance(raw, (int, float, str)) else default
    if value < 1:
        raise ConfigError(f"memories.associative.policies.entity_summaries.{key} must be >= 1")
    return value


def entity_summary_node(record: MemoryRecord) -> str | None:
    """The entity node an entity summary record is about (its ``about:`` tag)."""
    prefix = constants.ENTITY_SUMMARY_ABOUT_PREFIX
    for tag in record.tags:
        if tag.startswith(prefix) and tag[len(prefix) :].strip():
            return entity_node_id(record.namespace, canonical_entity(tag[len(prefix) :]))
    return None


def entity_summary_name(record: MemoryRecord) -> str:
    """The display name an entity summary record is about."""
    prefix = constants.ENTITY_SUMMARY_ABOUT_PREFIX
    return next((t[len(prefix) :] for t in record.tags if t.startswith(prefix)), "")


def _display_name(canonical: str, members: list[MemoryRecord]) -> str:
    """The most frequent spelling the members give the entity (ties: lowest)."""
    counts: Counter[str] = Counter()
    for member in members:
        for name in _record_names(member):
            if canonical_entity(name) == canonical:
                counts[" ".join(name.split())] += 1
    return min(counts, key=lambda n: (-counts[n], n)) if counts else canonical


def _fact_lines(members: list[MemoryRecord]) -> list[str]:
    ordered = sorted(members, key=chrono_key)
    return [f"[{m.valid_from:%Y-%m-%d}] {' '.join(m.content.split())}" for m in ordered]


def _newest_lines(lines: list[str], max_chars: int) -> str:
    """The newest lines that fit ``max_chars``, in date order (no-LLM fallback)."""
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        if size + len(line) + (1 if kept else 0) > max_chars:
            break
        kept.append(line)
        size += len(line) + (1 if len(kept) > 1 else 0)
    if not kept and lines:
        return lines[-1][:max_chars].rstrip()
    return "\n".join(reversed(kept))


@dataclass
class _EntityWork:
    node: str
    display: str
    members: list[MemoryRecord]
    fingerprint: str
    old: list[MemoryRecord]
    lines: list[str]
    text: str = ""


async def summarize_entities(ctx: PipelineContext) -> dict[str, object]:
    """GP-6 (#17): one summary record per entity node whose membership changed.

    For each ``ent:`` node in a namespace, the members are the live (active,
    unquarantined) records with a live ``mentions`` edge to it. An entity whose
    membership fingerprint (member ids and content fingerprints) matches the last
    sweep's watermark (``entity_summarized`` markers) and still has its summary is
    skipped: no LLM call. Otherwise its dated fact lines are the summary for free
    while they fit ``free_chars`` (2,000); longer ones are summarised by the
    ``summarize_entity`` role, ``batch_size`` (30) entities per call, or, without
    the role or on a failed call, cut to the newest lines that fit.

    The summary is a derived record (channel ``entity_summary``, tags
    ``entity_summary`` and ``about:<Name>``): its parents are the members, so a
    hard forget of a member cascades to it (ADR-039); its trust is the least
    member trust; it passes the derived-record firewall. A changed membership
    supersedes the old summary (archived); an entity with no live member left
    loses its summary; a soft-forgotten member changes the membership, so the next
    sweep re-derives the summary without it. Off by default
    (``memories.associative.policies.entity_summaries``); needs the entity layer.
    """
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    opts = _entity_summary_options(ctx)
    if opts is None:
        return {"status": "skipped", "reason": "associative.policies.entity_summaries is off"}
    if ctx.graph is None:
        return {"status": "skipped", "reason": "no graph store (associative memory disabled)"}
    free_chars = _int_option(opts, "free_chars", constants.ENTITY_SUMMARY_FREE_CHARS)
    batch = _int_option(opts, "batch_size", constants.ENTITY_SUMMARY_BATCH)
    screen = ctx.screen or _local_screen(ctx)
    inflate = CompressionPolicy.bind()
    index = ctx.session_index
    await index.refresh(ctx.storage)
    counts = {"summarized": 0, "free": 0, "llm_calls": 0, "unchanged": 0, "superseded": 0}
    quarantined = 0
    errors: list[str] = []
    for namespace in await ctx.storage.list_namespaces():
        async with ctx.lock(namespace):
            members_of: dict[str, set[str]] = {}
            for edge in await ctx.graph.edge_list(namespace):
                if edge.rel_type != MENTIONS_REL or edge.weight <= 0:
                    continue
                parsed = parse_entity_node(edge.dst)
                if parsed is not None and parsed[0] == namespace:
                    members_of.setdefault(edge.dst, set()).add(edge.src)
            live: dict[str, list[MemoryRecord]] = {}
            for record in await ctx.storage.list_records(namespace, "semantic"):
                if (
                    record.source.channel != constants.ENTITY_SUMMARY_CHANNEL
                    or record.status
                    not in (
                        RecordStatus.ACTIVATED,
                        RecordStatus.QUARANTINED,
                    )
                ):
                    continue
                node = entity_summary_node(record)
                if node is not None:
                    live.setdefault(node, []).append(record)
            work: list[_EntityWork] = []
            for node in sorted(members_of):
                members: list[MemoryRecord] = []
                for record_id in sorted(members_of[node]):
                    member = await ctx.storage.get_record(record_id)
                    if (
                        member is not None
                        and member.namespace == namespace
                        and member.status is RecordStatus.ACTIVATED
                        and not member.quarantined
                        and member.source.channel != constants.ENTITY_SUMMARY_CHANNEL
                    ):
                        members.append(inflate.inflate(member))
                if not members:
                    continue
                fp = fingerprint_payload(
                    {
                        "entity_members": sorted(
                            f"{m.record_id}:{m.content_fingerprint}" for m in members
                        )
                    }
                )
                old = live.pop(node, [])
                key = fingerprint_payload({"entity_summary": [node, fp]})
                if old and (
                    index.entity_summaries.get((namespace, node)) == fp
                    or any(r.source.message_id == key for r in old)
                ):
                    counts["unchanged"] += 1
                    continue
                parsed = parse_entity_node(node)
                assert parsed is not None
                lines = _fact_lines(members)
                work.append(
                    _EntityWork(node, _display_name(parsed[1], members), members, fp, old, lines)
                )
            # Free first: the lines themselves, while they fit.
            costly: list[_EntityWork] = []
            for item in work:
                joined = "\n".join(item.lines)
                if len(joined) <= free_chars:
                    item.text = joined
                    counts["free"] += 1
                else:
                    costly.append(item)
            for start in range(0, len(costly), batch):
                chunk = costly[start : start + batch]
                answers: dict[int, str] = {}
                if ctx.summarize_entities is not None:
                    counts["llm_calls"] += 1
                    try:
                        answers = await ctx.summarize_entities(
                            [
                                (
                                    item.display,
                                    item.lines[-constants.ENTITY_SUMMARY_MAX_INPUT_LINES :],
                                )
                                for item in chunk
                            ]
                        )
                    except Exception as exc:  # the LLM is an enhancer, never a gate (N6)
                        errors.append(f"{namespace}: summarize_entity failed: {exc}")
                        _log.warning(
                            "summarize_entities.llm_failed", namespace=namespace, error=str(exc)
                        )
                for offset, item in enumerate(chunk, start=1):
                    text = " ".join(str(answers.get(offset, "")).split())
                    item.text = text if text else _newest_lines(item.lines, free_chars)
            written: list[dict[str, object]] = []
            for item in work:
                summary, reasons = await _write_entity_summary(ctx, namespace, item, screen)
                counts["summarized"] += 1
                quarantined += int(bool(reasons))
                counts["superseded"] += await _archive_summaries(
                    ctx, namespace, item.old, "membership_drift"
                )
                written.append(
                    {
                        "record_id": summary.record_id,
                        "namespace": namespace,
                        "entity": item.node,
                        "content_fingerprint": item.fingerprint,
                    }
                )
            # An entity with no live member left keeps no summary.
            for old in live.values():
                counts["superseded"] += await _archive_summaries(ctx, namespace, old, "entity_gone")
            if written:
                event = MemoryEvent(
                    kind=EventKind.MARKER,
                    namespace=namespace,
                    actor="system",
                    payload={
                        "marker": ENTITY_SUMMARIZED_MARKER,
                        "stage": "summarize_entities",
                        "entities": written,
                    },
                )
                await ctx.append_event(event)
                index.observe(event)
    stats: dict[str, object] = {"status": "ok" if not errors else "partial", **counts}
    stats["quarantined"] = quarantined
    if errors:
        stats["errors"] = errors
    return stats


async def _write_entity_summary(
    ctx: PipelineContext, namespace: str, item: _EntityWork, screen: ScreenDerived
) -> tuple[MemoryRecord, list[str]]:
    """Append one entity summary record (firewall-screened, trust-capped)."""
    assert ctx.append_event is not None
    member_ids = sorted(m.record_id for m in item.members)
    trust = min(m.trust for m in item.members)
    summary = MemoryRecord(
        namespace=namespace,
        memory_type="semantic",
        content=item.text,
        tags=[
            constants.ENTITY_SUMMARY_TAG,
            f"{constants.ENTITY_SUMMARY_ABOUT_PREFIX}{item.display}",
        ],
        valid_from=max(m.valid_from for m in item.members),
        source=SourceInfo(
            role=constants.DERIVED_ROLE,
            channel=constants.ENTITY_SUMMARY_CHANNEL,
            message_id=fingerprint_payload({"entity_summary": [item.node, item.fingerprint]}),
            parents=member_ids,
        ),
        trust=trust,
        # Echoed injection framing stays flagged (as consolidation summaries do).
        instruction_flag=instruction_shaped(item.text)
        or any(m.instruction_flag for m in item.members),
    )
    summary, reasons = await screen(summary, [trust])
    payload: dict[str, object] = {"record": summary.model_dump(mode="json")}
    if reasons:
        payload["firewall"] = {"reasons": reasons}
        _log.warning(
            "memory.quarantined", namespace=namespace, record_id=summary.record_id, reasons=reasons
        )
    await ctx.append_event(
        MemoryEvent(kind=EventKind.WRITE, namespace=namespace, actor="system", payload=payload)
    )
    return summary, reasons


async def _archive_summaries(
    ctx: PipelineContext, namespace: str, old: list[MemoryRecord], reason: str
) -> int:
    assert ctx.append_event is not None
    for record in old:
        await ctx.append_event(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=namespace,
                actor="system",
                payload={
                    "record_id": record.record_id,
                    "set": {
                        "status": RecordStatus.ARCHIVED.value,
                        "superseded_at": datetime.now(UTC).isoformat(),
                    },
                    "transition": "entity_summary->superseded",
                    "reason": reason,
                },
            )
        )
    return len(old)


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
class CommunityState:
    """KB-12 incremental community state of one namespace (from the log)."""

    #: node -> anchor (the smallest member of its community).
    anchors: dict[str, str] = field(default_factory=dict)
    #: Sleeps since the last full refresh.
    sleeps: int = 0
    #: Nodes placed incrementally since the last full refresh.
    placed: int = 0


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
    #: KB-12: namespace -> community partition + refresh counters, folded from
    #: ``community_partition`` markers (``community.incremental``).
    communities: dict[str, CommunityState] = field(default_factory=dict)
    #: #30: (namespace, fact record id) -> the LLM list class ("" = none), folded
    #: from ``list_classes`` markers so a fact is classed once.
    list_classes: dict[tuple[str, str], str] = field(default_factory=dict)
    #: GP-7 (#18): namespace -> {alias canonical: target canonical}, folded from
    #: the merge decisions of ``entity_resolved`` markers.
    entity_aliases: dict[str, dict[str, str]] = field(default_factory=dict)
    #: GP-6 (#17): (namespace, entity node id) -> membership fingerprint the
    #: ``summarize_entities`` stage last summarised (``entity_summarized`` markers).
    entity_summaries: dict[tuple[str, str], str] = field(default_factory=dict)

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
            self.list_classes.pop((event.namespace, str(payload.get("record_id", ""))), None)
        elif event.kind is EventKind.CONSOLIDATE:
            key = str(payload.get("session_key", ""))
            # #56: a summary of a still-open session is not a consolidated
            # session yet; the derived stages wait for its closing CONSOLIDATE.
            if key and not payload.get("open_session"):
                members = [str(m) for m in payload.get("member_record_ids", [])]
                self.sessions[(event.namespace, key)] = members
        elif event.kind is EventKind.MARKER:
            marker = payload.get("marker")
            if marker == GRAPH_EXTRACTED_MARKER:
                for record_id, source_fp in _graph_marker_sources(payload.get("sources")):
                    self.graph_sources[(event.namespace, record_id)] = source_fp
            elif marker == COMMUNITY_PARTITION_MARKER:
                self._fold_partition(event.namespace, payload)
            elif marker == LIST_CLASSES_MARKER and isinstance(payload.get("labels"), dict):
                for record_id, label in payload["labels"].items():
                    self.list_classes[(event.namespace, str(record_id))] = str(label)
            elif marker == ENTITY_RESOLVED_MARKER:
                self._fold_resolutions(event.namespace, payload.get("decisions"))
            elif marker == ENTITY_SUMMARIZED_MARKER:
                for entry in _erasable_entries(payload.get("entities")):
                    entity, fp = entry.get("entity"), entry.get("content_fingerprint")
                    if entity and fp:  # a redacted entry (erased summary) is no watermark
                        self.entity_summaries[(event.namespace, str(entity))] = str(fp)
            elif marker in ("stage_done", "stage_cleared"):
                fp = payload.get("members_fp")
                self.mark(
                    str(payload.get("stage", "")),
                    event.namespace,
                    str(payload.get("session_key", "")),
                    done=marker == "stage_done",
                    members_fp=str(fp) if fp else None,
                )

    def _fold_resolutions(self, namespace: str, raw: object) -> None:
        aliases = self.entity_aliases.setdefault(namespace, {})
        # Entries sharing a ``group`` are one decision keyed by several cited
        # records; an entry without one (pre-fix logs) is its own decision.
        groups: dict[object, list[dict[str, Any]]] = {}
        for i, entry in enumerate(_erasable_entries(raw)):
            groups.setdefault(entry.get("group", ("solo", i)), []).append(entry)
        for entries in groups.values():
            # Any erased entry (a forgotten cited record) erases the decision.
            if not all(e.get("entity") and e.get("attribute") for e in entries):
                continue
            first = entries[0]
            name, target = first.get("entity"), first.get("attribute")
            if first.get("method") in MERGE_METHODS and name and target:
                aliases[canonical_entity(str(name))] = canonical_entity(str(target))

    def _fold_partition(self, namespace: str, payload: dict[str, Any]) -> None:
        state = self.communities.setdefault(namespace, CommunityState())
        changed = payload.get("set")
        if isinstance(changed, dict):
            state.anchors.update({str(n): str(a) for n, a in changed.items()})
        dropped = payload.get("drop")
        if isinstance(dropped, list):
            for node in dropped:
                state.anchors.pop(str(node), None)
        state.sleeps = int(payload.get("sleeps_since_refresh", 0))
        state.placed = int(payload.get("placed_since_refresh", 0))

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
    members.sort(key=chrono_key)
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
    """#29: one mined fact's happened date: the label, the event time a
    deterministic resolution gives (None: keep the H2 event time), and the time the
    fact was said (the anchor of its relative phrases; tagged ``said:<date>``)."""

    said: datetime
    label: str | None = None
    start: datetime | None = None


def _happened(
    fact: ExtractedFact, segment: list[MemoryRecord], numbered: bool, week: WeekMode
) -> _Happened:
    """#29: deterministic first. A relative phrase in the fact's statement, resolved
    against the cited turn it was said in (else the latest cited turn), or the
    segment's day when it has only one; else in its cited turns (each against its own
    date); else the miner's own ``date``.

    The said time is that anchor, else the latest cited turn, else the segment's last
    turn. A span starting after the said day is a plan, not an event: it keeps its
    label but never becomes the event time (a future ``valid_from`` would win every
    later conflict on its key).
    """
    cited = cited_turns(segment, getattr(fact, "turns", [])) if numbered else []
    days = {m.valid_from.date() for m in segment}
    turn = anchor_turn(fact.value, cited, week=week)
    anchor = (
        turn.valid_from if turn is not None else (segment[0].valid_from if len(days) == 1 else None)
    )
    said = anchor or segment[-1].valid_from
    span = resolve_happened([(fact.value, anchor)], week=week) if anchor is not None else None
    if span is None and cited:
        span = resolve_happened(((m.content, m.valid_from) for m in cited), week=week)
    if span is not None:
        start = datetime.combine(span[0], time(), tzinfo=UTC)
        past = span[0] <= said.date()
        return _Happened(said, happened_label(*span), start if past else None)
    label = normalise_label(getattr(fact, "date", None))
    if label is not None and _fact_date(label.split("..", 1)[0], segment[-1].valid_from) is None:
        label = None  # R2-7: out of range, as for the event time
    return _Happened(said, label)


async def _fill_dates(
    dater: DateFacts,
    transcript: str,
    mined: list[ExtractedFact],
    happened: list[_Happened],
    latest: datetime,
) -> None:
    """#29: one call dates the facts no rule or miner did; a failure leaves them undated.

    R2-7: a date past ``latest`` (the session's last turn) plus the slack is dropped."""
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
        if label is not None and _fact_date(label.split("..", 1)[0], latest) is not None:
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
    # W5: the rule miner always cites its line, so a fact's parent is its own turn.
    numbered = bool(getattr(options, "mine_evidence_turns", False)) or (
        getattr(options, "miner", "llm") == "rules"
    )
    event_dates = bool(getattr(options, "mine_event_dates", False))
    dater = ctx.date_facts if getattr(options, "mine_event_dates_llm", False) else None
    week = ctx.config.read.relative_week
    multiview = bool(getattr(options, "mine_multiview", False))

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
                    await _fill_dates(dater, transcript, mined, happened, segment[-1].valid_from)
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
                extra: dict[str, Any] = {}
                if multiview:  # #28: the view fields ride along as tags
                    extra.update(persons=list(fact.persons), location=fact.location)
                    extra["topic"] = fact.topic
                if happened and happened[index].label:
                    h = happened[index]
                    extra["happened"] = str(h.label)
                    extra["said"] = f"{h.said:%Y-%m-%d}"
                    when = h.start or when
                    if when.date() > h.said.date():
                        when = h.said  # a plan is not the newest statement on its key
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
    stats = await _run_session_stage(ctx, "mine_facts", "facts", legacy, work)
    if getattr(options, "list_cards", False) and stats.get("status") != "skipped":
        # #30: the list cards follow the facts, every cycle (a forgotten fact
        # re-derives its card even when no new session was mined).
        stats["list_cards"] = await derive_list_cards(ctx)
    return stats


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


def _prompt_line(text: str) -> str:
    """#62: one prompt input as a single line: whitespace collapsed (a stored newline
    cannot open a new prompt section) and the engine's markers escaped."""
    return escape_markers(" ".join(text.split()))


def _label_span(label: str | None) -> tuple[date, date] | None:
    """#62: the inclusive days a date label (day, month, year or ``a..b``) denotes."""
    if not label:
        return None
    first = label_start(label)
    if first is None:
        return None
    if ".." in label:
        last = label_start(label.split("..", 1)[1])
        return (first, last) if last is not None and last >= first else None
    if len(label) == 4:  # a year
        return first, date(first.year, 12, 31)
    if len(label) == 7:  # a month
        nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
        return first, nxt - timedelta(days=1)
    return first, first


def _record_span(record: MemoryRecord) -> tuple[date, date] | None:
    """#62: when a stored statement's event happened: its ``happened:`` label, else
    the day of its ``valid_from``."""
    return _label_span(happened_of(record) or record.valid_from.date().isoformat())


def _in_memory(fact: ExtractedFact, known: list[MemoryRecord]) -> bool:
    """#62 (ADR-049): is ``fact`` already in memory? One known statement must cover
    its content words (``PREDICT_CALIBRATE_COVERED``) and, for an event with a date,
    happen on an overlapping date: the same event on another date is a new
    occurrence (a repeat camping trip still counts for "how many times")."""
    words = content_words(fact.value)
    if not words:
        return True
    need = constants.PREDICT_CALIBRATE_COVERED
    kind = getattr(fact, "kind", "event") or "event"
    span = None if kind == "state" else _label_span(normalise_label(fact.date))
    for record in known:
        if len(words & content_words(record.content)) / len(words) < need:
            continue
        if span is not None:
            other = _record_span(record)
            if other is None or other[1] < span[0] or span[1] < other[0]:
                continue
        return True
    return False


async def _known_statements(
    ctx: PipelineContext, namespace: str, members: list[MemoryRecord]
) -> list[MemoryRecord]:
    """#62: what memory already holds, for the prediction and the coverage check:
    live semantic records at or above ``PREDICT_CALIBRATE_KNOWN_MIN_TRUST`` (a
    low-trust record pre-stating a fact must not suppress the true one) not derived
    from this session's turns (no summaries, cues or list cards), the best lexical
    overlap with the session first, ``PREDICT_CALIBRATE_KNOWLEDGE_K``."""
    member_ids = {m.record_id for m in members}
    session_words = content_words(" ".join(m.content for m in members))
    scored: list[tuple[int, str, MemoryRecord]] = []
    for record in await ctx.storage.list_records(namespace, "semantic"):
        if (
            record.status is not RecordStatus.ACTIVATED
            or record.quarantined
            or record.instruction_flag
            or record.trust < constants.PREDICT_CALIBRATE_KNOWN_MIN_TRUST
            or record.source.channel == "consolidation"
            or constants.CUE_TAG in record.tags
            or constants.LIST_CARD_TAG in record.tags
            or member_ids & set(record.source.parents)
        ):
            continue
        overlap = len(content_words(record.content) & session_words)
        scored.append((-overlap, record.record_id, record))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [record for _, _, record in scored[: constants.PREDICT_CALIBRATE_KNOWLEDGE_K]]


async def predict_calibrate(ctx: PipelineContext) -> dict[str, object]:
    """#62 (Nemori predict-calibrate, research-grade, ADR-049): store what is new.

    Per consolidated session, once (done marker): the ``predict_episode`` role
    predicts the session's facts from what memory already holds plus the session's
    opening; the ``calibrate`` role compares the transcript with the prediction and
    the known statements and returns the facts memory does not hold yet (a fact the
    prediction guessed right is still new). A returned fact is dropped only when it
    is already IN MEMORY (:func:`_in_memory`: a known statement at or above the
    trust floor covers it, on an overlapping date for a dated event); a predicted
    line never suppresses storage. The rest go through the write door as derived
    semantic facts whose parents are the session's turns (erasure cascades, trust
    capped at the turns). Every prompt input is collapsed to single lines with the
    engine's markers escaped.
    """
    policy = ConsolidationPolicy.bind(_policy_options(ctx, "episodic", "consolidation"))
    if not getattr(policy.options, "predict_calibrate", False):
        return {"status": "skipped", "reason": "consolidation.predict_calibrate is off"}
    if ctx.predict_episode is None or ctx.calibrate is None or ctx.deposit_surprise is None:
        return {"status": "skipped", "reason": "no predict_episode/calibrate LLM role bound"}
    predictor, calibrator, deposit = ctx.predict_episode, ctx.calibrate, ctx.deposit_surprise

    async def work(namespace: str, key: str, members: list[MemoryRecord]) -> tuple[int, list[str]]:
        known = await _known_statements(ctx, namespace, members)
        knowledge = [_prompt_line(r.content) for r in known]
        opening = members[0]
        cue = _prompt_line(opening.content)[: constants.PREDICT_CALIBRATE_CUE_CHARS]
        prediction = await predictor(cue, f"{opening.valid_from:%Y-%m-%d}", knowledge)
        predicted = "\n".join(
            line for line in (_prompt_line(x) for x in prediction.splitlines()) if line
        )
        transcript = "\n".join(
            f"[{m.valid_from:%Y-%m-%d}] {_prompt_line(m.content)}" for m in members
        )
        surprises = await calibrator(predicted, transcript, knowledge)
        parents = [m.record_id for m in members]
        written = 0
        errors: list[str] = []
        for fact in surprises:
            text = f"{fact.entity} {fact.attribute}: {fact.value}"
            if not fact.value.strip() or _in_memory(fact, known):
                continue  # already in memory: not new
            kind = getattr(fact, "kind", "event") or "event"
            when = _fact_date(getattr(fact, "date", None), members[-1].valid_from)
            try:
                await deposit(
                    namespace,
                    text,
                    fact.entity or None,
                    fact.attribute or None,
                    parents,
                    when or opening.valid_from,
                    key,
                    kind=kind,
                )
            except Exception as exc:  # one bad fact must not lose the rest
                errors.append(f"{namespace}:{key}: surprise deposit failed: {exc}")
                continue
            written += 1
        return written, errors

    legacy = _legacy_tag_check(ctx, "semantic", lambda r, key: f"calibrated:{key}" in r.tags)
    return await _run_session_stage(ctx, "predict_calibrate", "surprises", legacy, work)


#: Name -> pipeline. Runners register from this table; the M11-adjacent names
#: are stable identifiers used in schedules and dead-letter reporting.
PIPELINES: dict[str, Pipeline] = {
    "retention_expire": retention_expire,
    "consolidate": consolidate,
    "reorganize": reorganize,
    "extract_graph": extract_graph,
    "summarize_entities": summarize_entities,
    "mine_facts": mine_facts,
    "predict_calibrate": predict_calibrate,
    "anticipate": anticipate,
    "reflect_profile": reflect_profile,
    "check_watches": check_watches,
    "session_lifecycle": session_lifecycle,
    "decay_sweep": decay_sweep,
    "compress": compress,
    "sleep_compute": sleep_compute,
    "event_log_prune": event_log_prune,
}
