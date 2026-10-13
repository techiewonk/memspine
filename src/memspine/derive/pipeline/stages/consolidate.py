"""Derive stage: consolidate (session summaries, incremental fold) (F-15)."""

from __future__ import annotations

from datetime import UTC, datetime

from memspine.compose.compression import CompressionPolicy
from memspine.config import constants
from memspine.core.policies.consolidation import ConsolidationPolicy, ConsolidationTrigger
from memspine.derive.pipeline.stages.context import (
    PipelineContext,
    _parents_gone,
    _policy_options,
    sweep_namespaces,
)
from memspine.memories.episodic.sessions import Session, detect_sessions
from memspine.observability.logging import get_logger
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus, SourceInfo
from memspine.trust.firewall import instruction_shaped

_log = get_logger(__name__)


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

    for namespace in await sweep_namespaces(ctx):
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
    if not await _append_summary(ctx, namespace, session, summary, stale, now, extra):
        return summaries, superseded  # I72: a member was forgotten while summarising
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
) -> bool:
    """WRITE the summary, archive the summaries it supersedes, then CONSOLIDATE.

    I72: the summary text was authored outside the namespace lock, so the unit runs
    under it and is skipped (returns False, nothing archived) when a member the
    summary restates was forgotten meanwhile; the next sweep re-summarises the
    surviving members."""
    assert ctx.append_event is not None
    async with ctx.lock(namespace):
        if await _parents_gone(ctx, namespace, summary.source.parents, "consolidate"):
            return False
        await _append_summary_locked(ctx, namespace, session, summary, stale, now, extra)
    return True


async def _append_summary_locked(
    ctx: PipelineContext,
    namespace: str,
    session: Session,
    summary: MemoryRecord,
    stale: list[MemoryRecord],
    now: datetime,
    extra: dict[str, object],
) -> None:
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
    appended = await _append_summary(
        ctx,
        namespace,
        session,
        closed,
        [open_summary],
        now,
        {"summary_mode": "close", "open_session": False},
    )
    return int(appended)
