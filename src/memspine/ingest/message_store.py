"""Durable lookups for public message identities stored by the intake transaction.

The identities live in the records store's intake ledger
(:class:`~memspine.services.storage.intake_port.IntakeLedger`, RM-137).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from memspine.exceptions import ConfigError
from memspine.services.storage.intake_port import IntakeLedger, intake_ledger_of


async def lookup_messages(
    ledger: IntakeLedger, namespace: str, message_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Return requested identities and record IDs without scanning raw records."""
    return await ledger.lookup_messages(namespace, message_ids)


async def lookup_conversations(
    ledger: IntakeLedger, namespace: str, conversation_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Return lightweight namespace-scoped conversation/session reservations."""
    return await ledger.lookup_conversations(namespace, conversation_ids)


async def tombstone_records(ledger: IntakeLedger, record_ids: Sequence[str]) -> int:
    """Redact metadata for hard-forgotten records while retaining non-reusable IDs."""
    return await ledger.tombstone_records(record_ids)


async def backfill_from_storage(storage: Any) -> int:
    """Index existing source envelopes without reading/copying record content.

    This is a startup repair for databases created before the identity projection existed. IDs
    already present (including erased tombstones) are never replaced.
    """
    ledger = intake_ledger_of(storage)
    if ledger is None:
        raise ConfigError("the records store offers no intake_ledger capability")
    return await ledger.backfill_message_identities()
