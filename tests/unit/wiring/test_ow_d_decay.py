"""OW-D / O4-09: activation tiers and multi-resolution compression (``offline.decay_v2``), model-free."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema.observability import TapConfig
from memspine.derive.pipeline.stages import decay_v2
from memspine.derive.pipeline.stages.maintenance import compress, decay_sweep
from memspine.observability import tap
from memspine.store import lineage as lineage_store
from memspine.store.events import EventKind
from memspine.store.records import MemoryRecord, RecordStatus

TOPICS = ["roof repairs", "garden lighting", "bike storage", "street cleaning", "water meters"]


def _long(i: int) -> str:
    return " ".join(
        f"Meeting {i} reviewed {topic} number {j * 7 + i} and the committee approved the plan."
        for j, topic in enumerate(TOPICS)
    )


@pytest.fixture(autouse=True)
def _tap_reset():
    tap.shutdown()
    yield
    tap.shutdown()


def _engine(*, v2: bool, lineage: bool = False, **offline: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        embedding={"provider": "hash", "settings": {}},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
        read={"record_access": False},
        write={"lineage_table": lineage},
        offline={"decay_v2": v2, **offline},
    )


def _at(monkeypatch: pytest.MonkeyPatch, days: float) -> None:
    now = datetime.now(UTC) + timedelta(days=days)
    monkeypatch.setattr(decay_v2, "_now", lambda: now)


async def _tiers(eng: Engine, ns: str = "a") -> dict[str, str]:
    store = eng._require_started()
    return {
        r.content: r.tier
        for t in ("episodic", "semantic")
        for r in await store.list_records(ns, t)
        if r.source.channel != "multires"
    }


async def _transition_reasons(eng: Engine) -> list[str]:
    events = await eng._require_started().read_events(0, 10000)
    return [str(e.payload.get("reason")) for e in events if e.kind is EventKind.DECAY_TRANSITION]


async def test_flag_off_keeps_the_idle_day_tiers() -> None:
    eng = _engine(v2=False)
    await eng.start()
    try:
        await eng.write("a plain turn of talk", namespace="a", memory_type="episodic")
        stats = await decay_sweep(eng._pipeline_ctx())
        assert stats == {"status": "ok", "transitions": 0}  # fresh: still hot
        assert await _transition_reasons(eng) == []
    finally:
        await eng.stop()


async def test_idle_records_go_cold_once_and_protected_ones_stay_hot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine(v2=True, decay_protected_kinds=["semantic"])
    await eng.start()
    try:
        await eng.write("an idle episodic turn", namespace="a", memory_type="episodic")
        await eng.write(
            "a pinned episodic turn", namespace="a", memory_type="episodic", tags=["pinned"]
        )
        await eng.write("a protected kind fact", namespace="a", memory_type="semantic")
        _at(monkeypatch, 45)
        ctx = eng._pipeline_ctx()
        stats = await decay_sweep(ctx)
        assert stats["status"] == "ok" and stats["transitions"] == 1
        tiers = await _tiers(eng)
        assert tiers == {
            "an idle episodic turn": "dormant",
            "a pinned episodic turn": "hot",
            "a protected kind fact": "hot",
        }
        again = await decay_sweep(ctx)
        assert again["transitions"] == 0  # idempotent
        assert await _transition_reasons(eng) == ["idle"]
    finally:
        await eng.stop()


async def test_reactivation_climbs_with_the_margin(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(v2=True)
    await eng.start()
    try:
        await eng.write("a turn that gets used again", namespace="a", memory_type="episodic")
        ctx = eng._pipeline_ctx()
        _at(monkeypatch, 45)
        await decay_sweep(ctx)
        assert set((await _tiers(eng)).values()) == {"dormant"}
        _at(
            monkeypatch, 5
        )  # activation back at about 0.51: above 0.30 + margin, under 0.50 + margin
        await decay_sweep(ctx)
        assert set((await _tiers(eng)).values()) == {"warm"}
        assert (await _transition_reasons(eng))[-1] == "reactivated"
    finally:
        await eng.stop()


async def test_nothing_is_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(v2=True)
    await eng.start()
    try:
        for i in range(3):
            await eng.write(f"turn number {i} of the chat", namespace="a", memory_type="episodic")
        _at(monkeypatch, 400)
        await decay_sweep(eng._pipeline_ctx())
        recs = await eng._require_started().list_records("a", "episodic")
        assert len(recs) == 3 and all(r.status is RecordStatus.ACTIVATED for r in recs)
    finally:
        await eng.stop()


async def _cold_group(eng: Engine, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ids = []
    for i in range(3):
        rec = await eng.write(_long(i), namespace="a", memory_type="episodic", group_id="g1")
        ids.append(rec.record_id)
    _at(monkeypatch, 400)
    ctx = eng._pipeline_ctx()
    await decay_sweep(ctx)
    return ids


def _multires(records: list[MemoryRecord]) -> list[MemoryRecord]:
    return [
        r for r in records if r.source.channel == "multires" and r.status is RecordStatus.ACTIVATED
    ]


async def test_multires_runs_up_to_the_cap_and_keeps_the_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine(v2=True, lineage=True, decay_multires_jobs=1)
    await eng.start()
    calls: list[str] = []

    async def summarize(text: str) -> str:
        calls.append(text)
        return "The committee approved eight budget items. Plans were approved."

    try:
        ids = await _cold_group(eng, monkeypatch)
        ctx = eng._pipeline_ctx()
        ctx.summarize = summarize
        stats = await compress(ctx)
        assert stats["multires_planned"] == 1 and stats["multires_run"] == 1 and len(calls) == 1
        store = eng._require_started()
        made = _multires(await store.list_records("a", "semantic"))
        levels = sorted(next(t for t in r.tags if t.startswith("res:")) for r in made)
        assert levels == ["res:keyword", "res:paragraph", "res:sentence"]
        for rec in made:
            assert sorted(rec.source.parents) == sorted(ids)
        edges = await lineage_store.parents_of(store._client.engine, [made[0].record_id])
        assert {e.parent for e in edges[made[0].record_id]} == set(ids)
        # Members are kept, only their retrieval tier is lowered.
        members = [r for r in await store.list_records("a", "episodic") if r.record_id in ids]
        assert len(members) == 3 and all(r.status is RecordStatus.ACTIVATED for r in members)
        assert all(r.tier == "dormant" for r in members)
        # Covered now: the next sleep plans nothing new and makes no call.
        again = await compress(ctx)
        assert again["multires_run"] == 0 and len(calls) == 1
        assert len(_multires(await store.list_records("a", "semantic"))) == 3
    finally:
        await eng.stop()


async def test_cap_zero_plans_and_reports_only(monkeypatch: pytest.MonkeyPatch) -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    eng = _engine(v2=True)
    await eng.start()
    calls = 0

    async def summarize(text: str) -> str:
        nonlocal calls
        calls += 1
        return "x"

    try:
        await _cold_group(eng, monkeypatch)
        ctx = eng._pipeline_ctx()
        ctx.summarize = summarize
        with tap.request("job", stage="compress"):
            stats = await compress(ctx)
        assert stats["multires_planned"] == 1 and stats["multires_run"] == 0 and calls == 0
        assert _multires(await eng._require_started().list_records("a", "semantic")) == []
    finally:
        await eng.stop()
    events = [
        e for t in tap.memory_traces() for e in t["events"] if e["stage"] == "job.decay.compress"
    ]
    assert (
        events
        and events[0]["fields"]["jobs_planned"] == 1
        and events[0]["fields"]["jobs_over_cap"] == 1
    )


async def test_without_a_model_the_levels_are_extractive(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(v2=True, decay_multires_jobs=1)
    await eng.start()
    try:
        await _cold_group(eng, monkeypatch)
        ctx = eng._pipeline_ctx()
        ctx.summarize = None
        stats = await compress(ctx)
        assert stats["multires_run"] == 1
        made = _multires(await eng._require_started().list_records("a", "semantic"))
        para = next(r for r in made if "res:paragraph" in r.tags)
        assert "committee" in para.content and len(para.content) <= 200 * 4
    finally:
        await eng.stop()


async def test_tap_reports_tier_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    eng = _engine(v2=True)
    await eng.start()
    try:
        await eng.write("an idle episodic turn", namespace="a", memory_type="episodic")
        await eng.write(
            "a pinned episodic turn", namespace="a", memory_type="episodic", tags=["pinned"]
        )
        _at(monkeypatch, 45)
        with tap.request("job", stage="decay_sweep"):
            await decay_sweep(eng._pipeline_ctx())
    finally:
        await eng.stop()
    events = [e for t in tap.memory_traces() for e in t["events"] if e["stage"] == "job.decay"]
    assert len(events) == 1
    f = events[0]["fields"]
    assert f["records"] == 2 and f["protected"] == 1 and f["moves"] == 1 and f["down"] == 1
    assert f["by_move"] == {"hot_to_dormant": 1}
    assert not any(
        "field_rejected" in e["fields"] for t in tap.memory_traces() for e in t["events"]
    )
