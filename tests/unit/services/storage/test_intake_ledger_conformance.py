"""RM-137: the intake ledger port's contract, run against every implementation.

``IntakeLedger`` (``services/storage/intake_port.py``) is what ingestion needs from
storage.records: the intake intents, message identities, derivation discovery and policy
activation. SQLite (in-memory and file) always runs; Postgres needs ``MEMSPINE_TEST_PG_URL``
(a scratch server; each run gets a throwaway database). MongoDB declares no ``intake_ledger``
capability (it has no SQL outbox for discovery to feed), so its part of the contract is the
refusal: no ``intake_ledger()`` on the store and a boot-time refusal of background derivation.
"""

from __future__ import annotations

import importlib.util
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memspine.ingest import intake_store
from memspine.ingest.discovery import activate_derivation_policy, discover_committed_derivations
from memspine.ingest.intake import IntakeBook, IntakeChange
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.ingestion import RequiredDerivation
from memspine.schemas.steps.shared import Scope, Segment, SourceEnvelope
from memspine.services.storage.intake_port import (
    INTAKE_LEDGER_CAPABILITY,
    IntakeLedger,
    intake_ledger_of,
)
from memspine.services.storage.intake_sql import SqlIntakeLedger

PG_URL = os.environ.get("MEMSPINE_TEST_PG_URL")
NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
NS = "tenant/user"


# ------------------------------------------------------------------------------- providers
@pytest.fixture
def pg_database() -> Iterator[str]:
    if not PG_URL or importlib.util.find_spec("psycopg") is None:
        pytest.skip("intake ledger on Postgres: set MEMSPINE_TEST_PG_URL (scratch server)")
    import psycopg
    from sqlalchemy.engine import make_url

    name = f"il_{uuid.uuid4().hex[:12]}"
    base = make_url(PG_URL)
    admin = base.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(params=["sqlite_memory", "sqlite_file", "postgres"])
