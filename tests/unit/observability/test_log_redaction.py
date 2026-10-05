"""#12: exception text is redacted before it reaches a log line."""

from __future__ import annotations

from memspine.observability.logging import redact_error, redact_error_fields


def test_redact_error_masks_secrets_pii_and_url_credentials() -> None:
    exc = RuntimeError(
        "connect failed: postgresql://admin:hunter2pass@db.internal/mem "
        "token=abcd1234efgh5678 for alice@example.com ssn 123-45-6789"
    )
    text = redact_error(exc)
    for leaked in ("hunter2pass", "abcd1234efgh5678", "alice@example.com", "123-45-6789"):
        assert leaked not in text
    assert "postgresql://***@db.internal" in text


def test_redact_error_truncates_long_messages() -> None:
    text = redact_error("x" * 5000)
    assert len(text) < 600 and text.endswith("[truncated]")


def test_processor_redacts_only_error_fields() -> None:
    event = {
        "event": "llm.failed",
        "error": "bad key AKIAABCDEFGHIJKLMNOP",
        "detail": ValueError("password=supersecret99"),
        "namespace": "alice@example.com",
    }
    out = redact_error_fields(None, "warning", event)
    assert "AKIA" not in out["error"]
    assert "supersecret99" not in out["detail"]
    assert out["namespace"] == "alice@example.com"  # not an error field
