"""event_log.mode rolling + ephemeral persistence semantics across a real reopen (D-45).

These modes are about the *at-rest lifetime of the log*, and their whole point is
what happens to persisted state after a process boundary — exactly what a
``:memory:`` unit test cannot exercise. Two regressions are guarded:

* ROLLING — once the rolling window prunes history past a projector high-water
  mark, a full ``rebuild()`` is no longer possible; the code must SAY SO
  (``RebuildUnavailableError``, replay.py:54-71) rather than silently rebuild a
  partial read model. And pruning the LOG must not cost the already-materialized
  read model (catch-up from persisted offsets still serves it).

* EPHEMERAL — "ephemeral silently persists, defeating its purpose." The event LOG
  must write literally nothing to disk (``memory_events`` stays empty across a
  reopen) and ``can_rebuild`` must be False / ``rebuild()`` must raise.

DISCOVERED CONTRACT (asserted, not assumed): ``event_log.mode`` governs ONLY the
log. The read-model projection (``memory_records``) is written by the projectors
regardless of mode, so in ephemeral mode the *records* still land on disk while
the *log* does not — you keep the current read model but lose rebuild/audit. That
is by design (D-45), so this test asserts "the LOG persisted nothing", NOT a false
"nothing at all survived".
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from memspine.core.events import EventLogMode
from memspine.exceptions import RebuildUnavailableError

#: A fixed cutoff far past any event's wall-clock ``ts`` — pruning with it deletes
#: every already-applied event deterministically (no dependence on "now").
_FAR_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


def _raw_event_row_count(db_path: str) -> int:
    """``memory_events`` row count read straight off the closed file — the
    ground truth for 'did the log persist anything to disk'."""
    con = sqlite3.connect(db_path)
    try:
        return int(con.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0])
    finally:
        con.close()


async def test_rolling_prune_truncates_window_then_rebuild_is_refused(
    make_file_engine: Any,
) -> None:
    """ROLLING: after the window prunes past the high-water mark, a reopen serves
    the retained read model but ``rebuild()`` raises ``RebuildUnavailableError``
    — the documented D-45 behavior, never silent partial data."""
    engine = make_file_engine(event_log={"mode": "rolling"})
    await engine.start()
    try:
        assert engine.describe()["event_log"]["mode"] == "rolling"
        for i in range(4):
            await engine.write(f"rolling fact {i}", entity=f"e{i}", attribute="a")

        storage = engine._storage
        assert storage is not None
        assert storage.mode is EventLogMode.ROLLING

        # Window intact: rebuild works and reproduces all four records.
        assert storage.can_rebuild is True
        counts = await engine.rebuild()
        assert counts["records"] == 4

        # Prune the whole applied window (all four events are past every
        # projector's mark and older than the far-future cutoff).
        pruned = await storage.prune_events(older_than=_FAR_FUTURE)
        assert pruned == 4
        assert await storage.read_events(after_seq=0) == []
    finally:
        await engine.stop()

    # ── reopen on the SAME file: read model retained, rebuild refused ─────────
    reopened = make_file_engine(event_log={"mode": "rolling"})
    await reopened.start()
    try:
        storage = reopened._storage
        assert storage is not None

        # The pruned LOG did NOT cost the materialized read model — catch-up from
        # the persisted offsets serves all four records (no silent data loss).
        records = await reopened.retrieve(memory_type="semantic")
        assert len(records) == 4

        # But history is gone, so a full rebuild is impossible and SAYS SO.
        try:
            await reopened.rebuild()
        except RebuildUnavailableError as exc:
            assert "pruned" in str(exc).lower()
        else:  # pragma: no cover - the assertion above documents the contract
            raise AssertionError(
                "rolling rebuild after a full prune must raise RebuildUnavailableError"
            )
    finally:
        await reopened.stop()


async def test_ephemeral_log_persists_nothing_across_reopen(
    make_file_engine: Any, db_path: str
) -> None:
    """EPHEMERAL: events dispatch to projectors but the LOG writes nothing to
    disk — ``memory_events`` stays empty across a reopen and ``rebuild()`` is
    refused. (The read-model projection still lands on disk by design; that is
    asserted explicitly so the nuance is documented, not mistaken for a leak.)"""
    engine = make_file_engine(event_log={"mode": "ephemeral"})
    await engine.start()
    try:
        assert engine.describe()["event_log"]["mode"] == "ephemeral"
        assert engine.describe()["event_log"]["rebuildable"] is False

        for i in range(3):
            await engine.write(f"ephemeral fact {i}", entity=f"e{i}", attribute="a")

        storage = engine._storage
        assert storage is not None
        assert storage.mode is EventLogMode.EPHEMERAL
        assert storage.can_rebuild is False
        # The log door wrote nothing — read_events is empty even in-session.
        assert await storage.read_events(after_seq=0) == []
    finally:
        await engine.stop()

    # The LOG genuinely persisted zero rows to disk (the anti-regression).
    assert _raw_event_row_count(db_path) == 0

    # ── reopen: still no log, still no rebuild ────────────────────────────────
    reopened = make_file_engine(event_log={"mode": "ephemeral"})
    await reopened.start()
    try:
        storage = reopened._storage
        assert storage is not None
        assert await storage.read_events(after_seq=0) == []
        assert storage.can_rebuild is False

        try:
            await reopened.rebuild()
        except RebuildUnavailableError as exc:
            assert "ephemeral" in str(exc).lower()
        else:  # pragma: no cover - the assertion above documents the contract
            raise AssertionError("ephemeral rebuild must raise RebuildUnavailableError")

        # DISCOVERED + ASSERTED nuance: the read-model projection is mode-agnostic,
        # so the records themselves DID persist to disk and remain retrievable —
        # ephemeral drops the LOG (rebuild/audit), not the current read model.
        records = await reopened.retrieve(memory_type="semantic")
        assert len(records) == 3
    finally:
        await reopened.stop()
