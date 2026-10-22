"""Semantic GLiNER output remains source-bound annotation data, never fact keys."""

from __future__ import annotations

from typing import Any

from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.core.policies.conflict import ConflictPolicy
from memspine.core.policies.dedup import DedupPolicy
from memspine.derive.gliner_mentions import bind_gliner_mentions, parse_gliner_mentions
from memspine.memories.semantic.store import SemanticMemory
from memspine.services.embedding.hash import HashEmbedding
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.store.events import EventKind, MemoryEvent, fingerprint_payload
from memspine.store.records import EntityMention, MemoryRecord, RecordFingerprint, SourceInfo


class FakeMentionExtractor:
    async def extract_mentions(self, content: str):
        labels = ("person", "organization")
        raw: dict[str, list[dict[str, Any]]] = {label: [] for label in labels}
        for label, quote in (("person", "Mira"), ("organization", "Acme")):
            start = content.find(quote)
            if start >= 0:
                raw[label].append(
                    {
                        "text": quote,
                        "confidence": 0.8,
                        "start": start,
                        "end": start + len(quote),
                    }
                )
        return parse_gliner_mentions(content, {"entities": raw}, labels)


class EmptyMentionExtractor:
    async def extract_mentions(self, content: str):
        return ()


async def make_semantic(*, cosine_threshold: float = 0.9) -> SemanticMemory:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    projector = RecordProjector(storage)

    async def append_and_project(event: MemoryEvent) -> None:
        appended = await storage.append_event(event)
        assert appended.seq is not None
        await projector.apply(appended)
        await storage.set_offset(projector.name, appended.seq)

    return SemanticMemory(
        storage=storage,
        embedder=HashEmbedding(dim=64),
        append_event=append_and_project,
        conflict=ConflictPolicy.bind(),
        dedup=DedupPolicy.bind({"lsh_threshold": 0.5, "cosine_threshold": cosine_threshold}),
        extractor=FakeMentionExtractor(),
    )


def source_record(
    content: str, *, message_id: str, namespace: str = "tenant/alice"
) -> MemoryRecord:
    return MemoryRecord(
        namespace=namespace,
        memory_type="semantic",
        content=content,
        source=SourceInfo(
            role="user",
            channel="message_ingest",
            message={
                "message_id": message_id,
                "role": "user",
                "sender": {"id": "alice-17", "kind": "person"},
                "created_at": "2026-08-05T09:30:00+00:00",
            },
            parents=["source-parent-1"],
        ),
    )


async def test_gliner_mentions_persist_in_fingerprint_without_entering_fact_ladder() -> None:
    semantic = await make_semantic()
    record = source_record("Mira joined Acme.", message_id="msg-1")

    result = await semantic.write(record)

    assert result.action == "added"
    assert result.record.entity is None and result.record.attribute is None
    (person, org) = result.record.fingerprint.entity_mentions
    assert [(m.text, m.label, m.start, m.end, m.confidence) for m in (person, org)] == [
        ("Mira", "person", 0, 4, 0.8),
        ("Acme", "organization", 12, 16, 0.8),
    ]
    assert all(m.asserted is False for m in (person, org))
    assert all(m.source_record_id == record.record_id for m in (person, org))
    assert all(m.source_namespace == "tenant/alice" for m in (person, org))
    assert all(m.source_content_fingerprint == record.content_fingerprint for m in (person, org))
    assert all(m.source_message_id == "msg-1" and m.sender_id == "alice-17" for m in (person, org))
    assert all(
        m.sender_role == "user" and m.created_at == "2026-08-05T09:30:00+00:00"
        for m in (person, org)
    )
    assert all(m.parents == ("source-parent-1",) for m in (person, org))

    events = await semantic._storage.read_events()
    writes = [event for event in events if event.kind is EventKind.WRITE]
    assert len(writes) == 1
    restored = MemoryRecord.model_validate(writes[0].payload["record"])
    assert restored.fingerprint.entity_mentions == (person, org)
    assert restored.entity is None and restored.attribute is None
    assert EventKind.CONFLICT not in {event.kind for event in events}


async def test_mentions_from_distinct_records_do_not_create_same_key_conflicts() -> None:
    semantic = await make_semantic()

    one = await semantic.write(source_record("Mira joined Acme.", message_id="msg-1"))
    two = await semantic.write(
        source_record("A parcel arrived at Acme for Mira.", message_id="msg-2")
    )

    assert one.action == two.action == "added"
    assert one.record.entity is two.record.entity is None
    assert one.record.attribute is two.record.attribute is None
    assert one.record.fingerprint.entity_mentions[0].source_message_id == "msg-1"
    assert two.record.fingerprint.entity_mentions[0].source_message_id == "msg-2"
    events = await semantic._storage.read_events()
    assert EventKind.CONFLICT not in {event.kind for event in events}


