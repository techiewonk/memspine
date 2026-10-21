"""TW-B: T5-04 erasure plan (LIB-09) and T5-05 retention plan + audit chain (LIB-41), model-free."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memspine import Engine
from memspine.config.schema import AuditConfig, MemspineConfig, PrivacyConfig, RetentionConfig
from memspine.derive.write import write_derived
from memspine.observability import tap
from memspine.read.pipeline import request as rq
from memspine.schemas.durable.erasure import ErasureManifest
from memspine.store import lineage as lineage_store
from memspine.store.events import EventKind
from memspine.store.records import MemoryRecord, RecordStatus, SourceInfo
from memspine.trust.erasure_plan import ErasureState, assert_manifest_has_no_content
from memspine.trust.retention import AuditRecord, verify_chain

NS = "u"
T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)
SECRET = "Lisbon"


def _engine(path: Path | None = None, **over: Any) -> Engine:
    cfg: dict[str, Any] = {
        "template": "core",
        "dotenv_path": None,
        "storage": {
            "provider": "sqlite",
            "settings": {
                "path": str(path / "m.db") if path else ":memory:",
                "busy_timeout_s": 5.0,
            },
        },
        "embedding": {"provider": "hash", "settings": {}},
        "read": {"hybrid": False, "record_access": False, "cache": {"enabled": True}},
        "memories": {"episodic": {"enabled": True}, "semantic": {"enabled": True}},
    }
    cfg.update(over)
    return Engine(**cfg)


async def _turns(eng: Engine) -> tuple[MemoryRecord, MemoryRecord]:
    await eng.ingest_messages(
        [
            {"role": "user", "content": f"I moved to {SECRET} last spring"},
            {"role": "user", "content": "my sister plays the cello"},
        ],
        namespace=NS,
        session_id="s1",
        valid_from=T0,
    )
    recs = await eng._require_started().list_records(NS, "episodic")
    by_text = {r.content: r for r in recs}
    return by_text[f"I moved to {SECRET} last spring"], by_text["my sister plays the cello"]


async def _derive(
    eng: Engine, text: str, parents: list[str], roles: dict[str, str] | None = None
) -> MemoryRecord:
    rec = MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        content=text,
        source=SourceInfo(role="assistant", channel="mining", parents=parents),
        valid_from=T0,
    )
    storage = eng._require_started()
    async with eng._write_locks.setdefault(NS, asyncio.Lock()):
        return await write_derived(eng, storage, NS, rec, "semantic", "system", roles=roles)


async def _markers(eng: Engine, name: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in await eng._require_started().read_events(after_seq=0, limit=10000):
        if ev.kind is EventKind.MARKER and ev.payload.get("marker") == name:
            out.append(ev.payload)
    return out


# ---------------------------------------------------------------------------------- flags


def test_flags_default_off() -> None:
    assert PrivacyConfig().erasure_v2 is False
    assert RetentionConfig().v2 is False and RetentionConfig().profile is None
    assert AuditConfig().chain_v2 is False
    cfg = MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 1.0,
                "close_timeout_s": 1.0,
                "startup_timeout_s": None,
            }
        }
    )
    assert cfg.privacy.erasure_v2 is False and cfg.audit.chain_v2 is False


# ---------------------------------------------------------------------------------- T5-04


async def test_erasure_off_cascades_as_before() -> None:
    eng = _engine(write={"lineage_table": True})
    await eng.start()
    try:
        a, _ = await _turns(eng)
        fact = await _derive(eng, f"User lives in {SECRET}", [a.record_id])
        await eng.forget(a.record_id, namespace=NS, hard=True)
        storage = eng._require_started()
        assert await storage.get_record(fact.record_id) is None  # legacy cascade deleted it
        assert eng.erasure_status()["erased"] == 0  # no erasure state: the flag is off
        assert eng.erasure_queue() == []
        assert not await _markers(eng, "erasure_manifest")
    finally:
        await eng.stop()


async def test_end_to_end_forget_hides_dependants_and_purges() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    eng = _engine(
        write={"lineage_table": True},
        privacy={"erasure_v2": True},
        observability={"tap": {"enabled": True, "sink": "memory"}},
    )
    await eng.start()
    try:
        a, b = await _turns(eng)
        only_a = await _derive(eng, f"User lives in {SECRET}", [a.record_id])
        both = await _derive(eng, "User and sister talk about music", [a.record_id, b.record_id])
        summary = await _derive(
            eng,
            f"Summary: moved to {SECRET}, sister plays cello",
            [a.record_id, b.record_id],
            roles={a.record_id: "member", b.record_id: "member"},
        )
        storage = eng._require_started()
        # a cached read that names the derived fact
        await eng.read(SECRET, namespace=NS)  # fills the read cache before the erasure

        real_event = tap.event
        tap.event = lambda stage, /, **f: (events.append((stage, f)), real_event(stage, **f))[1]  # type: ignore[assignment]
        try:
            await eng.forget(a.record_id, namespace=NS, hard=True)
        finally:
            tap.event = real_event  # type: ignore[assignment]

        # erased record: gone; hidden dependant: row stays, invisible at once
        assert await storage.get_record(a.record_id) is None
        hidden_row = await storage.get_record(only_a.record_id)
        assert hidden_row is not None and hidden_row.status is RecordStatus.ACTIVATED
        assert not eng._consent_ok(hidden_row)
        # "keep" (another live support parent) stays visible; the summary is re-run, hidden
        assert eng._consent_ok(await storage.get_record(both.record_id))
        assert not eng._consent_ok(await storage.get_record(summary.record_id))

        queue = {q["record_id"]: q for q in eng.erasure_queue()}
        assert set(queue) == {only_a.record_id, summary.record_id}
        assert queue[only_a.record_id]["action"] == "hide_and_rederive"
        assert queue[summary.record_id]["action"] == "rerun_summary"
        assert queue[summary.record_id]["remaining_sources"] == [b.record_id]

        # no read leg returns the erased or hidden text, with the cache on
        for query in (SECRET, "User lives", "Summary"):
            got = await eng.read(query, namespace=NS)
            assert not {only_a.record_id, summary.record_id, a.record_id} & {
                r.record_id for r in got.context.records
            }
            hits = await eng.search(query, namespace=NS)
            assert not {only_a.record_id, summary.record_id} & {r.record_id for r, _ in hits}

        # the manifest: ids and the generation, no content
        (marker,) = await _markers(eng, "erasure_manifest")
        man = ErasureManifest.model_validate(marker["manifest"])
        assert_manifest_has_no_content(man)
        assert man.ids.record_ids == [a.record_id] and man.erasure_generation >= 1
        assert SECRET not in json.dumps(marker)
        assert sorted(marker["hidden"]) == sorted([only_a.record_id, summary.record_id])

        # the purge job reached done, with progress
        status = eng.erasure_status()
        (purge,) = status["purges"].values()
        assert purge["state"] == "done" and purge["done"] == purge["total"] > 0
        done_markers = await _markers(eng, "erasure_purge")
        assert done_markers and done_markers[-1]["state"] == "done"

        # lineage edges that touch the erased record are gone; the others stay
        left = await lineage_store.load_edges(storage._client.engine, [both.record_id])
        assert all(a.record_id not in (e.parent, e.child) for e in left)
        assert any(e.parent == b.record_id for e in left)

        # tap events
        names = [n for n, _ in events]
        assert "trust.erasure.plan" in names and "trust.erasure.done" in names
        plan_f = next(f for n, f in events if n == "trust.erasure.plan")
        assert plan_f["erased"] == 1 and plan_f["dependants"]["hide_and_rederive"] == 1
        done_f = next(f for n, f in events if n == "trust.erasure.done")
        assert done_f["state"] == "done"

        # the offline runner releases a re-derived id; the erased id never returns
        assert await eng.erasure_release([only_a.record_id, a.record_id], NS) == 1
        assert eng._consent_ok(await storage.get_record(only_a.record_id))
        assert [q["record_id"] for q in eng.erasure_queue()] == [summary.record_id]
    finally:
        await eng.stop()


async def test_pol1_cache_hit_is_revalidated_against_the_erasure_state() -> None:
    eng = _engine(privacy={"erasure_v2": True})
    await eng.start()
    try:
        await _turns(eng)
        seen: list[Any] = []
        real_get = rq._cache_get

        async def capture(*a: Any) -> Any:
            seen.append(a)
            return await real_get(*a)

        rq._cache_get = capture  # type: ignore[assignment]
        try:
            first = await eng.read(SECRET, namespace=NS)
        finally:
            rq._cache_get = real_get  # type: ignore[assignment]
        victim = first.context.records[0].record_id
        hit, _ = await real_get(*seen[0])
        assert hit is not None  # warm: served from the cache
        # hide the id with no generation bump: the cached entry must not be served (POL-1)
        eng.__dict__["_erasure_state"] = ErasureState(hidden=frozenset({victim}), generation=0)
        hit, _ = await real_get(*seen[0])
        assert hit is None
    finally:
        await eng.stop()


async def test_state_survives_restart_and_hidden_id_stays_invisible(tmp_path: Path) -> None:
    kw: dict[str, Any] = {"write": {"lineage_table": True}, "privacy": {"erasure_v2": True}}
    eng = _engine(tmp_path, **kw)
    await eng.start()
    try:
        a, _ = await _turns(eng)
        fact = await _derive(eng, f"User lives in {SECRET}", [a.record_id])
        await eng.forget(a.record_id, namespace=NS, hard=True)
        gen = eng.erasure_status()["generation"]
    finally:
        await eng.stop()
    eng2 = _engine(tmp_path, **kw)
    await eng2.start()
    try:
        row = await eng2._require_started().get_record(fact.record_id)
        assert row is not None and not eng2._consent_ok(row)
        assert [q["record_id"] for q in eng2.erasure_queue()] == [fact.record_id]
        assert eng2.erasure_status()["generation"] >= 1 and gen >= 1
        assert await eng2.erasure_resume_purges() == []  # the purge was already done
    finally:
        await eng2.stop()


async def test_cascade_false_hides_nothing() -> None:
    eng = _engine(write={"lineage_table": True}, privacy={"erasure_v2": True})
    await eng.start()
    try:
        a, _ = await _turns(eng)
        fact = await _derive(eng, f"User lives in {SECRET}", [a.record_id])
        await eng.forget(a.record_id, namespace=NS, hard=True, cascade=False)
        row = await eng._require_started().get_record(fact.record_id)
        assert row is not None and eng._consent_ok(row) and eng.erasure_queue() == []
    finally:
        await eng.stop()


async def test_parents_fallback_without_the_lineage_table() -> None:
    eng = _engine(privacy={"erasure_v2": True})  # write.lineage_table off: source.parents
    await eng.start()
    try:
        a, _ = await _turns(eng)
        fact = MemoryRecord(
            namespace=NS,
            memory_type="semantic",
            content=f"User lives in {SECRET}",
            source=SourceInfo(role="assistant", channel="mining", parents=[a.record_id]),
            valid_from=T0,
        )
        async with eng._write_locks.setdefault(NS, asyncio.Lock()):
            fact = await write_derived(eng, eng._require_started(), NS, fact, "semantic", "system")
        await eng.forget(a.record_id, namespace=NS, hard=True)
        row = await eng._require_started().get_record(fact.record_id)
        assert row is not None and not eng._consent_ok(row)
        assert eng.erasure_queue()[0]["action"] == "hide_and_rederive"
    finally:
        await eng.stop()


# ---------------------------------------------------------------------------------- T5-05


async def test_retention_off_is_unchanged() -> None:
    eng = _engine(retention={"classes": [{"ttl_days": 1.0, "memory_type": "semantic"}]})
    await eng.start()
    try:
        await eng.write("old fact", namespace=NS, memory_type="semantic", entity="X", attribute="a")
        later = datetime.now(UTC) + timedelta(days=3)
        stats = await eng.expire_retention(later)
        assert stats["expired"] == 1 and "profile" not in stats
    finally:
        await eng.stop()


async def test_retention_v2_expires_by_class_and_never_l0_under_full() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    eng = _engine(
        retention={
            "v2": True,
            "classes": [
                {"ttl_days": 1.0, "memory_type": "semantic"},
                {"ttl_days": 1.0, "memory_type": "episodic"},
            ],
        },
        observability={"tap": {"enabled": True, "sink": "memory"}},
    )
    await eng.start()
    try:
        turn, _ = await _turns(eng)
        fact = await eng.write(
            "old fact", namespace=NS, memory_type="semantic", entity="X", attribute="a"
        )
        later = datetime.now(UTC) + timedelta(days=3)
        real_event = tap.event
        tap.event = lambda stage, /, **f: (events.append((stage, f)), real_event(stage, **f))[1]  # type: ignore[assignment]
        try:
            stats = await eng.expire_retention(later)
        finally:
            tap.event = real_event  # type: ignore[assignment]
        storage = eng._require_started()
        assert await storage.get_record(fact.record_id) is None  # L1 fact expired
        assert await storage.get_record(turn.record_id) is not None  # L0 turn: never under full
        assert stats["expired"] == 1 and stats["tombstones"] == 1
        assert stats["l0_protected"] >= 1 and stats["rebuild"] == "full"
        boundary = eng.retention_boundary(NS)
        assert boundary is not None and boundary.mode == "full"
        plan_f = next(f for n, f in events if n == "job.retention.plan")
        assert plan_f["expire"] == 1 and plan_f["rebuild"] == "full"
        assert any(n == "job.retention.done" for n, _ in events)
    finally:
        await eng.stop()


async def test_retention_v2_holds_and_template_profile() -> None:
    eng = _engine(
        retention={"v2": True, "profile": "assistant"},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
    )
    await eng.start()
    try:
        # no classes configured, a template profile: the stage runs and finds nothing due yet
        stats = await eng.expire_retention(datetime.now(UTC))
        assert stats["status"] == "ok" and stats["expired"] == 0 and stats["profile"] == "assistant"
    finally:
        await eng.stop()


async def test_retention_v2_rolling_log_advertises_limited_rebuild() -> None:
    eng = _engine(
        event_log={"mode": "rolling", "retention_days": 30},
        retention={"v2": True, "classes": [{"ttl_days": 1.0, "memory_type": "semantic"}]},
    )
    await eng.start()
    try:
        await eng.write("f", namespace=NS, memory_type="semantic", entity="X", attribute="a")
        stats = await eng.expire_retention(datetime.now(UTC))
        assert stats["rebuild"] == "limited"
        boundary = eng.retention_boundary(NS)
        assert boundary is not None and boundary.mode == "rolling" and boundary.rebuild == "limited"
        prune = await eng._runner.run("event_log_prune", eng._pipeline_ctx())
        assert prune["rebuild"] == "limited"
    finally:
        await eng.stop()


# ---------------------------------------------------------------------------------- audit


async def _chain(eng: Engine) -> list[AuditRecord]:
    return [AuditRecord.model_validate(p["record"]) for p in await _markers(eng, "audit_chain_v2")]


async def test_audit_chain_off_keeps_legacy_events() -> None:
    eng = _engine(audit={"actions": True})
    await eng.start()
    try:
        a, _ = await _turns(eng)
        await eng.forget(a.record_id, namespace=NS, hard=True)
        assert not await _markers(eng, "audit_chain_v2")
        assert await eng.audit_chain_ok()
    finally:
        await eng.stop()


async def test_audit_chain_v2_records_verify_and_detect_tampering(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    eng = _engine(
        tmp_path,
        audit={"chain_v2": True, "reads": True},
        observability={"tap": {"enabled": True, "sink": "memory"}},
    )
    await eng.start()
    try:
        a, _ = await _turns(eng)
        real_event = tap.event
        tap.event = lambda stage, /, **f: (events.append((stage, f)), real_event(stage, **f))[1]  # type: ignore[assignment]
        try:
            await eng.search(SECRET, namespace=NS)
            await eng.forget(a.record_id, namespace=NS, hard=True, reason="User request: GDPR!")
            report = await eng.verify_audit_chain_v2()
        finally:
            tap.event = real_event  # type: ignore[assignment]
        records = await _chain(eng)
        assert [r.action for r in records] == ["read", "erase"]
        assert records[1].ids == (a.record_id,) and records[1].op == "forget"
        assert records[1].reason == "user_request:_gdpr_"  # a short code, never free text
        assert SECRET not in json.dumps([r.model_dump(mode="json") for r in records])
        assert report.ok and report.checked == 2 and await eng.audit_chain_ok()
        names = [n for n, _ in events]
        assert "trust.audit.append" in names and "trust.audit.verify" in names
        assert next(f for n, f in events if n == "trust.audit.verify")["ok"] is True
        assert verify_chain(records).ok
        # the legacy audit events are not written
        log = await eng._require_started().read_events(after_seq=0, limit=10000)
        kinds = {e.kind for e in log}
        assert EventKind.AUDIT not in kinds and EventKind.READ_AUDIT not in kinds
    finally:
        await eng.stop()
    forged = [records[0].model_copy(update={"actor": "mallory"}), records[1]]
    bad = verify_chain(forged)
    assert not bad.ok and bad.broken_at == 1 and bad.reason == "hash"
    assert not verify_chain([records[1]]).ok  # a dropped first record is a gap


async def test_audit_profile_decides_what_is_audited() -> None:
    # reads are not audited unless audit.reads (or a profile that audits reads) says so
    eng = _engine(audit={"chain_v2": True})
    await eng.start()
    try:
        a, _ = await _turns(eng)
        await eng.search(SECRET, namespace=NS)
        assert not await _chain(eng)
        await eng.forget(a.record_id, namespace=NS, hard=True)  # erase: always audited
        assert [r.action for r in await _chain(eng)] == ["erase"]
    finally:
        await eng.stop()

    eng = _engine(audit={"chain_v2": True}, retention={"v2": True, "profile": "multi_agent"})
    await eng.start()
    try:
        await _turns(eng)
        await eng.search(SECRET, namespace=NS)
        assert [r.action for r in await _chain(eng)] == ["read"]  # the template audits reads
    finally:
        await eng.stop()
