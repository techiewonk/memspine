"""Application-owned SQLite journal for durable local micro-step results.

The database must live on a persistent filesystem volume. This is a step-result
store, not native Resonate persistence and not an exactly-once side-effect
boundary. Leaves must still use idempotency keys or reconciliation for effects
that can succeed before their result is journaled.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

type JsonValue = bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None


class JournalError(RuntimeError):
    """Base class for step-journal errors."""


class InputConflictError(JournalError):
    """Raised when a step identity is reused with a different input fingerprint."""


class StepBusyError(JournalError):
    """Raised when another unexpired lease owns a step."""


class StaleClaimError(JournalError):
    """Raised when a claim no longer owns the current fencing token."""


class LeaseExpiredError(StaleClaimError):
    """Raised when a claim attempts work after its lease has expired."""


@dataclass(frozen=True, slots=True)
class StepClaim:
    operation_id: str
    step_key: str
    input_fingerprint: str
    owner_id: str
    fencing_token: int


@dataclass(frozen=True, slots=True)
class CompletedStep:
    input_fingerprint: str
    output: JsonValue


type ClaimResult = StepClaim | CompletedStep


class StepJournal:
    """Persist completed JSON step outputs and fence concurrent attempts.

    A separate SQLite connection is used for each method. Keep this database on
    a persistent local volume that survives an Activity retry on the same
    machine; it is not a fleet-wide store or a substitute for Resonate server
    persistence. The injected clock is intended for deterministic tests.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        lease_seconds: float = 60.0,
        busy_timeout_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        raw_path = str(path)
        if raw_path == ":memory:" or raw_path.startswith("file:"):
            raise ValueError("journal requires a filesystem-backed SQLite path")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or not math.isfinite(lease_seconds)
            or lease_seconds <= 0
        ):
            raise ValueError("lease_seconds must be positive")
        if (
            isinstance(busy_timeout_seconds, bool)
            or not isinstance(busy_timeout_seconds, (int, float))
            or not math.isfinite(busy_timeout_seconds)
            or busy_timeout_seconds < 0
        ):
            raise ValueError("busy_timeout_seconds must be non-negative")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds
        self.busy_timeout_seconds = busy_timeout_seconds
        self._clock = clock
        self._initialize()

    def claim_step(
        self,
        operation_id: str,
        step_key: str,
        input_fingerprint: str,
        owner_id: str,
    ) -> ClaimResult:
        """Claim a step, or return its completed output when input still matches."""
        _validate_identity("operation_id", operation_id)
        _validate_identity("step_key", step_key)
        _validate_identity("input_fingerprint", input_fingerprint)
        _validate_identity("owner_id", owner_id)
        with self._transaction() as connection:
            now = self._current_time()
            row = connection.execute(
                """SELECT input_fingerprint, state, owner_id, fencing_token,
                          lease_expires_at, output_json
                   FROM micro_steps WHERE operation_id = ? AND step_key = ?""",
                (operation_id, step_key),
            ).fetchone()
            if row is None:
                token = 1
                connection.execute(
                    """INSERT INTO micro_steps (
                           operation_id, step_key, input_fingerprint, state,
                           owner_id, fencing_token, lease_expires_at, output_json
                       ) VALUES (?, ?, ?, 'RUNNING', ?, ?, ?, NULL)""",
                    (
                        operation_id,
                        step_key,
                        input_fingerprint,
                        owner_id,
                        token,
                        now + self.lease_seconds,
                    ),
                )
                return StepClaim(operation_id, step_key, input_fingerprint, owner_id, token)

            stored_fingerprint, state, current_owner, token, expires_at, output_json = row
            if stored_fingerprint != input_fingerprint:
                raise InputConflictError(
                    f"step {operation_id!r}/{step_key!r} already belongs to another input fingerprint"
                )
            if state == "COMPLETED":
                return CompletedStep(input_fingerprint, _decode_json(output_json))
            if state == "RUNNING" and expires_at > now:
                raise StepBusyError(
                    f"step {operation_id!r}/{step_key!r} is leased by {current_owner!r}"
                )

            next_token = token + 1
            connection.execute(
                """UPDATE micro_steps SET state = 'RUNNING', owner_id = ?,
                       fencing_token = ?, lease_expires_at = ?, output_json = NULL
                   WHERE operation_id = ? AND step_key = ?""",
                (owner_id, next_token, now + self.lease_seconds, operation_id, step_key),
            )
            return StepClaim(operation_id, step_key, input_fingerprint, owner_id, next_token)

    def renew(self, claim: StepClaim) -> None:
        """Extend an active claim lease without changing its fencing token."""
        with self._transaction() as connection:
            now = self._current_time()
            row = self._owned_row(connection, claim)
            if row[0] <= now:
                raise LeaseExpiredError("step claim lease has expired")
            connection.execute(
                """UPDATE micro_steps SET lease_expires_at = ?
                   WHERE operation_id = ? AND step_key = ?""",
                (now + self.lease_seconds, claim.operation_id, claim.step_key),
            )

    def complete(self, claim: StepClaim, output: JsonValue) -> None:
        """Atomically commit JSON output for a currently owned, unexpired claim."""
        encoded = _encode_json(output)
        with self._transaction() as connection:
            now = self._current_time()
            row = self._owned_row(connection, claim)
            if row[0] <= now:
                raise LeaseExpiredError("step claim lease has expired")
            connection.execute(
                """UPDATE micro_steps SET state = 'COMPLETED', owner_id = NULL,
                       lease_expires_at = 0, output_json = ?
                   WHERE operation_id = ? AND step_key = ?""",
                (encoded, claim.operation_id, claim.step_key),
            )

    def fail(self, claim: StepClaim) -> None:
        """Release an owned step for retry; failure details stay with the caller."""
        with self._transaction() as connection:
            now = self._current_time()
            row = self._owned_row(connection, claim)
            if row[0] <= now:
                raise LeaseExpiredError("step claim lease has expired")
            connection.execute(
                """UPDATE micro_steps SET state = 'FAILED', owner_id = NULL,
                       lease_expires_at = 0, output_json = NULL
                   WHERE operation_id = ? AND step_key = ?""",
                (claim.operation_id, claim.step_key),
            )

    def _owned_row(self, connection: sqlite3.Connection, claim: StepClaim) -> tuple[float]:
        row = connection.execute(
            """SELECT lease_expires_at FROM micro_steps
               WHERE operation_id = ? AND step_key = ?
                 AND input_fingerprint = ? AND state = 'RUNNING'
                 AND owner_id = ? AND fencing_token = ?""",
            (
                claim.operation_id,
                claim.step_key,
                claim.input_fingerprint,
                claim.owner_id,
                claim.fencing_token,
            ),
        ).fetchone()
        if row is None:
            raise StaleClaimError("step claim is no longer the current owner")
        return cast(tuple[float], row)

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise JournalError(f"unsupported step journal schema version: {version}")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS micro_steps (
                       operation_id TEXT NOT NULL,
                       step_key TEXT NOT NULL,
                       input_fingerprint TEXT NOT NULL,
                       state TEXT NOT NULL CHECK (state IN ('RUNNING', 'FAILED', 'COMPLETED')),
                       owner_id TEXT,
                       fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
                       lease_expires_at REAL NOT NULL,
                       output_json TEXT,
                       PRIMARY KEY (operation_id, step_key),
                       CHECK ((state = 'COMPLETED' AND output_json IS NOT NULL AND owner_id IS NULL)
                           OR (state != 'COMPLETED' AND output_json IS NULL))
                   )"""
            )
            connection.execute("PRAGMA user_version = 1")

    def _current_time(self) -> float:
        now = self._clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("journal clock must return a finite number")
        return float(now)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_seconds,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1000)}")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()


def _validate_identity(name: str, value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{name} must be a non-empty string of at most 512 characters")


def _encode_json(value: JsonValue) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("step output must be JSON-serializable") from exc


def _decode_json(value: str | None) -> JsonValue:
    if value is None:
        raise JournalError("completed step is missing its persisted output")
    return cast(JsonValue, json.loads(value))
