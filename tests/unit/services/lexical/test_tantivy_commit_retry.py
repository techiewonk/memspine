"""A Windows sharing violation on commit (os error 5) is retried; other errors are not."""

from __future__ import annotations

import pytest

from memspine.services.lexical import tantivy


class FlakyWriter:
    def __init__(
        self, failures: int, message: str = "An IO error occurred: 'Access is denied. (os error 5)'"
    ) -> None:
        self.failures = failures
        self.message = message
        self.calls = 0

    def commit(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise ValueError(self.message)


def test_sharing_violation_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tantivy, "TANTIVY_COMMIT_BACKOFF_S", 0.0)
    writer = FlakyWriter(failures=2)
    tantivy._commit_with_retry(writer)
    assert writer.calls == 3


def test_persistent_violation_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tantivy, "TANTIVY_COMMIT_BACKOFF_S", 0.0)
    writer = FlakyWriter(failures=100)
    with pytest.raises(ValueError, match="os error 5"):
        tantivy._commit_with_retry(writer)
    assert writer.calls == tantivy.TANTIVY_COMMIT_RETRIES


def test_other_errors_are_not_retried() -> None:
    writer = FlakyWriter(failures=1, message="schema mismatch")
    with pytest.raises(ValueError, match="schema mismatch"):
        tantivy._commit_with_retry(writer)
    assert writer.calls == 1
