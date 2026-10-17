"""OW-C wiring: O4-05 pattern observations (offline.patterns_v2), O4-08 standing answers
(offline.standing_v2 sleep side, read.standing_v2 read side). Model-free; hash embedder."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memspine import Engine
from memspine.config.schema import MemspineConfig, OfflineConfig, ReadConfig
from memspine.config.schema.observability import TapConfig
from memspine.derive.pipeline.stages.offline_common import fact_envelope
from memspine.derive.pipeline.stages.patterns import render_observation
from memspine.derive.pipeline.stages.standing import HIT_LOG, answer_from_record, standing_block
from memspine.observability import tap
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.workers.schedule import SLEEP_CYCLE_ORDER

NS = "u"
T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


def _engine(path: Path | str = ":memory:", **kw: Any) -> Engine:
    offline = kw.pop("offline", {})
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": str(path)},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={"lineage_table": kw.pop("lineage", False)},
        offline=offline,
        read={"record_access": False, **kw.pop("read", {})},
    )


async def _turn(eng: Engine, text: str, when: datetime, role: str = "user") -> MemoryRecord:
    await eng.ingest_messages(
        [{"role": role, "content": text}],
        namespace=NS,
        session_id=f"s{when:%m%d}",
        valid_from=when,
    )
    turns = await eng._require_started().list_records(NS, "episodic")
    return next(t for t in turns if t.content == text and t.valid_from == when)


async def _fact(
    eng: Engine, entity: str, attribute: str, value: str, when: datetime, kind: str, session: str
) -> MemoryRecord:
    turn = await _turn(eng, f"{entity} {attribute} {value} {when:%Y%m%d}", when)
    stored = await eng._deposit_mined_fact(
        NS,
        f"{entity} {attribute}: {value}",
        entity,
        attribute,
        [turn.record_id],
        when,
        session,
        kind=kind,
    )
    assert stored is not None
    return stored


async def _weekly(eng: Engine, n: int = 4) -> list[MemoryRecord]:
    # distinct wording: identical repeated sentences would be merged by the write-time dedup (M5)
    things = [
        "bouldering session",
        "lead wall routes",
        "hangboard training",
        "outdoor crag trip",
        "slab technique class",
        "overhang problems",
    ]
    return [
        await _fact(eng, "Ana", "climbing", things[i], T0 + timedelta(days=7 * i), "event", f"s{i}")
        for i in range(n)
    ]


async def _list(eng: Engine, channel: str, live: bool = True) -> list[MemoryRecord]:
    rows = await eng._require_started().list_records(NS, "semantic")
    return [
        r
        for r in rows
        if r.source.channel == channel and (not live or r.status is RecordStatus.ACTIVATED)
    ]


def _tap_events() -> list[dict[str, Any]]:
    """Every tap event of the memory sink, including those of the sleep stages' child traces."""
    out: list[dict[str, Any]] = []

    def walk(trace: dict[str, Any]) -> None:
        out.extend(trace["events"])
        for child in trace.get("children", []):
            walk(child)

    for t in tap.memory_traces():
        walk(t)
    return out


# ----------------------------------------------------------------- config / schedule


def test_flags_default_off_and_order_unchanged() -> None:
    cfg = OfflineConfig()
    assert cfg.patterns_v2 is False and cfg.standing_v2 is False
    assert ReadConfig().standing_v2 is False
    assert MemspineConfig().offline == cfg
    assert "pattern_observations" not in SLEEP_CYCLE_ORDER
    assert "standing_answers" not in SLEEP_CYCLE_ORDER


async def test_sleep_order_inserts_the_stages_only_when_on() -> None:
    eng = _engine(offline={"patterns_v2": True, "standing_v2": True})
    await eng.start()
    try:
        keys = list(await eng.sleep())
        # FX-EVAL: after the graph stages, so a single build sleep sees the graph facts
        assert keys.index("extract_graph") < keys.index("pattern_observations")
        assert keys.index("pattern_observations") < keys.index("standing_answers")
        assert keys.index("standing_answers") < keys.index("summarize_entities")
    finally:
        await eng.stop()
    off = _engine()
    await off.start()
    try:
        stats = await off.sleep()
        assert "pattern_observations" not in stats and "standing_answers" not in stats
    finally:
        await off.stop()


