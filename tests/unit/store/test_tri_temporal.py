"""F-02b: tri-temporal fields (source_at, event precision / basis), the 0008 migration, the
LayerRegistry, the timeview adapter's known_at, and the envelope time axes."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest
from alembic import command
from alembic.script import ScriptDirectory

from memspine import Engine
from memspine.config.schema.root import MemspineConfig
from memspine.read.pipeline import request as rq
from memspine.read.pipeline.timeview import _as_envelope
from memspine.read.timeview import TimeView, apply
from memspine.services.storage.sqlite.migrations import alembic_config, upgrade_to_head
from memspine.store.envelope import from_envelope, to_envelope
from memspine.store.layer_registry import LayerRegistry, LayerTemplate
from memspine.store.records import MemoryRecord, SourceInfo
from memspine.store.tri_temporal import derived_times, turn_times

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)
T1 = T0 + timedelta(days=3)

_NEW = {"source_at", "source_precision", "event_precision", "event_approximate", "event_basis"}


def _engine(path: Path | str = ":memory:", **kw: object) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={'provider': "sqlite", 'settings': {'path': str(path), 'busy_timeout_s': 5.0}},
        embedding={'provider': "hash", 'settings': {}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        **kw,  # type: ignore[arg-type]
    )


async def _seed(eng: Engine) -> list[MemoryRecord]:
    await eng.ingest_messages(
        [
            {"role": "user", "content": "we went camping by the lake"},
            {"role": "assistant", "content": "camping sounds fun"},
        ],
        namespace="u",
        session_id="s1",
        valid_from=T0,
    )
    return await eng.conversation("u", "s1")


def _columns(path: Path) -> set[str]:
    with sqlite3.connect(path) as db:
        return {row[1] for row in db.execute("PRAGMA table_info(memory_records)")}


_SELECT = "SELECT record_id, source_at, source_precision, event_precision, event_basis FROM memory_records"


def _times(path: Path) -> dict[str, tuple]:
    with sqlite3.connect(path) as db:
        return {r[0]: r[1:] for r in db.execute(_SELECT)}


# ---- pure mapping -----------------------------------------------------------------------------


def test_turn_times_need_a_timestamp() -> None:
    assert turn_times(None) == {}
    got = turn_times(T0)
    assert got["source_at"] == T0 and got["event_basis"] == "explicit"
    assert got["source_precision"] == got["event_precision"] == "instant"


def _parent(rid: str, at: datetime | None, vf: datetime) -> MemoryRecord:
    extra = {"source_at": at, "source_precision": "instant"} if at else {}
    return MemoryRecord(
        record_id=rid, namespace="n", memory_type="episodic", content=rid, valid_from=vf, **extra
    )


def test_derived_inherits_earliest_source_at() -> None:
    parents = [_parent("b", T1, T1), _parent("a", T0, T0), _parent("c", None, T0)]
    out = derived_times(channel="mining", tags=[], valid_from=T1, parents=parents)
    assert out == {
        "source_at": T0,
        "source_precision": "instant",
    }  # no event claim for a plain fact


def test_undated_parents_give_nothing() -> None:
    assert (
        derived_times(channel="mining", tags=[], valid_from=T0, parents=[_parent("a", None, T0)])
        == {}
    )


def test_window_start_is_inferred_and_an_extracted_date_is_explicit() -> None:
    parents = [_parent("a", T0, T0), _parent("b", T1, T1)]
    ep = derived_times(channel="window_episode", tags=[], valid_from=T0, parents=parents)
    assert (ep["event_basis"], ep["event_precision"]) == ("inferred", "day")
    undated = derived_times(channel="mining", tags=["window_fact"], valid_from=T0, parents=parents)
    assert undated["event_basis"] == "inferred"
    dated = derived_times(channel="mining", tags=["window_fact"], valid_from=T1, parents=parents)
    assert dated["event_basis"] == "explicit"


# ---- the write path -----------------------------------------------------------------------------


async def test_new_turns_carry_source_time() -> None:
    eng = _engine()
    await eng.start()
    try:
        turns = await _seed(eng)
        await eng.ingest_messages(
            [{"message_id": "bare-turn", "role": "user", "content": "no timestamp"}],
            namespace="v",
            session_id="s2",
        )
        bare = (await eng.conversation("v", "s2"))[0]
    finally:
        await eng.stop()
    assert [t.source_at for t in turns] == [T0, T0]
    assert {(t.source_precision, t.event_precision, t.event_basis) for t in turns} == {
        ("instant", "instant", "explicit")
    }
    assert bare.source_at is not None and bare.event_basis == "explicit"
    assert bare.source.message is not None
    assert bare.source.message["created_at_generated"] is True


async def test_derived_records_inherit_and_window_facts_say_inferred() -> None:
    eng = _engine()
    await eng.start()
    try:
        turns = await _seed(eng)
        storage = eng._storage
        parents = [t.record_id for t in turns]
        episode = MemoryRecord(
            namespace="u",
            memory_type="semantic",
            content="camping happened",
            source=SourceInfo(role="assistant", channel="window_episode", parents=parents),
            valid_from=turns[0].valid_from,
        )
        stored = await eng._write_locked(storage, "u", episode, "semantic", "system")
        fact = MemoryRecord(
            namespace="u",
            memory_type="semantic",
            content="user trip: camping by the lake",
            entity="user",
            attribute="trip",
            source=SourceInfo(role="assistant", channel="mining", parents=parents),
            valid_from=turns[0].valid_from,
            tags=["window_fact"],
        )
        fact_stored = await eng._write_locked(storage, "u", fact, "semantic", "system")
        reread = await storage.get_record(stored.record_id)
    finally:
        await eng.stop()
    assert stored.source_at == T0
    assert (stored.event_basis, stored.event_precision) == ("inferred", "day")
    assert fact_stored.event_basis == "inferred"
    assert reread is not None and reread.source_at == T0 and reread.event_basis == "inferred"


def test_dump_omits_unset_fields_and_keeps_set_ones() -> None:
    plain = MemoryRecord(namespace="n", memory_type="semantic", content="x")
    assert not _NEW & set(plain.model_dump(mode="json"))
    dated = plain.model_copy(update=turn_times(T0))
    assert _NEW - {"event_approximate"} <= set(dated.model_dump(mode="json"))


# ---- migration -----------------------------------------------------------------------------------


async def test_migration_down_up_and_backfill(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    eng = _engine(path)
    await eng.start()
    try:
        turns = await _seed(eng)
    finally:
        await eng.stop()
    first, second = turns[0].record_id, turns[1].record_id
    assert _columns(path) >= _NEW
    assert _times(path)[first] == (T0.isoformat(), "instant", "instant", "explicit")
    command.downgrade(alembic_config(path), "f04_outbox")
    assert not _NEW & _columns(path)
    with sqlite3.connect(path) as db:  # the second turn becomes a window episode of the first
        source = {"role": "assistant", "channel": "window_episode", "parents": [first]}
        db.execute(
            "UPDATE memory_records SET memory_type='semantic', source=? WHERE record_id=?",
            (orjson.dumps(source), second),
        )
        db.execute(
            "UPDATE memory_records SET tags=? WHERE record_id=?",
            (orjson.dumps(["ts_defaulted"]), first),
        )
    upgrade_to_head(path)
    got = _times(path)
    assert got[first] == (None, None, None, None)  # write-clock time: nothing derivable
    assert got[second] == (
        None,
        None,
        "day",
        "inferred",
    )  # parent undated: no source_at, window start
    upgrade_to_head(path)  # a second upgrade is a no-op
    command.downgrade(alembic_config(path), "f04_outbox")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE memory_records SET tags=NULL WHERE record_id=?", (first,))
    upgrade_to_head(path)
    got = _times(path)
    assert got[first][1:] == ("instant", "instant", "explicit") and got[first][0] is not None
    assert got[second][0] == got[first][0]  # inherited from the dated parent
    assert got[second][3] == "inferred"


def test_single_alembic_head() -> None:
    script = ScriptDirectory.from_config(alembic_config(Path("unused.db")))
    assert script.get_heads() == ["cleanup_fence_recovery"]
    rev = script.get_revision("f02b_tri_temporal")
    assert rev is not None and rev.down_revision == "f04_outbox"


# ---- LayerRegistry -------------------------------------------------------------------------------


def test_template_enables_layers_from_config() -> None:
    assert LayerTemplate.from_config(MemspineConfig(runtime={"provider_services": {"health_timeout_s": 1.0, "close_timeout_s": 1.0, "startup_timeout_s": None}})).enabled == {"L0"}
    with_worker = LayerTemplate.from_config(MemspineConfig(runtime={"provider_services": {"health_timeout_s": 1.0, "close_timeout_s": 1.0, "startup_timeout_s": None}}), worker_model=True)
    assert with_worker.enabled == {"L0", "L1", "L3"}
    cfg = MemspineConfig.model_validate(
        {
            "runtime": {
                "provider_services": {
                    "health_timeout_s": 1.0,
                    "close_timeout_s": 1.0,
                    "startup_timeout_s": None,
                }
            },
            "write": {"window_extract": "all"},
            "memories": {"reflective": {"enabled": True}, "procedural": {"enabled": True}},
        }
    )
    assert LayerTemplate.from_config(cfg).enabled == {"L0", "L2", "L4", "L5"}


def test_registry_enabled_and_populated_are_separate() -> None:
    reg = LayerRegistry.of("n", LayerTemplate(frozenset({"L0", "L1"})), {"L0": 3, "L2": 2, "L9": 4})
    assert reg.populated == {"L0", "L2"}
    assert reg.enabled == {"L0", "L1", "L2"}  # populated implies on
    assert reg.is_enabled("L1") and not reg.is_populated("L1")
    # MF-14: report() also carries configured/available/artifacts/selected; keep the enabled/count checks.
    assert (reg.report()["L2"]["enabled"], reg.report()["L2"]["count"]) == (True, 2)
    assert (reg.report()["L4"]["enabled"], reg.report()["L4"]["count"]) == (False, 0)


async def test_registry_counts_by_layer_per_namespace() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _seed(eng)
        await eng.write(
            "Alice lives in Munich",
            namespace="u",
            memory_type="semantic",
            entity="alice",
            attribute="city",
        )
        await eng.write("other ns", namespace="v", memory_type="semantic")
        template = LayerTemplate.from_config(eng._config())
        reg = await LayerRegistry.load(eng._storage, "u", template)
        other = await LayerRegistry.load(eng._storage, "v", template)
    finally:
        await eng.stop()
    assert reg.count("L0") == 2 and reg.count("L1") == 1
    assert reg.populated == {"L0", "L1"} and not reg.is_populated("L2")
    assert other.populated == {"L1"}


async def test_router_layer_state_uses_the_registry_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine(read={"router_rules": True})
    await eng.start()
    try:
        await eng.ingest_messages(
            [{"role": "user", "content": "we went camping"}],
            namespace="u",
            session_id="s1",
            group_id="g1",
            valid_from=T0,
        )
        read = eng._config().read
        state = await rq.layer_state(eng, "u", read)
        assert state.is_populated("L0") and state.is_populated("L2")  # one session of raw turns
        assert not state.is_populated("L5")

        async def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("no registry")

        monkeypatch.setattr(LayerRegistry, "load", boom)
        fallback = await rq.layer_state(eng, "u", read)
        assert fallback.is_populated("L0") and fallback.is_populated("L2")
    finally:
        await eng.stop()


# ---- timeview and envelope -----------------------------------------------------------------------


def test_timeview_known_at_uses_source_at() -> None:
    recorded = T1 + timedelta(days=30)
    key = {
        "namespace": "n",
        "memory_type": "semantic",
        "content": "x",
        "entity": "e",
        "attribute": "a",
    }
    said = MemoryRecord(
        record_id="said", valid_from=T0, recorded_at=recorded, **key, **turn_times(T0)
    )
    late = MemoryRecord(
        record_id="late",
        valid_from=T0,
        recorded_at=recorded,
        **key,
        source_at=recorded + timedelta(days=60),
        source_precision="instant",
    )
    assert _as_envelope(said).time.source_time == T0
    legacy = MemoryRecord(record_id="legacy", valid_from=T0, recorded_at=recorded, **key)
    assert _as_envelope(legacy).time.source_time is None  # never guessed from valid_from
    probe = recorded + timedelta(days=1)
    hits = apply(TimeView.at_known(probe), [_as_envelope(r) for r in (said, late)]).hits
    assert {h.record_id for h in hits} == {"said"}  # "late" was said after the probe: not yet known


def test_timeview_event_start_keeps_precision_and_basis() -> None:
    rec = MemoryRecord(
        namespace="n",
        memory_type="semantic",
        content="x",
        valid_from=T0,
        source_at=T1,
        event_precision="day",
        event_basis="inferred",
    )
    start = _as_envelope(rec).time.event.start  # type: ignore[union-attr]
    assert (start.precision, start.basis) == ("day", "inferred")
    plain = MemoryRecord(namespace="n", memory_type="semantic", content="x", valid_from=T0)
    start = _as_envelope(plain).time.event.start  # type: ignore[union-attr]
    assert (start.precision, start.basis) == ("instant", "explicit")


def test_envelope_time_axes_carry_precision_and_basis() -> None:
    rec = MemoryRecord(
        namespace="org/u",
        memory_type="semantic",
        content="x",
        valid_from=T0,
        valid_to=T1,
        source_at=T1,
        source_precision="instant",
        event_precision="day",
        event_basis="inferred",
    )
    env = to_envelope(rec)
    assert env.time.source_time == T1
    assert env.time.event is not None and env.time.event.start is not None
    assert (env.time.event.start.precision, env.time.event.start.basis) == ("day", "inferred")
    assert env.time.event.end is not None and env.time.event.end.at == T1
    back = from_envelope(env)
    assert (back.source_at, back.event_basis, back.event_precision) == (T1, "inferred", "day")
    bare = to_envelope(MemoryRecord(namespace="org/u", memory_type="semantic", content="y"))
    assert bare.time.event is None  # nothing says how the event time was obtained
