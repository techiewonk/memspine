"""Durable message-identity lookup tests."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from memspine import Engine
from memspine.ingest import gateway, intake_store
from memspine.ingest.intake import IntakeBook
from memspine.ingest.intake_store import (
    Overlay,
    conversation_identity,
    intake_intent,
    intake_source,
    intake_write,
    message_identity,
)
from memspine.ingest.message_store import (
    backfill_from_storage,
    lookup_conversations,
    lookup_messages,
)
from memspine.schemas.steps.shared import Scope, Segment, SourceEnvelope


@pytest.fixture
async def engine() -> AsyncIterator[Engine]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
    )
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


async def test_identity_projection_is_durable_without_raw_text(engine: Engine) -> None:
    record = (
        await engine.ingest_messages(
            [
                {
                    "message_id": "message-1",
                    "conversation_id": "conversation-1",
                    "role": "user",
                    "content": "private raw text",
                    "metadata": {"origin": "test"},
                }
            ],
            namespace="identity-test",
            session_id="session-1",
        )
    )[0]
    db = await gateway.gateway_of(engine)._engine()

    messages = await lookup_messages(db, "identity-test", ["missing", "message-1"])
    conversations = await lookup_conversations(db, "identity-test", ["conversation-1"])
    assert messages["message-1"]["record_id"] == record.record_id
    assert messages["message-1"]["intent_status"] == "applied"
    assert messages["message-1"]["session_id"] == "session-1"
    assert messages["message-1"]["metadata"] == {"origin": "test"}
    assert "content" not in messages["message-1"]
    assert "private raw text" not in str(messages)
    assert conversations["conversation-1"]["session_id"] == "session-1"
    assert await lookup_messages(db, "other-namespace", ["message-1"]) == {}

    async with db.begin() as conn:
        await conn.execute(
            update(intake_intent)
            .where(intake_intent.c.intent_id == messages["message-1"]["intent_id"])
            .values(status="accepted")
        )
        await conn.execute(
            update(message_identity)
            .where(message_identity.c.message_id == "message-1")
            .values(record_id=None)
        )
    pending = await lookup_messages(db, "identity-test", ["message-1"])
    assert pending["message-1"]["intent_status"] == "accepted"
    assert pending["message-1"]["record_id"] is None
    async with db.begin() as conn:
        await conn.execute(
            update(intake_intent)
            .where(intake_intent.c.intent_id == messages["message-1"]["intent_id"])
            .values(status="applied")
        )
        await conn.execute(
            update(message_identity)
            .where(message_identity.c.message_id == "message-1")
            .values(record_id=record.record_id)
        )

    async with db.begin() as conn:
        await conn.execute(
            delete(message_identity).where(message_identity.c.namespace == "identity-test")
        )
        await conn.execute(
            delete(conversation_identity).where(
                conversation_identity.c.namespace == "identity-test"
            )
        )
    assert await backfill_from_storage(engine._require_started()) == 1
    restored = await lookup_messages(db, "identity-test", ["message-1"])
    assert restored["message-1"]["record_id"] == record.record_id
    assert restored["message-1"]["intent_status"] is None

    await engine.forget(record.record_id, namespace="identity-test", hard=True)
    erased = await lookup_messages(db, "identity-test", ["message-1"])
    assert erased["message-1"]["erased"] is True
    assert erased["message-1"]["record_id"] is None
    assert set(erased["message-1"]) == {
        "erased",
        "message_id",
        "conversation_id",
        "session_id",
        "intent_id",
        "record_id",
        "intent_status",
    }
    with pytest.raises(ValueError, match="erased message.*cannot be reused"):
        await engine.ingest_messages(
            [
                {
                    "message_id": "message-1",
                    "conversation_id": "conversation-1",
                    "role": "user",
                    "content": "private raw text",
                    "metadata": {"origin": "test"},
                }
            ],
            namespace="identity-test",
            session_id="session-1",
        )


async def test_identity_conflict_rolls_back_the_entire_acceptance(tmp_path) -> None:
    db = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'identity-conflict.db'}")
    await intake_store.ensure_schema(db)

    async def acceptance(source_id: str, conversation_id: str, text: str):
        overlay = Overlay()
        env = SourceEnvelope(
            source_id=source_id,
            kind="conversation",
            scope=Scope(tenant="tenant", user="user", session="session-1"),
            segments=[Segment(seg_index=0, role="user", content=text)],
        )
        IntakeBook(overlay, ephemeral=True).accept(env, [], None, now=datetime.now(UTC))
        change = overlay.change
        assert change is not None
        payload = {
            "adapter": "messages",
            "fresh": [
                {
                    "wid": change.new_write_ids[0],
                    "turn": {
                        "_message_envelope": {
                            "message_id": "same-public-id",
                            "conversation_id": conversation_id,
                            "session_id": "session-1",
                            "input_fingerprints": {"content": "fingerprint-only"},
                        }
                    },
                }
            ],
        }
        return change, payload

    first, first_payload = await acceptance("source-1", "conversation-1", "one")
    await intake_store.commit(
        db,
        first,
        namespace="ns",
        apply_payload=json.dumps(first_payload),
        created_at=datetime.now(UTC).isoformat(),
    )
    second, second_payload = await acceptance("source-2", "conversation-2", "two")
    with pytest.raises(ValueError, match="message_id identity conflict"):
        await intake_store.commit(
            db,
            second,
            namespace="ns",
            apply_payload=json.dumps(second_payload),
            created_at=datetime.now(UTC).isoformat(),
        )

    async with db.connect() as conn:
        intents = (await conn.execute(select(intake_intent.c.intent_id))).scalars().all()
        sources = (await conn.execute(select(intake_source.c.source_id))).scalars().all()
        writes = (await conn.execute(select(intake_write.c.write_id))).scalars().all()
    assert intents == [first.receipt.intent_id]
    assert sources == ["source-1"]
    assert writes == list(first.new_write_ids)
    await db.dispose()
