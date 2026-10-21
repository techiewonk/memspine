"""Fast checks for the offline real-GLiNER acceptance gate's evidence validators."""

from __future__ import annotations

import importlib
import json
import sys
from types import SimpleNamespace

from scripts import check_gliner_ingestion as gate

from memspine.schemas.durable.evidence import Evidence, Span, TurnRef
from memspine.store.events import fingerprint_payload


def _source_record() -> SimpleNamespace:
    message = {
        "message_id": "message-1",
        "session_id": "session-1",
        "conversation_id": "conversation-1",
        "role": "user",
        "sender": {"id": "mira-user", "kind": "human", "name": "Mira"},
        "created_at": "2024-03-09T14:45:00+05:30",
    }
    source = SimpleNamespace(
        message=message,
        role="user",
        channel="messages",
        parents=[],
    )
    return SimpleNamespace(
        record_id="record-1",
        namespace=gate.NAMESPACE,
        content="I work for Acme in Utrecht.",
        content_fingerprint="content-fingerprint",
        version=1,
        source=source,
    )


def _mention(source: SimpleNamespace) -> dict[str, object]:
    text = "Acme"
    start = source.content.index(text)
    message = source.source.message
    content_fingerprint = fingerprint_payload({"content": source.content})
    source_identity = {
        "namespace": source.namespace,
        "record_id": source.record_id,
        "version": source.version,
        "content_fingerprint": content_fingerprint,
        "source_role": source.source.role,
        "source_channel": source.source.channel,
        "source_message_id": message["message_id"],
        "sender_id": message["sender"]["id"],
        "sender_role": message["role"],
        "created_at": message["created_at"],
        "parents": list(source.source.parents),
    }
    return {
        "text": text,
        "label": "organization",
        "start": start,
        "end": start + len(text),
        "confidence": 0.97,
        "asserted": False,
        "source_namespace": gate.NAMESPACE,
        "source_record_id": source.record_id,
        "source_version": source.version,
        "source_content_fingerprint": content_fingerprint,
        "source_binding_hash": fingerprint_payload(source_identity),
        "source_role": source.source.role,
        "source_channel": source.source.channel,
        "source_message_id": message["message_id"],
        "sender_id": message["sender"]["id"],
        "sender_role": message["role"],
        "created_at": message["created_at"],
        "parents": [],
    }


def test_gate_import_does_not_mutate_process_path() -> None:
    before = list(sys.path)
    importlib.reload(gate)
    assert sys.path == before


def test_window_fixture_is_source_quoted_and_fails_closed_for_other_content() -> None:
    prompt = [
        {"role": "system", "content": "You turn numbered conversation lines into memory items"},
        {
            "role": "user",
            "content": "Lines:\n[1] [2024-03-09] Mira: Mira works for Acme in Utrecht.",
        },
    ]
    reply = gate._deterministic_window_reply(prompt)
    assert reply is not None
    fact = json.loads(reply)["facts"][0]
    assert fact["lines"] == [1]
    assert fact["span"] == "Mira works for Acme in Utrecht."
    assert fact["text"] == "Mira works for Acme."
    assert (
        gate._deterministic_window_reply(
            [prompt[0], {"role": "user", "content": "Unrelated synthetic prompt"}]
        )
        is None
    )


def test_mention_validator_requires_exact_span_and_source_attribution() -> None:
    source = _source_record()
    mentions = [_mention(source)]
    assert gate._verify_mentions(mentions, {source.record_id: source}) == []

    mentions[0]["text"] = "Acme Labs"
    errors = gate._verify_mentions(mentions, {source.record_id: source})
    assert any("span does not exactly match" in error for error in errors)


def test_grounding_validator_checks_turn_id_version_and_exact_support_quote() -> None:
    source = _source_record()
    start = source.content.index("Acme")
    evidence = Evidence(
        role="support",
        turn=TurnRef(source_id=source.record_id, source_version=source.version, seg_index=0),
        span=Span(start=start, end=start + 4, quote="Acme"),
        source_family="mira-user",
    )
    envelope = SimpleNamespace(evidence=[evidence], grounding="grounded")
    summary = gate._grounding_summary(envelope, {source.record_id: source})
    assert summary["status"] == "ok"
    assert summary["all_quotes_match_source_content"] is True

    changed = Evidence(
        role="support",
        turn=TurnRef(source_id=source.record_id, source_version=2, seg_index=0),
        span=Span(start=start, end=start + 4, quote="Acme"),
        source_family="mira-user",
    )
    assert (
        gate._grounding_summary(
            SimpleNamespace(evidence=[changed], grounding="grounded"),
            {source.record_id: source},
        )["status"]
        == "failed"
    )