async def test_dedup_merge_keeps_spans_bound_to_each_original_snapshot() -> None:
    semantic = await make_semantic(cosine_threshold=0.0)
    kept_input = source_record("Mira joined Acme today.", message_id="msg-kept")
    incoming_input = source_record("Yesterday Mira joined Acme today.", message_id="msg-drop")

    kept = await semantic.write(kept_input)
    merged = await semantic.write(incoming_input)

    assert merged.action == "merged"
    assert merged.record.record_id == kept.record.record_id
    kept_mentions = merged.record.fingerprint.entity_mentions
    assert all(m.source_record_id == kept_input.record_id for m in kept_mentions)
    assert all(kept.record.content[m.start : m.end] == m.text for m in kept_mentions)

    events = await semantic._storage.read_events()
    (merge_event,) = [event for event in events if event.kind is EventKind.MERGE]
    dropped = MemoryRecord.model_validate(merge_event.payload["dropped_record"])
    assert dropped.record_id == incoming_input.record_id
    assert dropped.content == incoming_input.content
    assert dropped.fingerprint.entity_mentions
    for mention in dropped.fingerprint.entity_mentions:
        assert mention.source_record_id == incoming_input.record_id
        assert mention.source_message_id == "msg-drop"
        assert dropped.content[mention.start : mention.end] == mention.text
        assert mention.source_content_fingerprint == dropped.content_fingerprint

    # The kept record does not receive the dropped record's offset map.
    assert all(m.source_record_id != dropped.record_id for m in kept_mentions)


def test_bound_annotation_type_round_trips_as_typed_unasserted_data() -> None:
    mention = EntityMention(
        text="Mira",
        label="person",
        start=0,
        end=4,
        confidence=0.7,
        asserted=False,
        source_namespace="tenant/alice",
        source_record_id="record-1",
        source_version=1,
        source_content_fingerprint="a" * 16,
        source_binding_hash="b" * 16,
        source_role="user",
        source_channel="message_ingest",
    )

    restored = EntityMention.model_validate_json(mention.model_dump_json())

    assert restored == mention and restored.asserted is False


def test_empty_mention_field_is_omitted_for_existing_record_payloads() -> None:
    assert "entity_mentions" not in RecordFingerprint().model_dump(mode="json")


def test_binding_hashes_current_text_even_when_record_fingerprint_is_stale() -> None:
    original = source_record("Mira joined Acme.", message_id="msg-stale")
    changed_text = "Yesterday Mira joined Acme."
    stale = original.model_copy(update={"content": changed_text})
    start = changed_text.index("Mira")
    parsed = parse_gliner_mentions(
        changed_text,
        {"person": [{"text": "Mira", "start": start, "end": start + 4}]},
        ["person"],
    )

    (mention,) = bind_gliner_mentions(stale, parsed)

    expected_hash = fingerprint_payload({"content": changed_text})
    assert stale.content_fingerprint == original.content_fingerprint
    assert mention.source_content_fingerprint == expected_hash
    assert mention.source_content_fingerprint != original.content_fingerprint


async def test_empty_extraction_clears_preexisting_annotations_for_current_source() -> None:
    semantic = await make_semantic()
    record = source_record("Mira joined Acme.", message_id="msg-stale")
    old_mention = bind_gliner_mentions(
        record,
        parse_gliner_mentions(
            record.content,
            {"person": [{"text": "Mira", "start": 0, "end": 4}]},
            ["person"],
        ),
    )[0]
    record = record.model_copy(
        update={
            "content": "No named entities here.",
            "fingerprint": record.fingerprint.model_copy(
                update={"entity_mentions": (old_mention,)}
            ),
        }
    )
    semantic._extractor = EmptyMentionExtractor()

    result = await semantic.write(record)

    assert result.record.fingerprint.entity_mentions == ()


def test_missing_or_empty_sender_id_does_not_fall_back_to_principal() -> None:
    record = source_record("Mira arrived.", message_id="msg-no-sender")
    record = record.model_copy(
        update={
            "source": record.source.model_copy(
                update={
                    "principal": "authenticated-principal",
                    "message": {"message_id": "msg-no-sender", "sender": {"id": " "}},
                }
            )
        }
    )
    spans = parse_gliner_mentions(
        record.content,
        {"person": [{"text": "Mira", "start": 0, "end": 4}]},
        ["person"],
    )

    (mention,) = bind_gliner_mentions(record, spans)

    assert mention.sender_id is None
