"""Retry checks remain authoritative when a projection has no raw text."""

import pytest

from memspine.ingest.message_retry import input_fingerprint, prepare_retries


def test_skipped_message_cannot_change_payload_on_retry() -> None:
    original = {"message_id": "m", "role": "system", "content": "original"}
    identity = {
        "message_id": "m", "session_id": "s", "conversation_id": "c",
        "created_at": "2026-10-16T10:00:00+00:00",
        "input_fingerprints": {k: input_fingerprint(v) for k, v in original.items()},
    }
    _, reused = prepare_retries([original], [], "s", None, {"m": identity})
    assert reused == {"m": None}
    with pytest.raises(ValueError, match="changed content"):
        prepare_retries([{**original, "content": "changed"}], [], "s", None, {"m": identity})
