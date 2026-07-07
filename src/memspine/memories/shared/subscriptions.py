"""Shared-memory subscriptions (ADR-016, v0.1 minimal): standing queries.

A subscription is an ordinary ``memory_type="shared"`` record whose
``content`` is the standing query text and whose ``attribute`` is
``"subscription"``. It is caller free text, so it enters through the
firewall-gated door like any write (unlike grants, whose content the engine
builds deterministically).

v0.1 is pull-based: callers list their subscriptions and feed each query to
``Engine.shared_search`` themselves. Push delivery (running subscriptions in
the sleep cycle and delivering hits) is deferred to the taskiq build —
recorded in ADR-016.
"""

from __future__ import annotations

from dataclasses import dataclass

from memspine.core.records import MemoryRecord, RecordStatus, SourceInfo

__all__ = [
    "SUBSCRIPTION_ATTRIBUTE",
    "Subscription",
    "active_subscriptions",
    "make_subscription_record",
    "parse_subscription",
]

#: The ``attribute`` value that marks a shared record as a subscription.
SUBSCRIPTION_ATTRIBUTE = "subscription"


@dataclass(frozen=True)
class Subscription:
    """One standing query: run ``query`` via ``shared_search`` on demand."""

    namespace: str
    query: str
    record_id: str


def make_subscription_record(
    namespace: str,
    query: str,
    *,
    source: SourceInfo | None = None,
) -> MemoryRecord:
    """A new standing-query record (content = the query, verbatim)."""
    return MemoryRecord(
        namespace=namespace,
        memory_type="shared",
        content=query,
        attribute=SUBSCRIPTION_ATTRIBUTE,
        source=source or SourceInfo(role="user"),
    )


def parse_subscription(record: MemoryRecord) -> Subscription | None:
    """The subscription a record carries, or None when not subscription-shaped."""
    if record.memory_type != "shared" or record.attribute != SUBSCRIPTION_ATTRIBUTE:
        return None
    return Subscription(
        namespace=record.namespace, query=record.content, record_id=record.record_id
    )


def active_subscriptions(records: list[MemoryRecord]) -> list[MemoryRecord]:
    """Live subscription records among ``records`` (quarantined content is a
    held standing query — it must not drive retrieval until corroborated, E1)."""
    return [
        record
        for record in records
        if record.attribute == SUBSCRIPTION_ATTRIBUTE
        and record.status is RecordStatus.ACTIVATED
        and not record.quarantined
    ]
