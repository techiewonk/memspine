"""Trust: retention planning (T5-05; LIB-41 ``trust/retention.py``), behind ``retention.v2``.

``Engine.expire_retention`` (the ``retention`` sleep stage) plans with ``retention_plan`` instead of
walking ``retention.classes`` by hand:

* each record becomes a ``RetentionRecord`` (kind, layer, tier from ``pii_tier``, first consent
  tag as purpose, ``recorded_at``); a legal hold or a type that refuses deletion (``may_delete``)
  is a hold, a record waiting in the erasure re-derivation queue is ``pending``;
* records matching a ``retention.classes`` entry (namespace glob and type) take that entry's TTL
  (outranking the ``retention.profile`` template); the rest take the template's classes, or never
  expire;
* layer L0 (turns) never expires under ``event_log.mode: full``. Under ``rolling`` / ``ephemeral``
  the window pruning of the *events* stays with the ``event_log_prune`` stage, so a turn record is
  planned by its class TTL like any other layer;
* expired records go through ``Engine.forget(hard=True)`` (so legal holds, the audit trail and
  ``privacy.erasure_v2`` all apply). The tombstone is the erasure manifest / audit entry and the
  lineage edges stay: the plan keeps lineage on expiry;
* the plan's boundary and rebuild mode (full / limited / unavailable) are kept per namespace
  (``Engine.retention_boundary``) and reported, so a rolling store advertises a limited rebuild.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from memspine.exceptions import ConflictError, MemspineError
from memspine.observability import tap
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.erasure import RetentionBoundary
from memspine.trust.consent import tier_from_pii
from memspine.trust.pipeline import erasure_v2
from memspine.trust.privacy import matching_class
from memspine.trust.retention import (
    RetentionPlan,
    RetentionRecord,
    retention_plan,
)
from memspine.trust.retention_profile import build_profile, class_rule

__all__ = ["boundary", "expire", "plan_namespace"]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def boundary(eng: Any, namespace: str = "default") -> RetentionBoundary | None:
    """The last retention boundary planned for ``namespace`` (None before the first pass)."""
    got: dict[str, RetentionBoundary] = eng.__dict__.get("_retention_boundary", {})
    return got.get(namespace)


def _record_of(eng: Any, record: Any, mode: str, pending: set[str]) -> RetentionRecord:
    policy = eng._retention(record)
    layer = record.layer
    if layer == "L0" and mode != "full":
        layer = "L1"  # events are pruned by event_log_prune; a turn is planned by its class TTL
    return RetentionRecord(
        id=record.record_id,
        kind=record.memory_type,
        layer=layer,
        tier=tier_from_pii(record.pii_tier),
        purpose=record.consent_tags[0] if record.consent_tags else "*",
        created_at=_aware(record.recorded_at),
        legal_hold=not policy.may_delete(record),
        pending=record.record_id in pending,
    )


def plan_namespace(
    eng: Any, ns: str, records: list[Any], now: datetime
) -> tuple[list[RetentionPlan], dict[str, str]]:
    """The plans for the records of one namespace (one per retention-class group) and the class
    name of every id the plans expire."""
    config = eng._config()
    profile = build_profile(config)
    classes = config.retention.classes
    pending = set(erasure_v2.state(eng).hidden) | {
        str(i["record_id"]) for i in erasure_v2.queue(eng)
    }
    groups: dict[int | None, list[RetentionRecord]] = {}
    for rec in records:
        if rec.memory_type == "shared":
            continue  # grants and subscriptions are authorization state, not memory
        cls = matching_class(rec, classes)
        key = None if cls is None else classes.index(cls)
        groups.setdefault(key, []).append(_record_of(eng, rec, profile.event_log_mode, pending))
    if not groups:
        groups[None] = []
    previous = boundary(eng, ns)
    scope = OwnerScope(tenant=ns, owner=ns)
    plans: list[RetentionPlan] = []
    for key in sorted(groups, key=lambda k: -1 if k is None else k):
        prof = (
            profile
            if key is None
            else profile.model_copy(
                update={"classes": (class_rule(key, classes[key].ttl_days), *profile.classes)}
            )
        )
        plan = retention_plan(groups[key], now, prof, scope=scope, previous=previous)
        previous = plan.boundary
        plans.append(plan)
    eng.__dict__.setdefault("_retention_boundary", {})[ns] = plans[-1].boundary
    return plans, {e.record_id: e.retention_class for p in plans for e in p.expire}


async def expire(eng: Any, now: datetime | None = None) -> dict[str, object]:
    """The v2 retention pass: plan per namespace, then hard-forget what expired."""
    storage = eng._require_started()
    config = eng._config()
    clock = _aware(now or eng._clock())
    expired: list[str] = []
    held: dict[str, int] = {}
    errors: list[str] = []
    tombstones = 0
    l0_protected = 0
    rebuild = "full"
    for ns in await storage.list_namespaces():
        records = await storage.list_records(ns)
        by_id = {r.record_id: r for r in records}
        plans, class_of = plan_namespace(eng, ns, records, clock)
        for plan in plans:
            for h in plan.held:
                held[h.reason] = held.get(h.reason, 0) + 1
            l0_protected += plan.l0_protected
            rebuild = plan.rebuild
        todo = [e.record_id for p in plans for e in p.expire if e.record_id in by_id]
        if tap.enabled():
            tap.event(
                "job.retention.plan",
                namespace=ns,
                profile=plans[-1].profile,
                expire=len(todo),
                held=sum(len(p.held) for p in plans),
                retained=sum(len(p.retained) for p in plans),
                tombstones=sum(len(p.tombstones) for p in plans),
                l0_protected=sum(p.l0_protected for p in plans),
                rebuild=plans[-1].rebuild,
                boundary_mode=plans[-1].boundary.mode,
            )
        for rid in todo:
            if rid in expired:
                continue
            group = [rid]
            if not erasure_v2.enabled(eng):  # a cascade deletes dependants: all must be deletable
                group += await eng._descendants(storage, ns, [rid])
                blocked = False
                for gid in group:
                    rec = await storage.get_record(gid)
                    if rec is not None and not eng._retention(rec).may_delete(rec):
                        blocked = True
                if blocked:
                    held["descendant_hold"] = held.get("descendant_hold", 0) + 1
                    continue
            try:
                await eng.forget(
                    rid,
                    namespace=ns,
                    hard=True,
                    actor="system",
                    reason=f"retention:{class_of.get(rid, 'class')}",
                )
            except (MemspineError, ConflictError) as exc:
                errors.append(f"{rid}: {exc}")
                continue
            expired.extend(group)
            tombstones += 1
    stats: dict[str, object] = {"status": "partial" if errors else "ok"}
    stats.update(
        {
            "expired": len(expired),
            "held": sum(held.values()),
            "held_by_reason": dict(sorted(held.items())),
            "tombstones": tombstones,
            "l0_protected": l0_protected,
            "rebuild": rebuild,
            "profile": build_profile(config).name,
        }
    )
    if errors:
        stats["errors"] = errors
    if tap.enabled():
        tap.event(
            "job.retention.done",
            expired=len(expired),
            held=sum(held.values()),
            tombstones=tombstones,
            rebuild=rebuild,
            errors=len(errors),
        )
    return stats
