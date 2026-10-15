# ruff: noqa: E501
"""Derive stages: activation tiers and multi-resolution compression (O4-09, ``offline.decay_v2``).

The wiring of ``memspine.derive.decay`` (LIB-33), K_C opt-in:

* **Tier sweep.** Today's sweep puts a record in the tier its idle days fall in. This one scores each record
  (recency, frequency, importance), moves it down while the activation is under a band and back up only past
  the band plus a margin (hysteresis, so it cannot flap), and never moves a protected record (pinned,
  standing, contested, or a protected kind) to a colder tier; a cold one is restored to hot. Legal-held and
  quarantined records are not touched. Each move is a ``DECAY_TRANSITION`` delta on the tier, as before; the
  reason is ``idle``, ``reactivated`` or ``protected_restore``. Nothing is deleted, and a tier does not change
  ranking (O-22).
* **Compression plan.** After the zstd step, the records of each group (a ``group_id``, else a month of one
  type) that sit in the cold or dormant tier are planned into multi-resolution jobs (paragraph, sentence,
  keyword). ``offline.decay_multires_jobs`` of them run per sleep (0 plans and reports only): one
  ``summarize`` call each, outside the lock; the lower levels are cut from the paragraph with no further
  call. The summaries go through the derived-write door with every member as a parent. The members are kept
  and only lowered to the job's ``member_tier_after`` (never raised).

Off by default.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from memspine.compose.compression import CompressionPolicy
from memspine.config import constants
from memspine.core.policies.consolidation import extractive_summary
from memspine.core.policies.retention import RetentionPolicy
from memspine.derive.decay import (
    ColdGroup,
    CompressBudget,
    CompressJob,
    DecayPolicy,
    GroupMember,
    RecordStats,
    Resolution,
    Tier,
    compression_plan,
    tier_plan,
)
from memspine.derive.pipeline.stages.context import (
    PipelineContext,
    _parents_gone,
    _policy_options,
    live_records,
    sweep_namespaces,
)
from memspine.derive.pipeline.stages.offline_door import (
    MULTIRES_CHANNEL,
    MULTIRES_TAG,
    archive_records,
    est_tokens,
    is_live,
    write_summary_record,
)
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus, SourceInfo, chrono_key

_log = get_logger(__name__)

_SWEEP_TYPES = ("episodic", "semantic", "resource")
_LAYERS = ("L0", "L1", "L2", "L3", "L4", "L5")
_TIER_NAMES = {t: t.name.lower() for t in Tier}
_TIER_OF = {name: tier for tier, name in _TIER_NAMES.items()}
_STOP = frozenset(
    {
        "about",
        "after",
        "again",
        "also",
        "because",
        "been",
        "before",
        "being",
        "between",
        "could",
        "does",
        "from",
        "have",
        "into",
        "just",
        "more",
        "most",
        "only",
        "other",
        "over",
        "said",
        "same",
        "some",
        "such",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "very",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
        "would",
        "your",
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


def _tier_of(record: MemoryRecord) -> Tier:
    return _TIER_OF.get(record.tier, Tier.HOT)


def _layer_of(record: MemoryRecord) -> str:
    for tag in record.tags:
        if tag.startswith("layer:") and tag[6:] in _LAYERS:
            return tag[6:]
    return "L1"


def decay_policy(ctx: PipelineContext) -> DecayPolicy:
    cfg = ctx.config.offline
    return DecayPolicy(
        boundaries=cfg.decay_boundaries,
        margin=cfg.decay_margin,
        protected_kinds=frozenset(cfg.decay_protected_kinds),
    )


def _stats(record: MemoryRecord, held: bool) -> RecordStats:
    tags = set(record.tags)
    return RecordStats(
        record_id=record.record_id,
        kind=record.memory_type,
        layer=_layer_of(record),  # type: ignore[arg-type]
        recorded_at=record.recorded_at,
        last_accessed_at=record.scoring.last_accessed_at,
        access_count=record.scoring.access_count,
        importance=min(1.0, max(0.0, record.scoring.importance)),
        tier=_tier_of(record),
        pinned="pinned" in tags,
        standing="standing" in tags,
        contested="contested" in tags,
        legal_hold=held,
        quarantined=record.quarantined or record.status is RecordStatus.QUARANTINED,
        deleted=record.status is RecordStatus.DELETED,
    )


async def decay_sweep_v2(ctx: PipelineContext) -> dict[str, object]:
    """O4-09: activation tiers with hysteresis and protected kinds. Idempotent: only a change emits."""
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    policy = decay_policy(ctx)
    now = _now()
    transitions = 0
    errors: list[str] = []
    for namespace in await sweep_namespaces(ctx):
        moved: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        seen = protected = frozen = 0
        for memory_type in _SWEEP_TYPES:
            retention = RetentionPolicy.bind(_policy_options(ctx, memory_type, "retention"))
            records = {
                r.record_id: r
                for r in await ctx.storage.list_records(namespace, memory_type)
                if r.status is not RecordStatus.DELETED
            }
            stats = [_stats(r, retention.on_legal_hold(r)) for r in records.values()]
            seen += len(stats)
            frozen += sum(1 for s in stats if s.legal_hold or s.quarantined)
            protected += sum(
                1
                for s in stats
                if s.pinned or s.standing or s.contested or s.kind in policy.protected_kinds
            )
            for move in tier_plan(stats, now, policy):
                record = records[move.record_id]
                old, new = _TIER_NAMES[move.from_tier], _TIER_NAMES[move.to_tier]
                try:
                    await ctx.append_event(
                        MemoryEvent(
                            kind=EventKind.DECAY_TRANSITION,
                            namespace=namespace,
                            actor="system",
                            payload={
                                "record_id": record.record_id,
                                "set": {"tier": new},
                                "transition": f"{old}->{new}",
                                "reason": move.reason,
                            },
                        )
                    )
                    transitions += 1
                    moved[f"{old}_to_{new}"] += 1
                    reasons[move.reason] += 1
                except Exception as exc:  # one bad record must not kill the sweep
                    errors.append(f"{record.record_id}: {exc}")
                    _log.warning(
                        "decay_sweep_v2.record_failed",
                        namespace=namespace,
                        record_id=record.record_id,
                        error=str(exc),
                    )
        if tap.enabled():
            tap.event(
                "job.decay",
                ns=namespace,
                records=seen,
                frozen=frozen,
                protected=protected,
                moves=sum(moved.values()),
                down=reasons["idle"],
                up=reasons["reactivated"],
                protected_restored=reasons["protected_restore"],
                by_move=dict(sorted(moved.items())),
            )
    stats_out: dict[str, object] = {
        "status": "partial" if errors else "ok",
        "transitions": transitions,
    }
    if errors:
        stats_out["errors"] = errors
    return stats_out


# --------------------------------------------------------------------------------------------------------
# Multi-resolution compression
# --------------------------------------------------------------------------------------------------------


def _clip(text: str, tokens: int) -> str:
    """At most ``tokens`` tokens (four characters each), cut at a word boundary."""
    limit = tokens * 4
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(" ,;:")


def _first_sentence(text: str) -> str:
    text = " ".join(text.split())
    for end in (". ", "! ", "? "):
        cut = text.find(end)
        if cut != -1:
            return text[: cut + 1]
    return text


def _keywords(text: str, tokens: int) -> str:
    words = [
        w for w in "".join(c.lower() if c.isalnum() else " " for c in text).split() if len(w) > 3
    ]
    freq = Counter(w for w in words if w not in _STOP)
    ranked = sorted(freq, key=lambda w: (-freq[w], w))
    return _clip(", ".join(ranked), tokens)


def level_texts(job: CompressJob, paragraph: str) -> dict[Resolution, str]:
    """The text of each planned level: the paragraph as given (clipped), the shorter ones cut from it."""
    out: dict[Resolution, str] = {}
    for spec in job.levels:
        if spec.level is Resolution.PARAGRAPH:
            out[spec.level] = _clip(paragraph, spec.token_budget)
        elif spec.level is Resolution.SENTENCE:
            out[spec.level] = _clip(_first_sentence(paragraph), spec.token_budget)
        else:
            out[spec.level] = _keywords(paragraph, spec.token_budget)
    return {k: v for k, v in out.items() if v}


async def multires_v2(ctx: PipelineContext) -> dict[str, object]:
    """Plan multi-resolution jobs for cold groups and run up to ``offline.decay_multires_jobs`` of them."""
    assert ctx.append_event is not None
    cfg = ctx.config.offline
    inflate = CompressionPolicy.bind()
    counts: Counter[str] = Counter()
    errors: list[str] = []
    for namespace in await sweep_namespaces(ctx):
        local: Counter[str] = Counter()
        try:
            await _multires_namespace(
                ctx, namespace, cfg.decay_multires_jobs, cfg.decay_group_min_size, inflate, local
            )
        except Exception as exc:  # one bad namespace must not kill the sweep
            errors.append(f"{namespace}: {exc}")
            _log.warning("multires_v2.namespace_failed", namespace=namespace, error=str(exc))
        counts.update(local)
        if tap.enabled():
            tap.event(
                "job.decay.compress",
                ns=namespace,
                cold_records=local["cold_records"],
                groups=local["groups"],
                jobs_planned=local["planned"],
                jobs_run=local["run"],
                jobs_over_cap=local["over_cap"],
                cap=cfg.decay_multires_jobs,
                model_calls=local["model_calls"],
                levels_written=local["levels"],
                members_lowered=local["lowered"],
                superseded=local["superseded"],
                skipped_parents_gone=local["gone"],
                quarantined=local["quarantined"],
            )
    out: dict[str, object] = {
        "multires_planned": counts["planned"],
        "multires_run": counts["run"],
        "multires_levels": counts["levels"],
    }
    if errors:
        out["multires_errors"] = errors
    return out


async def _multires_namespace(
    ctx: PipelineContext,
    namespace: str,
    cap: int,
    min_group: int,
    inflate: CompressionPolicy,
    counts: Counter[str],
) -> None:
    assert ctx.append_event is not None
    covered: set[str] = set()
    for record in await ctx.storage.list_records(namespace, "semantic"):
        if record.source.channel == MULTIRES_CHANNEL and is_live(record):
            covered.update(record.source.parents)
    groups: dict[str, list[GroupMember]] = {}
    cold: dict[str, MemoryRecord] = {}
    for memory_type in _SWEEP_TYPES:
        retention = RetentionPolicy.bind(_policy_options(ctx, memory_type, "retention"))
        for record in await live_records(ctx, namespace, memory_type):
            if (
                not is_live(record)
                or record.source.channel == MULTIRES_CHANNEL
                or _tier_of(record) < Tier.COLD
            ):
                continue
            body = inflate.inflate(record)
            tags = set(record.tags)
            gid = record.group_id or f"{memory_type}:{record.valid_from:%Y-%m}"
            groups.setdefault(gid, []).append(
                GroupMember(
                    record_id=record.record_id,
                    layer=_layer_of(record),  # type: ignore[arg-type]
                    tokens=est_tokens(body.content),
                    tier=_tier_of(record),
                    pinned="pinned" in tags,
                    standing="standing" in tags,
                    contested="contested" in tags,
                    legal_hold=retention.on_legal_hold(record),
                    quarantined=record.quarantined,
                    compressed=record.record_id in covered,
                )
            )
            cold[record.record_id] = body
    counts["cold_records"] += len(cold)
    counts["groups"] += len(groups)
    jobs = compression_plan(
        [ColdGroup(group_id=g, members=tuple(m)) for g, m in sorted(groups.items())],
        CompressBudget(min_group_size=min_group),
    )
    counts["planned"] += len(jobs)
    counts["over_cap"] += max(0, len(jobs) - cap)
    for job in jobs[:cap]:
        members = sorted((cold[r] for r in job.lineage if r in cold), key=chrono_key)
        if not members:
            continue
        paragraph_budget = next(
            (s.token_budget for s in job.levels if s.level is Resolution.PARAGRAPH), 200
        )
        paragraph = extractive_summary([m.content for m in members], paragraph_budget * 4)
        if ctx.summarize is not None:
            try:
                paragraph = await ctx.summarize("\n".join(m.content for m in members))
                counts["model_calls"] += 1
            except Exception as exc:  # the LLM is an enhancer, never a gate (N6)
                _log.warning("multires_v2.summarize_fallback", namespace=namespace, error=str(exc))
        texts = level_texts(job, paragraph)
        async with ctx.lock(namespace):
            if await _parents_gone(ctx, namespace, list(job.lineage), "multires"):
                counts["gone"] += 1
                continue
            trust = min(m.trust for m in members)
            flagged = any(m.instruction_flag for m in members)
            for level, text in texts.items():
                record = MemoryRecord(
                    namespace=namespace,
                    memory_type="semantic",
                    content=text,
                    tags=[MULTIRES_TAG, f"res:{level.value}", f"mres:{job.job_id}"],
                    valid_from=max(m.valid_from for m in members),
                    source=SourceInfo(
                        role=constants.DERIVED_ROLE,
                        channel=MULTIRES_CHANNEL,
                        message_id=f"{job.job_id}:{level.value}",
                        parents=list(job.lineage),
                    ),
                    trust=trust,
                    instruction_flag=flagged,
                )
                _, quarantined = await write_summary_record(
                    ctx, namespace, record, job.lineage, "L2", "multires"
                )
                counts["levels"] += 1
                counts["quarantined"] += int(quarantined)
            counts["superseded"] += await _supersede_covered(ctx, namespace, job)
            counts["lowered"] += await _lower_members(ctx, namespace, job, cold)
            counts["run"] += 1


async def _supersede_covered(ctx: PipelineContext, namespace: str, job: CompressJob) -> int:
    """Archive the earlier multi-resolution summaries wholly inside this job's lineage."""
    lineage = set(job.lineage)
    stale = [
        r
        for r in await live_records(ctx, namespace, "semantic")
        if r.source.channel == MULTIRES_CHANNEL
        and r.status is RecordStatus.ACTIVATED
        and f"mres:{job.job_id}" not in r.tags
        and r.source.parents
        and set(r.source.parents) <= lineage
    ]
    return await archive_records(ctx, namespace, stale, "multires->superseded", "regrouped")


async def _lower_members(
    ctx: PipelineContext, namespace: str, job: CompressJob, members: dict[str, MemoryRecord]
) -> int:
    """Lower retrieval tier of the members to ``member_tier_after`` (never raised; never deleted)."""
    assert ctx.append_event is not None
    lowered = 0
    for rid in job.lineage:
        record = members.get(rid)
        if record is None:
            continue
        current = _tier_of(record)
        target = max(current, job.member_tier_after)
        if target == current:
            continue
        await ctx.append_event(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=namespace,
                actor="system",
                payload={
                    "record_id": rid,
                    "set": {"tier": _TIER_NAMES[target]},
                    "transition": f"{_TIER_NAMES[current]}->{_TIER_NAMES[target]}",
                    "reason": "multires",
                },
            )
        )
        lowered += 1
    return lowered


__all__ = ["decay_policy", "decay_sweep_v2", "level_texts", "multires_v2"]
