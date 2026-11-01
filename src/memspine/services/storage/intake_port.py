"""The intake ledger capability port (RM-137, D-22): what ingestion needs from storage.records.

The intake gateway (``memspine.ingest``) keeps a durable ledger next to the records: the
source state per ``(scope, source)``, one row per S3-3 write id, one intent per accepted
envelope, the public message and conversation identities, and the raw-to-derivation
discovery rows that feed the outbox. Ingestion talks to that ledger only through
:class:`IntakeLedger`; the SQL implementation is
:class:`~memspine.services.storage.intake_sql.SqlIntakeLedger` (SQLite and Postgres), reached
through ``SqlStorage.intake_ledger()``.

A records store offers the ledger by declaring the ``intake_ledger`` capability on its
descriptor and implementing ``intake_ledger()``. MongoDB does neither today: the discovery
half writes into the SQL outbox job book, which MongoDB does not have either, so a
configuration that needs background derivation on MongoDB is refused at boot
(:mod:`memspine.providers.records_features`) and the intake calls themselves fail with a
:class:`~memspine.exceptions.ConfigError` naming the capability.

Atomicity contract (each implementation must keep it):

* :meth:`IntakeLedger.record_intent` writes the source state, the new write ids, the intent
  (receipt, readiness, required-derivation manifest, apply payload) and the message and
  conversation identity claims in ONE transaction; any identity conflict rolls all of it
  back and raises ``ValueError`` (the message names an ``identity conflict``, which callers
  retry on, or an erased id, which they do not).
* :meth:`IntakeLedger.apply_intent` records the real record ids, the relinked live state and
  the applied receipt, and drops the apply payload, in ONE transaction.
* :meth:`IntakeLedger.discover_committed` inserts each outbox item and its discovery row in
  the same transaction, so a crash can neither skip nor duplicate a committed source
  (RM-142: an anti-join on the discovery rows, not a cursor).
* :meth:`IntakeLedger.activate_policy` rewrites the policy row and requeues every unfinished,
  retry-eligible item in one transaction.

The codec of the source state and write rows belongs to ingestion: the port carries them as
opaque JSON text and takes the pure functions that read or rewrite them as callables, which an
implementation calls inside its transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.ingestion import RequiredDerivation
from memspine.schemas.public.receipt import Readiness, Receipt

__all__ = [
    "INTAKE_LEDGER_CAPABILITY",
    "IntakeLedger",
    "IntakeSlice",
    "MessageClaim",
    "derivation_payload",
    "derivation_queue_scope",
    "intake_ledger_of",
]

#: The descriptor capability of a records provider that implements :class:`IntakeLedger`.
INTAKE_LEDGER_CAPABILITY = "intake_ledger"


@dataclass(frozen=True, slots=True)
class IntakeSlice:
    """The ledger rows one acceptance reads, in one consistent read.

    ``state`` is the source state JSON (None for a new source); ``writes`` maps each requested
    write id that exists to its live-segment JSON; ``intents`` maps every intent the caller
    named to its ``(receipt JSON, readiness JSON)``."""

    state: str | None
    writes: Mapping[str, str]
    intents: Mapping[str, tuple[str, str]]


@dataclass(frozen=True, slots=True)
class MessageClaim:
    """One public message identity reserved with an acceptance (namespace-scoped).

    ``data`` is the envelope metadata JSON (never content)."""

    message_id: str
    conversation_id: str
    session_id: str
    write_id: str
    data: str


def derivation_payload(manifest: RequiredDerivation) -> dict[str, object]:
    """The outbox payload of one discovered manifest."""
    return {
        "kind": "required_derivation",
        "intent_id": manifest.intent_id,
        "manifest": manifest.model_dump(mode="json"),
    }


def derivation_queue_scope(manifest: RequiredDerivation, stage: str) -> OwnerScope:
    """The outbox scope a manifest is queued under for ``stage``."""
    # WindowPipeline's durable scope is namespace-based. Intake identity scope
    # remains tenant/owner-based and is preserved inside the manifest.
    if stage == "window.discovery":
        return OwnerScope(tenant="default", owner=manifest.namespace)
    return manifest.scope


@runtime_checkable
class IntakeLedger(Protocol):
    """The intake ledger, message identity index and derivation discovery of a records store."""

    async def ensure_schema(self) -> None:
        """Idempotent: bring the ledger's tables or collections into existence."""
        ...

    # -- the intake ledger --------------------------------------------------------------------
    async def load_slice(
        self,
        scope_key: str,
        source_id: str,
        write_ids: Sequence[str],
        intent_ids_of: Callable[[str | None, Mapping[str, str]], Iterable[str]],
    ) -> IntakeSlice:
        """The source state, the named write rows and the intents ``intent_ids_of(state,
        writes)`` names, read together."""
        ...

    async def intents_by_id(self, intent_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Full intent rows (``intent_id``, ``scope_key``, ``namespace``, ``source_id``,
        ``status``, ``receipt``, ``readiness``, ``required_derivation``, ``apply``,
        ``created_at``) by id; unknown ids are absent."""
        ...

    async def pending_intents(self) -> list[dict[str, Any]]:
        """Accepted-but-not-applied intent rows, oldest first (``created_at``, then id)."""
        ...

    async def applied_scope_keys(self) -> list[str]:
        """Distinct scope keys with an applied intent, sorted."""
        ...

    async def record_ids(self, scope_key: str, write_ids: Sequence[str]) -> dict[str, str]:
        """The real record id of each named write that has one."""
        ...

    async def record_intent(
        self,
        *,
        scope_key: str,
        source_id: str,
        merge_state: Callable[[str | None], str],
        new_writes: Sequence[tuple[str, str]],
        intent_id: str,
        namespace: str,
        receipt: Receipt,
        readiness: Readiness,
        required_derivation: RequiredDerivation,
        apply_payload: str,
        created_at: str,
        messages: Sequence[MessageClaim],
    ) -> None:
        """The one transaction of an accept (see the module contract).

        ``merge_state(previous state JSON or None)`` returns the state to store;
        ``new_writes`` are ``(write id, live-segment JSON)`` rows with no record id yet."""
        ...

    async def apply_intent(
        self,
        *,
        intent_id: str,
        scope_key: str,
        source_id: str,
        real_ids: Mapping[str, str],
        relink_state: Callable[[str], str],
        receipt: Receipt,
        readiness: Readiness,
        required_derivation: RequiredDerivation,
    ) -> None:
        """The apply finished: one transaction. ``relink_state`` rewrites the stored source
        state with the real ids; it is called only when the state exists and ``real_ids`` is
        not empty."""
        ...

    async def set_readiness(self, intent_id: str, readiness: Readiness) -> None: ...

    async def forget_intent(self, intent_id: str) -> None: ...

    # -- derivation discovery -----------------------------------------------------------------
    async def discover_committed(
        self,
        *,
        scope_key: str,
        stage: str,
        discovered_at: datetime,
        fault: Callable[[str], None] | None = None,
        intent_ids: Sequence[str] | None = None,
    ) -> int:
        """Enqueue every applied, not yet discovered manifest of ``scope_key`` for ``stage``;
        returns the number of outbox items inserted. ``intent_ids`` limits the scan to those
        intents (the write path's just-applied one; None = the full reconciliation scan).
        Offsets are allocated under the store's write lock, safe against a concurrent
        discoverer or appender. ``fault`` (tests) is called with ``"after_outbox_insert"``
        inside the transaction."""
        ...

    async def activate_policy(
        self,
        *,
        scope_key: str,
        stage: str,
        policy_revision: str,
        registry_digest: str | None,
        activated_at: datetime,
    ) -> int:
        """Record the active policy revision and requeue only unfinished, retry-eligible items
        under it; returns how many were requeued. The caller guarantees a quiesced runner."""
        ...

    # -- message identity index ---------------------------------------------------------------
    async def lookup_messages(
        self, namespace: str, message_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]: ...

    async def lookup_conversations(
        self, namespace: str, conversation_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]: ...

    async def tombstone_records(self, record_ids: Sequence[str]) -> int:
        """Redact the identity rows of hard-forgotten records, keeping the ids non-reusable."""
        ...

    async def backfill_message_identities(self) -> int:
        """Index source envelopes of existing episodic records that have no identity row."""
        ...


def intake_ledger_of(storage: Any) -> IntakeLedger | None:
    """The intake ledger of a records store, or None when it offers none."""
    own = getattr(storage, "intake_ledger", None)
    if not callable(own):
        return None
    ledger = own()
    return ledger if isinstance(ledger, IntakeLedger) else None
