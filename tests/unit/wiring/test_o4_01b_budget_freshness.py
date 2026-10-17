"""O4-01b wiring: background budget (offline.budget_v2) and read freshness (read.freshness_v2,
read.freshness_fallback). Model-free: hash embedder, fake stage runners."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memspine import Engine
from memspine.config.schema import MemspineConfig, OfflineConfig, ReadConfig
from memspine.config.schema.observability import TapConfig
from memspine.derive.budget import (
    COVERS_SEQ,
    DerivationBudget,
    Spend,
    UsageFeed,
    is_model_stage,
    stage_usage,
)
from memspine.derive.job import LeaseStore, NamespaceJob
from memspine.observability import tap
from memspine.observability.trace import search_forensics
from memspine.read import freshness as fresh
from memspine.read.pipeline import freshness as read_freshness
from memspine.store.records import MemoryRecord, RecordFingerprint

NS = "u"
T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)
STAGES = ["consolidate", "mine_facts", "decay_sweep", "reflect_profile", "summarize_entities"]


# ----------------------------------------------------------------------------- helpers


def _tap_events() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(trace: dict[str, Any]) -> None:
        out.extend(trace["events"])
        for child in trace.get("children", []):
            walk(child)

    for t in tap.memory_traces():
        walk(t)
    return out


class Runner:
    """A fake stage runner: every model stage reports one call; records what ran, in order."""

    def __init__(self) -> None:
        self.ran: list[str] = []

    async def __call__(self, stage: str) -> dict[str, object]:
        self.ran.append(stage)
        return {"status": "ok", "llm_calls": 1 if is_model_stage(stage) else 0}


def _job(
    store: LeaseStore,
    runner: Runner,
    budget: DerivationBudget | None,
    ns: str = NS,
    job_id: str = "j",
) -> NamespaceJob:
    return NamespaceJob(
        store, ns, STAGES, runner, owner="o", job_id=job_id, budget=budget, head_seq=lambda: 7
    )


def _engine(path: Path | str = ":memory:", **kw: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={'provider': "sqlite", 'settings': {'path': str(path), 'busy_timeout_s': 5.0}},
        embedding={'provider': "hash", 'settings': {}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={"lineage_table": kw.pop("lineage", False)},
        offline=kw.pop("offline", {}),
        read={"record_access": False, **kw.pop("read", {})},
    )


async def _turn(eng: Engine, text: str, when: datetime) -> MemoryRecord:
    await eng.ingest_messages(
        [{"role": "user", "content": text}],
        namespace=NS,
        session_id=f"s{when:%m%d}",
        valid_from=when,
    )
    turns = await eng._require_started().list_records(NS, "episodic")
    return next(t for t in turns if t.content == text and t.valid_from == when)


async def _weekly(eng: Engine, n: int = 4) -> list[MemoryRecord]:
    things = ["bouldering session", "lead wall routes", "hangboard training", "outdoor crag trip"]
    out = []
    for i in range(n):
        when = T0 + timedelta(days=7 * i)
        turn = await _turn(eng, f"Ana climbing {things[i]} {when:%Y%m%d}", when)
        stored = await eng._deposit_mined_fact(
            NS, f"Ana climbing: {things[i]}", "Ana", "climbing", [turn.record_id], when, f"s{i}",
            kind="event",
        )  # fmt: skip
        assert stored is not None
        out.append(stored)
    return out


async def _observations(eng: Engine) -> list[MemoryRecord]:
    rows = await eng._require_started().list_records(NS, "semantic")
    return [r for r in rows if r.source.channel == "observation"]


# ----------------------------------------------------------------------------- flags and config


def test_flags_default_off() -> None:
    off, rd = OfflineConfig(), ReadConfig()
    assert off.budget_v2 is False
    assert off.budget_calls_per_cycle is None and off.budget_tokens_per_cycle is None
    assert rd.freshness_v2 is False and rd.freshness_fallback is False
    assert rd.freshness_stale_gap == 20 and rd.freshness_fallback_max == 20
    assert MemspineConfig(runtime={"provider_services": {"health_timeout_s": 1.0, "close_timeout_s": 1.0, "startup_timeout_s": None}}).offline == off


def test_covers_seq_is_not_dumped_while_unset() -> None:
    assert "covers_seq" not in RecordFingerprint().model_dump()
    assert RecordFingerprint(covers_seq=5).model_dump()["covers_seq"] == 5
    plain = MemoryRecord(namespace=NS, memory_type="episodic", content="x")
    assert "covers_seq" not in plain.model_dump(mode="json").get("fingerprint", {})
    stamped = plain.model_copy(update={"fingerprint": RecordFingerprint(covers_seq=9)})
    again = MemoryRecord.model_validate(stamped.model_dump(mode="json"))
    assert again.fingerprint.covers_seq == 9


# ----------------------------------------------------------------------------- the budget object


def test_stage_usage_reads_calls_and_tokens() -> None:
    assert stage_usage({"llm_calls": 2, "resolve_llm_calls": 1, "tokens": 30}) == Spend(3, 30)
    assert stage_usage({"prompt_tokens": 10, "completion_tokens": 5, "ok": True}) == Spend(0, 15)
    assert stage_usage({"status": "ok"}) == Spend()


def test_budget_exhausts_on_either_dimension_and_resets_per_cycle() -> None:
    b = DerivationBudget(max_calls=2, max_tokens=100)
    b.begin(NS)
    assert not b.exhausted(NS)
    b.charge(NS, {"llm_calls": 1, "tokens": 40})
    assert not b.exhausted(NS)
    b.charge(NS, {"llm_calls": 1})
    assert b.exhausted(NS)  # calls
    b.begin(NS)
    assert not b.exhausted(NS) and b.spent(NS) == Spend()
    b.charge(NS, {"tokens": 100})
    assert b.exhausted(NS)  # tokens
    b.begin("other")
    assert not b.exhausted("other")  # per namespace
    assert not DerivationBudget().limited


def test_budget_can_be_fed_by_a_ledger() -> None:
    class Ledger:
        def __init__(self) -> None:
            self.total = Spend(5, 500)  # spent before this cycle

        def spent(self, ns: str) -> Spend:
            return self.total

    ledger = Ledger()
    assert isinstance(ledger, UsageFeed)
    b = DerivationBudget(max_calls=3, feed=ledger)
    b.begin(NS)
    ledger.total = Spend(7, 900)
    b.charge(NS, {"llm_calls": 99})  # stats are ignored when a feed is present
    assert b.spent(NS) == Spend(2, 400) and not b.exhausted(NS)
    ledger.total = Spend(8, 950)
    b.charge(NS, {})
    assert b.exhausted(NS)


# ----------------------------------------------------------------------------- the job


async def test_flag_off_job_is_unchanged() -> None:
    store, runner = LeaseStore(), Runner()
    out = await _job(store, runner, None).run()
    assert runner.ran == STAGES and out.state == "succeeded" and out.deferred == []
    assert store.deferred(NS) == {}


async def test_exhaustion_defers_the_rest_counts_it_and_the_next_cycle_drains_first() -> None:
    store, runner = LeaseStore(), Runner()
    budget = DerivationBudget(max_calls=2)
    out = await _job(store, runner, budget).run()
    # two model stages spend the cap; the free stage still runs; the rest wait
    assert runner.ran == ["consolidate", "mine_facts", "decay_sweep"]
    assert out.deferred == ["reflect_profile", "summarize_entities"]
    assert out.stats["reflect_profile"] == {"status": "deferred", "reason": "budget"}
    assert out.state == "succeeded"
    assert store.deferred(NS) == {
        "reflect_profile": {"pending": 1, "total": 1, "last_cycle": 1},
        "summarize_entities": {"pending": 1, "total": 1, "last_cycle": 1},
    }
    # deferred units count toward the trigger N
    assert store.state(NS)["deposits_since_run"] == 2

    runner.ran.clear()
    out2 = await _job(store, runner, DerivationBudget(max_calls=2), job_id="j2").run()
    assert runner.ran[:2] == ["reflect_profile", "summarize_entities"]  # drained first
    assert out2.deferred == ["consolidate", "mine_facts"]  # the cap now pushes these back
    pend = {s: d["pending"] for s, d in store.deferred(NS).items()}
    assert pend["reflect_profile"] == 0 and pend["summarize_entities"] == 0
    assert pend["consolidate"] == 1 and pend["mine_facts"] == 1

    runner.ran.clear()
    out3 = await _job(store, runner, DerivationBudget(max_calls=10), job_id="j3").run()
    assert out3.deferred == []
    assert runner.ran[:2] == ["consolidate", "mine_facts"]
    assert {d["pending"] for d in store.deferred(NS).values()} == {0}
    totals = {s: d["total"] for s, d in store.deferred(NS).items()}
    assert totals == {
        "reflect_profile": 1,
        "summarize_entities": 1,
        "consolidate": 1,
        "mine_facts": 1,
    }


async def test_a_zero_budget_defers_every_model_stage_repeatedly() -> None:
    store, runner = LeaseStore(), Runner()
    for n in (1, 2, 3):
        out = await _job(store, runner, DerivationBudget(max_calls=0), job_id=f"j{n}").run()
        assert out.deferred == [
            "consolidate",
            "mine_facts",
            "reflect_profile",
            "summarize_entities",
        ]
        assert store.deferred(NS)["consolidate"]["total"] == n
        assert store.deferred(NS)["consolidate"]["pending"] == n
    assert set(runner.ran) == {"decay_sweep"}  # only the free stage ran


async def test_the_budget_is_per_namespace() -> None:
    store, runner = LeaseStore(), Runner()
    budget = DerivationBudget(max_calls=1)
    a = await _job(store, runner, budget, ns="a", job_id="ja").run()
    b = await _job(store, runner, budget, ns="b", job_id="jb").run()
    assert a.deferred == b.deferred and len(a.deferred) == 3


async def test_deferred_counts_survive_a_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "jobs.sleep")
    first = LeaseStore(path)
    await _job(first, Runner(), DerivationBudget(max_calls=1)).run()
    before = first.deferred(NS)
    first.close()
    second = LeaseStore(path)  # a new process
    assert second.deferred(NS) == before and before
    runner = Runner()
    out = await _job(second, runner, DerivationBudget(max_calls=10), job_id="j2").run()
    assert out.deferred == []
    assert runner.ran[:3] == [s for s in STAGES if s in before]
    assert {d["pending"] for d in second.deferred(NS).values()} == {0}


async def test_job_taps_budget_and_deferred() -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    try:
        with tap.request("job"):
            await _job(LeaseStore(), Runner(), DerivationBudget(max_calls=1)).run()
        events = _tap_events()
        budget = [e for e in events if e["stage"] == "job.budget"]
        deferred = [e for e in events if e["stage"] == "job.deferred"]
        assert budget and budget[0]["fields"]["calls"] == 1
        assert budget[0]["fields"]["exhausted"] is True
        assert len(deferred) == 3
        assert deferred[0]["fields"]["pending"] == 1 and deferred[0]["fields"]["total"] == 1
    finally:
        tap.shutdown()


# ----------------------------------------------------------------------------- the engine


async def test_engine_budget_defers_model_stages_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    off = {
        "sleep_v2": True,
        "sleep_manual_only": True,
        "budget_v2": True,
        "budget_calls_per_cycle": 0,
    }
    eng = _engine(path, offline=off)
    await eng.start()
    try:
        stats = await (await eng.sleep()).wait()
        assert stats["consolidate"]["status"] == "deferred"
        assert stats["decay_sweep"].get("status") != "deferred"
        held = eng._derive_sched.store.deferred("*")
        assert held["consolidate"]["total"] == 1
    finally:
        await eng.stop()
    again = _engine(path, offline=off)
    await again.start()
    try:
        assert again._derive_sched.store.deferred("*")["consolidate"]["total"] == 1
        stats = await (await again.sleep()).wait()
        assert again._derive_sched.store.deferred("*")["consolidate"]["total"] == 2
        assert stats["mine_facts"]["status"] == "deferred"
    finally:
        await again.stop()


async def test_engine_budget_flag_off_does_not_defer_or_stamp() -> None:
    eng = _engine(offline={"sleep_v2": True, "sleep_manual_only": True})
    await eng.start()
    try:
        assert eng._freshness_tracker is None and eng._derive_sched.budget is None
        stats = await (await eng.sleep()).wait()
        assert all(s.get("status") != "deferred" for s in stats.values())
        with pytest_raises_freshness():
            await eng.freshness(NS)
    finally:
        await eng.stop()


def pytest_raises_freshness() -> Any:
    import pytest

    from memspine.exceptions import MemspineError

    return pytest.raises(MemspineError)


# ----------------------------------------------------------------------------- covers_seq


async def test_covers_seq_is_stamped_on_derived_records() -> None:
    eng = _engine(offline={"budget_v2": True, "patterns_v2": True})
    await eng.start()
    try:
        await _weekly(eng)
        latest = eng._freshness_tracker.latest(NS)
        assert latest > 0
        await eng.sleep()
        obs = await _observations(eng)
        assert obs
        assert {o.fingerprint.covers_seq for o in obs} == {latest}
        # the stamp is stored with the record, not a runtime attribute
        stored = await eng._require_started().get_record(obs[0].record_id)
        assert stored is not None and stored.fingerprint.covers_seq == latest
        # raw turns and the directly deposited facts carry none
        turns = await eng._require_started().list_records(NS, "episodic")
        assert all(t.fingerprint.covers_seq is None for t in turns)
    finally:
        await eng.stop()


async def test_flag_off_derived_records_carry_no_covers_seq() -> None:
    eng = _engine(offline={"patterns_v2": True})
    await eng.start()
    try:
        await _weekly(eng)
        await eng.sleep()
        obs = await _observations(eng)
        assert obs and all(o.fingerprint.covers_seq is None for o in obs)
    finally:
        await eng.stop()


async def test_stamp_uses_the_snapshot_not_the_live_head() -> None:
    eng = _engine(offline={"budget_v2": True})
    await eng.start()
    try:
        token = COVERS_SEQ.set({NS: 41})
        try:
            written = await eng._deposit_mined_fact(
                NS, "Ana likes tea", "Ana", "drink", [], T0, "s0", kind="state"
            )
        finally:
            COVERS_SEQ.reset(token)
        assert written is not None and written.fingerprint.covers_seq == 41
        other = await eng._deposit_mined_fact(
            NS, "Ana likes jam", "Ana", "spread", [], T0, "s0", kind="state"
        )
        assert other is not None and other.fingerprint.covers_seq is None  # outside a derivation
    finally:
        await eng.stop()


# ------------------------------------------------------------------- the freshness library


def test_gap_is_the_latest_seq_minus_the_minimum_covers_seq() -> None:
    rep = fresh.build_report("n", 100, {"L1": [90, 80, 95], "L4": [10]}, threshold=20)
    by = {x.layer: x for x in rep.layers}
    assert by["L1"].min_covers_seq == 80 and by["L1"].gap == 20 and not by["L1"].stale
    assert by["L4"].gap == 90 and by["L4"].stale
    assert rep.stale and rep.stale_layers == ("L4",) and rep.stale_from == 10
    assert fresh.build_report("n", 100, {"L1": [81]}, 18).stale  # 19 > 18
    assert not fresh.build_report("n", 100, {"L1": [81]}, 19).stale  # 19 > 19 is false


def test_unstamped_records_are_counted_but_never_stale() -> None:
    layer = fresh.assess_layer("L2", [None, None], 500, 10)
    assert (layer.records, layer.unstamped, layer.gap, layer.stale) == (2, 2, None, False)
    mixed = fresh.assess_layer("L2", [None, 450], 500, 10)
    assert mixed.unstamped == 1 and mixed.gap == 50 and mixed.stale
    ahead = fresh.assess_layer("L2", [900], 500, 10)
    assert ahead.gap == 0 and not ahead.stale


def test_tracker_orders_bounds_and_lists_raw_turns_beyond_a_seq() -> None:
    t = fresh.SourceSeqTracker(cap=4)
    for seq in (3, 5, 9, 12, 20):
        t.note("n", seq, f"r{seq}")
    t.note("n", 9, "again")  # duplicate seq is ignored
    t.note("n", 7, "r7")  # out of order keeps the list ascending
    assert t.latest("n") == 20 and t.latest("none") == 0
    assert [s for s, _ in t.beyond("n", 0, 99)] == [20, 12, 9, 7]  # capped at 4, newest first
    assert t.beyond("n", 9, 99) == [(20, "r20"), (12, "r12")]
    assert t.beyond("n", 9, 1) == [(20, "r20")]
    assert t.count_beyond("n", 9) == 2
    assert t.snapshot() == {"n": 20}


# ----------------------------------------------------------------------------- the read side


async def _stale_engine(**read: Any) -> Engine:
    eng = _engine(
        lineage=True,  # the derived-write door sets the layer (L4 for observations)
        offline={"budget_v2": True, "patterns_v2": True},
        read={"freshness_v2": True, "freshness_stale_gap": 0, **read},
    )
    await eng.start()
    await _weekly(eng)
    await eng.sleep()  # stamps the observations at the current head
    return eng


async def test_freshness_report_gap_and_stale_flag() -> None:
    eng = await _stale_engine()
    try:
        first = await eng.freshness(NS)
        assert first["layers"]["L4"]["gap"] == 0 and first["stale"] is False
        covers = first["layers"]["L4"]["min_covers_seq"]
        newest = [
            (await _turn(eng, f"Bram moved to flat {i}", T0 + timedelta(days=60 + i)))
            for i in range(3)
        ]
        again = await eng.freshness(NS)
        latest = eng._freshness_tracker.latest(NS)
        assert latest > covers
        assert again["latest_seq"] == latest
        assert again["layers"]["L4"]["gap"] == latest - covers
        assert again["stale"] is True and again["stale_layers"] == ["L4"]
        assert len(newest) == 3
    finally:
        await eng.stop()


async def test_search_records_freshness_in_the_trace_without_changing_results() -> None:
    eng = await _stale_engine()
    try:
        await _turn(eng, "Noor started pottery lessons", T0 + timedelta(days=90))
        with search_forensics() as fx:
            on = await eng.search("pottery", namespace=NS, top_k=5)
        assert fx["freshness"]["stale"] is True and fx["freshness"]["fallback_added"] == 0
        # metadata only: the same ids come back in the same order on a repeat
        again = await eng.search("pottery", namespace=NS, top_k=5)
        assert [r.record_id for r, _ in on] == [r.record_id for r, _ in again]
    finally:
        await eng.stop()


async def test_stale_fallback_brings_in_the_raw_turns_beyond_covers_seq() -> None:
    eng = await _stale_engine(freshness_fallback=True, freshness_fallback_max=2)
    try:
        new = [
            await _turn(eng, f"Tomasz bought plant {i}", T0 + timedelta(days=70 + i))
            for i in range(3)
        ]
        rep = await read_freshness.report(eng, NS)
        assert rep.stale
        leg = read_freshness.fallback_leg(eng, NS, rep)
        # newest first, capped at freshness_fallback_max, only turns after covers_seq
        assert [h.record_id for h in leg] == [new[2].record_id, new[1].record_id]
        assert leg[0].score > leg[1].score
        with search_forensics() as fx:
            await eng.search("unrelated query about nothing", namespace=NS, top_k=5)
        assert fx["freshness"]["fallback_added"] == 2
        leg_ids = {
            h[0] if isinstance(h, tuple) else h.record_id
            for _, hits in fx["extra_legs"]
            for h in hits
        }
        assert {new[2].record_id, new[1].record_id} <= leg_ids
    finally:
        await eng.stop()


async def test_fallback_off_or_fresh_adds_nothing() -> None:
    eng = await _stale_engine()  # fallback off
    try:
        await _turn(eng, "Ilse adopted a cat", T0 + timedelta(days=80))
        assert await read_freshness.apply(eng, NS) == []
    finally:
        await eng.stop()
    eng = await _stale_engine(freshness_fallback=True)
    try:
        assert await read_freshness.apply(eng, NS) == []  # nothing newer than the stamp
    finally:
        await eng.stop()


async def test_read_freshness_tap_event() -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    eng = await _stale_engine(freshness_fallback=True)
    try:
        await _turn(eng, "Bram repainted the fence", T0 + timedelta(days=95))
        with tap.request("read"):
            await read_freshness.apply(eng, NS)
        ev = [e for e in _tap_events() if e["stage"] == "read.freshness"]
        assert ev and ev[-1]["fields"]["stale"] is True and ev[-1]["fields"]["fallback_added"] == 1
        assert ev[-1]["fields"]["stale_layers"] == ["L4"]
    finally:
        await eng.stop()
        tap.shutdown()
