"""Derive stage: pattern_observations (O4-05, LIB-31; opt-in L4, ``offline.patterns``)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC
from typing import Any

from memspine.config import constants
from memspine.derive import patterns as lib
from memspine.derive.pipeline.stages.context import (
    PipelineContext,
    _parents_gone,
    live_records,
    sweep_namespaces,
)
from memspine.derive.pipeline.stages.offline_common import (
    OBSERVATION_CHANNEL,
    archive_records,
    fact_envelope,
    is_input_fact,
    short_hash,
    tag_value,
)
from memspine.observability import tap
from memspine.store.records import MemoryRecord, RecordStatus, SourceInfo

__all__ = ["OBSERVATION_TAG", "pattern_observations", "render_observation"]

OBSERVATION_TAG = "observation"
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def render_observation(obs: lib.Observation) -> str:
    """The statement of an observation as one plain sentence (the library keeps language-neutral fields)."""
    f = obs.statement
    who = f"{f.get('subject', '')}".strip()
    head = f"{who} {f.get('predicate', '')}".strip()
    if obs.kind == "recurrence":
        extra = ""
        if "weekday" in f:
            extra = f" on {_WEEKDAYS[int(f['weekday'])]}"
        elif "day_of_month" in f and "month" in f:
            extra = f" on {int(f['month'])}/{int(f['day_of_month'])}"
        elif "day_of_month" in f:
            extra = f" on day {int(f['day_of_month'])} of the month"
        return f"{head}: recurs {f['period']}{extra} ({obs.count} occurrences)."
    if obs.kind == "frequency":
        return f"{head}: {f['occurrences']} occurrences in {f['window_days']} days."
    if obs.kind == "trend":
        return (
            f"{head}: {f['direction']} from {f['first_value']} to {f['last_value']} "
            f"over {f['points']} dated points."
        )
    if obs.kind == "co_occurrence":
        return (
            f"{who}: {f['predicate_a']} and {f['predicate_b']} appear together "
            f"in {f['units']} {f['unit']}s."
        )
    return f"{head}: {f['value']} (restated in {f['sessions']} sessions)."


def _group_tag(group_key: str) -> str:
    return f"obsgrp:{short_hash(group_key)}"


def _stub(r: MemoryRecord) -> lib.Observation:
    """A stored observation reduced to what ``invalidate`` reads: id, group and lineage."""
    return lib.Observation.model_construct(
        observation_id=tag_value(r.tags, "obs:") or r.record_id,
        group_key=tag_value(r.tags, "obsgrp:") or "",
        support_ids=tuple(sorted(r.source.parents)),
    )


def _config(ctx: PipelineContext) -> lib.PatternConfig:
    kinds = ctx.config.offline.patterns_kinds
    return lib.PatternConfig(kinds=frozenset(kinds)) if kinds else lib.PatternConfig()


async def pattern_observations(ctx: PipelineContext) -> dict[str, object]:
    """O4-05: mine pattern observations over each namespace's stored facts, once per sleep.

    Per namespace, under the write lock (no model call): the live facts are read as assertions, the five
    miners run, and each observation becomes a derived semantic record (channel ``observation``; parents =
    the supporting facts; trust capped at the least-trusted parent) written through the derived-write door.
    Idempotent: an observation whose id (kind + group + supporting ids) is already stored is not rewritten.
    A stored observation that has a supporting fact no longer live is invalidated by changed group, and any
    stored observation the new run no longer produces (its evidence grew or went) is retired.
    """
    if not ctx.config.offline.patterns:
        return {"status": "skipped", "reason": "offline.patterns is off"}
    if ctx.append_event is None or ctx.write_fact is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    cfg = _config(ctx)
    cap_new = ctx.config.offline.patterns_max_new
    emitted: Counter[str] = Counter()
    invalidated = retired = skipped_parents = deferred = groups_changed = 0
    errors: list[str] = []
    for namespace in await sweep_namespaces(ctx):
        async with ctx.lock(namespace):
            records = await live_records(ctx, namespace, "semantic")
            live_ids = {
                r.record_id
                for r in records
                if r.status is RecordStatus.ACTIVATED and not r.quarantined
            }
            stored = [
                r
                for r in records
                if r.source.channel == OBSERVATION_CHANNEL and r.status is RecordStatus.ACTIVATED
            ]
            envs = [e for r in records if is_input_fact(r) and (e := fact_envelope(r)) is not None]
            found = lib.find_observations(envs, config=cfg)
            by_id = {o.observation_id: o for o in found}
            # invalidation by changed group (ER-28 B): a supporting fact that is no longer live
            changed = {p for r in stored for p in r.source.parents if p not in live_ids}
            stubs = [_stub(r) for r in stored]
            gone = set(lib.invalidate(stubs, changed))
            groups = lib.invalidated_groups(stubs, changed)
            # a stored observation the run no longer produces is stale (evidence grew or went)
            retire = {
                r.record_id: r
                for r in stored
                if (tag_value(r.tags, "obs:") or "") in gone
                or (tag_value(r.tags, "obs:") or "") not in by_id
            }
            invalidated += len(gone)
            groups_changed += len(groups)
            retired += len(retire)
            if retire:
                await archive_records(
                    ctx.append_event,
                    namespace,
                    sorted(retire),
                    "observation->retired",
                    "patterns",
                )
            kept_ids = {tag_value(r.tags, "obs:") for r in stored if r.record_id not in retire}
            by_record = {r.record_id: r for r in records}
            written = 0
            for obs in found:
                if obs.observation_id in kept_ids:
                    continue  # already stored and still valid
                if written >= cap_new:
                    deferred += 1
                    continue
                parents = list(obs.support_ids)
                if await _parents_gone(ctx, namespace, parents, OBSERVATION_CHANNEL):  # I72
                    skipped_parents += 1
                    continue
                cap = [obs.trust, *(by_record[p].trust for p in parents if p in by_record)]
                record = MemoryRecord(
                    namespace=namespace,
                    memory_type="semantic",
                    content=render_observation(obs),
                    valid_from=obs.last_seen
                    if obs.last_seen.tzinfo
                    else obs.last_seen.replace(tzinfo=UTC),
                    source=SourceInfo(
                        role=constants.DERIVED_ROLE,
                        channel=OBSERVATION_CHANNEL,
                        message_id=obs.observation_id,
                        parents=parents,
                    ),
                    tags=[
                        OBSERVATION_TAG,
                        f"{OBSERVATION_TAG}:{obs.kind}",
                        f"obs:{obs.observation_id}",
                        _group_tag(obs.group_key),
                    ],
                )
                try:
                    stored_rec = await ctx.write_fact(record, cap)
                except Exception as exc:  # one bad observation must not lose the rest
                    errors.append(f"{namespace}: observation deposit failed: {exc}")
                    continue
                if stored_rec is not None and not stored_rec.quarantined:
                    emitted[obs.kind] += 1
                    written += 1
    stats: dict[str, object] = {
        "status": "ok" if not errors else "partial",
        "observations": sum(emitted.values()),
        "invalidated": invalidated,
        "retired": retired,
        "deferred": deferred,
        "skipped_parents": skipped_parents,
        "errors": errors,
    }
    if tap.enabled():
        by_kind: dict[str, Any] = dict(emitted)
        tap.event(
            "job.patterns",
            by_kind=by_kind,
            emitted=sum(emitted.values()),
            invalidated=invalidated,
            groups_changed=groups_changed,
            retired=retired,
            deferred=deferred,
            skipped_parents=skipped_parents,
        )
    return stats
