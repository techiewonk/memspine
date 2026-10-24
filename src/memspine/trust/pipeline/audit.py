"""Current audit orchestration over the content-free hash chain."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from memspine.config import constants
from memspine.store.scope import validate_namespace
from memspine.trust.pipeline import audit_chain


async def bulk_read_alerts(
    eng: Any,
    namespace: str = "default",
    *,
    window: timedelta = timedelta(hours=1),
    max_records: int = constants.BULK_READ_MAX_RECORDS,
) -> list[dict[str, object]]:
    """Principals whose recent audited reads exceed the distinct-record limit."""
    ns = validate_namespace(namespace)
    since: datetime = eng._clock() - window
    seen: dict[str, set[str]] = {}
    reads: dict[str, int] = {}
    for record in await audit_chain.records(eng):
        if record.action != "read" or record.tenant != ns or record.at < since:
            continue
        seen.setdefault(record.actor, set()).update(record.ids)
        reads[record.actor] = reads.get(record.actor, 0) + 1
    return [
        {"principal": who, "distinct_records": len(ids), "reads": reads[who]}
        for who, ids in sorted(seen.items())
        if len(ids) > max_records
    ]


async def _audit_read(eng: Any, verb: str, namespace: str, record_ids: Sequence[str]) -> None:
    if audit_chain.enabled(eng):
        await audit_chain.read(eng, verb, namespace, record_ids)


async def _audit_action(
    eng: Any,
    action: str,
    ns: str,
    record_ids: Sequence[str],
    *,
    actor: str,
    reason: str | None,
    **extra: object,
) -> None:
    """Audit structure and short reason codes; never persist free-form extra content."""
    if eng._config().audit.actions:
        await audit_chain.action(eng, action, ns, record_ids, actor=actor, reason=reason)


async def audit_chain_ok(eng: Any) -> bool:
    return (await audit_chain.verify(eng)).ok
