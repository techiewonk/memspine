"""Derive stage: reconcile (backlog, entity resolution, contradiction sweep) (O4-03, LIB-37).

Flag ``offline.reconcile_v2`` (default off; the stage then reports ``skipped`` and the sleep cycle
does not even schedule it). On: per namespace, under the namespace lock, the live semantic facts are
projected to envelopes (``envelope_adapter``) and :func:`memspine.derive.reconcile.sweep` re-folds every
(entity, attribute) group in event-time order (LIB-02 ``integrate``). The plan is applied through the
write door as lifecycle deltas:

- ``superseded`` / merged duplicate -> archived, ``evolve_to`` the group's current record, validity
  closed at the successor's start (a reverse-order ingest ends exactly like a forward one);
- ``contested`` -> the ``disputed`` tag (add-only), both values stay live;
- a closure with no status change -> ``valid_to`` only.

Pending entity merges need the persisted ``pending_merge`` outbox (W-19 B), which does not exist yet:
``reconcile_namespace`` accepts ``pending`` and ``entities`` (and re-keys through the library), the
stage feeds none until that outbox lands. No model call, so nothing needs budget.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime

from memspine.derive.fact_policy import FactPurpose
from memspine.derive.integrate import CardinalityRegistry, ordering_key, record_id
from memspine.derive.pipeline.stages.context import PipelineContext, live_records, sweep_namespaces
from memspine.derive.pipeline.stages.envelope_adapter import is_fact, key_of, to_envelope
from memspine.derive.reconcile import (
    PendingMerge,
    ReconcileQueue,
    SweepReport,
    apply_plan,
    sweep,
)
from memspine.derive.resolve import Candidate
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.schemas.durable.assertion import AssertionEnvelope
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus

_log = get_logger(__name__)

__all__ = ["reconcile", "reconcile_namespace"]


def reconcile_namespace(
    records: Iterable[MemoryRecord],
    namespace: str,
    *,
    now: datetime | None = None,
    pending: Iterable[PendingMerge] = (),
    entities: Iterable[Candidate] = (),
    queue: ReconcileQueue | None = None,
) -> tuple[SweepReport, list[AssertionEnvelope], list[AssertionEnvelope]]:
    """Pure: sweep one namespace's facts. Returns (report, envelopes before, envelopes after the plan).

    Every keyed attribute is declared cardinality ``one``: that is what the write-time conflict ladder
    (M4) already assumes for an (entity, attribute) key."""
    facts = [
        r for r in records if is_fact(r, purpose=FactPurpose.RECONCILE) and key_of(r) is not None
    ]
    envelopes = [to_envelope(r, namespace) for r in facts]
    registry = CardinalityRegistry()
    for e in envelopes:
        registry.declare(namespace, e.attribute or e.kind, "one")
    report = sweep(envelopes, registry, pending=pending, entities=entities, queue=queue, now=now)
    return report, envelopes, apply_plan(report.plan, envelopes)


def _successors(after: list[AssertionEnvelope]) -> dict[tuple[str, str], str]:
    """(entity, attribute) -> record id of the group's current record (latest live in event order)."""
    best: dict[tuple[str, str], AssertionEnvelope] = {}
    for e in sorted(after, key=ordering_key):
        if e.status in ("superseded", "hidden") or e.subject is None or e.attribute is None:
            continue
        best[(e.subject.surface, e.attribute)] = e
    return {k: record_id(v) for k, v in best.items()}


async def reconcile(ctx: PipelineContext) -> dict[str, object]:
    """O4-03: re-fold contradictions across sessions; archive/contest through the write door."""
    if not ctx.config.offline.reconcile_v2:
        return {"status": "skipped", "reason": "offline.reconcile_v2 off"}
    if ctx.append_event is None:
        return {"status": "skipped", "reason": "read-only context (no write door)"}
    now = datetime.now(UTC)
    totals: Counter[str] = Counter()
    errors: list[str] = []
    for namespace in await sweep_namespaces(ctx):
        try:
            async with ctx.lock(namespace):
                totals.update(await _reconcile_one(ctx, namespace, now))
        except Exception as exc:  # one bad namespace must not kill the sleep cycle
            errors.append(f"{namespace}: {exc}")
            _log.warning("reconcile.namespace_failed", namespace=namespace, error=str(exc))
    stats: dict[str, object] = {"status": "ok", **dict(totals)}
    if errors:
        stats.update(status="partial", errors=errors)
    return stats


