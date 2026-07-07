"""(entity, attribute) fact keying + point-in-time queries (M13.3, bi-temporal M4)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from memspine.core.records import MemoryRecord, RecordStatus

__all__ = ["FactStorage", "fact_at", "fact_key"]


class FactStorage(Protocol):
    async def list_records(
        self, namespace: str, memory_type: str | None = None
    ) -> list[MemoryRecord]: ...


def fact_key(record: MemoryRecord) -> tuple[str, str] | None:
    if record.entity is None or record.attribute is None:
        return None
    return (record.entity, record.attribute)


async def fact_at(
    storage: FactStorage,
    namespace: str,
    entity: str,
    attribute: str,
    as_of: datetime,
) -> MemoryRecord | None:
    """The fact that was valid at ``as_of`` (event time, not record time).

    Bi-temporality (M4): superseded facts keep closed ``[valid_from, valid_to)``
    intervals, so history questions ("what did we believe on June 1st?") have
    exact answers.
    """
    candidates = [
        record
        for record in await storage.list_records(namespace, "semantic")
        if record.entity == entity
        and record.attribute == attribute
        and record.status is not RecordStatus.DELETED
        and record.valid_from <= as_of
        and (record.valid_to is None or as_of < record.valid_to)
    ]
    if not candidates:
        return None
    # Latest recorded knowledge about that interval wins (record time).
    return max(candidates, key=lambda record: record.recorded_at)
