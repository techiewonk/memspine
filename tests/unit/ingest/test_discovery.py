from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from memspine.ingest.discovery import (
    activate_derivation_policy,
    discover_committed_derivations,
)
from memspine.ingest.intake_store import ensure_schema
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.ingestion import RequiredDerivation
from memspine.services.storage.intake_tables import intake_intent
from memspine.services.storage.outbox_tables import outbox_item, outbox_progress

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
SCOPE = OwnerScope(tenant="tenant", owner="alice")


def _manifest(intent: str, *, revision: str = "policy:one") -> RequiredDerivation:
    return RequiredDerivation(
        intent_id=intent,
        namespace="alice",
        scope=SCOPE,
        source_id=f"source:{intent}",
        source_version=1,
        source_write_ids=(f"write:{intent}",),
        source_record_ids=(f"record:{intent}",),
        required_layers=("raw_store", "facts"),
        policy_revision=revision,
        registry_digest="registry:one",
        activated_at=NOW,
    )


async def _put_intent(db, intent: str, created_at: datetime, *, status: str = "applied") -> None:
    manifest = _manifest(intent)
    async with db.begin() as conn:
        await conn.execute(
            insert(intake_intent).values(
                intent_id=intent,
                scope_key="scope:alice",
                namespace="alice",
                source_id=manifest.source_id,
                status=status,
                receipt="{}",
                readiness="{}",
                required_derivation=manifest.model_dump_json(),
                apply=None,
                created_at=created_at.isoformat(),
            )
        )


async def test_discovery_atomically_advances_and_is_idempotent(tmp_path) -> None:
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'discovery.db'}")
    await ensure_schema(db)
    await _put_intent(db, "a", NOW)
    await _put_intent(db, "b", NOW + timedelta(seconds=1), status="accepted")

    assert (
        await discover_committed_derivations(
            db, scope_key="scope:alice", discovered_at=NOW + timedelta(minutes=1)
        )
        == 1
    )
    assert (
        await discover_committed_derivations(
            db, scope_key="scope:alice", discovered_at=NOW + timedelta(minutes=2)
        )
        == 0
    )
    async with db.connect() as conn:
        rows = (await conn.execute(select(outbox_item))).all()
    assert len(rows) == 1
    payload = json.loads(rows[0].payload)
    assert payload["intent_id"] == "a"
    assert payload["manifest"]["source_record_ids"] == ["record:a"]
    await db.dispose()


async def test_discovery_crash_rolls_back_item_and_cursor(tmp_path) -> None:
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'discovery-crash.db'}")
    await ensure_schema(db)
    await _put_intent(db, "a", NOW)

    def crash(boundary: str) -> None:
        assert boundary == "after_outbox_insert"
        raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        await discover_committed_derivations(
            db, scope_key="scope:alice", discovered_at=NOW, fault=crash
        )
    assert await discover_committed_derivations(db, scope_key="scope:alice", discovered_at=NOW) == 1
    async with db.connect() as conn:
        rows = (await conn.execute(select(outbox_item))).all()
    assert len(rows) == 1
    await db.dispose()


async def test_explicit_policy_activation_requeues_only_unfinished(tmp_path) -> None:
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'activation.db'}")
    await ensure_schema(db)
    await _put_intent(db, "a", NOW)
    await discover_committed_derivations(db, scope_key="scope:alice", discovered_at=NOW)

    with pytest.raises(ValueError, match="quiesced"):
        await activate_derivation_policy(
            db,
            scope_key="scope:alice",
            stage="derivation",
            policy_revision="policy:two",
            registry_digest="registry:two",
            activated_at=NOW,
            runner_quiesced=False,
        )
    assert (
        await activate_derivation_policy(
            db,
            scope_key="scope:alice",
            stage="derivation",
            policy_revision="policy:two",
            registry_digest="registry:two",
            activated_at=NOW,
            runner_quiesced=True,
        )
        == 1
    )
    async with db.connect() as conn:
        rows = (await conn.execute(select(outbox_item).order_by(outbox_item.c.item_offset))).all()
    assert len(rows) == 2
    assert json.loads(rows[0].payload)["cancelled_by_policy_revision"] == "policy:two"
    replacement = json.loads(rows[1].payload)
    assert replacement["supersedes_offset"] == 0
    assert replacement["manifest"]["policy_revision"] == "policy:two"
    await db.dispose()


async def test_completed_manifest_is_not_backfilled_on_activation(tmp_path) -> None:
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'completed.db'}")
    await ensure_schema(db)
    await _put_intent(db, "done", NOW)
    await discover_committed_derivations(db, scope_key="scope:alice", discovered_at=NOW)
    async with db.begin() as conn:
        await conn.execute(
            insert(outbox_progress).values(
                tenant=SCOPE.tenant,
                owner=SCOPE.owner,
                owner_kind=SCOPE.owner_kind,
                job_kind="derivation",
                watermark=0,
                target_offset=0,
                holes="[]",
            )
        )
    assert (
        await activate_derivation_policy(
            db,
            scope_key="scope:alice",
            stage="derivation",
            policy_revision="policy:two",
            registry_digest="registry:two",
            activated_at=NOW,
            runner_quiesced=True,
        )
        == 0
    )
    await db.dispose()


@pytest.mark.parametrize("flag", ["erased", "excluded", "revoked"])
async def test_erased_excluded_or_revoked_item_is_never_requeued(tmp_path, flag: str) -> None:
    """RM-43: an unfinished item whose payload is marked erased/excluded/revoked stays
    untouched on activation: no cancellation stamp and no replacement item."""
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'{flag}.db'}")
    await ensure_schema(db)
    await _put_intent(db, "gone", NOW)
    await discover_committed_derivations(db, scope_key="scope:alice", discovered_at=NOW)
    async with db.begin() as conn:
        row = (await conn.execute(select(outbox_item))).one()
        marked = json.dumps({**json.loads(row.payload), flag: True}, sort_keys=True)
        await conn.execute(update(outbox_item).values(payload=marked))
    assert (
        await activate_derivation_policy(
            db,
            scope_key="scope:alice",
            stage="derivation",
            policy_revision="policy:two",
            registry_digest="registry:two",
            activated_at=NOW,
            runner_quiesced=True,
        )
        == 0
    )
    async with db.connect() as conn:
        rows = (await conn.execute(select(outbox_item))).all()
    assert len(rows) == 1
    assert "cancelled_by_policy_revision" not in json.loads(rows[0].payload)
    await db.dispose()


async def test_window_discovery_activation_uses_namespace_queue_scope(tmp_path) -> None:
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'window-activation.db'}")
    await ensure_schema(db)
    await _put_intent(db, "window", NOW)
    await discover_committed_derivations(
        db,
        scope_key="scope:alice",
        stage="window.discovery",
        discovered_at=NOW,
    )

    assert (
        await activate_derivation_policy(
            db,
            scope_key="scope:alice",
            stage="window.discovery",
            policy_revision="policy:two",
            registry_digest="registry:two",
            activated_at=NOW,
            runner_quiesced=True,
        )
        == 1
    )
    async with db.connect() as conn:
        rows = (
            await conn.execute(select(outbox_item).where(outbox_item.c.stage == "window.discovery"))
        ).all()
    assert len(rows) == 2
    assert {(row.tenant, row.owner) for row in rows} == {("default", "alice")}
    await db.dispose()
