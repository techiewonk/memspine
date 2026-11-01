"""Durable raw-to-derivation discovery and explicit policy activation.

The intake intent remains the authority for accepted raw work.  This scanner
reconciles applied intents into the existing durable outbox and records each one
as discovered in the *same transaction* as its outbox insert (the intake ledger
port's :meth:`~memspine.services.storage.intake_port.IntakeLedger.discover_committed`).
Repeating it after a crash is therefore harmless and cannot skip a committed source.

RM-142: the scan is an anti-join (applied intents with no discovery row for the
stage), not a creation-order cursor, so an intent that applies after a later one
(its first apply raised and ``recover`` finished it) is still discovered. An
applied intent without raw records is recorded as discovered with no work
(``DISCOVERY_NO_WORK_OFFSET``), so it never stalls the scope.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from memspine.services.storage.intake_port import IntakeLedger

__all__ = ["activate_derivation_policy", "discover_committed_derivations"]


async def discover_committed_derivations(
    ledger: IntakeLedger,
    *,
    scope_key: str,
    stage: str = "derivation",
    discovered_at: datetime,
    fault: Callable[[str], None] | None = None,
    intent_ids: Sequence[str] | None = None,
) -> int:
    """Atomically enqueue every newly applied manifest for ``scope_key``.

    Accepted-but-not-applied intents are deliberately ignored: their raw record
    identities are not committed yet.  Intents without record ids are recorded as
    discovered with no work. ``intent_ids`` scopes the scan (RM-137: the write path
    discovers only the intent it just applied; ``recover`` runs the full scan).
    """
    return await ledger.discover_committed(
        scope_key=scope_key,
        stage=stage,
        discovered_at=discovered_at,
        fault=fault,
        intent_ids=intent_ids,
    )


async def activate_derivation_policy(
    ledger: IntakeLedger,
    *,
    scope_key: str,
    stage: str,
    policy_revision: str,
    registry_digest: str | None,
    activated_at: datetime,
    runner_quiesced: bool,
) -> int:
    """Activate a revision and requeue only unfinished, retry-eligible work.

    Activation is intentionally an administrative boundary.  The runner must
    first be quiesced so an old-policy worker cannot race the rewrite. Completed
    items are historical evidence and are never backfilled.
    """
    if not runner_quiesced:
        raise ValueError("policy activation requires a quiesced derivation runner")
    return await ledger.activate_policy(
        scope_key=scope_key,
        stage=stage,
        policy_revision=policy_revision,
        registry_digest=registry_digest,
        activated_at=activated_at,
    )
