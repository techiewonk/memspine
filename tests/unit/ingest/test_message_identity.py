from datetime import UTC, datetime, timedelta

import pytest

from memspine.ingest.message_identity import normalise_messages

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


def message(**fields):
    return {"role": "user", "content": "", **fields}


def test_assigns_ids_and_one_session_with_new_conversation_per_root() -> None:
    result = normalise_messages(
        [message(content="a"), message(content="b")], namespace="n", now=NOW
    )
    assert len({m["message_id"] for m in result}) == 2
    assert result[0]["session_id"] == result[1]["session_id"]
    assert result[0]["session_id"] != "old-session"
    assert result[0]["conversation_id"] != result[1]["conversation_id"]
    assert all(m["created_at"] == NOW and m["_created_at_generated"] for m in result)


def test_reply_inherits_earlier_message_identity_and_sender_is_projected() -> None:
    source = [
        {"message_id": "m1", "role": "user", "content": "hello"},
        message(
            message_id="m2",
            role="assistant",
            reply_to="m1",
            sender={"id": "bot", "kind": "agent", "name": "Helper"},
        ),
    ]
    result = normalise_messages(source, namespace="n", session_id="s", now=NOW)
    assert result[1]["session_id"] == result[0]["session_id"] == "s"
    assert result[1]["conversation_id"] == result[0]["conversation_id"]
    assert result[1]["speaker"] == "bot" and result[1]["name"] == "Helper"
    assert source[1].get("speaker") is None


def test_existing_reply_requires_explicit_same_session() -> None:
    old = [{"message_id": "old", "session_id": "s", "conversation_id": "c"}]
    with pytest.raises(ValueError, match="explicit session_id"):
        normalise_messages([message(reply_to="old")], namespace="n", existing=old, now=NOW)
    reply = normalise_messages(
        [message(reply_to="old")], namespace="n", session_id="s", existing=old, now=NOW
    )[0]
    assert (reply["session_id"], reply["conversation_id"]) == ("s", "c")
    fresh = normalise_messages([message()], namespace="n", existing=old, now=NOW)[0]
    assert fresh["session_id"] != "s"
    with pytest.raises(ValueError, match="differs from parent session"):
        normalise_messages(
            [message(reply_to="old")], namespace="n", session_id="other", existing=old, now=NOW
        )


def test_existing_conversation_ownership_rejects_cross_session_reuse() -> None:
    with pytest.raises(ValueError, match="another session"):
        normalise_messages(
            [message(conversation_id="c")],
            namespace="n",
            session_id="new-session",
            existing_conversations={"c": "old-session"},
            now=NOW,
        )


def test_stored_retry_parent_is_available_even_when_retried_later_in_batch() -> None:
    old = [{"message_id": "parent", "session_id": "s", "conversation_id": "c"}]
    result = normalise_messages(
        [
            message(message_id="child", reply_to="parent"),
            message(message_id="parent", conversation_id="c"),
        ],
        namespace="n",
        session_id="s",
        existing=old,
        retry_ids={"parent"},
        now=NOW,
    )
    assert result[0]["conversation_id"] == result[1]["conversation_id"] == "c"
    assert result[0]["session_id"] == result[1]["session_id"] == "s"


def test_existing_identity_collision_requires_explicit_retry_id() -> None:
    old = [{"message_id": "existing", "session_id": "s", "conversation_id": "c"}]
    with pytest.raises(ValueError, match="duplicate"):
        normalise_messages([message(message_id="existing")], namespace="n", existing=old, now=NOW)


@pytest.mark.parametrize(
    "messages,kwargs,match",
    [
        ([message(message_id="x"), message(message_id="x")], {}, "duplicate"),
        ([message(message_id="x", reply_to="x")], {}, "itself"),
        ([message(reply_to="missing")], {}, "unresolved"),
        ([message(reply_to="later"), message(message_id="later")], {}, "forward"),
        ([message(conversation_id="m")], {"conversation_id": "call"}, "conflict"),
        ([message(session_id="m")], {"session_id": "call"}, "conflict"),
    ],
)
def test_rejects_invalid_identity_graph(messages, kwargs, match) -> None:
    with pytest.raises(ValueError, match=match):
        normalise_messages(messages, namespace="n", now=NOW, **kwargs)


def test_supplied_ids_and_historical_timestamps_are_preserved() -> None:
    earlier = NOW - timedelta(days=2)
    result = normalise_messages(
        [message(message_id="given", conversation_id="thread", timestamp=earlier)],
        namespace="n",
        session_id="session",
        now=NOW,
    )[0]
    assert result["message_id"] == "given"
    assert result["conversation_id"] == "thread"
    assert result["created_at"] is earlier
    assert result["timestamp"] is earlier
    assert result["_created_at_generated"] is False


def test_rejects_conflicting_timestamps_and_unsupported_sender() -> None:
    with pytest.raises(ValueError, match="timestamp conflict"):
        normalise_messages(
            [message(created_at=NOW, timestamp=NOW + timedelta(seconds=1))],
            namespace="n",
            now=NOW,
        )
    with pytest.raises(ValueError, match="sender.kind"):
        normalise_messages([message(sender={"id": "x", "kind": "robot"})], namespace="n", now=NOW)
    named_sender = normalise_messages([message(sender={"name": "Helper"})], namespace="n", now=NOW)[
        0
    ]
    assert named_sender["speaker"] == named_sender["name"] == "Helper"


def test_requires_legacy_role_content_and_validates_updated_at() -> None:
    with pytest.raises(ValueError, match="string role"):
        normalise_messages([{"content": "x"}], namespace="n", now=NOW)
    with pytest.raises(ValueError, match="string content"):
        normalise_messages([{"role": "user", "content": None}], namespace="n", now=NOW)
    with pytest.raises(ValueError, match="updated_at"):
        normalise_messages([message(updated_at="yesterday")], namespace="n", now=NOW)
    with pytest.raises(ValueError, match="JSON-serializable"):
        normalise_messages([message(metadata={"bad": object()})], namespace="n", now=NOW)