# ----------------------------------------------------------------- O4-05


async def test_patterns_off_writes_nothing() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _weekly(eng)
        assert "pattern_observations" not in await eng.sleep()
        assert await _list(eng, "observation", live=False) == []
    finally:
        await eng.stop()


def test_adapter_reads_event_attribute_and_preference_kind() -> None:
    rec = MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        content="Ana likes: sushi",
        entity="Ana",
        attribute="likes",
        valid_from=T0,
        tags=["atomic_fact", "mined:s1"],
    )
    env = fact_envelope(rec)
    assert env is not None
    assert env.kind == "like" and env.attribute == "likes" and env.value == "sushi"
    assert env.provenance.session_id == "s1"
    event = rec.model_copy(update={"attribute": None, "content": "Ana climbing: at the gym"})
    env2 = fact_envelope(event)
    assert env2 is not None and env2.attribute == "climbing" and env2.value == "at the gym"


async def test_patterns_on_writes_a_recurrence_with_lineage_and_trust_cap(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    eng = _engine(path, lineage=True, offline={"patterns_v2": True})
    tap.configure(TapConfig(enabled=True, sink="memory"))
    await eng.start()
    try:
        facts = await _weekly(eng)
        low = facts[1].model_copy(update={"trust": 0.3})
        await eng._require_started().upsert_record(low)
        stats = await eng.sleep()
        s = stats["pattern_observations"]
        assert s["status"] == "ok" and s["observations"] >= 1
        obs = await _list(eng, "observation")
        rec = next(o for o in obs if "observation:recurrence" in o.tags)
        assert "weekly" in rec.content and "climbing" in rec.content
        assert set(rec.source.parents) == {f.record_id for f in facts}
        assert rec.trust <= 0.3 + 1e-9  # capped at the least-trusted parent
        assert rec.layer == "L4"
        with sqlite3.connect(path) as db:
            rows = db.execute(
                "SELECT parent_id FROM lineage WHERE child_id = ?", (rec.record_id,)
            ).fetchall()
        assert {r[0] for r in rows} == {f.record_id for f in facts}
        again = await eng.sleep()
        assert again["pattern_observations"]["observations"] == 0  # idempotent
        names = [e["stage"] for e in _tap_events()]
        assert "job.patterns" in names
    finally:
        tap.shutdown()
        await eng.stop()


async def test_changed_support_retires_the_observation() -> None:
    eng = _engine(offline={"patterns_v2": True})
    await eng.start()
    try:
        facts = await _weekly(eng, 3)
        await eng.sleep()
        live = [o for o in await _list(eng, "observation") if "observation:recurrence" in o.tags]
        assert len(live) == 1
        storage = eng._require_started()
        await storage.upsert_record(facts[2].model_copy(update={"status": RecordStatus.ARCHIVED}))
        out = (await eng.sleep())["pattern_observations"]
        assert out["invalidated"] >= 1 and out["retired"] >= 1
        assert await _list(eng, "observation") == []
    finally:
        await eng.stop()


async def test_observations_are_not_inputs_of_the_next_run() -> None:
    eng = _engine(offline={"patterns_v2": True, "patterns_kinds": ["recurrence"]})
    await eng.start()
    try:
        await _weekly(eng, 5)
        await eng.sleep()
        before = await _list(eng, "observation")
        await eng.sleep()
        after = await _list(eng, "observation")
        assert {r.record_id for r in before} == {r.record_id for r in after}
    finally:
        await eng.stop()


def test_render_observation_is_plain() -> None:
    from memspine.derive.patterns import Observation

    obs = Observation(
        observation_id="obs_x",
        kind="trend",
        group_key="g",
        statement={
            "subject": "Ana",
            "predicate": "weight",
            "direction": "rising",
            "points": 3,
            "first_value": 60.0,
            "last_value": 64.0,
        },
        support_ids=("a",),
        count=3,
        restated=0,
        independent_sources=1,
        first_seen=T0,
        last_seen=T0,
        confidence=0.5,
        trust=0.5,
        members_fp="f",
    )
    assert render_observation(obs) == "Ana weight: rising from 60.0 to 64.0 over 3 dated points."


# ----------------------------------------------------------------- O4-08


async def _city(eng: Engine, value: str, day: int, session: str) -> MemoryRecord:
    return await _fact(eng, "Ana", "city", value, T0 + timedelta(days=day), "state", session)


async def test_standing_off_writes_nothing() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _city(eng, "Lisbon", 0, "s0")
        assert "standing_answers" not in await eng.sleep()
        assert await _list(eng, "standing", live=False) == []
    finally:
        await eng.stop()


async def test_standing_build_refresh_and_stale_skip(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    eng = _engine(path, lineage=True, offline={"standing_v2": True}, read={"standing_v2": True})
    tap.configure(TapConfig(enabled=True, sink="memory"))
    HIT_LOG.clear()
    await eng.start()
    try:
        first = await _city(eng, "Lisbon", 0, "s0")
        s = (await eng.sleep())["standing_answers"]
        assert s["built"] == 1
        std = await _list(eng, "standing")
        assert len(std) == 1 and std[0].layer == "L5"
        ans = answer_from_record(std[0])
        assert ans is not None and ans.values == ("Lisbon",)
        assert ans.support_ids == (first.record_id,)
        with sqlite3.connect(path) as db:
            n = db.execute(
                "SELECT COUNT(*) FROM lineage WHERE child_id = ?", (std[0].record_id,)
            ).fetchone()[0]
        assert n == 1
        assert (await eng.sleep())["standing_answers"]["built"] == 0  # unchanged: nothing rebuilt

        hit = (first, 1.0)
        block = await standing_block(eng, NS, [hit])
        assert block is not None and "Lisbon" in block.content
        # a newer fact makes it stale: never served, the live evidence is used
        await _city(eng, "Porto", 10, "s1")
        assert await standing_block(eng, NS, [hit]) is None
        s2 = (await eng.sleep())["standing_answers"]
        assert s2["stale"] == 1 and s2["built"] == 1
        std2 = await _list(eng, "standing")
        assert len(std2) == 1 and "Porto" in std2[0].content
        block2 = await standing_block(eng, NS, [hit])
        assert block2 is not None and "Porto" in block2.content
        assert "Lisbon" not in block2.content
        names = {e["stage"] for e in _tap_events()}
        assert {"job.standing", "compose.standing"} <= names
    finally:
        tap.shutdown()
        await eng.stop()


async def test_standing_cap_limits_new_answers() -> None:
    eng = _engine(offline={"standing_v2": True, "standing_max_answers": 1})
    await eng.start()
    try:
        await _city(eng, "Lisbon", 0, "s0")
        await _fact(eng, "Ana", "job", "nurse", T0 + timedelta(days=1), "state", "s1")
        s = (await eng.sleep())["standing_answers"]
        assert s["built"] == 1  # room for one
        assert len(await _list(eng, "standing")) == 1
    finally:
        await eng.stop()


async def test_guarded_instructions_only_firm_user_text() -> None:
    eng = _engine(read={"standing_v2": True}, offline={"standing_v2": True})
    await eng.start()
    try:
        fact = await _city(eng, "Lisbon", 0, "s0")
        await _turn(eng, "From now on, always answer in Dutch.", T0 + timedelta(days=2))
        await _turn(
            eng, "From now on, always answer in French.", T0 + timedelta(days=3), role="assistant"
        )
        block = await standing_block(eng, NS, [(fact, 1.0)])
        assert block is not None
        assert "Dutch" in block.content and "French" not in block.content
        assert "STANDING INSTRUCTIONS" in block.content
    finally:
        await eng.stop()
