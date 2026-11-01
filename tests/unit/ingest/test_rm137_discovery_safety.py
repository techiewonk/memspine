"""RM-137 review defects in raw-to-derivation discovery.

(5) The write path scans only the intent it just applied, the per-scope scans use the
``(scope_key, status, created_at)`` index, and no per-call ``create_all`` runs.
(6) Offsets are allocated under the write lock (SQLite ``BEGIN IMMEDIATE``), so two writers
discovering the same scope never collide, and a discovery failure after the apply never fails
the write: reconciliation (``recover`` at the next start) picks the intent up.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select

from memspine import Engine
from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.config.schema import MemspineConfig
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.ingestion import RequiredDerivation
from memspine.services.llm.base import LLMRouter
from memspine.services.storage import intake_tables
from memspine.services.storage.intake_sql import SqlIntakeLedger
from memspine.services.storage.intake_tables import intake_derivation_discovery, intake_intent
from memspine.services.storage.outbox_tables import outbox_item, outbox_metadata

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_window_write import FakeWindowLLM

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
SKEY = "scope:alice"


async def _client(path: Path) -> SQLiteClient:
    client = SQLiteClient(SQLiteSpec(str(path), busy_timeout_s=5.0))
    await client.open()
    return client


@pytest.fixture
async def shared_file(tmp_path: Path) -> AsyncIterator[tuple[SqlIntakeLedger, SqlIntakeLedger]]:
    """Two independent engines (two pools, as two processes would have) on one SQLite file."""
    path = tmp_path / "shared.db"
    a, b = await _client(path), await _client(path)
    first, second = SqlIntakeLedger(a.engine), SqlIntakeLedger(b.engine)
    await first.ensure_schema()
    try:
        yield first, second
    finally:
        await a.close()
        await b.close()


async def _applied(ledger: SqlIntakeLedger, n: int) -> list[str]:
    ids = [f"intent-{i:03d}" for i in range(n)]
    async with ledger.engine.begin() as conn:
        for i, intent in enumerate(ids):
            manifest = RequiredDerivation(
                intent_id=intent,
                namespace="alice",
                scope=OwnerScope(tenant="tenant", owner="alice"),
                source_id=f"source:{intent}",
                source_version=1,
                source_write_ids=(f"write:{intent}",),
                source_record_ids=(f"record:{intent}",),
                required_layers=("raw_store", "facts"),
                policy_revision="policy:one",
                activated_at=NOW,
            )
            await conn.execute(
                insert(intake_intent).values(
                    intent_id=intent,
                    scope_key=SKEY,
                    namespace="alice",
                    source_id=manifest.source_id,
                    status="applied",
                    receipt="{}",
                    readiness="{}",
                    required_derivation=manifest.model_dump_json(),
                    apply=None,
                    created_at=f"{NOW.isoformat()}#{i:03d}",
                )
            )
    return ids


# ------------------------------------------------------------------------------------- (5)
def test_intent_table_has_the_scope_status_index() -> None:
    indexes = {ix.name: [c.name for c in ix.columns] for ix in intake_intent.indexes}
    assert indexes["ix_intake_intent_scope_status"] == ["scope_key", "status", "created_at"]


async def test_discovery_scan_uses_the_index(shared_file: Any) -> None:
    ledger, _ = shared_file
    async with ledger.engine.connect() as conn:
        plan = (
            await conn.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT intent_id FROM intake_intent "
                "WHERE scope_key = 'x' AND status = 'applied' ORDER BY created_at"
            )
        ).all()
    assert any("ix_intake_intent_scope_status" in str(row) for row in plan), plan


async def test_discovery_runs_no_create_all(
    shared_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _ = shared_file
    await _applied(ledger, 2)

    def refuse(*_a: Any, **_k: Any) -> None:
        raise AssertionError("create_all on the discovery path")

    monkeypatch.setattr(outbox_metadata, "create_all", refuse)
    monkeypatch.setattr(intake_tables.intake_metadata, "create_all", refuse)
    assert await ledger.discover_committed(scope_key=SKEY, stage="s", discovered_at=NOW) == 2
    assert (
        await ledger.activate_policy(
            scope_key=SKEY,
            stage="s",
            policy_revision="policy:two",
            registry_digest=None,
            activated_at=NOW,
        )
        == 2
    )


# ------------------------------------------------------------------------------------- (6)
async def test_two_writers_discovering_one_scope_never_collide(shared_file: Any) -> None:
    """Full scans from one engine race scoped scans from the other on the same file; every
    intent is discovered exactly once with contiguous, unique offsets, and nothing raises."""
    first, second = shared_file
    ids = await _applied(first, 24)
    tasks = []
    for i, intent in enumerate(ids):
        tasks.append(
            second.discover_committed(
                scope_key=SKEY, stage="s", discovered_at=NOW, intent_ids=(intent,)
            )
        )
        if i % 3 == 0:
            tasks.append(first.discover_committed(scope_key=SKEY, stage="s", discovered_at=NOW))
    async with asyncio.timeout(60):
        inserted = await asyncio.gather(*tasks)
    assert sum(inserted) == len(ids)
    async with first.engine.connect() as conn:
        offsets = sorted((await conn.execute(select(outbox_item.c.item_offset))).scalars().all())
        found = (await conn.execute(select(intake_derivation_discovery.c.intent_id))).all()
    assert offsets == list(range(len(ids)))
    assert sorted(r.intent_id for r in found) == ids


# ------------------------------------------------------------------- (6) the write survives
async def _engine(monkeypatch: pytest.MonkeyPatch, path: Path) -> Engine:
    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter({"window_extract": FakeWindowLLM()})  # type: ignore[dict-item]

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(path), "busy_timeout_s": 5.0},
            }
        },
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "llm": {
                "window_extract": {
                    "provider": "openai_compatible",
                    "timeout_s": 30.0,
                    "settings": {"api_base": "http://test.invalid/v1", "model": "fake/window"},
                }
            },
        },
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={
            "segment_max_turns": 6,
            "segment_gap_s": 0,
            "batch_max_windows": 1,
            "window_extract": "facts",
            "window_async": False,
            "window_idle_s": 0.0,
        },
        workers={"outbox": True},
    )
    await eng.start()
    return eng


def _count(path: Path, table: str) -> int:
    con = sqlite3.connect(path)
    try:
        return int(con.execute(f"select count(*) from {table}").fetchone()[0])
    finally:
        con.close()


async def test_a_discovery_failure_does_not_fail_the_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from sqlalchemy.exc import OperationalError

    from memspine.ingest import gateway

    db = tmp_path / "deferred.db"
    eng = await _engine(monkeypatch, db)
    seen: list[tuple[str, ...]] = []

    async def locked(*_a: Any, **kwargs: Any) -> int:
        seen.append(tuple(kwargs.get("intent_ids") or ()))
        raise OperationalError("discover", {}, Exception("database is locked"))

    try:
        with monkeypatch.context() as patch:
            patch.setattr(gateway, "discover_committed_derivations", locked)
            record = await eng.write(
                "Mira adopted a greyhound named Juno.",
                namespace="a",
                memory_type="episodic",
                session_id="s1",
            )
        assert await eng._require_started().get_record(record.record_id) is not None
        assert len(seen) == 1 and len(seen[0]) == 1  # scoped to the one intent just applied
    finally:
        await eng.stop()
    assert _count(db, "intake_intent") == 1
    assert _count(db, "intake_derivation_discovery") == 0  # left to reconciliation

    again = await _engine(monkeypatch, db)  # recover() runs the full scan at start
    try:
        assert _count(db, "intake_derivation_discovery") == 1
        assert len(await again._require_started().list_records("a", "episodic")) == 1
    finally:
        await again.stop()
