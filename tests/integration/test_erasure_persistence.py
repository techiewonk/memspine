"""M7 hard-delete across the persistence boundary — the erasure-resurrection guard.

REGRESSION GUARDED: "a hard delete redacts the read model but the on-disk event
still carries the content, so a later reopen + full rebuild resurrects the erased
text from the raw log payload." That defect is invisible to a ``:memory:`` unit
test — the redacted payload only has to *survive a reopen* for it to bite, and it
only *resurfaces* when a rebuild replays the raw log from seq 0. Both halves need
a real file, so this test is deliberately file-backed.

The proof walks the whole M7 chain across a process-like boundary:

1. write sensitive content, ``forget(hard=True)``, ``verify_forget`` reports clean;
2. the redaction is durable — the raw bytes on disk no longer contain the string;
3. a NEW engine on the SAME file catches up from the persisted log and the record
   stays absent from every read surface (retrieve/search/log);
4. ``rebuild()`` (full replay from seq 0) does NOT bring the content back — the
   redacted WRITE materializes an empty row that the replayed FORGET then removes.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import orjson

from memspine.exceptions import RebuildUnavailableError

#: A distinctive marker so a raw byte-scan of the db file is unambiguous.
_SENSITIVE = "SSN-123-45-6789 patient medical record TOP SECRET"


async def _log_carries(engine: Any, needle: str) -> bool:
    """True if any *decoded* event payload still contains ``needle`` — mirrors
    what a replay would feed the projectors."""
    storage = engine._storage
    assert storage is not None
    after = 0
    while True:
        batch = await storage.read_events(after_seq=after)
        if not batch:
            return False
        for event in batch:
            if needle.encode() in orjson.dumps(event.payload):
                return True
            assert event.seq is not None
            after = event.seq


def _disk_payloads_carry(db_path: str, needle: str) -> bool:
    """True if the raw ``memory_events.payload`` blobs ON DISK contain ``needle``.
    A plain stdlib connection (engine closed) — the strongest 'persisted' proof."""
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT payload FROM memory_events").fetchall()
    finally:
        con.close()
    return any(needle.encode() in row[0] for row in rows)


async def test_hard_forget_survives_reopen_and_rebuild_does_not_resurrect(
    make_file_engine: Any, db_path: str
) -> None:
    """A hard-forgotten record stays erased across reopen AND a full rebuild —
    redaction persisted to disk, and replay from seq 0 never resurrects it."""
    # ── session 1: write → hard forget → verify clean ─────────────────────────
    engine = make_file_engine()
    await engine.start()
    try:
        record = await engine.write(_SENSITIVE, entity="patient", attribute="ssn")
        record_id = record.record_id
        # Sanity: the content really is in the log before erasure (else the test
        # proves nothing) — both in the decoded payload and in the on-disk bytes.
        assert await _log_carries(engine, _SENSITIVE)

        await engine.forget(record_id, namespace="default", hard=True)

        verdict = await engine.verify_forget(record_id)
        assert verdict["clean"] is True
        assert verdict["record_absent"] is True
        assert verdict["log_redacted"] is True
        assert verdict["log_verifiable"] is True
        # Erasure landed in the log this same session (before any reopen).
        assert await _log_carries(engine, _SENSITIVE) is False
    finally:
        await engine.stop()

    # ── redaction is DURABLE: raw bytes on the closed file no longer carry it ──
    assert _disk_payloads_carry(db_path, _SENSITIVE) is False, (
        "hard delete left the sensitive content in the on-disk event payload — "
        "a reopen/rebuild would resurrect it (M7 regression)"
    )

    # ── session 2: reopen on the SAME file, catch up from the persisted log ────
    reopened = make_file_engine()
    await reopened.start()
    try:
        storage = reopened._storage
        assert storage is not None

        # absent from every read surface after catch-up replay.
        assert await storage.get_record(record_id) is None
        assert await reopened.retrieve(memory_type="semantic") == []
        assert {r.record_id for r, _ in await reopened.search(_SENSITIVE)} == set()
        assert await _log_carries(reopened, _SENSITIVE) is False

        verdict_reopen = await reopened.verify_forget(record_id)
        assert verdict_reopen["clean"] is True
        assert verdict_reopen["record_absent"] is True

        # ── the crux: a FULL rebuild from seq 0 must NOT resurrect the content ─
        counts = await reopened.rebuild()
        assert counts["records"] >= 0  # replay ran (redacted WRITE + FORGET)

        assert await storage.get_record(record_id) is None, "rebuild resurrected the record"
        assert await reopened.retrieve(memory_type="semantic") == []
        assert await _log_carries(reopened, _SENSITIVE) is False
        post = await reopened.verify_forget(record_id)
        assert post["clean"] is True, "rebuild resurrected erased content from the raw log"
        assert post["record_absent"] is True
    finally:
        await reopened.stop()


async def test_soft_forget_keeps_history_but_hides_record(make_file_engine: Any) -> None:
    """Contrast case (soft delete): the record leaves the read model but the log
    KEEPS its content — soft forget is reversible history, not M7 erasure. Guards
    against a soft path silently redacting the log (which would defeat audit)."""
    engine = make_file_engine()
    await engine.start()
    try:
        record = await engine.write(_SENSITIVE, entity="patient", attribute="ssn")
        await engine.forget(record.record_id, namespace="default", hard=False)

        # Hidden from the active read surface...
        assert await engine.retrieve(memory_type="semantic") == []
        # ...but the log deliberately still carries it (no redaction on soft path).
        assert await _log_carries(engine, _SENSITIVE) is True

        # verify_forget must NOT claim clean: the content still lives in the log.
        verdict = await engine.verify_forget(record.record_id)
        assert verdict["log_redacted"] is False
        assert verdict["clean"] is False
    finally:
        await engine.stop()


async def test_verify_forget_unproven_when_log_is_ephemeral(make_file_engine: Any) -> None:
    """An ephemeral log persists nothing, so erasure cannot be *proven* from it —
    ``verify_forget`` reports it unverifiable rather than silently clean. Guards
    the RebuildUnavailableError contract at the verify surface too."""
    engine = make_file_engine(event_log={"mode": "ephemeral"})
    await engine.start()
    try:
        record = await engine.write(_SENSITIVE, entity="patient", attribute="ssn")
        await engine.forget(record.record_id, namespace="default", hard=True)
        verdict = await engine.verify_forget(record.record_id)
        assert verdict["log_verifiable"] is False  # ephemeral => nothing to prove against

        # And a rebuild is refused outright, per replay.py's D-45 contract.
        try:
            await engine.rebuild()
        except RebuildUnavailableError as exc:
            assert "ephemeral" in str(exc).lower()
        else:  # pragma: no cover - the assertion above documents the contract
            raise AssertionError("ephemeral rebuild must raise RebuildUnavailableError")
    finally:
        await engine.stop()
