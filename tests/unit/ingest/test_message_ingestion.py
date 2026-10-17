"""Public canonical message ingestion contracts, using hash embeddings only."""

from collections.abc import AsyncIterator

import pytest

from memspine import Engine
from memspine.ingest import gateway


@pytest.fixture
async def engine() -> AsyncIterator[Engine]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={'provider': "sqlite", 'settings': {'path': ":memory:", 'busy_timeout_s': 5.0}},
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
    )
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


async def test_canonical_path_persists_sender_identity_and_history(engine: Engine) -> None:
    rows = await engine.ingest_messages(
        [
            {
                "message_id": "m1",
                "role": "user",
                "content": "Noor likes quiet trails.",
                "sender": {"id": "user-123", "kind": "human", "name": "Noor"},
                "created_at": "2024-02-03T09:00:00+05:30",
            },
            {
                "message_id": "m2",
                "role": "assistant",
                "content": "Which trails?",
                "reply_to": "m1",
                "sender": {"id": "travel-agent", "kind": "agent", "name": "Travel Agent"},
            },
            {"message_id": "m3", "role": "user", "content": "A separate topic."},
        ],
        namespace="history",
        session_id="S1",
    )
    first, reply, root = [r.source.message for r in rows]
    assert first["session_id"] == reply["session_id"] == root["session_id"] == "S1"
    assert first["conversation_id"] == reply["conversation_id"] != root["conversation_id"]
    assert first["created_at"] == "2024-02-03T09:00:00+05:30"
    assert first["created_at_generated"] is False and reply["created_at_generated"] is True
    assert first["sender"]["id"] == "user-123"
    assert "reply_to:" + rows[0].record_id in rows[1].tags
    assert rows[0].axes.speaker == "user-123"
    hits = await engine.search("quiet trails", namespace="history", tags=["sender_id:user-123"])
    assert hits and all("sender_id:user-123" in r.tags for r, _ in hits)
    await engine.rebuild()
    loaded = await engine._require_started().get_record(rows[0].record_id)
    assert loaded.source.message == first


async def test_missing_ids_are_returned_and_cross_call_reply_uses_source_id(engine: Engine) -> None:
    first = (
        await engine.ingest_messages([{"role": "user", "content": "A new root."}], namespace="ids")
    )[0]
    meta = first.source.message
    assert meta["message_id"] and meta["session_id"] and meta["conversation_id"]
    child = (
        await engine.ingest_messages(
            [
                {"role": "assistant", "content": "A reply.", "reply_to": meta["message_id"]},
            ],
            namespace="ids",
            session_id=meta["session_id"],
        )
    )[0]
    assert child.source.message["conversation_id"] == meta["conversation_id"]
    assert f"reply_to:{first.record_id}" in child.tags


@pytest.mark.parametrize(
    "bad",
    [
        {"role": "user", "content": "bad", "reply_to": "missing"},
        {"role": "user", "content": "bad", "created_at": "not-a-date"},
        {"role": "user", "content": "bad", "sender": {"kind": "invented"}},
    ],
)
async def test_invalid_batch_is_rejected_before_raw_deposit(engine: Engine, bad: dict) -> None:
    with pytest.raises(ValueError):
        await engine.ingest_messages(
            [
                {"role": "user", "content": "valid first message"},
                bad,
            ],
            namespace="invalid",
        )
    assert await engine.list_records(namespace="invalid") == []


