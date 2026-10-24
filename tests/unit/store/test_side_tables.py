"""RM-111 WP5 (ADR-081 decision 7): sleep leases, the cost ledger and dead letters are tables of the
authoritative SQL database, never side files.

SQLite runs everywhere. The SQLCipher wiring is checked with the stdlib stand-in driver of
``tests/unit/clients/test_sqlite_encryption.py``; the real round trip runs only when the
``[encrypt]`` driver is importable. The Postgres tests need ``MEMSPINE_TEST_PG_URL`` (a scratch DSN).
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
import textwrap
import types
import uuid
from pathlib import Path
from typing import Any, ClassVar

import pytest
import sqlalchemy as sa

from memspine import Engine
from memspine.clients.sqlite.sync import sqlite_sync_engine
from memspine.derive.failure_ledger import FailureLedger
from memspine.derive.job import LeaseStore
from memspine.economy.ledger import CallCost, LedgerStore
from memspine.services.storage.postgres import migrations as pg_migrations
from memspine.services.storage.sqlite import migrations as sqlite_migrations
from memspine.store.side_tables import (
    ensure_side_tables,
    side_metadata,
)

PG_URL = os.environ.get("MEMSPINE_TEST_PG_URL")
KEY_ENV = "MEMSPINE_TEST_SIDE_KEY"
SECRET = "side-table-secret"
_HAS_SQLCIPHER = any(
    importlib.util.find_spec(n) is not None for n in ("sqlcipher3", "pysqlcipher3")
)
SIDE_TABLE_NAMES = set(side_metadata.tables)


def _side_files(base: Path) -> list[Path]:
    return [
        p for s in (".sleep", ".cost", ".deadletters.sqlite") if (p := Path(f"{base}{s}")).exists()
    ]


def _cost(ns: str = "-") -> CallCost:
    return CallCost(0, ns, "semantic", "mine_facts", "generative", "m", 1, 10, 2, 3.5, "ok")


def _sync_sqlite(db: Path) -> sa.Engine:
    sqlite_migrations.upgrade_to_head(db)
    return sqlite_sync_engine(str(db), busy_timeout_s=5.0)


# ---------------------------------------------------------------------------------- schema
def test_both_alembic_trees_create_the_side_tables(tmp_path: Path) -> None:
    sqlite_migrations.upgrade_to_head(tmp_path / "a.db")
    pg_migrations.upgrade_to_head(f"sqlite:///{tmp_path / 'b.db'}")
    for name in ("a.db", "b.db"):
        insp = sa.inspect(sa.create_engine(f"sqlite:///{tmp_path / name}"))
        assert set(insp.get_table_names()) >= SIDE_TABLE_NAMES, name
        assert "dead_letters_ns" in {ix["name"] for ix in insp.get_indexes("dead_letters")}


def test_runtime_create_matches_the_alembic_revision(tmp_path: Path) -> None:
    """``ensure_side_tables`` (the ``create_all`` path) builds what the revision builds."""
    alembic_db = sa.create_engine(f"sqlite:///{tmp_path / 'a.db'}")
    sqlite_migrations.upgrade_to_head(tmp_path / "a.db")
    created = sa.create_engine(f"sqlite:///{tmp_path / 'c.db'}")
    ensure_side_tables(created)

    def shape(engine: sa.Engine) -> dict[str, Any]:
        insp = sa.inspect(engine)
        return {
            t: (
                sorted((c["name"], bool(c["nullable"])) for c in insp.get_columns(t)),
                insp.get_pk_constraint(t)["constrained_columns"],
            )
            for t in sorted(SIDE_TABLE_NAMES)
        }

    assert shape(alembic_db) == shape(created)


# ---------------------------------------------------------------------------------- import


def _engine(db: Path, **extra: Any) -> Engine:
    storage_settings: dict[str, Any] = {"path": str(db), "busy_timeout_s": 5.0}
    storage_settings.update(extra.pop("storage_settings", {}))
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"records": {"provider": "sqlite", "settings": storage_settings}},
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        offline={"sleep": True, "sleep_manual_only": True},
        economy={"ledger": True, "ledger_persist": True},
        read={"hybrid": False},
        **extra,
    )


# ---------------------------------------------------------------------------------- leases
def test_two_engines_on_one_file_share_one_exclusive_lease(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    sqlite_migrations.upgrade_to_head(db)
    a = LeaseStore(sqlite_sync_engine(str(db), busy_timeout_s=5.0))
    b = LeaseStore(sqlite_sync_engine(str(db), busy_timeout_s=5.0))
    held = a.claim("ns", "a", "job-a", 60.0)
    assert held is not None
    assert b.claim("ns", "b", "job-b", 60.0) is None
    assert b.get_lease("ns") == held
    assert a.release(held)
    taken = b.claim("ns", "b", "job-b", 60.0)
    assert taken is not None and taken.epoch == held.epoch + 1
    assert a.heartbeat(held, 60.0) is None  # the stale holder is fenced
    assert [r[1] for r in b.fence_log()] == ["heartbeat"]


_CLAIM = textwrap.dedent(
    """
    import sys
    from memspine.derive.job import LeaseStore
    from memspine.clients.sqlite.sync import sqlite_sync_engine
    store = LeaseStore(sqlite_sync_engine(sys.argv[1], busy_timeout_s=10.0))
    lease = store.claim("ns", sys.argv[2], "job-" + sys.argv[2], 600.0)
    print("won" if lease is not None else "lost")
    """
)


def test_leases_are_exclusive_across_processes(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    sqlite_migrations.upgrade_to_head(db)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CLAIM, str(db), f"p{i}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for i in range(4)
    ]
    results = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        results.append(out.strip())
    assert sorted(results) == ["lost", "lost", "lost", "won"]
    assert _side_files(db) == []


# ---------------------------------------------------------------------------------- stores
def test_engine_backed_ledgers_write_off_the_caller_and_persist(tmp_path: Path) -> None:
    engine = _sync_sqlite(tmp_path / "m.db")
    dead = FailureLedger(engine)
    dead.record("a", "w1", 0, "uncited")
    dead.record("a", "w2", 1, "bad_span")
    assert dead.count("a") == 2  # waits for the queued writes
    assert [d.code for d in dead.list()] == ["uncited", "bad_span"]
    dead.close()
    cost = LedgerStore(engine, flush_every=2)
    for _ in range(3):
        cost.add(_cost("ns-a"))
    cost.close()
    assert len(LedgerStore(engine).load(10)) == 3
    assert _side_files(tmp_path / "m.db") == []


# ---------------------------------------------------------------------------------- SQLCipher
class _Recording(sqlite3.Connection):
    statements: ClassVar[list[str]] = []
    paths: ClassVar[list[str]] = []

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:  # type: ignore[override]
        _Recording.statements.append(sql)
        return super().execute(sql, *args)


@pytest.fixture
def fake_cipher(monkeypatch: pytest.MonkeyPatch) -> None:
    _Recording.statements = []
    _Recording.paths = []
    dbapi2 = types.ModuleType("sqlcipher3.dbapi2")

    def connect(path: str, **kwargs: Any) -> sqlite3.Connection:
        _Recording.paths.append(str(path))
        return sqlite3.connect(path, factory=_Recording, **kwargs)

    dbapi2.connect = connect  # type: ignore[attr-defined]
    for name in dir(sqlite3):
        if not name.startswith("_") and name != "connect":
            setattr(dbapi2, name, getattr(sqlite3, name))
    package = types.ModuleType("sqlcipher3")
    package.dbapi2 = dbapi2  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sqlcipher3", package)
    monkeypatch.setitem(sys.modules, "sqlcipher3.dbapi2", dbapi2)


async def _encrypted_round_trip(db: Path) -> None:
    eng = _engine(db, storage_settings={"encryption": {"mode": "sqlcipher", "key_env": KEY_ENV}})
    await eng.start()
    try:
        await eng.write("Ana lives in Lyon", namespace="a")
        handle = await eng.sleep()
        await handle.wait(timeout=60)
        assert eng.cost_report() is not None
        assert eng._derive_sched is not None and eng._derive_sched.store.offload
    finally:
        await eng.stop()


async def test_encrypted_store_keeps_side_tables_inside_keyed_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_cipher: None
) -> None:
    monkeypatch.setenv(KEY_ENV, SECRET)
    db = tmp_path / "enc.db"
    await _encrypted_round_trip(db)
    assert _side_files(db) == []  # imported, and nothing written beside the database
    # every connection the engine opened (storage, leases, ledgers) went through the keyed driver,
    # and only to the database itself: no side file was ever opened through it or created
    assert _Recording.paths and set(_Recording.paths) == {str(db)}
    keyed = [s for s in _Recording.statements if s.startswith("PRAGMA key")]
    assert len(keyed) == len(_Recording.paths)
    assert all(s == f"PRAGMA key = '{SECRET}'" for s in keyed)
    with sqlite3.connect(db) as con:  # the stand-in is plain SQLite: the rows are in the main file
        assert con.execute("SELECT COUNT(*) FROM sleep_lease").fetchone()[0] >= 1
        assert con.execute("SELECT COUNT(*) FROM dead_letters").fetchone() == (0,)


@pytest.mark.skipif(not _HAS_SQLCIPHER, reason="SQLCipher driver ([encrypt]) not installed")
async def test_real_sqlcipher_side_tables_are_encrypted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # pragma: no cover - needs the [encrypt] extra
    monkeypatch.setenv(KEY_ENV, SECRET)
    db = tmp_path / "real.db"
    await _encrypted_round_trip(db)
    assert _side_files(db) == []
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(db).execute("SELECT count(*) FROM sleep_lease").fetchone()
    from memspine.clients.sqlite import load_sqlcipher_driver

    con = load_sqlcipher_driver().connect(str(db))
    con.execute(f"PRAGMA key = '{SECRET}'")
    assert con.execute("SELECT COUNT(*) FROM sleep_lease").fetchone()[0] >= 1
    assert con.execute("SELECT COUNT(*) FROM cost_entry").fetchone()[0] >= 1
    con.close()


# ---------------------------------------------------------------------------------- Postgres
pg = pytest.mark.skipif(not PG_URL, reason="set MEMSPINE_TEST_PG_URL to a scratch Postgres DSN")


def _pg_engine() -> sa.Engine:
    from memspine.clients.postgres import normalize_pg_url
    from memspine.clients.postgres.sync import migration_connection

    assert PG_URL is not None
    with migration_connection(PG_URL) as connection:  # RM-113: a lent connection
        pg_migrations.ensure_schema(connection)
    return sa.create_engine(normalize_pg_url(PG_URL))


@pg
def test_postgres_tree_has_the_side_tables() -> None:
    engine = _pg_engine()
    try:
        assert set(sa.inspect(engine).get_table_names()) >= SIDE_TABLE_NAMES
    finally:
        engine.dispose()


@pg
def test_postgres_leases_are_exclusive_across_engines() -> None:
    a, b = _pg_engine(), _pg_engine()
    try:
        ns = f"rm111-{uuid.uuid4().hex[:8]}"
        first, second = LeaseStore(a), LeaseStore(b)
        assert first.offload and second.offload
        held = first.claim(ns, "a", "job-a", 60.0)
        assert held is not None
        assert second.claim(ns, "b", "job-b", 60.0) is None
        assert first.release(held)
        assert second.claim(ns, "b", "job-b", 60.0) is not None
    finally:
        a.dispose()
        b.dispose()
