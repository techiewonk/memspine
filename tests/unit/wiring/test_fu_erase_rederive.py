"""FU-ERASE: re-derivation of hidden dependants (``privacy.erasure_rederive``) and the kept
classification through event-log member lists. Model-free (hash embedder, stub LLM callables).

1  an erased turn under a session summary yields a new summary without its text, the old row is
   purged, and a byte scan of the sqlite file and its WAL finds no marker text
2  no live parent left -> hard-forgotten, ``dropped_no_rederive``
3  a restart mid-queue resumes
4  a mined fact is re-mined over its remaining live parents
5  flag off / stage order / priority
6  ``classify_descendants`` counts event-log member lists as support
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import PrivacyConfig
from memspine.derive.pipeline.stages import erasure_rederive as stage
from memspine.derive.write import write_derived
from memspine.prompts.models import ExtractedFact
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord, SourceInfo
from memspine.trust.pipeline import erasure_state as erasure
from memspine.workers.schedule import sleep_stage_order
from memspine.workers.taskiq_runner import PIPELINE_PRIORITIES

NS = "u"
T0 = datetime(2024, 3, 1, 9, 0, tzinfo=UTC)
MARK = "Quillfeather"


def _engine(path: Path | None = None, rederive: bool = True, **priv: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {
                    "path": str(path / "m.db") if path else ":memory:",
                    "busy_timeout_s": 5.0,
                },
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        read={"hybrid": False, "record_access": False, "cache": {"enabled": True}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={"lineage_table": True},
        privacy={"erasure_rederive": rederive, **priv},
    )


async def _turns(eng: Engine) -> tuple[MemoryRecord, MemoryRecord, MemoryRecord]:
    await eng.ingest_messages(
        [
            {"role": "user", "content": f"I keep a hive named {MARK} behind the mill"},
            {"role": "user", "content": "my sister plays the cello"},
            {"role": "user", "content": "we walk the dog on sundays"},
        ],
        namespace=NS,
        session_id="s1",
        valid_from=T0,
    )
    recs = {r.content: r for r in await eng._require_started().list_records(NS, "episodic")}
    return (
        recs[f"I keep a hive named {MARK} behind the mill"],
        recs["my sister plays the cello"],
        recs["we walk the dog on sundays"],
    )


async def _derive(
    eng: Engine,
    text: str,
    parents: list[str],
    channel: str = "consolidation",
    member: bool = True,
    **kw: Any,
) -> MemoryRecord:
    """A derived record; ``member`` names its parents as members (re-run, not kept, on erasure)."""
    rec = MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        content=text,
        source=SourceInfo(role="assistant", channel=channel, parents=parents, message_id="k0"),
        valid_from=T0,
        **kw,
    )
    async with eng._write_locks.setdefault(NS, asyncio.Lock()):
        out = await write_derived(
            eng,
            eng._require_started(),
            NS,
            rec,
            "summary",
            "system",
            roles=dict.fromkeys(parents, "member") if member else None,
        )
    if channel == "consolidation":  # as consolidate does: the log names the members
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.CONSOLIDATE,
                namespace=NS,
                actor="system",
                payload={
                    "session_key": "k0",
                    "member_record_ids": parents,
                    "summary_record_id": out.record_id,
                },
            )
        )
    return out


def _stub_summarize(eng: Engine) -> list[str]:
    seen: list[str] = []

    async def summarize(text: str) -> str:
        seen.append(text)
        return "Summary: " + text.replace("\n", "; ")

    eng._summarize = summarize
    return seen


async def _run(eng: Engine) -> dict[str, object]:
    return await eng._runner.run("erasure_rederive", eng._pipeline_ctx())


def _on_disk(db: Path, needle: str) -> list[str]:
    raw = needle.encode()
    return [
        p.name for p in (db, db.with_name(db.name + "-wal")) if p.exists() and raw in p.read_bytes()
    ]


def test_flag_defaults_off_and_stage_registered() -> None:
    assert PrivacyConfig().erasure_rederive is False
    assert PIPELINE_PRIORITIES["erasure_rederive"] not in {
        v for k, v in PIPELINE_PRIORITIES.items() if k != "erasure_rederive"
    }


async def test_stage_order_only_with_both_flags() -> None:
    for rederive, expect in ((True, True), (False, False)):
        eng = _engine(rederive=rederive)
        await eng.start()
        try:
            assert ("erasure_rederive" in sleep_stage_order(eng._pipeline_ctx())) is expect
            if not rederive:
                assert (await _run(eng))["status"] == "skipped"
        finally:
            await eng.stop()


async def test_session_summary_is_rederived_without_the_erased_turn(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    await eng.start()
    try:
        a, b, c = await _turns(eng)
        seen = _stub_summarize(eng)
        old = await _derive(
            eng,
            f"Summary: {a.content}; {b.content}; {c.content}",
            [a.record_id, b.record_id, c.record_id],
        )
        await eng.forget(a.record_id, namespace=NS, hard=True)
        assert [i["record_id"] for i in eng.erasure_queue()] == [old.record_id]
        stats = await _run(eng)
        assert stats["status"] == "ok" and stats["rederived"] == 1, stats
        storage = eng._require_started()
        assert await storage.get_record(old.record_id) is None  # purged, not just hidden
        (new,) = [
            r
            for r in await storage.list_records(NS, "semantic")
            if f"rederived_from:{old.record_id}" in r.tags
        ]
        assert MARK not in new.content and "cello" in new.content
        assert sorted(new.source.parents) == sorted([b.record_id, c.record_id])
        assert all(MARK not in s for s in seen)  # the model never saw the erased turn
        assert eng.erasure_queue(include_dropped=True) == []
        assert eng.erasure_status()["hidden"] == 0
        assert (await eng.verify_forget(a.record_id, NS))["clean"] is True
        # idempotent: a second run has nothing to do
        assert (await _run(eng))["status"] == "ok"
    finally:
        await eng.stop()
    assert _on_disk(tmp_path / "m.db", MARK) == []


async def test_no_live_parents_is_dropped(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    await eng.start()
    try:
        a, b, _ = await _turns(eng)
        _stub_summarize(eng)
        only_a = await _derive(eng, f"A hive named {MARK}", [a.record_id])
        # a kind that cannot be re-derived is dropped too, even with a live parent
        odd = await _derive(eng, f"Odd {MARK}", [a.record_id, b.record_id], channel="chat")
        await eng.forget(a.record_id, namespace=NS, hard=True)
        stats = await _run(eng)
        assert stats["dropped_no_rederive"] == 2, stats
        storage = eng._require_started()
        assert await storage.get_record(only_a.record_id) is None
        assert await storage.get_record(odd.record_id) is None
        assert eng.erasure_queue() == []
        states = {i["record_id"]: i.get("state") for i in eng.erasure_queue(include_dropped=True)}
        assert states == {
            only_a.record_id: "dropped_no_rederive",
            odd.record_id: "dropped_no_rederive",
        }
    finally:
        await eng.stop()
    assert _on_disk(tmp_path / "m.db", MARK) == []


async def test_restart_mid_queue_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(tmp_path)
    await eng.start()
    a, b, c = await _turns(eng)
    _stub_summarize(eng)
    s1 = await _derive(eng, f"One {MARK}", [a.record_id, b.record_id])
    s2 = await _derive(eng, f"Two {MARK}", [a.record_id, c.record_id])
    await eng.forget(a.record_id, namespace=NS, hard=True)
    real = stage._one

    async def flaky(e: Any, ctx: Any, entry: dict[str, object]) -> str:
        if entry["record_id"] == s2.record_id:
            raise RuntimeError("crash")
        return await real(e, ctx, entry)

    monkeypatch.setattr(stage, "_one", flaky)
    stats = await _run(eng)
    assert stats["status"] == "partial" and stats["rederived"] == 1
    assert [i["record_id"] for i in eng.erasure_queue()] == [s2.record_id]
    await eng.stop()
    monkeypatch.setattr(stage, "_one", real)

    eng = _engine(tmp_path)
    await eng.start()
    try:
        _stub_summarize(eng)
        assert [i["record_id"] for i in eng.erasure_queue()] == [s2.record_id]
        stats = await _run(eng)
        assert stats["status"] == "ok" and stats["rederived"] == 1
        assert eng.erasure_queue() == [] and eng.erasure_status()["hidden"] == 0
        storage = eng._require_started()
        assert await storage.get_record(s1.record_id) is None
        assert await storage.get_record(s2.record_id) is None
    finally:
        await eng.stop()
    assert _on_disk(tmp_path / "m.db", MARK) == []


async def test_dropped_state_survives_a_restart(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    await eng.start()
    a, _, _ = await _turns(eng)
    only_a = await _derive(eng, f"A hive named {MARK}", [a.record_id])
    await eng.forget(a.record_id, namespace=NS, hard=True)
    await _run(eng)
    await eng.stop()
    eng = _engine(tmp_path)
    await eng.start()
    try:
        assert eng.erasure_queue() == []
        assert eng.erasure_status()["dropped_no_rederive"] == 1
        (e,) = eng.erasure_queue(include_dropped=True)
        assert e["record_id"] == only_a.record_id
    finally:
        await eng.stop()


async def test_mined_fact_is_remined_over_the_live_parents(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    await eng.start()
    try:
        a, b, _ = await _turns(eng)
        seen: list[str] = []

        async def miner(transcript: str) -> list[ExtractedFact]:
            seen.append(transcript)
            return [
                ExtractedFact(entity="Sister", attribute="instrument", value="cello", kind="state")
            ]

        eng._build_fact_miner = lambda: miner  # type: ignore[method-assign]
        old = await _derive(
            eng,
            f"Sister instrument: cello ({MARK})",
            [a.record_id, b.record_id],
            channel="mining",
            entity="Sister",
            attribute="instrument",
            tags=["atomic_fact"],
        )
        await eng.forget(a.record_id, namespace=NS, hard=True)
        stats = await _run(eng)
        assert stats["rederived"] == 1, stats
        assert all(MARK not in t for t in seen)
        storage = eng._require_started()
        assert await storage.get_record(old.record_id) is None
        (new,) = [
            r
            for r in await storage.list_records(NS, "semantic")
            if f"rederived_from:{old.record_id}" in r.tags
        ]
        assert new.source.parents == [b.record_id] and MARK not in new.content
        # no miner bound / the live turns no longer state it: dropped
    finally:
        await eng.stop()
    assert _on_disk(tmp_path / "m.db", MARK) == []


# ---------------------------------------------------------------- member-list classification


async def _member_summary(eng: Engine, members: list[MemoryRecord]) -> MemoryRecord:
    """A summary with NO ``source.parents`` and NO lineage edges: only the log names its members."""
    rec = MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        content="session recap",
        source=SourceInfo(role="assistant", channel="consolidation", message_id="k1"),
        valid_from=T0,
    )
    await eng._append_and_project(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace=NS,
            actor="system",
            payload={
                "record": rec.model_dump(mode="json"),
                "consolidation": {
                    "session_key": "k1",
                    "member_record_ids": [m.record_id for m in members],
                },
            },
        )
    )
    return rec


async def test_classify_counts_member_lists_as_support() -> None:
    eng = _engine(rederive=False)
    await eng.start()
    try:
        a, b, _ = await _turns(eng)
        summary = await _member_summary(eng, [a, b])
        assert (await eng._require_started().get_record(summary.record_id)).source.parents == []  # type: ignore[union-attr]
        await eng.forget(a.record_id, namespace=NS, hard=True)
        assert [i["record_id"] for i in eng.erasure_queue()] == [
            summary.record_id
        ]  # hidden, re-run
        # the offline runner re-derived it elsewhere and released it: b still supports it
        await eng.erasure_release([summary.record_id], NS)
        verdict = await eng.verify_forget(a.record_id, NS)
        assert verdict["descendants_kept"] == [summary.record_id], verdict
        assert verdict["descendants_leaked"] == [] and verdict["clean"] is True
    finally:
        await eng.stop()


async def test_classify_still_flags_a_member_summary_whose_members_are_all_gone() -> None:
    eng = _engine(rederive=False)
    await eng.start()
    try:
        a, _, _ = await _turns(eng)
        summary = await _member_summary(eng, [a])
        await eng.forget(a.record_id, namespace=NS, hard=True)
        await eng.erasure_release([summary.record_id], NS)
        verdict = await eng.verify_forget(a.record_id, NS)
        assert verdict["descendants_leaked"] == [summary.record_id]
        assert erasure.state(eng).erased  # sanity
    finally:
        await eng.stop()
