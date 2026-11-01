"""SQL implementation of the intake ledger port (RM-137; SQLite and Postgres).

The tables are ``services/storage/intake_tables.py`` (the ledger, message identities and
derivation discovery) and ``services/storage/outbox_tables.py`` (the outbox job book the
discovery feeds). The statements are the ones ``memspine.ingest`` ran before RM-137, moved
behind :class:`~memspine.services.storage.intake_port.IntakeLedger` unchanged: every
multi-row write is one ``engine.begin()`` transaction.

RM-137 concurrency: discovery and policy activation allocate outbox offsets
(``max(item_offset) + 1``), so they run in a write-locked transaction: ``BEGIN IMMEDIATE`` on
SQLite (the write lock is taken before the first read, so a second writer waits instead of
failing to upgrade a stale read snapshot), and on Postgres transaction-scoped advisory locks per
discovery ``(scope, stage)`` and per outbox ``(scope, stage)`` log (the key
``SqlStorage.append_with_outbox`` takes too). A transaction that still loses a race is retried a
bounded number of times (``INTAKE_DISCOVERY_ATTEMPTS``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import orjson
from sqlalchemy import and_, delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from memspine.config.constants import (
    DISCOVERY_NO_WORK_OFFSET,
    INTAKE_DISCOVERY_ATTEMPTS,
    INTAKE_DISCOVERY_RETRY_S,
    OUTBOX_CANCELLED_KEY,
)
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.ingestion import RequiredDerivation
from memspine.schemas.public.receipt import Readiness, Receipt
from memspine.services.storage.intake_port import (
    IntakeSlice,
    MessageClaim,
    derivation_payload,
    derivation_queue_scope,
)
from memspine.services.storage.intake_tables import (
    conversation_identity,
    intake_derivation_discovery,
    intake_derivation_policy,
    intake_intent,
    intake_metadata,
    intake_source,
    intake_write,
    message_identity,
)
from memspine.services.storage.outbox_tables import outbox_item, outbox_metadata, outbox_progress

__all__ = ["SqlIntakeLedger"]

#: Envelope fields the identity backfill copies (public metadata only, never content).
_BACKFILL_FIELDS = (
    "message_id",
    "conversation_id",
    "session_id",
    "sender",
    "created_at",
    "updated_at",
    "metadata",
    "reply_to",
    "ingested_at",
    "created_at_generated",
    "input_fingerprints",
)


class SqlIntakeLedger:
    """The intake ledger over the records database's async engine (borrowed, never opened)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @property
    def engine(self) -> AsyncEngine:
        """The borrowed engine (tests inspect the tables through it)."""
        return self._engine

    async def ensure_schema(self) -> None:
        """Idempotent, called once per ledger: the intake tables and the outbox tables that
        discovery feeds (both are in the fresh baseline; this covers bare databases)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(intake_metadata.create_all)
            await conn.run_sync(outbox_metadata.create_all)

    @asynccontextmanager
    async def _write_tx(self) -> AsyncIterator[AsyncConnection]:
        """A transaction holding the write lock from its first read (see the module doc)."""
        if self._engine.dialect.name != "sqlite":
            async with self._engine.begin() as conn:
                yield conn
            return
        async with self._engine.connect() as raw:
            conn = await raw.execution_options(isolation_level="AUTOCOMMIT")
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                await conn.exec_driver_sql("ROLLBACK")
                raise
            await conn.exec_driver_sql("COMMIT")

    async def _advisory_lock(self, conn: AsyncConnection, key: str) -> None:
        if self._engine.dialect.name == "postgresql":
            await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})

    async def _outbox_lock(self, conn: AsyncConnection, scope: OwnerScope, stage: str) -> None:
        # The key SqlStorage.append_with_outbox takes for the same (scope, stage) offset log.
        key = f"outbox_item:{scope.tenant}:{scope.owner}:{scope.owner_kind}:{stage}"
        await self._advisory_lock(conn, key)

    async def _retrying(self, body: Callable[[], Awaitable[int]]) -> int:
        """Run ``body`` (one write-locked transaction), retrying a lost race."""
        attempt = 1
        while True:
            try:
                return await body()
            except (IntegrityError, OperationalError) as exc:
                lost_race = isinstance(exc, IntegrityError) or _is_busy(exc)
                if not lost_race or attempt >= INTAKE_DISCOVERY_ATTEMPTS:
                    raise
            await asyncio.sleep(INTAKE_DISCOVERY_RETRY_S * attempt)
            attempt += 1

    # ------------------------------------------------------------------------------ the ledger
    async def load_slice(
        self,
        scope_key: str,
        source_id: str,
        write_ids: Sequence[str],
        intent_ids_of: Callable[[str | None, Mapping[str, str]], Iterable[str]],
    ) -> IntakeSlice:
        state: str | None = None
        writes: dict[str, str] = {}
        intents: dict[str, tuple[str, str]] = {}
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(intake_source.c.state).where(
                        intake_source.c.scope_key == scope_key,
                        intake_source.c.source_id == source_id,
                    )
                )
            ).first()
            if row is not None:
                state = row.state
            if write_ids:
                rows = (
                    await conn.execute(
                        select(intake_write.c.write_id, intake_write.c.data).where(
                            intake_write.c.scope_key == scope_key,
                            intake_write.c.write_id.in_(list(write_ids)),
                        )
                    )
                ).all()
                for r in rows:
                    writes[r.write_id] = r.data
            wanted = sorted(set(intent_ids_of(state, writes)))
            if wanted:
                intent_rows = (
                    await conn.execute(
                        select(
                            intake_intent.c.intent_id,
                            intake_intent.c.receipt,
                            intake_intent.c.readiness,
                        ).where(intake_intent.c.intent_id.in_(wanted))
                    )
                ).all()
                for intent_row in intent_rows:
                    intents[intent_row.intent_id] = (intent_row.receipt, intent_row.readiness)
        return IntakeSlice(state, writes, intents)

    async def intents_by_id(self, intent_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        wanted = sorted(set(intent_ids))
        if not wanted:
            return out
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(intake_intent).where(intake_intent.c.intent_id.in_(wanted))
                )
            ).all()
        for r in rows:
            out[r.intent_id] = dict(r._mapping)
        return out

    async def pending_intents(self) -> list[dict[str, Any]]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(intake_intent)
                    .where(intake_intent.c.status == "accepted")
                    .order_by(intake_intent.c.created_at, intake_intent.c.intent_id)
                )
            ).all()
        return [dict(r._mapping) for r in rows]

    async def applied_scope_keys(self) -> list[str]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(intake_intent.c.scope_key)
                    .where(intake_intent.c.status == "applied")
                    .distinct()
                    .order_by(intake_intent.c.scope_key)
                )
            ).all()
        return [str(row.scope_key) for row in rows]

    async def record_ids(self, scope_key: str, write_ids: Sequence[str]) -> dict[str, str]:
        if not write_ids:
            return {}
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(intake_write.c.write_id, intake_write.c.record_id).where(
                        intake_write.c.scope_key == scope_key,
                        intake_write.c.write_id.in_(list(write_ids)),
                        intake_write.c.record_id.is_not(None),
                    )
                )
            ).all()
        return {r.write_id: r.record_id for r in rows}

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
        skey = scope_key
        try:
            async with self._engine.begin() as conn:
                row = (
                    await conn.execute(
                        select(intake_source.c.state).where(
                            intake_source.c.scope_key == skey,
                            intake_source.c.source_id == source_id,
                        )
                    )
                ).first()
                if row is None:
                    await conn.execute(
                        insert(intake_source).values(
                            scope_key=skey, source_id=source_id, state=merge_state(None)
                        )
                    )
                else:
                    await conn.execute(
                        update(intake_source)
                        .where(
                            intake_source.c.scope_key == skey,
                            intake_source.c.source_id == source_id,
                        )
                        .values(state=merge_state(row.state))
                    )
                for wid, data in new_writes:
                    await conn.execute(
                        insert(intake_write).values(
                            scope_key=skey,
                            write_id=wid,
                            source_id=source_id,
                            record_id=None,
                            data=data,
                        )
                    )
                await conn.execute(
                    insert(intake_intent).values(
                        intent_id=intent_id,
                        scope_key=skey,
                        namespace=namespace,
                        source_id=source_id,
                        status="accepted",
                        receipt=receipt.model_dump_json(),
                        readiness=readiness.model_dump_json(),
                        required_derivation=required_derivation.model_dump_json(),
                        apply=apply_payload,
                        created_at=created_at,
                    )
                )
                for claim in messages:
                    mid, cid, sid = claim.message_id, claim.conversation_id, claim.session_id
                    old = (
                        await conn.execute(
                            select(message_identity).where(
                                message_identity.c.namespace == namespace,
                                message_identity.c.message_id == mid,
                            )
                        )
                    ).first()
                    if old is not None:
                        if json.loads(old.data).get("erased") is True:
                            raise ValueError(f"message_id {mid!r} was erased and cannot be reused")
                        if (
                            old.intent_id == intent_id
                            and old.conversation_id == cid
                            and old.session_id == sid
                        ):
                            continue
                        raise ValueError(
                            f"message_id identity conflict for {mid!r} in namespace {namespace!r}"
                        )
                    conversation = (
                        await conn.execute(
                            select(conversation_identity).where(
                                conversation_identity.c.namespace == namespace,
                                conversation_identity.c.conversation_id == cid,
                            )
                        )
                    ).first()
                    if conversation is not None and conversation.session_id != sid:
                        raise ValueError(
                            f"conversation_id {cid!r} is already assigned to session_id {conversation.session_id!r}"
                        )
                    if conversation is None:
                        await conn.execute(
                            insert(conversation_identity).values(
                                namespace=namespace,
                                conversation_id=cid,
                                session_id=sid,
                                intent_id=intent_id,
                            )
                        )
                    await conn.execute(
                        insert(message_identity).values(
                            namespace=namespace,
                            message_id=mid,
                            conversation_id=cid,
                            session_id=sid,
                            intent_id=intent_id,
                            write_id=claim.write_id,
                            record_id=None,
                            data=claim.data,
                        )
                    )
        except IntegrityError as exc:
            raise ValueError("message or conversation identity conflict in namespace") from exc

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
        skey = scope_key
        async with self._engine.begin() as conn:
            for wid, rid in real_ids.items():
                await conn.execute(
                    update(intake_write)
                    .where(intake_write.c.scope_key == skey, intake_write.c.write_id == wid)
                    .values(record_id=rid)
                )
                await conn.execute(
                    update(message_identity)
                    .where(
                        message_identity.c.intent_id == intent_id,
                        message_identity.c.write_id == wid,
                    )
                    .values(record_id=rid)
                )
            row = (
                await conn.execute(
                    select(intake_source.c.state).where(
                        intake_source.c.scope_key == skey, intake_source.c.source_id == source_id
                    )
                )
            ).first()
            if row is not None and real_ids:
                await conn.execute(
                    update(intake_source)
                    .where(
                        intake_source.c.scope_key == skey, intake_source.c.source_id == source_id
                    )
                    .values(state=relink_state(row.state))
                )
            await conn.execute(
                update(intake_intent)
                .where(intake_intent.c.intent_id == intent_id)
                .values(
                    status="applied",
                    apply=None,
                    receipt=receipt.model_dump_json(),
                    readiness=readiness.model_dump_json(),
                    required_derivation=required_derivation.model_dump_json(),
                )
            )

    async def set_readiness(self, intent_id: str, readiness: Readiness) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                update(intake_intent)
                .where(intake_intent.c.intent_id == intent_id)
                .values(readiness=readiness.model_dump_json())
            )

    async def forget_intent(self, intent_id: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(delete(intake_intent).where(intake_intent.c.intent_id == intent_id))

    # ---------------------------------------------------------------------- derivation discovery
    async def discover_committed(
        self,
        *,
        scope_key: str,
        stage: str,
        discovered_at: datetime,
        fault: Callable[[str], None] | None = None,
        intent_ids: Sequence[str] | None = None,
    ) -> int:
        async def body() -> int:
            return await self._discover_once(scope_key, stage, discovered_at, fault, intent_ids)

        return await self._retrying(body)

    async def _discover_once(
        self,
        scope_key: str,
        stage: str,
        discovered_at: datetime,
        fault: Callable[[str], None] | None,
        intent_ids: Sequence[str] | None,
    ) -> int:
        async with self._write_tx() as conn:
            await self._advisory_lock(conn, f"intake_discovery:{scope_key}:{stage}")
            known = intake_derivation_discovery.alias("known")
            scoped = (
                () if intent_ids is None else (intake_intent.c.intent_id.in_(list(intent_ids)),)
            )
            rows = (
                await conn.execute(
                    select(intake_intent)
                    .select_from(
                        intake_intent.outerjoin(
                            known,
                            and_(
                                known.c.intent_id == intake_intent.c.intent_id,
                                known.c.stage == stage,
                            ),
                        )
                    )
                    .where(
                        intake_intent.c.scope_key == scope_key,
                        intake_intent.c.status == "applied",
                        known.c.intent_id.is_(None),
                        *scoped,
                    )
                    .order_by(intake_intent.c.created_at, intake_intent.c.intent_id)
                )
            ).all()
            inserted = 0
            for row in rows:
                manifest = RequiredDerivation.model_validate_json(row.required_derivation)
                offset = DISCOVERY_NO_WORK_OFFSET
                if manifest.source_record_ids:
                    queue_scope = derivation_queue_scope(manifest, stage)
                    await self._outbox_lock(conn, queue_scope, stage)
                    top = (
                        await conn.execute(
                            select(func.max(outbox_item.c.item_offset)).where(
                                outbox_item.c.tenant == queue_scope.tenant,
                                outbox_item.c.owner == queue_scope.owner,
                                outbox_item.c.owner_kind == queue_scope.owner_kind,
                                outbox_item.c.stage == stage,
                            )
                        )
                    ).scalar()
                    offset = 0 if top is None else int(top) + 1
                    await conn.execute(
                        insert(outbox_item).values(
                            tenant=queue_scope.tenant,
                            owner=queue_scope.owner,
                            owner_kind=queue_scope.owner_kind,
                            stage=stage,
                            item_offset=offset,
                            payload=json.dumps(derivation_payload(manifest), sort_keys=True),
                            created_at=discovered_at.isoformat(),
                        )
                    )
                    if fault is not None:
                        fault("after_outbox_insert")
                    inserted += 1
                # An applied intent without raw records (every turn excluded) has no work; it
                # is recorded as discovered so it is neither rescanned nor blocking.
                await conn.execute(
                    insert(intake_derivation_discovery).values(
                        intent_id=manifest.intent_id,
                        stage=stage,
                        item_offset=offset,
                        discovered_at=discovered_at.isoformat(),
                    )
                )
            return inserted

    async def activate_policy(
        self,
        *,
        scope_key: str,
        stage: str,
        policy_revision: str,
        registry_digest: str | None,
        activated_at: datetime,
    ) -> int:
        async def body() -> int:
            return await self._activate_once(
                scope_key, stage, policy_revision, registry_digest, activated_at
            )

        return await self._retrying(body)

    async def _activate_once(
        self,
        scope_key: str,
        stage: str,
        policy_revision: str,
        registry_digest: str | None,
        activated_at: datetime,
    ) -> int:
        async with self._write_tx() as conn:
            await self._advisory_lock(conn, f"intake_discovery:{scope_key}:{stage}")
            policy_values = {
                "policy_revision": policy_revision,
                "registry_digest": registry_digest,
                "activated_at": activated_at.isoformat(),
            }
            changed = await conn.execute(
                update(intake_derivation_policy)
                .where(
                    intake_derivation_policy.c.scope_key == scope_key,
                    intake_derivation_policy.c.stage == stage,
                )
                .values(**policy_values)
            )
            if changed.rowcount == 0:
                await conn.execute(
                    insert(intake_derivation_policy).values(
                        scope_key=scope_key, stage=stage, **policy_values
                    )
                )
            rows = (
                await conn.execute(
                    select(
                        intake_intent.c.required_derivation,
                        intake_derivation_discovery.c.item_offset,
                    )
                    .select_from(
                        intake_derivation_discovery.join(
                            intake_intent,
                            intake_intent.c.intent_id == intake_derivation_discovery.c.intent_id,
                        )
                    )
                    .where(
                        intake_intent.c.scope_key == scope_key,
                        intake_derivation_discovery.c.stage == stage,
                    )
                )
            ).all()
            requeued = 0
            for row in rows:
                manifest = RequiredDerivation.model_validate_json(row.required_derivation)
                queue_scope = derivation_queue_scope(manifest, stage)
                if manifest.policy_revision == policy_revision:
                    continue
                progress = (
                    await conn.execute(
                        select(outbox_progress.c.watermark, outbox_progress.c.holes).where(
                            outbox_progress.c.tenant == queue_scope.tenant,
                            outbox_progress.c.owner == queue_scope.owner,
                            outbox_progress.c.owner_kind == queue_scope.owner_kind,
                            outbox_progress.c.job_kind == stage,
                        )
                    )
                ).first()
                retryable = progress is None or int(row.item_offset) > int(progress.watermark)
                if progress is not None and not retryable:
                    holes = json.loads(progress.holes)
                    retryable = any(
                        int(hole.get("offset", -1)) == int(row.item_offset)
                        and not hole.get("waived", False)
                        and hole.get("reason") != "budget_exhausted"
                        for hole in holes
                    )
                if not retryable:
                    continue
                await self._outbox_lock(conn, queue_scope, stage)
                old = (
                    await conn.execute(
                        select(outbox_item.c.payload).where(
                            outbox_item.c.tenant == queue_scope.tenant,
                            outbox_item.c.owner == queue_scope.owner,
                            outbox_item.c.owner_kind == queue_scope.owner_kind,
                            outbox_item.c.stage == stage,
                            outbox_item.c.item_offset == row.item_offset,
                        )
                    )
                ).first()
                if old is None:
                    continue
                old_payload = json.loads(old.payload)
                if any(old_payload.get(flag) for flag in ("erased", "excluded", "revoked")):
                    continue
                await conn.execute(
                    update(outbox_item)
                    .where(
                        outbox_item.c.tenant == queue_scope.tenant,
                        outbox_item.c.owner == queue_scope.owner,
                        outbox_item.c.owner_kind == queue_scope.owner_kind,
                        outbox_item.c.stage == stage,
                        outbox_item.c.item_offset == row.item_offset,
                    )
                    .values(
                        payload=json.dumps(
                            {**old_payload, OUTBOX_CANCELLED_KEY: policy_revision},
                            sort_keys=True,
                        )
                    )
                )
                top = (
                    await conn.execute(
                        select(func.max(outbox_item.c.item_offset)).where(
                            outbox_item.c.tenant == queue_scope.tenant,
                            outbox_item.c.owner == queue_scope.owner,
                            outbox_item.c.owner_kind == queue_scope.owner_kind,
                            outbox_item.c.stage == stage,
                        )
                    )
                ).scalar()
                new_manifest = manifest.model_copy(
                    update={
                        "policy_revision": policy_revision,
                        "registry_digest": registry_digest,
                        "activated_at": activated_at,
                    }
                )
                await conn.execute(
                    insert(outbox_item).values(
                        tenant=queue_scope.tenant,
                        owner=queue_scope.owner,
                        owner_kind=queue_scope.owner_kind,
                        stage=stage,
                        item_offset=int(top) + 1,
                        payload=json.dumps(
                            {
                                **derivation_payload(new_manifest),
                                "supersedes_offset": int(row.item_offset),
                            },
                            sort_keys=True,
                        ),
                        created_at=activated_at.isoformat(),
                    )
                )
                # RM-142: discovery now points at the superseding item (the cancelled one is
                # skipped by the runner at selection time).
                await conn.execute(
                    update(intake_derivation_discovery)
                    .where(
                        intake_derivation_discovery.c.intent_id == manifest.intent_id,
                        intake_derivation_discovery.c.stage == stage,
                    )
                    .values(item_offset=int(top) + 1)
                )
                requeued += 1
            return requeued

    # ------------------------------------------------------------------- message identity index
    async def lookup_messages(
        self, namespace: str, message_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        wanted = list(dict.fromkeys(message_ids))
        if not wanted:
            return {}
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(message_identity, intake_intent.c.status.label("intent_status"))
                    .outerjoin(
                        intake_intent,
                        intake_intent.c.intent_id == message_identity.c.intent_id,
                    )
                    .where(
                        message_identity.c.namespace == namespace,
                        message_identity.c.message_id.in_(wanted),
                    )
                )
            ).all()
        return {
            row.message_id: {
                **json.loads(row.data),
                "message_id": row.message_id,
                "conversation_id": row.conversation_id,
                "session_id": row.session_id,
                "intent_id": row.intent_id,
                "record_id": row.record_id,
                "intent_status": row.intent_status,
            }
            for row in rows
        }

    async def lookup_conversations(
        self, namespace: str, conversation_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        wanted = list(dict.fromkeys(conversation_ids))
        if not wanted:
            return {}
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(conversation_identity).where(
                        conversation_identity.c.namespace == namespace,
                        conversation_identity.c.conversation_id.in_(wanted),
                    )
                )
            ).all()
        return {
            row.conversation_id: {
                "conversation_id": row.conversation_id,
                "session_id": row.session_id,
                "intent_id": row.intent_id,
            }
            for row in rows
        }

    async def tombstone_records(self, record_ids: Sequence[str]) -> int:
        wanted = list(dict.fromkeys(record_ids))
        if not wanted:
            return 0
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(message_identity)
                .where(message_identity.c.record_id.in_(wanted))
                .values(
                    intent_id=None,
                    write_id=None,
                    record_id=None,
                    data=json.dumps({"erased": True}, sort_keys=True),
                )
            )
            return int(result.rowcount or 0)

    async def backfill_message_identities(self) -> int:
        """IDs already present (including erased tombstones) are never replaced."""
        from memspine.services.storage.sqlite.schema import memory_records

        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(
                        memory_records.c.record_id,
                        memory_records.c.namespace,
                        memory_records.c.source,
                    ).where(memory_records.c.memory_type == "episodic")
                )
            ).all()
        # Gather only public envelope fields; message identity metadata never includes content.
        found: list[tuple[str, str, dict[str, Any]]] = []
        for row in rows:
            source = orjson.loads(row.source)
            meta = source.get("message") if isinstance(source, dict) else None
            if not isinstance(meta, dict):
                continue
            mid, cid, sid = (
                meta.get("message_id"),
                meta.get("conversation_id"),
                meta.get("session_id"),
            )
            if all(isinstance(value, str) and value for value in (mid, cid, sid)):
                public = {key: meta[key] for key in _BACKFILL_FIELDS if key in meta}
                found.append((str(row.record_id), str(row.namespace), public))
        inserted = 0
        async with self._engine.begin() as conn:
            for rid, namespace, meta in found:
                mid, cid, sid = meta["message_id"], meta["conversation_id"], meta["session_id"]
                existing = (
                    await conn.execute(
                        select(message_identity.c.message_id).where(
                            message_identity.c.namespace == namespace,
                            message_identity.c.message_id == mid,
                        )
                    )
                ).first()
                if existing is not None:
                    continue
                reservation = (
                    await conn.execute(
                        select(conversation_identity.c.session_id).where(
                            conversation_identity.c.namespace == namespace,
                            conversation_identity.c.conversation_id == cid,
                        )
                    )
                ).first()
                if reservation is not None and reservation.session_id != sid:
                    raise ValueError(
                        f"conversation_id {cid!r} maps to multiple sessions in namespace {namespace!r}"
                    )
                if reservation is None:
                    await conn.execute(
                        conversation_identity.insert().values(
                            namespace=namespace,
                            conversation_id=cid,
                            session_id=sid,
                            intent_id=None,
                        )
                    )
                await conn.execute(
                    message_identity.insert().values(
                        namespace=namespace,
                        message_id=mid,
                        conversation_id=cid,
                        session_id=sid,
                        intent_id=None,
                        write_id=None,
                        record_id=rid,
                        data=json.dumps(meta, sort_keys=True),
                    )
                )
                inserted += 1
        return inserted


def _is_busy(exc: OperationalError) -> bool:
    """SQLite lost the write-lock race (still busy after the busy timeout)."""
    message = str(exc)
    return "database is locked" in message or "database is busy" in message
