"""#46-#50: subject-access export, correct(), retention classes, read audit + hash
chain, purpose enforcement and the remote-LLM tier gate (all opt-in)."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest

from memspine import Engine
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.privacy import principal_scope
from memspine.core.records import PiiTier, RecordStatus
from memspine.services.llm.tier_gate import WITHHELD_MARKER, TierGatedLLM
from memspine.workers.schedule import RETENTION_STAGE


def _engine(**extra: Any) -> Engine:
    base: dict[str, Any] = {
        "template": "core",
        "dotenv_path": None,
        "storage": {"path": ":memory:"},
        "embedding": {"provider": "hash"},
        "read": {"hybrid": False},
        "memories": {"semantic": {"enabled": True}},
    }
    base.update(extra)
    return Engine(**base)


@pytest.fixture
async def engine() -> AsyncIterator[Engine]:
    eng = await _engine().start()
    yield eng
    await eng.stop()


async def _events(engine: Engine) -> list[MemoryEvent]:
    storage = engine._require_started()
    return await storage.read_events(after_seq=0, limit=100_000)


# ── #46 export ───────────────────────────────────────────────────────────────


async def test_export_round_trip_live_archived_once_forgotten_absent(engine: Engine) -> None:
    ns = "user/ana"
    city = await engine.write("Ana lives in Lyon", namespace=ns, entity="ana", attribute="city")
    moved = await engine.write("Ana lives in Nice", namespace=ns, entity="ana", attribute="city")
    keep = await engine.write("Ana likes green tea", namespace=ns)
    hard = await engine.write("Ana's passport number is X1234567", namespace=ns)
    soft = await engine.write("Ana once owned a red bicycle", namespace=ns)
    other = await engine.write("Bob likes coffee", namespace="user/bob")
    await engine.forget(hard.record_id, namespace=ns, hard=True)
    await engine.forget(soft.record_id, namespace=ns)

    lines = await engine.export(ns, include_events=True)
    assert lines == await engine.export(ns, include_events=True)  # deterministic
    parsed = [orjson.loads(line) for line in lines]
    assert parsed[0]["type"] == "export" and parsed[0]["namespace"] == ns
    record_ids = [p["record"]["record_id"] for p in parsed if p["type"] == "record"]
    assert sorted(record_ids) == sorted([city.record_id, moved.record_id, keep.record_id])
    assert len(record_ids) == len(set(record_ids))  # exactly once
    statuses = {p["record"]["record_id"]: p["record"]["status"] for p in parsed[1:4]}
    assert statuses[city.record_id] == RecordStatus.ARCHIVED.value
    assert all("source" in p["record"] for p in parsed if p["type"] == "record")
    blob = "\n".join(lines)
    assert "X1234567" not in blob  # hard-forgotten: gone everywhere
    assert "red bicycle" not in blob  # soft-forgotten: scrubbed from the events too
    assert other.record_id not in blob and "coffee" not in blob
    assert any(p["type"] == "event" for p in parsed)


async def test_export_subject_and_history_switch(engine: Engine) -> None:
    ns = "u"
    ana = await engine.write("Ana lives in Lyon", namespace=ns, entity="Ana", attribute="city")
    await engine.write("Bob lives in Rome", namespace=ns, entity="bob", attribute="city")
    lines = await engine.export(ns, subject="ana", include_history=False)
    records = [orjson.loads(line) for line in lines[1:]]
    assert [r["record"]["record_id"] for r in records] == [ana.record_id]
    assert "history" not in records[0]["record"]


# ── #47 correct ──────────────────────────────────────────────────────────────


async def test_correct_supersedes_by_key_and_logs_actor_reason(engine: Engine) -> None:
    old = await engine.write("Ana lives in Lyon", namespace="u", entity="ana", attribute="city")
    new = await engine.correct(
        ("ana", "city"), "Ana lives in Nice", actor="ana", reason="moved", namespace="u"
    )
    storage = engine._require_started()
    archived = await storage.get_record(old.record_id)
    assert archived is not None and archived.status is RecordStatus.ARCHIVED
    assert archived.evolve_to == new.record_id
    current = await storage.find_active_fact("u", "ana", "city")
    assert current is not None and current.record_id == new.record_id
    writes = [e for e in await _events(engine) if e.payload.get("correction")]
    assert writes and writes[-1].actor == "ana"
    assert writes[-1].payload["correction"] == {
        "supersedes": old.record_id,
        "actor": "ana",
        "reason": "moved",
    }


async def test_correct_bypasses_contest_lower_trust() -> None:
    # The ``base`` template turns contest_lower_trust on: an ordinary lower-trust
    # write contests the fact; an explicit correction supersedes it.
    eng = await Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
    ).start()
    try:
        fact = await eng.write("Ana lives in Lyon", namespace="u", entity="ana", attribute="city")
        outcome = await eng.write_ex(
            "Ana lives in Nice",
            namespace="u",
            entity="ana",
            attribute="city",
            actor="tool",
        )
        assert outcome.action in ("contested", "rejected")
        fixed = await eng.correct(fact.record_id, "Ana lives in Nice", actor="tool", namespace="u")
        current = await eng._require_started().find_active_fact("u", "ana", "city")
        assert current is not None and current.record_id == fixed.record_id
        assert "disputed" not in current.tags
    finally:
        await eng.stop()


async def test_correct_refuses_foreign_or_dead_records(engine: Engine) -> None:
    from memspine.exceptions import ConflictError

    rec = await engine.write("Ana likes tea", namespace="a")
    with pytest.raises(ConflictError):
        await engine.correct(rec.record_id, "x", namespace="b")
    await engine.forget(rec.record_id, namespace="a")
    with pytest.raises(ConflictError):
        await engine.correct(rec.record_id, "x", namespace="a")


# ── #48 retention classes ────────────────────────────────────────────────────


async def test_retention_expires_with_fake_clock_and_respects_holds() -> None:
    eng = await _engine(
        retention={"classes": [{"namespace": "tmp/*", "ttl_days": 7}]},
        memories={
            "semantic": {
                "enabled": True,
                "policies": {"retention": {"legal_hold_namespaces": ["tmp/held"]}},
            }
        },
        audit={"actions": True},
    ).start()
    try:
        old = await eng.write("scratch note one", namespace="tmp/a")
        held = await eng.write("held note", namespace="tmp/held")
        regulated = await eng.write("regulated note", namespace="tmp/a", pii_tier=PiiTier.REGULATED)
        kept = await eng.write("durable note", namespace="keep")
        storage = eng._require_started()

        stats = await eng.sleep()
        assert stats[RETENTION_STAGE]["expired"] == 0  # nothing old yet
        eng._clock = lambda: datetime.now(UTC) + timedelta(days=8)
        stats = await eng.sleep()
        assert stats[RETENTION_STAGE] == {"status": "ok", "expired": 1, "held": 2}
        assert await storage.get_record(old.record_id) is None  # hard forget
        for rid in (held.record_id, regulated.record_id, kept.record_id):
            assert await storage.get_record(rid) is not None
        audits = [e for e in await _events(eng) if e.kind is EventKind.AUDIT]
        assert audits[-1].payload["action"] == "forget"
        assert audits[-1].payload["reason"].startswith("retention:tmp/*")
        assert (await eng.verify_forget(old.record_id, namespace="tmp/a"))["log_redacted"]
        assert await eng.audit_chain_ok()  # erasure never rewrites audit events
    finally:
        await eng.stop()


async def test_no_retention_classes_keeps_the_sleep_cycle_unchanged(engine: Engine) -> None:
    stats = await engine.sleep()
    assert RETENTION_STAGE not in stats
    assert (await engine.expire_retention())["status"] == "skipped"


# ── #49 read audit + hash chain ──────────────────────────────────────────────


async def test_read_audit_records_principal_ids_purpose_and_chains() -> None:
    eng = await _engine(audit={"reads": True, "actions": True}).start()
    try:
        rec = await eng.write("Ana likes green tea", namespace="u")
        with principal_scope("svc-support"):
            hits = await eng.search("green tea", namespace="u", purpose="support")
        await eng.retrieve(namespace="u")
        await eng.forget(rec.record_id, namespace="u", actor="ana", reason="user request")
        events = await _events(eng)
        reads = [e for e in events if e.kind is EventKind.READ_AUDIT]
        assert [e.payload["verb"] for e in reads] == ["search", "retrieve"]
        assert reads[0].actor == "svc-support" and reads[0].payload["purpose"] == "support"
        assert reads[0].payload["record_ids"] == [r.record_id for r, _ in hits]
        forget = [e for e in events if e.kind is EventKind.AUDIT][-1]
        assert forget.actor == "ana" and forget.payload["reason"] == "user request"
        assert forget.payload["record_ids"] == [rec.record_id]
        report = await eng.verify_audit_chain()
        assert report.ok and report.events == 3
        assert await eng.audit_chain_ok()
    finally:
        await eng.stop()


async def test_nested_read_verbs_audit_once() -> None:
    eng = await _engine(
        audit={"reads": True}, memories={"semantic": {"enabled": True}, "shared": {"enabled": True}}
    ).start()
    try:
        await eng.write("Ana likes green tea", namespace="u")
        await eng.shared_search("tea", namespace="u")
        await eng.assemble("tea", namespace="u")
        await eng.read("tea", namespace="u", mode="retrieve")
        verbs = [e.payload["verb"] for e in await _events(eng) if e.kind is EventKind.READ_AUDIT]
        assert verbs == ["shared_search", "assemble", "read"]
    finally:
        await eng.stop()


async def test_audit_events_change_no_projection() -> None:
    # record_access off: reads are side-effect free, so only audit events land.
    eng = _engine(audit={"reads": True}, read={"hybrid": False, "record_access": False})
    await eng.start()
    try:
        await eng.write("Ana likes green tea", namespace="u")
        before = [r.model_dump() for r in await eng.retrieve(namespace="u")]
        for _ in range(3):
            await eng.search("tea", namespace="u")
        assert [r.model_dump() for r in await eng.retrieve(namespace="u")] == before
        await eng.rebuild()  # replay ignores the audit events
        assert [r.model_dump() for r in await eng.retrieve(namespace="u")] == before
        assert sum(e.kind is EventKind.READ_AUDIT for e in await _events(eng)) == 6
    finally:
        await eng.stop()


async def test_audit_chain_detects_tampering_and_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"

    def make() -> Engine:
        return _engine(storage={"path": str(db)}, audit={"reads": True})

    eng = await make().start()
    await eng.write("Ana likes green tea", namespace="u")
    await eng.search("tea", namespace="u")
    await eng.search("green", namespace="u")
    await eng.stop()

    eng = await make().start()  # the chain head reloads from the log
    await eng.search("tea", namespace="u")
    assert await eng.audit_chain_ok()
    await eng.stop()

    with sqlite3.connect(db) as conn:
        seq, payload = conn.execute(
            "SELECT seq, payload FROM memory_events WHERE kind = ? ORDER BY seq LIMIT 1",
            (EventKind.READ_AUDIT.value,),
        ).fetchone()
        body = orjson.loads(payload)
        body["record_ids"] = []  # hide what was read
        conn.execute(
            "UPDATE memory_events SET payload = ? WHERE seq = ?",
            (orjson.dumps(body, option=orjson.OPT_SORT_KEYS), seq),
        )

    eng = await make().start()
    try:
        report = await eng.verify_audit_chain()
        assert not report.ok and report.broken_at == seq
        assert not await eng.audit_chain_ok()
    finally:
        await eng.stop()


async def test_audit_off_appends_nothing(engine: Engine) -> None:
    await engine.write("Ana likes green tea", namespace="u")
    await engine.search("tea", namespace="u")
    await engine.export("u")
    kinds = {e.kind for e in await _events(engine)}
    assert EventKind.READ_AUDIT not in kinds and EventKind.AUDIT not in kinds


# ── #50 purposes + remote-LLM gate ───────────────────────────────────────────


async def _purpose_engine(untagged: str = "allow") -> Engine:
    eng = await _engine(consent={"enforce": True, "untagged": untagged}).start()
    await eng.write("Ana's support ticket about green tea", namespace="u", purposes=["support"])
    await eng.write("Ana's tea preference for promotions", namespace="u", purposes=["marketing"])
    await eng.write("Ana drinks tea every morning", namespace="u")
    await eng.write("Ana's shared tea note", namespace="u", purposes=["*"])
    return eng


async def test_purpose_gate_on_search_retrieve_assemble() -> None:
    eng = await _purpose_engine()
    try:

        def texts(records: list[Any]) -> set[str]:
            return {r.content for r in records}

        hits = await eng.search("tea", namespace="u", top_k=10, purpose="support")
        assert texts([r for r, _ in hits]) == {
            "Ana's support ticket about green tea",
            "Ana drinks tea every morning",
            "Ana's shared tea note",
        }
        listed = await eng.retrieve(namespace="u")  # no purpose: untagged + "*"
        assert texts(listed) == {"Ana drinks tea every morning", "Ana's shared tea note"}
        context = await eng.assemble("tea", namespace="u", purpose="marketing")
        assert "Ana's support ticket about green tea" not in texts(context.records)
    finally:
        await eng.stop()


async def test_purpose_gate_untagged_deny_and_off_by_default(engine: Engine) -> None:
    eng = await _purpose_engine(untagged="deny")
    try:
        listed = await eng.retrieve(namespace="u", purpose="support")
        assert {r.content for r in listed} == {
            "Ana's support ticket about green tea",
            "Ana's shared tea note",
        }
    finally:
        await eng.stop()
    await engine.write("tagged", namespace="u", purposes=["support"])
    assert len(await engine.retrieve(namespace="u")) == 1  # enforce off: unchanged


class _FakeLLM:
    provider_id = "fake"
    model = "fake/model"

    def __init__(self) -> None:
        self.seen: list[list[dict[str, str]]] = []

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.seen.append(messages)
        return "ok"


async def test_remote_llm_gate_withholds_high_tier_text() -> None:
    eng = await _engine(
        consent={"remote_llm_max_tier": "low"},
        llm={
            "roles": {
                "summarize": {"model": "openai/gpt-4o"},
                "extract": {"model": "ollama/llama3"},
            }
        },
    ).start()
    try:
        assert eng._llm is not None
        remote = eng._llm.provider("summarize")
        assert isinstance(remote, TierGatedLLM)
        assert not isinstance(eng._llm.provider("extract"), TierGatedLLM)  # local
        fake = _FakeLLM()
        remote._inner = fake  # type: ignore[assignment]
        secret = "Ana's diagnosis is condition Z"
        await eng.write(secret, namespace="u", pii_tier=PiiTier.HIGH)
        await eng.write("Ana likes tea a lot", namespace="u", pii_tier=PiiTier.LOW)
        prompt = f"notes: {secret} | Ana likes tea a lot | {orjson.dumps(secret).decode()}"
        await eng.llm("summarize").chat([{"role": "user", "content": prompt}])
        sent = fake.seen[-1][0]["content"]
        assert secret not in sent and WITHHELD_MARKER in sent
        assert "Ana likes tea a lot" in sent  # at or below the tier: allowed
    finally:
        await eng.stop()
