"""Close tolerates a writer still borrowed by a cancelled call (CI flake: ``Already borrowed``)."""

from __future__ import annotations

import pytest

from memspine.services.lexical import tantivy


class _Writer:
    def __init__(self, busy: int, error: str = "Already borrowed") -> None:
        self.busy, self.error, self.calls = busy, error, 0

    def wait_merging_threads(self) -> None:
        self.calls += 1
        if self.calls <= self.busy:
            raise RuntimeError(self.error)


def test_join_retries_until_the_writer_is_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tantivy, "TANTIVY_COMMIT_BACKOFF_S", 0.0)
    w = _Writer(busy=2)
    tantivy._wait_merging_threads(w)
    assert w.calls == 3


def test_join_gives_up_quietly_when_still_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tantivy, "TANTIVY_COMMIT_BACKOFF_S", 0.0)
    w = _Writer(busy=99)
    tantivy._wait_merging_threads(w)  # no raise: shutdown must not fail on a busy writer
    assert w.calls == tantivy.TANTIVY_COMMIT_RETRIES


def test_other_runtime_errors_still_raise() -> None:
    with pytest.raises(RuntimeError, match="disk full"):
        tantivy._wait_merging_threads(_Writer(busy=1, error="disk full"))