async def test_duplicate_source_id_and_session_conflicts_are_rejected(engine: Engine) -> None:
    root = (
        await engine.ingest_messages(
            [
                {"message_id": "root", "role": "user", "content": "root"},
            ],
            namespace="conflict",
            session_id="S1",
        )
    )[0]
    before = len(await engine._require_started().read_events())
    again = await engine.ingest_messages(
        [
            {"message_id": "root", "role": "user", "content": "root"},
        ],
        namespace="conflict",
        session_id="S1",
    )
    assert again[0].record_id == root.record_id
    assert len(await engine._require_started().read_events()) == before
    with pytest.raises(ValueError, match="revision"):
        await engine.ingest_messages(
            [
                {"message_id": "root", "role": "user", "content": "changed"},
            ],
            namespace="conflict",
            session_id="S1",
        )
    with pytest.raises(ValueError, match="session"):
        await engine.ingest_messages(
            [
                {"role": "assistant", "content": "reply", "reply_to": "root"},
            ],
            namespace="conflict",
        )
    with pytest.raises(ValueError, match="another session"):
        await engine.ingest_messages(
            [
                {
                    "role": "user",
                    "content": "root",
                    "conversation_id": root.source.message["conversation_id"],
                },
            ],
            namespace="conflict",
            session_id="S2",
        )


async def test_stored_parent_can_be_retried_after_its_new_child(engine: Engine) -> None:
    parent = (
        await engine.ingest_messages(
            [{"message_id": "stored-parent", "role": "user", "content": "parent"}],
            namespace="retry-parent",
            session_id="S1",
        )
    )[0]
    rerun = await engine.ingest_messages(
        [
            {
                "message_id": "new-child",
                "role": "assistant",
                "content": "child",
                "reply_to": "stored-parent",
            },
            {"message_id": "stored-parent", "role": "user", "content": "parent"},
        ],
        namespace="retry-parent",
        session_id="S1",
    )
    child = next(r for r in rerun if r.source.message["message_id"] == "new-child")
    assert sum(r.source.message["message_id"] == "stored-parent" for r in rerun) == 1
    assert f"reply_to:{parent.record_id}" in child.tags


async def test_precommit_identity_conflict_does_not_trigger_unrelated_recovery(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw = gateway.gateway_of(engine)
    submit_calls = 0
    recover_calls = 0

    async def conflicting_then_empty(*args, **kwargs):
        nonlocal submit_calls
        submit_calls += 1
        if submit_calls == 1:
            raise ValueError("message or conversation identity conflict in namespace")
        return []

    async def track_recover(*args, **kwargs):
        nonlocal recover_calls
        recover_calls += 1
        return None

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(gateway, "ingest_resolved_messages", conflicting_then_empty)
    monkeypatch.setattr(gateway, "recover", track_recover)
    monkeypatch.setattr("memspine.ingest.message_ingestion.asyncio.sleep", no_sleep)
    assert (
        await engine.ingest_messages(
            [{"message_id": "conflict-race", "role": "user", "content": "text"}],
            namespace="conflict-race",
        )
        == []
    )
    assert submit_calls == 2
    assert recover_calls == 0
    assert gw._message_recovery_needed is False


def test_no_old_api_aliases() -> None:
    assert hasattr(Engine, "ingest_messages") and hasattr(Engine, "ingest_data")
    assert not hasattr(Engine, "write_messages") and not hasattr(Engine, "ingest")


async def test_retry_recovers_accepted_intent_without_duplicate(
    engine: Engine, monkeypatch
) -> None:
    from memspine.ingest.gateway import gateway_of

    gw = gateway_of(engine)
    original = gw._apply

    async def fail_after_acceptance(*args, **kwargs):
        raise RuntimeError("simulated apply interruption")

    monkeypatch.setattr(gw, "_apply", fail_after_acceptance)
    message = {"message_id": "recover-me", "role": "user", "content": "A durable source."}
    with pytest.raises(RuntimeError, match="simulated"):
        await engine.ingest_messages([message], namespace="recovery", session_id="s")
    monkeypatch.setattr(gw, "_apply", original)
    recovered = await engine.ingest_messages([message], namespace="recovery", session_id="s")
    assert len(recovered) == 1
    assert len(await engine.list_records(namespace="recovery")) == 1
    again = await engine.ingest_messages([message], namespace="recovery", session_id="s")
    assert again[0].record_id == recovered[0].record_id
