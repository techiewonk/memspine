"""OW-B wiring: O4-03 reconcile (LIB-37) and O4-04 histories (LIB-20), model-free."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest

from memspine import Engine
from memspine.config.schema import OfflineConfig
from memspine.derive.pipeline.stages import histories as histories_mod
from memspine.derive.pipeline.stages import reconcile as reconcile_mod
from memspine.derive.pipeline.stages.envelope_adapter import is_fact, to_envelope
from memspine.derive.pipeline.stages.histories import build_value_history
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.workers.schedule import HISTORIES_STAGE, RECONCILE_STAGE, SLEEP_CYCLE_ORDER

NS = "u"
T = [datetime(2024, m, 1, tzinfo=UTC) for m in (1, 3, 6, 9)]


def _engine(path: Path, *, reconcile: bool = False, histories: bool = False, lineage: bool = False):
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": str(path / "m.db")},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        offline={"reconcile_v2": reconcile, "histories_v2": histories},
        write={"lineage_table": lineage},
    )


async def _fact(eng: Engine, text: str, when: datetime, attr: str = "city", entity: str = "Ilse"):
    return await eng.write(
        text, namespace=NS, memory_type="semantic", entity=entity, attribute=attr, valid_from=when
    )


async def _live(eng: Engine) -> set[str]:
    recs = await eng._require_started().list_records(NS, "semantic")
    return {r.content for r in recs if r.status is RecordStatus.ACTIVATED}


async def _run(eng: Engine, name: str) -> dict[str, object]:
    assert eng._runner is not None
    return await eng._runner.run(name, eng._pipeline_ctx())


def test_flags_default_off_and_stage_not_scheduled() -> None:
    cfg = OfflineConfig()
    assert cfg.reconcile_v2 is False and cfg.histories_v2 is False
    assert RECONCILE_STAGE not in SLEEP_CYCLE_ORDER and HISTORIES_STAGE not in SLEEP_CYCLE_ORDER


async def test_flag_off_stage_skips_and_store_unchanged(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    await eng.start()
    try:
        await _fact(eng, "Ilse lives in Rotterdam", T[1])
        await _fact(eng, "Ilse lives in Utrecht", T[0])  # arrives late (reverse order)
        before = await _live(eng)
        assert (await _run(eng, "reconcile"))["status"] == "skipped"
        assert (await _run(eng, "histories"))["status"] == "skipped"
        assert await _live(eng) == before == {"Ilse lives in Rotterdam", "Ilse lives in Utrecht"}
        stats = await eng.sleep()
        assert "reconcile" not in stats and "histories" not in stats
    finally:
        await eng.stop()


async def test_reconcile_reverse_order_ends_like_forward(tmp_path: Path) -> None:
    eng = _engine(tmp_path, reconcile=True)
    await eng.start()
    try:
        await _fact(eng, "Ilse lives in Rotterdam", T[1])
        late = await _fact(eng, "Ilse lives in Utrecht", T[0])
        assert await _live(eng) == {"Ilse lives in Rotterdam", "Ilse lives in Utrecht"}
        stats = await _run(eng, "reconcile")
        assert stats["status"] == "ok" and stats["superseded"] == 1
        assert await _live(eng) == {"Ilse lives in Rotterdam"}
        old = await eng._require_started().get_record(late.record_id)
        assert old is not None and old.status is RecordStatus.ARCHIVED
        assert old.evolve_to is not None and old.superseded_at is not None
        # idempotent: a second sweep changes nothing
        again = await _run(eng, "reconcile")
        assert again.get("superseded", 0) == 0 and again.get("closed", 0) == 0
    finally:
        await eng.stop()


async def test_reconcile_runs_in_the_sleep_cycle_when_on(tmp_path: Path) -> None:
    eng = _engine(tmp_path, reconcile=True, histories=True)
    await eng.start()
    try:
        await _fact(eng, "Ilse lives in Rotterdam", T[1])
        await _fact(eng, "Ilse lives in Utrecht", T[0])
        stats = await eng.sleep()
        assert stats["reconcile"]["status"] == "ok" and stats["histories"]["status"] == "ok"
        names = list(stats)
        assert names.index("reconcile") == names.index("histories") - 1
        assert names.index("reconcile") > names.index("extract_graph")
        assert "Ilse lives in Utrecht" not in await _live(eng)
    finally:
        await eng.stop()


async def test_reconcile_does_not_touch_unrelated_keys_or_episodes(tmp_path: Path) -> None:
    eng = _engine(tmp_path, reconcile=True)
    await eng.start()
    try:
        await _fact(eng, "Ilse lives in Rotterdam", T[1])
        await _fact(eng, "Ilse plays the cello", T[0], attr="instrument")
        await _fact(eng, "Bram lives in Delft", T[0], entity="Bram")
        before = await _live(eng)
        stats = await _run(eng, "reconcile")
        assert stats.get("superseded", 0) == 0
        assert await _live(eng) == before
    finally:
        await eng.stop()


async def test_reconcile_contests_a_lower_trust_newer_value(tmp_path: Path) -> None:
    eng = _engine(tmp_path, reconcile=True)
    await eng.start()
    try:
        storage = eng._require_started()
        a = await _fact(eng, "Ilse works at Acme", T[0], attr="employer")
        b = await _fact(eng, "Ilse works at Veldkamp", T[1], attr="employer")
        await storage.upsert_record(b.model_copy(update={"trust": 0.2}))
        await storage.upsert_record(a.model_copy(update={"trust": 0.9}))
        stats = await _run(eng, "reconcile")
        assert stats["tagged_disputed"] == 2
        recs = {r.content: r for r in await storage.list_records(NS, "semantic")}
        assert "disputed" in recs["Ilse works at Acme"].tags
        assert "disputed" in recs["Ilse works at Veldkamp"].tags
        assert recs["Ilse works at Acme"].status is RecordStatus.ACTIVATED
    finally:
        await eng.stop()


def test_adapter_excludes_summaries_cues_and_held_records() -> None:
    base = {"namespace": NS, "memory_type": "semantic", "content": "x y"}
    assert is_fact(MemoryRecord(**base))
    assert not is_fact(MemoryRecord(**base, quarantined=True))
    assert not is_fact(MemoryRecord(**base, tags=["anticipatory_cue"]))
    assert not is_fact(MemoryRecord(**{**base, "memory_type": "episodic"}))
    env = to_envelope(MemoryRecord(**base, entity="Ilse", attribute="city"), NS)
    assert env.record_id and env.scope.tenant == NS and env.attribute == "city"


async def test_histories_merges_near_duplicates_but_not_numbers_or_names(tmp_path: Path) -> None:
    eng = _engine(tmp_path, histories=True)
    await eng.start()
    try:
        storage = eng._require_started()
        mk = lambda c, t: MemoryRecord(  # noqa: E731
            namespace=NS, memory_type="semantic", content=c, valid_from=t, entity="Ilse"
        )  # NEW-01: merge candidates are keyed facts (entity set)
        for rec in (
            mk("Ilse likes tomatoes and basil on the balcony", T[0]),
            mk("Ilse likes basil and tomatoes on the balcony", T[1]),
            mk("Ilse owns 3 cats and a very old grey dog", T[0]),
            mk("Ilse owns 4 cats and a very old grey dog", T[1]),
            mk("Ilse works at Acme Corp downtown", T[0]),
            mk("Ilse works at Apex Corp downtown", T[1]),
        ):
            await storage.upsert_record(rec)
        stats = await _run(eng, "histories")
        assert stats["status"] == "ok" and stats["merged"] == 1
        live = await _live(eng)
        assert "Ilse likes tomatoes and basil on the balcony" in live
        assert "Ilse likes basil and tomatoes on the balcony" not in live
        assert {
            "Ilse owns 3 cats and a very old grey dog",
            "Ilse owns 4 cats and a very old grey dog",
        } <= live
        assert {"Ilse works at Acme Corp downtown", "Ilse works at Apex Corp downtown"} <= live
        assert stats["blocked"] >= 2
        dup = next(
            r
            for r in await storage.list_records(NS, "semantic")
            if "basil and tomatoes" in r.content
        )
        assert dup.status is RecordStatus.ARCHIVED and dup.evolve_to is not None
    finally:
        await eng.stop()


async def test_histories_writes_merge_member_lineage_when_table_on(tmp_path: Path) -> None:
    eng = _engine(tmp_path, histories=True, lineage=True)
    await eng.start()
    try:
        storage = eng._require_started()
        a = MemoryRecord(
            namespace=NS,
            memory_type="semantic",
            content="Ilse likes tomatoes and basil",
            valid_from=T[0],
            entity="Ilse",  # NEW-01: unkeyed records are no longer merge candidates
        )
        b = MemoryRecord(
            namespace=NS,
            memory_type="semantic",
            content="Ilse likes basil and tomatoes",
            valid_from=T[1],
            entity="Ilse",
        )
        await storage.upsert_record(a)
        await storage.upsert_record(b)
        stats = await _run(eng, "histories")
        assert stats["merged"] == 1 and stats["lineage_edges"] == 1
    finally:
        await eng.stop()
    with sqlite3.connect(tmp_path / "m.db") as db:
        rows = db.execute("SELECT child_id, parent_id, role FROM lineage").fetchall()
    assert rows == [(a.record_id, b.record_id, "merge_member")]


async def test_value_history_is_order_independent(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    await eng.start()
    try:
        await _fact(eng, "Ilse lives in Rotterdam", T[1])
        await _fact(eng, "Ilse lives in Utrecht", T[0])
        await _fact(eng, "Ilse lives in Leiden", T[2])
        records = await eng._require_started().list_records(NS, "semantic")
        h = build_value_history(records, NS)
        line = h.line()
        assert [s.value for s in line.spans] == [
            "Ilse lives in Utrecht",
            "Ilse lives in Rotterdam",
            "Ilse lives in Leiden",
        ]
        assert [s.value for s in line.current()] == ["Ilse lives in Leiden"]
        assert [s.value for s in h.value_at(datetime(2024, 4, 1, tzinfo=UTC))] == [
            "Ilse lives in Rotterdam"
        ]
        assert build_value_history(list(reversed(records)), NS) == h
    finally:
        await eng.stop()


class _Tap:
    seen: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    @staticmethod
    def enabled() -> bool:
        return True

    @staticmethod
    def event(name: str, /, **fields: Any) -> None:
        _Tap.seen.append((name, fields))


async def test_tap_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _Tap.seen = []
    monkeypatch.setattr(reconcile_mod, "tap", _Tap)
    monkeypatch.setattr(histories_mod, "tap", _Tap)
    eng = _engine(tmp_path, reconcile=True, histories=True)
    await eng.start()
    try:
        await _fact(eng, "Ilse lives in Rotterdam", T[1])
        await _fact(eng, "Ilse lives in Utrecht", T[0])
        await _run(eng, "reconcile")
        await _run(eng, "histories")
    finally:
        await eng.stop()
    by_name = {n: f for n, f in _Tap.seen}
    sweep = by_name["job.reconcile.sweep"]
    assert sweep["superseded"] == 1 and sweep["closures"] == 1 and sweep["merges"] == 0
    assert {"splits", "rekeys", "contests", "closures"} <= set(sweep)
    hist = by_name["job.histories"]
    assert {"groups", "merged", "blocked", "blocked_by_reason"} <= set(hist)
