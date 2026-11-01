"""Durable raw-to-derivation discovery and explicit policy activation.

The intake intent remains the authority for accepted raw work.  This scanner
reconciles applied intents into the existing SQL outbox and records each one in
``intake_derivation_discovery`` in the *same transaction* as its outbox insert.
Repeating it after a crash is therefore harmless and cannot skip a committed source.

RM-142: the scan is an anti-join (applied intents with no discovery row for the
stage), not a creation-order cursor, so an intent that applies after a later one
(its first apply raised and ``recover`` finished it) is still discovered. An
applied intent without raw records is recorded as discovered with no work
(``DISCOVERY_NO_WORK_OFFSET``), so it never stalls the scope.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from memspine.config.constants import DISCOVERY_NO_WORK_OFFSET, OUTBOX_CANCELLED_KEY
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.ingestion import RequiredDerivation
from memspine.services.storage.intake_tables import (
    intake_derivation_discovery,
    intake_derivation_policy,
    intake_intent,
)
from memspine.services.storage.outbox_tables import outbox_item, outbox_metadata, outbox_progress

__all__ = ["activate_derivation_policy", "discover_committed_derivations"]


def _payload(manifest: RequiredDerivation) -> dict[str, object]:
    return {
        "kind": "required_derivation",
        "intent_id": manifest.intent_id,
        "manifest": manifest.model_dump(mode="json"),
    }


def _queue_scope(manifest: RequiredDerivation, stage: str) -> OwnerScope:
    # WindowPipeline's durable scope is namespace-based. Intake identity scope
    # remains tenant/owner-based and is preserved inside the manifest.
    if stage == "window.discovery":
        return OwnerScope(tenant="default", owner=manifest.namespace)
    return manifest.scope


async def discover_committed_derivations(
    engine: AsyncEngine,
    *,
    scope_key: str,
    stage: str = "derivation",
    discovered_at: datetime,
    fault: Callable[[str], None] | None = None,
) -> int:
    """Atomically enqueue every newly applied manifest for ``scope_key``.

    Accepted-but-not-applied intents are deliberately ignored: their raw record
    identities are not committed yet.  Intents without record ids are likewise
    left for the next reconciliation pass.
    """
    async with engine.begin() as conn:
        await conn.run_sync(outbox_metadata.create_all)
        known = intake_derivation_discovery.alias("known")
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
                )
                .order_by(intake_intent.c.created_at, intake_intent.c.intent_id)
            )
        ).all()
        inserted = 0
        for row in rows:
            manifest = RequiredDerivation.model_validate_json(row.required_derivation)
            offset = DISCOVERY_NO_WORK_OFFSET
            if manifest.source_record_ids:
                queue_scope = _queue_scope(manifest, stage)
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
                        payload=json.dumps(_payload(manifest), sort_keys=True),
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


async def activate_derivation_policy(
    engine: AsyncEngine,
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
    async with engine.begin() as conn:
        await conn.run_sync(outbox_metadata.create_all)
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
            queue_scope = _queue_scope(manifest, stage)
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
                        {**_payload(new_manifest), "supersedes_offset": int(row.item_offset)},
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
