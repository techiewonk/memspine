"""Shared memory (R2/ADR-016): cross-namespace read grants + subscriptions.

Grants are WRITE events through the door (new information rides the log);
reads across a grant are live views into the grantor namespace — never
copies — surfaced with trust capped at ``TRUST_RETRIEVED_CAP`` (E1).
"""

from memspine.memories.shared.grants import (
    Grant,
    SharedMemory,
    active_grants,
    make_grant_record,
    parse_grant,
)
from memspine.memories.shared.subscriptions import (
    Subscription,
    active_subscriptions,
    make_subscription_record,
    parse_subscription,
)

__all__ = [
    "Grant",
    "SharedMemory",
    "Subscription",
    "active_grants",
    "active_subscriptions",
    "make_grant_record",
    "make_subscription_record",
    "parse_grant",
    "parse_subscription",
]
