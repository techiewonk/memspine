"""M7 erasure walker: every carrier shape scrubs, and the proof mirrors it."""

from __future__ import annotations

from memspine.core.erasure import payload_retains_content, redact_record


def _snapshot(record_id: str, content: str, history: list[dict] | None = None) -> dict:
    node: dict = {"record_id": record_id, "namespace": "ns", "content": content}
    if history is not None:
        node["history"] = history
    return node


def test_top_level_snapshot_is_scrubbed() -> None:
    payload = {"record": _snapshot("r1", "secret")}
    assert payload_retains_content(payload, "r1")
    assert redact_record(payload, "r1")
    assert payload["record"]["content"] == ""
    assert not payload_retains_content(payload, "r1")


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
    assert payload["record"]["history"][0]["content"] == ""
    assert not payload_retains_content(payload, "r1")


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
    assert payload_retains_content(payload, "r1")


def test_merge_absorbed_duplicate_is_scrubbed_including_history() -> None:
    payload = {
        "kept_record_id": "r1",
        "dropped_record": _snapshot(
            "other-id", "same secret content", history=[{"version": 1, "content": "older secret"}]
        ),
    }
    assert payload_retains_content(payload, "r1")
    assert redact_record(payload, "r1")
    assert payload["dropped_record"]["content"] == ""
    assert payload["dropped_record"]["history"][0]["content"] == ""
    assert not payload_retains_content(payload, "r1")


def test_cold_tier_delta_carrier_is_scrubbed() -> None:
    payload = {
        "record_id": "r1",
        "set": {"content": "", "content_zstd": "eJwrzs9NVUgqSk3MBgAX+QP7"},
    }
    assert payload_retains_content(payload, "r1")
    assert redact_record(payload, "r1")
    assert payload["set"]["content_zstd"] is None
    assert not payload_retains_content(payload, "r1")


def test_other_records_are_untouched() -> None:
    payload = {"record": _snapshot("other", "not yours")}
    assert not payload_retains_content(payload, "r1")
    assert not redact_record(payload, "r1")
    assert payload["record"]["content"] == "not yours"
