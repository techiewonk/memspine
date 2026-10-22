"""FU-SAFE (audit finding 7): the offline raw-read gate, and lessons / legacy summaries through
the derived-write door. Model-free (hash embedder, stub callables).

1  ``live_records`` / ``live_record`` drop erased, hidden, quarantined and do-not-derive records
   when ``consent.v2`` or ``privacy.erasure_v2`` is on; the identity with both off
2  a hidden turn is never fed to the miner or the consolidation summariser during ``sleep``
3  a procedural lesson goes through the door under ``write.lineage_table`` and is hidden when the
   step it came from is erased; flag off or no parent: as before
4  a consolidation summary gets lineage edges (and the layer stamp) under the flag, none without
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from memspine import Engine
from memspine.derive.pipeline.stages.context import live_record, live_records
from memspine.prompts.models import ExtractedFact
from memspine.store import lineage as lineage_store
from memspine.store.records import MemoryRecord, SourceInfo
from memspine.trust.erasure_plan import ErasureState

NS = "u"
T0 = datetime(2024, 3, 1, 9, 0, tzinfo=UTC)
MARK = "Quillfeather"


def _engine(**over: Any) -> Engine:
    cfg: dict[str, Any] = {
        "template": "core",
        "dotenv_path": None,
        "storage": {
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        "embedding": {"provider": "hash", "settings": {}},
        "read": {"hybrid": False, "record_access": False},
        "memories": {
            "episodic": {"enabled": True, "policies": {"consolidation": {"mine_facts": True}}},
            "semantic": {"enabled": True},
            "procedural": {"enabled": True},
        },
    }
    cfg.update(over)
    return Engine(**cfg)


async def _turns(eng: Engine) -> list[MemoryRecord]:
    texts = [
        f"Melanie: I keep a hive named {MARK} behind the mill",
        "Caroline: that sounds lovely",
        "Melanie: my sister plays the cello",
    ]
    for i, text in enumerate(texts):
        await eng.write(
            text,
            namespace=NS,
            memory_type="episodic",
            source=SourceInfo(role="user", channel="message"),
            session_id="s1",
            group_id="s1",
            valid_from=T0 + timedelta(minutes=i),
        )
    recs = await eng._require_started().list_records(NS, "episodic")
    return sorted(recs, key=lambda r: r.valid_from or T0)


async def _parents(eng: Engine, record_id: str) -> list[str]:
    storage = eng._require_started()
    found = await lineage_store.parents_of(storage._client.engine, [record_id])
    return [e.parent for e in found.get(record_id, [])]


def _hide(eng: Engine, *, hidden: tuple[str, ...] = (), erased: tuple[str, ...] = ()) -> None:
    eng.__dict__["_erasure_state"] = ErasureState(frozenset(erased), frozenset(hidden), 1)


async def test_gate_filters_erased_hidden_quarantined_and_is_identity_when_off() -> None:
    eng = _engine(privacy={"erasure_v2": True})
    await eng.start()
    try:
        a, b, c = await _turns(eng)
        ctx = eng._pipeline_ctx()
        assert ctx.live_gate is not None
        assert len(await live_records(ctx, NS, "episodic")) == 3
        _hide(eng, hidden=(a.record_id,), erased=(b.record_id,))
        live = await live_records(ctx, NS, "episodic")
        assert [r.record_id for r in live] == [c.record_id]
        assert await live_record(ctx, a.record_id) is None
        assert await live_record(ctx, b.record_id) is None
        assert (await live_record(ctx, c.record_id)) is not None
        assert await live_record(ctx, "missing") is None
        q = c.model_copy(update={"quarantined": True})
        assert await ctx.live_gate([q]) == []
    finally:
        await eng.stop()

    off = _engine()
    await off.start()
    try:
        a, b, c = await _turns(off)
        _hide(off, hidden=(a.record_id,))  # no flag: no gate, nothing changes
        ctx = off._pipeline_ctx()
        assert ctx.live_gate is None
        assert len(await live_records(ctx, NS, "episodic")) == 3
    finally:
        await off.stop()


async def test_do_not_derive_subject_is_dropped_under_consent_v2() -> None:
    eng = _engine(consent={"v2": True})
    await eng.start()
    try:
        await _turns(eng)
        ctx = eng._pipeline_ctx()
        assert ctx.live_gate is not None
        await eng.consent_do_not_derive(NS, "Melanie")
        rec = MemoryRecord(namespace=NS, memory_type="semantic", content="x", entity="Melanie")
        other = MemoryRecord(namespace=NS, memory_type="semantic", content="y", entity="Ilse")
        kept = await ctx.live_gate([rec, other])
        assert [r.record_id for r in kept] == [other.record_id]
    finally:
        await eng.stop()


async def test_hidden_turn_never_reaches_miner_or_summariser(monkeypatch: Any) -> None:
    mined: list[str] = []
    summarised: list[str] = []

    async def mine(text: str) -> list[ExtractedFact]:
        mined.append(text)
        return []

    async def summarize(text: str) -> str:
        summarised.append(text)
        return "Summary: " + text.replace("\n", "; ")

    eng = _engine(privacy={"erasure_v2": True})
    monkeypatch.setattr(eng, "_build_fact_miner", lambda: mine)
    await eng.start()
    try:
        a, *_ = await _turns(eng)
        await eng.write(
            "Caroline: the dog needs a walk",
            namespace=NS,
            memory_type="episodic",
            source=SourceInfo(role="user", channel="message"),
            session_id="s1",
            group_id="s1",
            valid_from=T0 + timedelta(minutes=9),
        )
        eng._summarize = summarize
        _hide(eng, hidden=(a.record_id,))
        await eng.sleep()
        assert mined or summarised  # the stages did run on the visible turns
        assert all(MARK not in t for t in [*mined, *summarised])
    finally:
        await eng.stop()


async def test_lesson_goes_through_the_door_and_follows_erasure() -> None:
    eng = _engine(write={"lineage_table": True}, privacy={"erasure_v2": True})
    await eng.start()
    try:
        step = await eng.write("clicked search, page timed out", memory_type="episodic")
        lesson = await eng.record_outcome(
            "book a flight to Oslo",
            "failure",
            error="TimeoutError",
            derived_from=[step.record_id],
        )
        assert lesson is not None
        storage = eng._require_started()
        assert await _parents(eng, lesson.record_id) == [step.record_id]
        stored = await storage.get_record(lesson.record_id)
        assert stored is not None and stored.layer
        await eng.forget(step.record_id, namespace="default", hard=True)
        assert eng.erasure_status()["hidden"] >= 1
        assert lesson.record_id in {i["record_id"] for i in eng.erasure_queue()}
    finally:
        await eng.stop()


async def test_lesson_flag_off_and_no_parent_unchanged() -> None:
    off = _engine()
    await off.start()
    try:
        step = await off.write("a step", memory_type="episodic")
        lesson = await off.record_outcome("t", "failure", derived_from=[step.record_id])
        assert lesson is not None
        assert await _parents(off, lesson.record_id) == []
    finally:
        await off.stop()

    on = _engine(write={"lineage_table": True})
    await on.start()
    try:  # no parent: written as before, never refused
        lesson = await on.record_outcome("t2", "failure")
        assert lesson is not None
    finally:
        await on.stop()


async def test_consolidation_summary_gets_lineage_only_under_the_flag() -> None:
    for flag in (True, False):
        eng = _engine(write={"lineage_table": flag})
        await eng.start()
        try:
            turns = await _turns(eng)
            await eng.sleep()
            storage = eng._require_started()
            summaries = [
                r
                for r in await storage.list_records(NS, "semantic")
                if r.source.channel == "consolidation"
            ]
            assert summaries
            ids = {t.record_id for t in turns}
            parents = await _parents(eng, summaries[0].record_id)
            if flag:
                assert parents and set(parents) <= ids
            else:
                assert parents == []
        finally:
            await eng.stop()
