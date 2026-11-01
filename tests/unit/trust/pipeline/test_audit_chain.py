"""``memspine.trust.pipeline.audit_chain``: content-free, hash-chained audit records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.store.events import EventKind, MemoryEvent
from memspine.trust.pipeline import audit_chain
from memspine.trust.privacy import principal_scope, read_scope
from memspine.trust.retention import GENESIS_HASH, AuditEntry, AuditRecord

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _engine(**audit: bool) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        read={"hybrid": False},
        audit=audit or {"actions": True},
    )


def _entry(**over: Any) -> AuditEntry:
    base: dict[str, Any] = {"actor": "ops", "action": "erase", "op": "forget", "at": T0}
    base.update(over)
    return AuditEntry(**base)


async def _forge(eng: Engine, record: dict[str, Any]) -> None:
    """Write a marker straight into the log, bypassing the chain (a tamperer)."""
    await eng._append_and_project(
        MemoryEvent(
            kind=EventKind.MARKER,
            namespace="default",
            actor="system",
            payload={"marker": audit_chain.MARKER_AUDIT, "record": record},
        )
    )


@pytest.mark.parametrize(
    ("name", "kind", "op"),
    [
        ("forget", "erase", "forget"),
        ("erase_subject", "erase", "erase_subject"),
        ("retention_expiry", "erase", "retention_expiry"),
        ("correct", "write", "correct"),
        ("grant", "consent", "grant"),
        ("unsubscribe", "consent", "unsubscribe"),
        ("approve_quarantined", "admin", "approve_quarantined"),
        ("Weird Name!", "admin", "weird_name_"),
    ],
)
def test_classify_maps_verbs_to_audit_actions(name: str, kind: str, op: str) -> None:
    assert audit_chain._classify(name) == (kind, op)


@pytest.mark.parametrize(
    ("flags", "on"),
    [
        ({"actions": True}, True),
        ({"reads": True}, True),
        ({"reads": False, "actions": False}, False),
    ],
)
async def test_enabled_follows_either_audit_flag(flags: dict[str, bool], on: bool) -> None:
    eng = await _engine(**flags).start()
    try:
        assert audit_chain.enabled(eng) is on
    finally:
        await eng.stop()


async def test_actions_seal_a_linked_chain_that_verifies() -> None:
    eng = await _engine().start()
    try:
        await audit_chain.action(eng, "forget", "a", ["r1"], actor="dpo", reason="art17")
        await audit_chain.action(eng, "grant", "a", ["g1", "g2"], actor="alice", reason=None)
        recs = await audit_chain.records(eng)
        assert [r.op for r in recs] == ["forget", "grant"]
        assert [r.seq for r in recs] == [1, 2]
        assert recs[0].prev_hash == GENESIS_HASH
        assert recs[1].prev_hash == recs[0].hash
        assert all(r.hash == r.compute_hash() for r in recs)
        assert recs[0].tenant == "a" and recs[1].ids == ("g1", "g2")
        report = await audit_chain.verify(eng)
        assert report.ok and report.checked == 2
    finally:
        await eng.stop()


async def test_free_text_is_reduced_to_identifiers_and_codes() -> None:
    eng = await _engine().start()
    try:
        await audit_chain.action(
            eng,
            "forget",
            "a",
            ["rec 1 <script>"],
            actor="Alice Smith (DPO)",
            reason="User asked: delete my data NOW",
        )
        (rec,) = await audit_chain.records(eng)
        assert rec.actor == "Alice_Smith__DPO_"
        assert rec.ids == ("rec_1__script_",)
        assert rec.reason == "user_asked:_delete_my_data_now"  # ":" is a code character
        assert " " not in rec.model_dump_json()
    finally:
        await eng.stop()


async def test_unmatched_purpose_is_recorded_as_wildcard() -> None:
    eng = await _engine(reads=True).start()
    try:
        with principal_scope("svc:bot"), read_scope("support ticket #4"):
            await audit_chain.read(eng, "search", "a", ["r1"])
        with read_scope("support.tickets"):
            await audit_chain.read(eng, "search", "a", ["r2"])
        first, second = await audit_chain.records(eng)
        assert first.purpose == "*" and first.actor == "svc:bot"
        assert second.purpose == "support.tickets" and second.actor == "anonymous"
    finally:
        await eng.stop()


async def test_reads_are_skipped_unless_audit_reads() -> None:
    eng = await _engine(actions=True).start()
    try:
        await audit_chain.read(eng, "search", "a", ["r1"])
        assert await audit_chain.append(eng, _entry(action="read", op="search")) is None
        assert await audit_chain.records(eng) == []
        # an ordinary write is not audited either; a correction is
        assert await audit_chain.append(eng, _entry(action="write", op="remember")) is None
        assert await audit_chain.append(eng, _entry(action="write", op="correct")) is not None
    finally:
        await eng.stop()


async def test_an_entry_earlier_than_the_head_is_clamped_to_keep_time_order() -> None:
    eng = await _engine().start()
    try:
        first = await audit_chain.append(eng, _entry(at=T0))
        second = await audit_chain.append(eng, _entry(at=T0 - timedelta(hours=1)))
        assert first is not None and second is not None
        assert second.at == first.at
        assert (await audit_chain.verify(eng)).ok
    finally:
        await eng.stop()


async def test_head_is_reloaded_from_the_log() -> None:
    eng = await _engine().start()
    try:
        await audit_chain.append(eng, _entry())
        eng.__dict__.pop("_audit_head")  # a fresh process: the head comes from the log
        again = await audit_chain.append(eng, _entry())
        assert again is not None and again.seq == 2
        assert (await audit_chain.verify(eng)).ok
    finally:
        await eng.stop()


async def test_verify_detects_an_edited_record() -> None:
    eng = await _engine().start()
    try:
        sealed = await audit_chain.append(eng, _entry(ids=("r1",)))
        assert sealed is not None
        tampered = sealed.model_copy(update={"seq": 2, "prev_hash": sealed.hash, "ids": ("r9",)})
        # keep the hash of a different body: the record no longer matches its own digest
        forged = tampered.model_dump(mode="json") | {"hash": sealed.hash}
        await _forge(eng, forged)
        report = await audit_chain.verify(eng)
        assert not report.ok
        assert report.reason == "hash" and report.broken_at == 2 and report.checked == 1
    finally:
        await eng.stop()


async def test_verify_detects_a_broken_link() -> None:
    eng = await _engine().start()
    try:
        await audit_chain.append(eng, _entry())
        body = AuditRecord(**_entry().model_dump(), seq=2, prev_hash="f" * 64, hash=GENESIS_HASH)
        rehashed = body.model_copy(update={"hash": body.compute_hash()})
        await _forge(eng, rehashed.model_dump(mode="json"))
        report = await audit_chain.verify(eng)
        assert not report.ok and report.reason == "prev_hash" and report.broken_at == 2
    finally:
        await eng.stop()


async def test_verify_detects_a_sequence_gap() -> None:
    eng = await _engine().start()
    try:
        sealed = await audit_chain.append(eng, _entry())
        assert sealed is not None
        body = AuditRecord(**_entry().model_dump(), seq=3, prev_hash=sealed.hash, hash=GENESIS_HASH)
        rehashed = body.model_copy(update={"hash": body.compute_hash()})
        await _forge(eng, rehashed.model_dump(mode="json"))
        report = await audit_chain.verify(eng)
        assert not report.ok and report.reason == "seq_gap" and report.broken_at == 3
    finally:
        await eng.stop()


async def test_verify_of_an_empty_chain_is_ok() -> None:
    eng = await _engine().start()
    try:
        report = await audit_chain.verify(eng)
        assert report.ok and report.checked == 0
    finally:
        await eng.stop()
