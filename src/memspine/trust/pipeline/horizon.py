"""Trust: horizon moved out of the engine (F-12)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from memspine.core.policies.retention import RetentionPolicy
from memspine.exceptions import ConflictError, MemspineError
from memspine.store.records import MemoryRecord
from memspine.trust.pipeline.state import _as_options_dict
from memspine.trust.privacy import matching_class


async def expire_retention(eng: Any, now: datetime | None = None) -> dict[str, object]:
    """#48: hard-forget every record past its retention class's TTL.

    Soft-forgotten records count too: a soft forget keeps the content in the
    read model and the log, so past the TTL it is hard-erased like any other.
    The age is measured from ``recorded_at``. A record whose type's ``retention``
    policy refuses deletion (legal hold, regulated PII: ``may_delete``) is kept,
    and so is one any of whose descendants would be; the rest go through
    :meth:`forget` (``hard=True``, cascading), each with an audit reason naming
    the class. No classes configured: nothing runs."""
    storage = eng._require_started()
    classes = eng._config().retention.classes
    if not classes:
        return {"status": "skipped", "reason": "no retention.classes"}
    clock = now or eng._clock()
    expired: list[str] = []
    held = 0
    errors: list[str] = []
    for ns in await storage.list_namespaces():
        for record in await storage.list_records(ns):
            if record.record_id in expired:
                continue
            cls = matching_class(record, classes)
            if cls is None or (record.memory_type == "shared" and cls.memory_type is None):
                continue
            if clock - record.recorded_at < timedelta(days=cls.ttl_days):
                continue
            group = [record]
            for child_id in await eng._descendants(storage, ns, [record.record_id]):
                child = await storage.get_record(child_id)
                if child is not None:
                    group.append(child)
            if not all(eng._retention(r).may_delete(r) for r in group):
                held += 1
                continue
            try:
                await eng.forget(
                    record.record_id,
                    namespace=ns,
                    hard=True,
                    actor="system",
                    reason=f"retention:{cls.namespace}/{cls.memory_type or '*'}"
                    f"/{cls.ttl_days:g}d",
                )
            except (MemspineError, ConflictError) as exc:
                errors.append(f"{record.record_id}: {exc}")
                continue
            expired.extend(r.record_id for r in group)
    stats: dict[str, object] = {"status": "partial" if errors else "ok"}
    stats.update({"expired": len(expired), "held": held})
    if errors:
        stats["errors"] = errors
    return stats


def _retention(eng: Any, record: MemoryRecord) -> RetentionPolicy:
    """The ``retention`` policy bound for ``record``'s memory type."""
    return RetentionPolicy.bind(
        _as_options_dict(
            eng._memory_policy(eng._config(), record.memory_type).get("retention")
        )
    )