async def ledger(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[IntakeLedger]:
    kind = request.param
    if kind == "postgres":
        from sqlalchemy.ext.asyncio import create_async_engine

        url = request.getfixturevalue("pg_database")
        engine = create_async_engine(url.replace("postgresql://", "postgresql+psycopg://", 1))
        ledger = SqlIntakeLedger(engine)
        await ledger.ensure_schema()
        try:
            yield ledger
        finally:
            await engine.dispose()
        return
    from memspine.clients.sqlite import SQLiteClient, SQLiteSpec

    path = ":memory:" if kind == "sqlite_memory" else str(tmp_path / "ledger.db")
    client = SQLiteClient(SQLiteSpec(path, busy_timeout_s=5.0))
    await client.open()
    ledger = SqlIntakeLedger(client.engine)
    await ledger.ensure_schema()
    try:
        yield ledger
    finally:
        await client.close()


# --------------------------------------------------------------------------------- helpers
def _accept(source_id: str, texts: list[str]) -> IntakeChange:
    overlay = intake_store.Overlay()
    env = SourceEnvelope(
        source_id=source_id,
        kind="conversation",
        scope=Scope(tenant="tenant", user="user", session="session-1"),
        segments=[Segment(seg_index=i, role="user", content=t) for i, t in enumerate(texts)],
    )
    IntakeBook(overlay, ephemeral=True).accept(env, [], None, now=NOW)
    assert overlay.change is not None
    return overlay.change


def _manifest(change: IntakeChange, *, records: tuple[str, ...] = ()) -> RequiredDerivation:
    return RequiredDerivation(
        intent_id=change.receipt.intent_id,
        namespace=NS,
        scope=OwnerScope(tenant="tenant", owner="user"),
        source_id=change.source_id,
        source_version=change.version,
        source_write_ids=tuple(change.new_write_ids),
        source_record_ids=records,
        required_layers=("raw_store", "facts"),
        policy_revision="policy:one",
        registry_digest="registry:one",
        activated_at=NOW,
    )


def _payload(change: IntakeChange, conversation: str | None = None, mid: str = "m-1") -> str:
    fresh = []
    for wid in change.new_write_ids:
        item: dict[str, object] = {"wid": wid, "turn": {}}
        if conversation is not None:
            item["turn"] = {
                "_message_envelope": {
                    "message_id": f"{mid}-{wid[:6]}",
                    "conversation_id": conversation,
                    "session_id": "session-1",
                }
            }
        fresh.append(item)
    return json.dumps({"adapter": "messages" if conversation else "document", "fresh": fresh})


async def _commit(
    ledger: IntakeLedger,
    change: IntakeChange,
    *,
    at: datetime = NOW,
    conversation: str | None = None,
) -> None:
    await intake_store.commit(
        ledger,
        change,
        namespace=NS,
        apply_payload=_payload(change, conversation),
        created_at=at.isoformat(),
        required_derivation=_manifest(change),
    )


async def _apply(ledger: IntakeLedger, change: IntakeChange) -> dict[str, str]:
    real = {wid: f"rec-{wid[:8]}" for wid in change.new_write_ids}
    await intake_store.applied_intent(
        ledger,
        intent_id=change.receipt.intent_id,
        skey=change.scope_key,
        source_id=change.source_id,
        real_ids=real,
        receipt=change.receipt,
        readiness=change.readiness,
        required_derivation=_manifest(change, records=tuple(real.values())),
    )
    return real


# ------------------------------------------------------------------------------- the ledger
async def test_intent_lifecycle_round_trips(ledger: IntakeLedger) -> None:
    change = _accept("src-1", ["one", "two"])
    intent = change.receipt.intent_id
    await _commit(ledger, change)

    overlay = await intake_store.load_overlay(
        ledger, change.scope_key, "src-1", list(change.new_write_ids)
    )
    assert overlay.get_source(change.scope_key, "src-1") is not None
    assert all(overlay.get_write(change.scope_key, w) for w in change.new_write_ids)
    assert overlay.get_receipt(intent) is not None
    assert [r["intent_id"] for r in await intake_store.pending_intents(ledger)] == [intent]
    assert await intake_store.applied_scope_keys(ledger) == []
    assert await intake_store.record_ids(ledger, change.scope_key, change.new_write_ids) == {}

    real = await _apply(ledger, change)
    row = (await intake_store.intents_by_id(ledger, [intent, "unknown"]))[intent]
    assert row["status"] == "applied" and row["apply"] is None
    assert await intake_store.pending_intents(ledger) == []
    assert await intake_store.applied_scope_keys(ledger) == [change.scope_key]
    assert await intake_store.record_ids(ledger, change.scope_key, change.new_write_ids) == real
    relinked = await intake_store.load_overlay(ledger, change.scope_key, "src-1", [])
    state = relinked.get_source(change.scope_key, "src-1")
    assert state is not None and {s.record_id for s in state.live} == set(real.values())

    ready = change.readiness.model_copy(update={"raw_search": "ready"})
    await intake_store.set_readiness(ledger, intent, ready)
    stored = (await intake_store.intents_by_id(ledger, [intent]))[intent]
    assert json.loads(stored["readiness"])["raw_search"] == "ready"


async def test_pending_intents_are_oldest_first(ledger: IntakeLedger) -> None:
    late, early = _accept("late", ["b"]), _accept("early", ["a"])
    await _commit(ledger, late, at=NOW + timedelta(seconds=5))
    await _commit(ledger, early, at=NOW)
    order = [r["source_id"] for r in await intake_store.pending_intents(ledger)]
    assert order == ["early", "late"]


async def test_message_identity_conflict_rolls_back_everything(ledger: IntakeLedger) -> None:
    first = _accept("src-1", ["one"])
    await intake_store.commit(
        ledger,
        first,
        namespace=NS,
        apply_payload=_payload(first, "conv-1", mid="same"),
        created_at=NOW.isoformat(),
        required_derivation=_manifest(first),
    )
    second = _accept("src-2", ["two"])
    # Same public message id (the helper derives it from the write id prefix): forge it.
    payload = json.loads(_payload(second, "conv-2", mid="same"))
    payload["fresh"][0]["turn"]["_message_envelope"]["message_id"] = json.loads(
        _payload(first, "conv-1", mid="same")
    )["fresh"][0]["turn"]["_message_envelope"]["message_id"]
    with pytest.raises(ValueError, match="identity conflict"):
        await intake_store.commit(
            ledger,
            second,
            namespace=NS,
            apply_payload=json.dumps(payload),
            created_at=NOW.isoformat(),
            required_derivation=_manifest(second),
        )
    assert list(await intake_store.intents_by_id(ledger, [second.receipt.intent_id])) == []
    assert await intake_store.load_overlay(ledger, second.scope_key, "src-2", []) is not None
    assert (await intake_store.load_overlay(ledger, second.scope_key, "src-2", [])).get_source(
        second.scope_key, "src-2"
    ) is None


async def test_conversation_is_bound_to_one_session(ledger: IntakeLedger) -> None:
    first = _accept("src-1", ["one"])
    await _commit(ledger, first, conversation="conv-1")
    second = _accept("src-2", ["two"])
    payload = json.loads(_payload(second, "conv-1", mid="other"))
    payload["fresh"][0]["turn"]["_message_envelope"]["session_id"] = "session-2"
    with pytest.raises(ValueError, match="already assigned to session_id"):
        await intake_store.commit(
            ledger,
            second,
            namespace=NS,
            apply_payload=json.dumps(payload),
            created_at=NOW.isoformat(),
            required_derivation=_manifest(second),
        )


async def test_message_lookup_tombstone_and_no_reuse(ledger: IntakeLedger) -> None:
    change = _accept("src-1", ["one"])
    await _commit(ledger, change, conversation="conv-1")
    real = await _apply(ledger, change)
    mid = json.loads(_payload(change, "conv-1"))["fresh"][0]["turn"]["_message_envelope"][
        "message_id"
    ]
    found = await ledger.lookup_messages(NS, [mid, "missing"])
    assert set(found) == {mid}
    assert found[mid]["record_id"] == next(iter(real.values()))
    assert found[mid]["intent_status"] == "applied"
    assert await ledger.lookup_messages("other/ns", [mid]) == {}
    conv = await ledger.lookup_conversations(NS, ["conv-1"])
    assert conv["conv-1"]["session_id"] == "session-1"

    assert await ledger.tombstone_records(list(real.values())) == 1
    erased = (await ledger.lookup_messages(NS, [mid]))[mid]
    assert erased["erased"] is True and erased["record_id"] is None
    again = _accept("src-again", ["again"])
    payload = json.loads(_payload(again, "conv-1"))
    payload["fresh"][0]["turn"]["_message_envelope"]["message_id"] = mid
    with pytest.raises(ValueError, match="erased and cannot be reused"):
        await intake_store.commit(
            ledger,
            again,
            namespace=NS,
            apply_payload=json.dumps(payload),
            created_at=NOW.isoformat(),
            required_derivation=_manifest(again),
        )


async def test_malformed_message_identity_commits_nothing(ledger: IntakeLedger) -> None:
    change = _accept("src-1", ["one"])
    payload = json.loads(_payload(change, "conv-1"))
    del payload["fresh"][0]["turn"]["_message_envelope"]["session_id"]
    with pytest.raises(ValueError, match="message identity requires"):
        await intake_store.commit(
            ledger,
            change,
            namespace=NS,
            apply_payload=json.dumps(payload),
            created_at=NOW.isoformat(),
            required_derivation=_manifest(change),
        )
    assert await intake_store.pending_intents(ledger) == []


# ------------------------------------------------------------------------- discovery and policy
async def test_discovery_is_idempotent_and_skips_accepted(ledger: IntakeLedger) -> None:
    applied, accepted = _accept("applied", ["a"]), _accept("accepted", ["b"])
    await _commit(ledger, applied)
    await _commit(ledger, accepted, at=NOW + timedelta(seconds=1))
    await _apply(ledger, applied)
    skey = applied.scope_key
    assert await discover_committed_derivations(ledger, scope_key=skey, discovered_at=NOW) == 1
    assert await discover_committed_derivations(ledger, scope_key=skey, discovered_at=NOW) == 0
    await _apply(ledger, accepted)
    assert await discover_committed_derivations(ledger, scope_key=skey, discovered_at=NOW) == 1


async def test_discovery_can_be_scoped_to_one_intent(ledger: IntakeLedger) -> None:
    one, two = _accept("one", ["a"]), _accept("two", ["b"])
    for change in (one, two):
        await _commit(ledger, change)
        await _apply(ledger, change)
    skey = one.scope_key
    scoped = await discover_committed_derivations(
        ledger, scope_key=skey, discovered_at=NOW, intent_ids=(two.receipt.intent_id,)
    )
    assert scoped == 1
    # the full reconciliation scan finds the one left behind, and only it
    assert await discover_committed_derivations(ledger, scope_key=skey, discovered_at=NOW) == 1


async def test_intent_without_records_is_discovered_with_no_work(ledger: IntakeLedger) -> None:
    change = _accept("empty", ["a"])
    await _commit(ledger, change)
    await intake_store.applied_intent(
        ledger,
        intent_id=change.receipt.intent_id,
        skey=change.scope_key,
        source_id=change.source_id,
        real_ids={},
        receipt=change.receipt,
        readiness=change.readiness,
        required_derivation=_manifest(change),
    )
    skey = change.scope_key
    assert await discover_committed_derivations(ledger, scope_key=skey, discovered_at=NOW) == 0
    assert await discover_committed_derivations(ledger, scope_key=skey, discovered_at=NOW) == 0


async def test_policy_activation_requeues_unfinished_once(ledger: IntakeLedger) -> None:
    change = _accept("src", ["a"])
    await _commit(ledger, change)
    await _apply(ledger, change)
    skey = change.scope_key
    await discover_committed_derivations(ledger, scope_key=skey, discovered_at=NOW)
    kwargs = {
        "scope_key": skey,
        "stage": "derivation",
        "registry_digest": "registry:two",
        "activated_at": NOW,
        "runner_quiesced": True,
    }
    with pytest.raises(ValueError, match="quiesced"):
        await activate_derivation_policy(
            ledger, **{**kwargs, "runner_quiesced": False}, policy_revision="policy:two"
        )
    assert await activate_derivation_policy(ledger, **kwargs, policy_revision="policy:two") == 1
    # the intent's stored manifest keeps its accepted revision: activating that one is a no-op
    assert await activate_derivation_policy(ledger, **kwargs, policy_revision="policy:one") == 0


# ------------------------------------------------------------------------------------ MongoDB
def test_sql_stores_offer_the_ledger_and_mongodb_does_not() -> None:
    from memspine.providers.registry import get_registry
    from memspine.services.storage.mongodb import MongoRecordsStore
    from memspine.services.storage.sql_base import SqlStorage

    registry = get_registry("storage_backends")
    for name in ("sqlite", "postgres"):
        assert INTAKE_LEDGER_CAPABILITY in registry.get(name).declared_capabilities
    assert INTAKE_LEDGER_CAPABILITY not in registry.get("mongodb").declared_capabilities
    assert callable(getattr(SqlStorage, "intake_ledger", None))
    assert not hasattr(MongoRecordsStore, "intake_ledger")
    assert intake_ledger_of(object()) is None
