"""Focused durability and fencing tests for the example SQLite step journal."""

from __future__ import annotations

import math
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from memspine.workers.step_journal import (
    CompletedStep,
    InputConflictError,
    LeaseExpiredError,
    StaleClaimError,
    StepBusyError,
    StepClaim,
    StepJournal,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _journal(path: Path, clock: FakeClock | None = None) -> StepJournal:
    return StepJournal(path, lease_seconds=10, clock=clock or FakeClock())


def test_completed_json_output_survives_close_and_reopen(tmp_path: Path) -> None:
    database = tmp_path / "persistent" / "steps.sqlite3"
    first = _journal(database)
    claim = first.claim_step("op-1", "normalize", "input-sha", "attempt-1")
    assert isinstance(claim, StepClaim)
    first.complete(claim, {"items": ["a", "b"], "round": 2})

    recovered = _journal(database).claim_step("op-1", "normalize", "input-sha", "attempt-2")

    assert recovered == CompletedStep("input-sha", {"items": ["a", "b"], "round": 2})


def test_step_identity_rejects_changed_input_fingerprint(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "steps.sqlite3")
    claim = journal.claim_step("op-1", "normalize", "input-sha-1", "attempt-1")
    assert isinstance(claim, StepClaim)

    with pytest.raises(InputConflictError):
        journal.claim_step("op-1", "normalize", "input-sha-2", "attempt-2")


def test_busy_and_stale_owners_are_fenced(tmp_path: Path) -> None:
    clock = FakeClock()
    journal = _journal(tmp_path / "steps.sqlite3", clock)
    original = journal.claim_step("op-1", "step", "input-sha", "owner-old")
    assert isinstance(original, StepClaim)

    with pytest.raises(StepBusyError):
        journal.claim_step("op-1", "step", "input-sha", "owner-overlap")

    clock.now += 11
    replacement = journal.claim_step("op-1", "step", "input-sha", "owner-new")
    assert isinstance(replacement, StepClaim)
    assert replacement.fencing_token == original.fencing_token + 1
    with pytest.raises(StaleClaimError):
        journal.complete(original, {"old": True})

    journal.complete(replacement, {"new": True})
    assert journal.claim_step("op-1", "step", "input-sha", "reader") == CompletedStep(
        "input-sha", {"new": True}
    )


def test_concurrent_claims_have_one_owner(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "steps.sqlite3")
    barrier = Barrier(2)

    def claim(owner_id: str) -> object:
        barrier.wait()
        try:
            return journal.claim_step("op-concurrent", "step", "input-sha", owner_id)
        except StepBusyError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, ("owner-a", "owner-b")))

    assert sum(isinstance(outcome, StepClaim) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, StepBusyError) for outcome in outcomes) == 1


def test_failed_step_can_be_retried_with_a_new_fencing_token(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "steps.sqlite3")
    failed_attempt = journal.claim_step("op-1", "step", "input-sha", "attempt-1")
    assert isinstance(failed_attempt, StepClaim)
    journal.fail(failed_attempt)

    retry = journal.claim_step("op-1", "step", "input-sha", "attempt-2")

    assert isinstance(retry, StepClaim)
    assert retry.fencing_token == failed_attempt.fencing_token + 1
    journal.complete(retry, ["recovered"])


def test_non_filesystem_database_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="filesystem-backed"):
        StepJournal(":memory:")


def test_expired_claim_cannot_be_renewed_or_failed(tmp_path: Path) -> None:
    clock = FakeClock()
    journal = _journal(tmp_path / "steps.sqlite3", clock)
    claim = journal.claim_step("op-1", "step", "input-sha", "owner-old")
    assert isinstance(claim, StepClaim)
    clock.now += 11

    with pytest.raises(LeaseExpiredError):
        journal.renew(claim)
    with pytest.raises(LeaseExpiredError):
        journal.fail(claim)

    recovered = journal.claim_step("op-1", "step", "input-sha", "owner-new")
    assert isinstance(recovered, StepClaim)
    assert recovered.fencing_token == claim.fencing_token + 1


def test_invalid_output_does_not_complete_the_step(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "steps.sqlite3")
    claim = journal.claim_step("op-1", "step", "input-sha", "owner")
    assert isinstance(claim, StepClaim)

    with pytest.raises(ValueError, match="JSON-serializable"):
        journal.complete(claim, object())  # type: ignore[arg-type]
    with pytest.raises(StepBusyError):
        journal.claim_step("op-1", "step", "input-sha", "another-owner")


def test_rejects_nonfinite_and_boolean_timeouts(tmp_path: Path) -> None:
    for argument in ("lease_seconds", "busy_timeout_seconds"):
        for value in (math.nan, math.inf, True):
            kwargs = {argument: value}
            with pytest.raises(ValueError):
                StepJournal(tmp_path / f"{argument}-{value}.sqlite3", **kwargs)  # type: ignore[arg-type]


def test_subprocess_crash_preserves_completed_and_reclaims_incomplete_step(tmp_path: Path) -> None:
    database = tmp_path / "crash" / "steps.sqlite3"
    child_code = "\n".join(
        (
            "import os, sys",
            "from memspine.workers.step_journal import StepClaim, StepJournal",
            "journal = StepJournal(sys.argv[1], lease_seconds=10, clock=lambda: 100.0)",
            "done = journal.claim_step('op-crash', 'done', 'fp-done', 'worker')",
            "assert isinstance(done, StepClaim)",
            "journal.complete(done, {'receipt': 'committed'})",
            "journal.claim_step('op-crash', 'interrupted', 'fp-pending', 'worker')",
            "os._exit(0)",
        )
    )
    subprocess.run([sys.executable, "-c", child_code, str(database)], check=True)

    recovered = StepJournal(database, lease_seconds=10, clock=lambda: 111.0)
    result = recovered.claim_step("op-crash", "done", "fp-done", "retry")
    assert result == CompletedStep("fp-done", {"receipt": "committed"})

    reclaimed = recovered.claim_step("op-crash", "interrupted", "fp-pending", "retry")
    assert isinstance(reclaimed, StepClaim)
    assert reclaimed.fencing_token == 2
