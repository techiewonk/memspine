"""M7 erasure walker: every carrier shape scrubs, and the proof mirrors it."""

from __future__ import annotations

from memspine.memories.associative.entity_index import LINKS_KEY as INDEX_LINKS_KEY
from memspine.trust.erasure import LINKS_KEY, redact_record, retained_fields


def test_entity_links_beside_the_snapshot_are_scrubbed_and_detected() -> None:
    """ADR-095: a WRITE's entity links carry name fingerprints (reversible by dictionary)."""
    assert LINKS_KEY == INDEX_LINKS_KEY == "entity_links"
    link = {"name_fp": "77858394ab35e922", "entity_id": "ent:ns:x", "type": "person"}
    payload = {"record": _snapshot("r1", "Alice"), "entity_links": [link]}
    other = {"record": _snapshot("r2", "Bob"), "entity_links": [dict(link)]}
    assert "entity_links" in retained_fields(payload, "r1")
    assert redact_record(payload, "r1")
    assert payload["entity_links"] == []
    assert not retained_fields(payload, "r1")
    assert not redact_record(other, "r1")  # another record's links stay
    assert other["entity_links"] == [link]


def test_message_envelope_metadata_sender_name_and_input_hashes_are_scrubbed() -> None:
    """ADR-095 (message metadata copy class)."""
    message = {
        "message_id": "m1",
        "sender": {"id": "u1", "kind": "human", "name": "Xanthippe"},
        "metadata": {"crm": "TCK-1"},
        "input_fingerprints": {"content": "ab" * 32},
        "source_provenance": {"origin": "raw_message", "source_id": "m1"},
    }
    record = {**_snapshot("r1", "secret"), "source": {"role": "user", "message": message}}
    payload = {"record": record}
    assert {"metadata", "input_fingerprints", "name"} <= retained_fields(payload, "r1")
    assert redact_record(payload, "r1")
    assert message["metadata"] is None and message["input_fingerprints"] == {}
    assert message["sender"] == {"id": "u1", "kind": "human", "name": None}
    assert message["message_id"] == "m1"  # opaque ids stay
    assert not retained_fields(payload, "r1")
    # an absorbed duplicate's envelope goes with the survivor
    dropped = {**_snapshot("r2", "x"), "source": {"message": {"metadata": {"k": "v"}}}}
    merged = {"kept_record_id": "r1", "dropped_record": dropped}
    assert redact_record(merged, "r1")
    assert dropped["source"]["message"]["metadata"] is None


def _snapshot(record_id: str, content: str, history: list[dict] | None = None) -> dict:
    node: dict = {"record_id": record_id, "namespace": "ns", "content": content}
    if history is not None:
        node["history"] = history
    return node


def test_top_level_snapshot_is_scrubbed() -> None:
    payload = {"record": _snapshot("r1", "secret")}
    assert retained_fields(payload, "r1")
    assert redact_record(payload, "r1")
    assert payload["record"]["content"] == ""
    assert not retained_fields(payload, "r1")


def test_history_entries_are_scrubbed_and_detected() -> None:
    """The CRITICAL blind spot: supersession (e.g. a persona update) parks the
    prior version's content in ``history`` with no record_id of its own."""
    payload = {
        "record": _snapshot(
            "r1", "v2 text", history=[{"version": 1, "content": "V1 SECRET", "reason": "update"}]
        )
    }
    assert redact_record(payload, "r1")
    assert payload["record"]["content"] == ""
    assert payload["record"]["history"] == []  # #2: archived versions go whole
    assert not retained_fields(payload, "r1")


def test_verify_predicate_detects_surviving_history_content() -> None:
    # If a future redactor edit skips history, the proof must still catch it.
    payload = {
        "record": {
            "record_id": "r1",
            "namespace": "ns",
            "content": "",  # top level already scrubbed
            "history": [{"version": 1, "content": "V1 SECRET"}],
        }
    }
    assert retained_fields(payload, "r1")


def test_merge_absorbed_duplicate_is_scrubbed_including_history() -> None:
    payload = {
        "kept_record_id": "r1",
        "dropped_record": _snapshot(
            "other-id", "same secret content", history=[{"version": 1, "content": "older secret"}]
        ),
    }
    assert retained_fields(payload, "r1")
    assert redact_record(payload, "r1")
    assert payload["dropped_record"]["content"] == ""
    assert payload["dropped_record"]["history"] == []
    assert not retained_fields(payload, "r1")


def test_cold_tier_delta_carrier_is_scrubbed() -> None:
    payload = {
        "record_id": "r1",
        "set": {"content": "", "content_zstd": "eJwrzs9NVUgqSk3MBgAX+QP7"},
    }
    assert retained_fields(payload, "r1")
    assert redact_record(payload, "r1")
    assert payload["set"]["content_zstd"] is None
    assert not retained_fields(payload, "r1")


def test_other_records_are_untouched() -> None:
    payload = {"record": _snapshot("other", "not yours")}
    assert not retained_fields(payload, "r1")
    assert not redact_record(payload, "r1")
    assert payload["record"]["content"] == "not yours"


def test_every_identifying_field_is_scrubbed_and_reported() -> None:
    """#2: the fact key, tags, fingerprint and dedup sketches identify the
    subject as surely as the content does."""
    payload = {
        "record": {
            "record_id": "r1",
            "namespace": "ns",
            "content": "Alice lives in Paris",
            "content_fingerprint": "abc123",
            "entity": "Alice",
            "attribute": "home_city",
            "tags": ["person:alice"],
            "history": [{"version": 1, "content": "Alice lived in Rome"}],
            "simhash": 12345,
            "minhash_sig": "AAAA",
            "trust": 0.9,
        }
    }
    assert retained_fields(payload, "r1") == {
        "content",
        "content_fingerprint",
        "entity",
        "attribute",
        "tags",
        "history",
        "simhash",
        "minhash_sig",
    }
    assert redact_record(payload, "r1")
    record = payload["record"]
    assert record["content"] == ""
    assert record["content_fingerprint"] == ""
    assert record["entity"] is None and record["attribute"] is None
    assert record["tags"] == [] and record["history"] == []
    assert record["simhash"] is None and record["minhash_sig"] is None
    assert record["trust"] == 0.9  # non-identifying fields stay
    assert retained_fields(payload, "r1") == set()


def test_verify_detects_a_surviving_fact_key() -> None:
    payload = {"record": {"record_id": "r1", "namespace": "ns", "content": "", "entity": "Alice"}}
    assert retained_fields(payload, "r1") == {"entity"}
    assert retained_fields(payload, "r1")


def test_tag_patch_delta_is_scrubbed() -> None:
    payload = {"record_id": "r1", "set": {"tags_add": ["alice-cue"], "status": "archived"}}
    assert retained_fields(payload, "r1") == {"tags_add"}
    assert redact_record(payload, "r1")
    assert payload["set"] == {"tags_add": [], "status": "archived"}