async def _reconcile_one(ctx: PipelineContext, namespace: str, now: datetime) -> Counter[str]:
    assert ctx.append_event is not None
    records = await live_records(ctx, namespace, "semantic")
    report, _before, after = reconcile_namespace(records, namespace, now=now)
    plan = report.plan
    by_id = {r.record_id: r for r in records}
    after_by_id = {record_id(e): e for e in after}
    current = _successors(after)
    closures = {c.record_id: c for c in plan.closures}
    counts: Counter[str] = Counter(
        facts=report.records_scanned,
        groups=report.groups_scanned,
        merges=len(plan.merges),
        splits=len(plan.splits),
        rekeys=len(plan.rekeys),
        contests=sum(len(f.newly) for f in plan.contest_flags),
        closures=len(plan.closures),
    )
    handled: set[str] = set()
    reasons: Counter[str] = Counter()
    for change in plan.status_changes:
        record = by_id.get(change.record_id)
        if record is None or record.status is not RecordStatus.ACTIVATED:
            counts["skipped"] += 1
            continue
        closure = closures.get(change.record_id)
        if change.new in ("superseded", "hidden"):
            env = after_by_id.get(change.record_id)
            key = key_of(record)
            successor = current.get((key[0], key[1])) if key is not None else None
            if successor is None or successor == record.record_id:
                counts["skipped"] += 1
                continue
            delta: dict[str, object] = {
                "status": RecordStatus.ARCHIVED.value,
                "superseded_at": now.isoformat(),
                "evolve_to": successor,
            }
            end = _closure_end(closure, env)
            if end is not None:
                delta["valid_to"] = end.isoformat()
            reason = "reconcile_merge" if change.new == "hidden" else "reconcile_superseded"
            await _transition(ctx, namespace, record.record_id, delta, reason)
            counts["superseded" if change.new == "superseded" else "merged_same"] += 1
            reasons[reason] += 1
            handled.add(change.record_id)
        elif change.new == "contested":
            if "disputed" not in record.tags:
                await _transition(
                    ctx,
                    namespace,
                    record.record_id,
                    {"tags_add": ["disputed"]},
                    "reconcile_contest",
                )
                counts["tagged_disputed"] += 1
                reasons["reconcile_contest"] += 1
            handled.add(change.record_id)
        else:  # contested -> active/tentative: the disputed tag is add-only, left as it is
            counts["skipped"] += 1
    for rid, closure in closures.items():
        record = by_id.get(rid)
        if rid in handled or record is None or record.status is not RecordStatus.ACTIVATED:
            continue
        end = _closure_end(closure, after_by_id.get(rid))
        if end is not None and end != record.valid_to and end >= record.valid_from:
            await _transition(
                ctx, namespace, rid, {"valid_to": end.isoformat()}, "reconcile_closure"
            )
            counts["closed"] += 1
            reasons["reconcile_closure"] += 1
    if tap.enabled():
        tap.event(
            "job.reconcile.sweep",
            namespace=namespace,
            records=report.records_scanned,
            groups=report.groups_scanned,
            merges=len(plan.merges),
            splits=len(plan.splits),
            rekeys=len(plan.rekeys),
            status_changes=len(plan.status_changes),
            contests=counts["contests"],
            closures=len(plan.closures),
            open_contests=len(report.open_contests),
            superseded=counts["superseded"],
            merged_same=counts["merged_same"],
            tagged_disputed=counts["tagged_disputed"],
            closed=counts["closed"],
            skipped=counts["skipped"],
            unresolved=len(report.unresolved),
            queue=len(report.queue),
            reasons=dict(reasons),
        )
    return counts


def _closure_end(closure: object, env: AssertionEnvelope | None) -> datetime | None:
    end = getattr(closure, "end", None)
    if end is not None:
        return end  # type: ignore[no-any-return]
    ev = env.time.event if env is not None else None
    return ev.end.at if ev is not None and ev.end is not None else None


async def _transition(
    ctx: PipelineContext, namespace: str, record_id_: str, delta: dict[str, object], reason: str
) -> None:
    assert ctx.append_event is not None
    await ctx.append_event(
        MemoryEvent(
            kind=EventKind.DECAY_TRANSITION,
            namespace=namespace,
            actor="system",
            payload={
                "record_id": record_id_,
                "set": delta,
                "transition": f"fact->{reason}",
                "reason": reason,
            },
        )
    )
