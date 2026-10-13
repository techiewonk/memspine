# ruff: noqa: E501
"""Derive stage: histories (near-duplicate merge, value histories) (O4-04, LIB-20).

Flag ``offline.histories_v2`` (default off; the stage then reports ``skipped`` and the sleep cycle
does not schedule it). On, per namespace under the namespace lock:

1. **Near-duplicates (S5, O-13 B).** ``find_near_duplicates`` clusters live semantic facts of one key
   (shingle similarity ``offline.near_duplicate_text``), guarded by numbers, dates, negation and
   modality (``scan_near_duplicates`` reports what the guard blocked, by reason). ``merge_group`` plans
   each cluster; the earliest record stays, the others are archived (``evolve_to`` the canonical,
   reason ``near_duplicate``). With ``write.lineage_table`` on, one ``merge_member`` lineage edge per
   dropped record is written (child = canonical, parent = dropped copy), the direction the erasure
   planner reads. Support is not stored on the record: a merged repeat never counts as corroboration
   (S4-3), the reinforcement count goes to the tap only.
2. **Value histories (S6, O-14 B).** ``value_history`` rebuilds the per-key value lines from live and
   superseded facts (LIB-02 replay, so arrival order does not matter). It is a disposable projection:
   nothing is stored; :func:`build_value_history` returns it for a caller (reads, tests).

No model call.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime

from memspine.derive.histories import (
    DupThresholds,
    ValueHistory,
    merge_group,
    scan_near_duplicates,
    value_history,
)
from memspine.derive.integrate import CardinalityRegistry
from memspine.derive.pipeline.stages.context import PipelineContext
from memspine.derive.pipeline.stages.envelope_adapter import is_fact, key_of, to_envelope
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.schemas.durable.lineage import LineageEdge
from memspine.store import lineage as lineage_store
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus

_log = get_logger(__name__)

_CAPITALISED = re.compile(r"[A-Z][\w'-]*")


def _names(text: str) -> frozenset[str]:
    """Capitalised tokens (names, places, brands): a near-duplicate must name the same ones. The
    library guard covers numbers, dates, negation and modality; a shingle score can still be high
    for "works at Acme Corp" vs "works at Apex Corp", so the stage adds this code-only check."""
    return frozenset(m.group(0).casefold() for m in _CAPITALISED.finditer(text))


__all__ = ["build_value_history", "histories"]


def build_value_history(records: Iterable[MemoryRecord], namespace: str) -> ValueHistory:
    """Pure: the value histories of a namespace's keyed facts (live and superseded). Every keyed
    attribute is cardinality ``one``, as the write-time conflict ladder assumes."""
    facts = [r for r in records if is_fact(r, include_history=True) and key_of(r) is not None]
    envelopes = [to_envelope(r, namespace, include_history=True) for r in facts]
    registry = CardinalityRegistry()
    for e in envelopes:
        registry.declare(namespace, e.attribute or e.kind, "one")
    return value_history(envelopes, registry)


async def histories(ctx: PipelineContext) -> dict[str, object]:
    """O4-04: merge near-duplicates and rebuild the value histories."""
    if not ctx.config.offline.histories_v2:
        return {"status": "skipped", "reason": "offline.histories_v2 off"}
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    now = datetime.now(UTC)
    totals: Counter[str] = Counter()
    errors: list[str] = []
    for namespace in await ctx.storage.list_namespaces():
        try:
            async with ctx.lock(namespace):
                totals.update(await _histories_one(ctx, namespace, now))
        except Exception as exc:  # one bad namespace must not kill the sleep cycle
            errors.append(f"{namespace}: {exc}")
            _log.warning("histories.namespace_failed", namespace=namespace, error=str(exc))
    stats: dict[str, object] = {"status": "ok", **dict(totals)}
    if errors:
        stats.update(status="partial", errors=errors)
    return stats


async def _histories_one(ctx: PipelineContext, namespace: str, now: datetime) -> Counter[str]:
    assert ctx.append_event is not None
    records = await ctx.storage.list_records(namespace, "semantic")
    by_id = {r.record_id: r for r in records}
    live = [r for r in records if is_fact(r)]
    envelopes = [to_envelope(r, namespace) for r in live]
    thresholds = DupThresholds(text=ctx.config.offline.near_duplicate_text)
    scan = scan_near_duplicates(envelopes, thresholds=thresholds)
    counts: Counter[str] = Counter(
        facts=len(live), groups=len(scan.groups), blocked=len(scan.blocked)
    )
    blocked_by: Counter[str] = Counter(p.guard for p in scan.blocked)
    edges: list[LineageEdge] = []
    for scanned in scan.groups:
        canon_names = _names(scanned.canonical.text)
        kept = tuple(e for e in scanned.records if _names(e.text) == canon_names)
        if len(kept) < len(scanned.records):
            blocked_by["names_differ"] += len(scanned.records) - len(kept)
            counts["blocked"] += len(scanned.records) - len(kept)
        if len(kept) < 2:
            continue
        group = replace(scanned, records=kept)
        plan = merge_group(group)
        canonical = by_id.get(plan.canonical_id)
        if canonical is None or canonical.status is not RecordStatus.ACTIVATED:
            counts["skipped"] += 1
            continue
        for member_id in plan.member_ids:
            member = by_id.get(member_id)
            if member is None or member.status is not RecordStatus.ACTIVATED:
                counts["skipped"] += 1
                continue
            await ctx.append_event(
                MemoryEvent(
                    kind=EventKind.DECAY_TRANSITION,
                    namespace=namespace,
                    actor="system",
                    payload={
                        "record_id": member_id,
                        "set": {
                            "status": RecordStatus.ARCHIVED.value,
                            "superseded_at": now.isoformat(),
                            "evolve_to": plan.canonical_id,
                        },
                        "transition": "fact->near_duplicate",
                        "reason": "near_duplicate",
                    },
                )
            )
            counts["merged"] += 1
            edges.extend(e for e in plan.edges if e.parent == member_id)
        counts["reinforcement"] += plan.reinforcement
    if edges and ctx.config.write.lineage_table:
        client = getattr(ctx.storage, "_client", None)
        if client is not None:
            counts["lineage_edges"] += await lineage_store.add_edges(
                client.engine, edges, namespace
            )
    history = build_value_history(records, namespace)
    spans = sum(len(line.spans) for line in history.lines)
    contests = sum(len(line.open_contests()) for line in history.lines)
    counts["keys"] = len(history.lines)
    counts["spans"] = spans
    counts["open_contests"] = contests
    if tap.enabled():
        tap.event(
            "job.histories",
            namespace=namespace,
            facts=len(live),
            groups=len(scan.groups),
            merged=counts["merged"],
            blocked=counts["blocked"],
            blocked_by_reason=dict(blocked_by),
            reinforcement=counts["reinforcement"],
            lineage_edges=counts["lineage_edges"],
            keys=len(history.lines),
            spans=spans,
            open_contests=contests,
            skipped=counts["skipped"],
        )
    return counts
