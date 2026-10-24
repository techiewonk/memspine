"""F-16a: the lineage table, the SQL LineageGraph, and the derived-write door."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.derive.write import DerivedWithoutParents, write_derived
from memspine.read.pipeline.lift import lineage_from_records
from memspine.schemas.durable.lineage import LineageEdge
from memspine.store import lineage as lineage_store
from memspine.store.records import MemoryRecord, SourceInfo
from memspine.trust.erasure_plan import plan_erasure

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)
NS = "u"


def _engine(path: Path, *, lineage: bool) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(path), "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={"lineage_table": lineage},
    )


async def _turns(eng: Engine) -> list[MemoryRecord]:
    await eng.ingest_messages(
        [
            {"role": "user", "content": "I moved to Lisbon last spring"},
            {"role": "user", "content": "my sister lives there too"},
        ],
        namespace=NS,
        session_id="s1",
        valid_from=T0,
    )
    return await eng._require_started().list_records(NS, "episodic")


def _fact(parents: list[str], text: str = "User lives in Lisbon") -> MemoryRecord:
    return MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        content=text,
        source=SourceInfo(role="assistant", channel="mining", parents=parents),
        valid_from=T0,
    )


async def _derive(eng: Engine, record: MemoryRecord, **kw: Any) -> MemoryRecord:
    storage = eng._require_started()
    async with eng._write_locks.setdefault(NS, asyncio.Lock()):
        return await write_derived(eng, storage, NS, record, "semantic", "system", **kw)


def _lineage_rows(path: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(path) as db:
        return db.execute(
            "SELECT child_id, parent_id, role FROM lineage ORDER BY created_seq"
        ).fetchall()


async def test_write_door_writes_edges_roles_and_caps_trust(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    eng = _engine(path, lineage=True)
    await eng.start()
    try:
        turns = await _turns(eng)
        a, b = turns[0], turns[1]
        storage = eng._require_started()
        await storage.upsert_record(a.model_copy(update={"trust": 0.3}))
        rec = await _derive(
            eng,
            _fact([a.record_id, b.record_id]),
            roles={a.record_id: "support", b.record_id: "context"},
            site="test",
        )
        assert rec.trust <= 0.3 + 1e-9  # POL-2: never above the least-trusted parent
        assert rec.layer in ("L0", "L1", "L2", "L3", "L4", "L5")
        assert rec.fingerprint.derivation
        edges = await lineage_store.parents_of(storage._client.engine, [rec.record_id])
        got = {(e.parent, e.role, e.parent_version) for e in edges[rec.record_id]}
        assert got == {(a.record_id, "support", a.version), (b.record_id, "context", b.version)}
    finally:
        await eng.stop()
    assert {r[2] for r in _lineage_rows(path)} == {"support", "context"}


async def test_refused_without_parents_and_tap_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memspine.derive.write as door

    seen: list[tuple[str, dict[str, Any]]] = []

    class _Tap:
        @staticmethod
        def enabled() -> bool:
            return True

        @staticmethod
        def event(name: str, /, **fields: Any) -> None:
            seen.append((name, fields))

    monkeypatch.setattr(door, "tap", _Tap)
    eng = _engine(tmp_path / "m.db", lineage=True)
    await eng.start()
    try:
        turns = await _turns(eng)
        with pytest.raises(DerivedWithoutParents):
            await _derive(eng, _fact([]), site="orphan")
        await _derive(
            eng, _fact([turns[0].record_id]), roles={turns[0].record_id: "support"}, site="ok"
        )
    finally:
        await eng.stop()
    assert [n for n, _ in seen] == ["derive.lineage.refused", "derive.lineage.write"]
    assert seen[0][1]["reason"] == "no_parents"
    assert seen[1][1]["edges"] == 1
    assert seen[1][1]["roles"] == ["support"]


async def test_disabled_lineage_table_uses_current_parent_links(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    eng = _engine(path, lineage=False)
    await eng.start()
    try:
        turns = await _turns(eng)
        rec = await _derive(eng, _fact([turns[0].record_id]))
        orphan = await _derive(eng, _fact([], "no parent, no refusal"))
        assert not rec.fingerprint.derivation
        assert orphan.record_id
    finally:
        await eng.stop()
    assert _lineage_rows(path) == []


async def test_planner_runs_over_the_sql_graph(tmp_path: Path) -> None:
    eng = _engine(tmp_path / "m.db", lineage=True)
    await eng.start()
    try:
        turns = await _turns(eng)
        a, b = turns[0], turns[1]
        both = await _derive(
            eng,
            _fact([a.record_id, b.record_id], "both"),
            roles={a.record_id: "support", b.record_id: "support"},
        )
        only = await _derive(eng, _fact([a.record_id], "only a"), roles={a.record_id: "support"})
        binding = await _derive(
            eng, _fact([b.record_id], "bound to b"), roles={b.record_id: "binding"}
        )
        engine = eng._require_started()._client.engine
        graph = await lineage_store.SqlLineageGraph.load(engine, [a.record_id])
        assert {e.child for e in graph.children(a.record_id)} == {both.record_id, only.record_id}
        graph = await lineage_store.SqlLineageGraph.load(engine, [a.record_id, b.record_id])
        plan = plan_erasure([a.record_id], graph, generation=1)
        assert plan.action_for(both.record_id) == "keep"  # b still supports it
        assert plan.action_for(only.record_id) == "hide_and_rederive"
        assert plan.action_for(binding.record_id) is None  # untouched by erasing a
        plan_b = plan_erasure([b.record_id], graph, generation=1)
        assert plan_b.action_for(binding.record_id) == "unbind_and_hide"
    finally:
        await eng.stop()


async def test_lift_adapter_reads_the_table_when_on(tmp_path: Path) -> None:
    eng = _engine(tmp_path / "m.db", lineage=True)
    await eng.start()
    try:
        turns = await _turns(eng)
        rec = await _derive(
            eng,
            _fact([turns[0].record_id, turns[1].record_id]),
            roles={turns[0].record_id: "support", turns[1].record_id: "binding"},
        )
        lin, fetched = await lineage_from_records(eng, [rec], 3)
        roles = {e.parent: e.role for e in lin[rec.record_id]}
        assert roles == {turns[0].record_id: "support", turns[1].record_id: "binding"}
        assert set(fetched) == {turns[0].record_id, turns[1].record_id}
        # A current raw record without table edges still exposes its declared source.parents.
        old = _fact([turns[0].record_id], "raw fact")
        await eng._require_started().upsert_record(old)
        lin2, _ = await lineage_from_records(eng, [old], 3)
        assert [(e.parent, e.role) for e in lin2[old.record_id]] == [
            (turns[0].record_id, "support")
        ]
    finally:
        await eng.stop()


async def test_add_edges_is_idempotent_and_rejects_self_loop(tmp_path: Path) -> None:
    eng = _engine(tmp_path / "m.db", lineage=True)
    await eng.start()
    try:
        engine = eng._require_started()._client.engine
        e = LineageEdge(child="c", parent="p", role="cites")
        assert await lineage_store.add_edges(engine, [e, e]) == 1
        assert await lineage_store.add_edges(engine, [e]) == 0
        with pytest.raises(ValueError):
            LineageEdge(child="x", parent="x", role="support")
    finally:
        await eng.stop()
