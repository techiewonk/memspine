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


def test_existing_reply_inherits_parent_or_starts_cross_session_conversation() -> None:
    old = [{"message_id": "old", "session_id": "s", "conversation_id": "c"}]
    inherited = normalise_messages([message(reply_to="old")], namespace="n", existing=old, now=NOW)[
        0
    ]
    assert (inherited["session_id"], inherited["conversation_id"]) == ("s", "c")
    reply = normalise_messages(
        [message(reply_to="old")], namespace="n", session_id="s", existing=old, now=NOW
    )[0]
    assert (reply["session_id"], reply["conversation_id"]) == ("s", "c")
    fresh = normalise_messages([message()], namespace="n", existing=old, now=NOW)[0]
    assert fresh["session_id"] != "s"
    crossed = normalise_messages(
        [message(reply_to="old")], namespace="n", session_id="other", existing=old, now=NOW
    )[0]
    assert crossed["session_id"] == "other"
    assert crossed["conversation_id"] != "c"
    assert crossed["reply_to"] == "old"
    with pytest.raises(ValueError, match="new conversation_id"):
        normalise_messages(
            [message(reply_to="old", conversation_id="c")],
            namespace="n",
            session_id="other",
            existing=old,
            now=NOW,
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
        [message(message_id="given", conversation_id="thread", created_at=earlier)],
        namespace="n",
        session_id="session",
        now=NOW,
    )[0]
    assert result["message_id"] == "given"
    assert result["conversation_id"] == "thread"
    assert result["created_at"] is earlier
    assert result["timestamp"] is earlier
    assert result["_created_at_generated"] is False


def test_rejects_alternate_spellings_and_unsupported_sender() -> None:
    with pytest.raises(ValueError, match="'timestamp' is not accepted: use 'created_at'"):
        normalise_messages([message(timestamp=NOW)], namespace="n", now=NOW)
    with pytest.raises(ValueError, match="'speaker' is not accepted: use 'sender.id or"):
        normalise_messages([message(speaker="Ana")], namespace="n", now=NOW)
    with pytest.raises(ValueError, match="'name' is not accepted: use 'sender.name'"):
        normalise_messages([message(name="Ana")], namespace="n", now=NOW)
    with pytest.raises(ValueError, match="'timezone' is not accepted: use 'tz'"):
        normalise_messages([message(timezone="UTC")], namespace="n", now=NOW)
    with pytest.raises(ValueError, match="'turn_id' is not accepted: use 'message_id'"):
        normalise_messages([message(turn_id="D1:1")], namespace="n", now=NOW)
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


def test_unknown_fields_are_refused_by_name_and_metadata_is_verbatim() -> None:
    """ADR-090 (DS-09): a strict envelope; caller extras go only in ``metadata``."""
    with pytest.raises(ValueError, match="unknown field 'mood'; put caller data in 'metadata'"):
        normalise_messages([message(mood="happy")], namespace="n", now=NOW)
    with pytest.raises(ValueError, match="unknown field '_private'"):
        normalise_messages([message(_private=1)], namespace="n", now=NOW)
    with pytest.raises(ValueError, match="sender has unknown field 'email'"):
        normalise_messages([message(sender={"id": "u", "email": "x"})], namespace="n", now=NOW)
    with pytest.raises(ValueError, match="'addressee' must be a non-empty string"):
        normalise_messages([message(addressee=3)], namespace="n", now=NOW)
    with pytest.raises(ValueError, match="visibility must be one of"):
        normalise_messages([message(visibility="public")], namespace="n", now=NOW)
    bag = {"session_id": "not-an-identity", "created_at": "never read", "role": "x"}
    result = normalise_messages([message(metadata=bag)], namespace="n", session_id="s", now=NOW)[0]
    # the bag is stored as given and never interpreted for identity, time or role
    assert result["metadata"] == bag
    assert result["session_id"] == "s" and result["role"] == "user"
    assert result["created_at"] is NOW


@pytest.mark.parametrize(
    "value,text_precision",
    [
        ("2026-10-01", "day"),
        ("2026-10-01T08:30", "minute"),
        ("2026-10-01T08:30:15", "instant"),
        (datetime(2026, 10, 1, 8, 30), "instant"),
    ],
)
def test_naive_and_date_only_times_stay_civil_text(value, text_precision) -> None:
    """ADR-090 (DS-08): no resolved instant and never a guessed zone."""
    result = normalise_messages([message(created_at=value)], namespace="n", now=NOW)[0]
    assert result["created_at"] == value
    assert result["timestamp"] is None
    clock = result["_source_clock"]
    assert clock["precision"] == "unknown"
    assert clock["text_precision"] == text_precision
    assert clock["timezone"] is None and clock["timezone_origin"] == "unknown"


def test_aware_time_resolves_and_a_declared_zone_resolves_civil_text() -> None:
    aware = normalise_messages(
        [message(created_at="2026-10-01T08:30:00+02:00")], namespace="n", now=NOW
    )[0]
    assert aware["timestamp"] == datetime(2026, 10, 1, 6, 30, tzinfo=UTC)
    assert aware["_source_clock"]["timezone_origin"] == "source"
    declared = normalise_messages(
        [message(created_at="2026-10-01T08:30:00", tz="Asia/Tokyo")], namespace="n", now=NOW
    )[0]
    assert declared["timestamp"] == datetime(2026, 9, 30, 23, 30, tzinfo=UTC)
    assert declared["_source_clock"]["timezone"] == "Asia/Tokyo"
    assert declared["_source_clock"]["precision"] == "instant"
    with pytest.raises(ValueError, match="not a known IANA zone"):
        normalise_messages(
            [message(created_at="2026-10-01T08:30:00", tz="Nowhere/Land")],
            namespace="n",
            now=NOW,
        )
