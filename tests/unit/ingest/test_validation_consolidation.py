from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from memspine.ingest.gateway import IntakeGateway
from memspine.ingest.intake import (
    IntakeBook,
    validate_source,
)
from memspine.ingest.message_identity import normalise_messages
from memspine.ingest.message_schema import ValidatedMessageBatch
from memspine.schemas.steps.shared import Scope, Segment, SourceEnvelope

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


def envelope(content: str = "hello", *, kind: str = "conversation") -> SourceEnvelope:
    role = "document" if kind == "document" else "user"
    return SourceEnvelope(
        source_id="source",
        kind=kind,
        scope=Scope(tenant="tenant", user="owner"),
        segments=[Segment(seg_index=0, role=role, content=content)],
    )


def outcome() -> SimpleNamespace:
    event = SimpleNamespace(kind="memory.write", payload={"seg_index": 0, "write_id": "w1"})
    return SimpleNamespace(events=[event], receipt=SimpleNamespace(event_ids=["evt-w1"]), plan=None)


def test_validated_envelope_is_an_isolated_normalized_snapshot() -> None:
    raw = envelope("hello\r\nworld\x00")
    validated = validate_source(raw)
    raw.segments[0].content = "caller changed input"
    exposed = validated.envelope
    exposed.segments[0].content = "caller changed returned copy"
    assert validated.envelope.segments[0].content == "hello\nworld"


def test_intake_book_validates_once_and_accept_validated_does_not(monkeypatch) -> None:
    import memspine.ingest.intake as intake

    calls = 0
    original = intake.validate_envelope

    def counted(env, limits=None):
        nonlocal calls
        calls += 1
        return original(env, limits)

    monkeypatch.setattr(intake, "validate_envelope", counted)
    book = IntakeBook()
    book.accept(envelope(), (), None, now=NOW)
    assert calls == 1
    validated = validate_source(envelope())
    assert calls == 2
    book.accept_validated(validated, (), None, now=NOW)
    assert calls == 2
    with pytest.raises(TypeError, match="ValidatedEnvelope"):
        book.accept_validated(envelope(), (), None, now=NOW)  # type: ignore[arg-type]


def test_validated_message_batch_copies_and_freezes_nested_fields() -> None:
    raw = normalise_messages(
        [{"role": "user", "content": "hello", "sender": {"id": "u"}}],
        namespace="ns",
        now=NOW,
    )
    batch = ValidatedMessageBatch.from_resolved(raw)
    raw[0]["sender"]["id"] = "changed"
    first = batch.to_messages()
    first[0]["sender"]["id"] = "changed again"
    assert batch.to_messages()[0]["sender"]["id"] == "u"


def test_message_payload_uses_validated_segment_text_role_and_speaker() -> None:
    turns = [
        {
            "message_id": "m1",
            "session_id": "s1",
            "conversation_id": "c1",
            "role": "assistant",
            "speaker": "raw-speaker",
            "content": "raw\r\ntext",
        }
    ]
    source = SourceEnvelope(
        source_id="source",
        kind="conversation",
        scope=Scope(tenant="tenant", user="owner"),
        segments=[
            Segment(
                seg_index=0,
                role="user",
                speaker="canonical-speaker",
                content="canonical\r\ntext",
            )
        ],
    )
    gateway = IntakeGateway(object())
    payload = gateway._payload(
        validate_source(source),
        "tenant/owner",
        "messages",
        {"turns": turns},
        outcome(),
        None,
        ["w1"],
        1,
    )
    result = payload["fresh"][0]["turn"]
    assert result["role"] == "user"
    assert result["speaker"] == "canonical-speaker"
    assert result["content"] == "canonical\ntext"


def test_document_payload_uses_validated_segment_text() -> None:
    gateway = IntakeGateway(object())
    payload = gateway._payload(
        validate_source(envelope("canonical\r\ntext", kind="document")),
        "tenant/owner",
        "document",
        {"chunks": ["raw\r\ntext"]},
        outcome(),
        None,
        ["w1"],
        1,
    )
    assert payload["fresh"][0]["text"] == "canonical\ntext"


def test_validated_message_batch_rejects_unresolved_identity_shape() -> None:
    with pytest.raises(ValueError, match="resolved identity"):
        ValidatedMessageBatch.from_resolved([{"role": "user", "content": "x"}])
