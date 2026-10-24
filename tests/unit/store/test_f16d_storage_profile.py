"""Current SQL schema: shared fresh baseline, dialect parity and integrity."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from memspine.clients.postgres.sync import MIGRATION_LOCK_KEY
from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.runtime.outbox import outbox_metadata
from memspine.services.storage.postgres import migrations as pg_migrations
from memspine.services.storage.sqlite import migrations as sqlite_migrations
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord, SourceInfo


def test_backends_use_one_tree_and_one_baseline() -> None:
    sqlite_config = sqlite_migrations.alembic_config(Path("unused.db"))
    postgres_config = pg_migrations.alembic_config("postgresql://unused")
    assert sqlite_config.get_main_option("script_location") == postgres_config.get_main_option(
        "script_location"
    )
    script = ScriptDirectory.from_config(sqlite_config)
    assert script.get_heads() == pg_migrations.heads() == ["0001"]
    assert len(list(script.walk_revisions())) == 1


def test_storage_profile_module_is_gone() -> None:
    """ADR-081: no profile selector and no profile description module remain."""
    import importlib.util

    assert importlib.util.find_spec("memspine.services.storage.profile") is None


def _family(col_type: Any) -> str:
    if isinstance(col_type, sa.Boolean):
        return "bool"
    if isinstance(col_type, sa.Integer):
        return "int"
    if isinstance(col_type, sa.Float | sa.Numeric):
        return "float"
    if isinstance(col_type, sa.LargeBinary):
        return "bytes"
    return "text"


def _logical(engine: sa.Engine, only: set[str] | None = None) -> dict[str, Any]:
    insp = sa.inspect(engine)
    out: dict[str, Any] = {}
    for table in sorted(insp.get_table_names()):
        if table == "alembic_version" or (only is not None and table not in only):
            continue
        cols = {
            c["name"]: (
                _family(c["type"]),
                bool(c["nullable"]) and c["name"] not in _pk(insp, table),
            )
            for c in insp.get_columns(table)
        }
        indexes = {
            (ix["name"], tuple(ix["column_names"]), bool(ix.get("unique")))
            for ix in insp.get_indexes(table)
            if ix["name"] and not str(ix["name"]).startswith("sqlite_autoindex")
        }
        out[table] = {"columns": cols, "pk": tuple(sorted(_pk(insp, table))), "indexes": indexes}
    return out


def _pk(insp: Any, table: str) -> list[str]:
    return list(insp.get_pk_constraint(table)["constrained_columns"])


def _sqlite_tree(path: Path) -> sa.Engine:
    sqlite_migrations.upgrade_to_head(path)
    return sa.create_engine(f"sqlite:///{path}")


def _pg_tree_on_sqlite(path: Path, revision: str = "head") -> sa.Engine:
    url = f"sqlite:///{path}"
    command.upgrade(pg_migrations.alembic_config(url), revision)
    return sa.create_engine(url)


def test_backend_entry_points_produce_the_same_logical_schema(tmp_path: Path) -> None:
    a = _logical(_sqlite_tree(tmp_path / "a.db"))
    b = _logical(_pg_tree_on_sqlite(tmp_path / "b.db"))
    assert set(a) == set(b)
    for table in a:
        assert a[table]["columns"] == b[table]["columns"], table
        assert a[table]["pk"] == b[table]["pk"], table
        assert a[table]["indexes"] == b[table]["indexes"], table


def test_every_expected_table_exists(tmp_path: Path) -> None:
    got = set(_logical(_pg_tree_on_sqlite(tmp_path / "t.db")))
    assert {
        "memory_events",
        "memory_records",
        "projector_offsets",
        "graph_nodes",
        "graph_edges",
        "lineage",
        *outbox_metadata.tables,
        "provider_deployment_identity",
        "provider_change_plans",
        "provider_change_operations",
        "provider_change_fences",
        "provider_projection_generations",
        "provider_projection_switches",
        "provider_cleanup_fence_recoveries",
        "provider_projector_bindings",
        "sleep_lease",
        "cost_entry",
        "dead_letters",
        "asset_registry",
        "intake_source",
        "intake_write",
        "intake_intent",
        "intake_message_identity",
        "intake_conversation_identity",
    } <= got
    assert "side_file_imports" not in got


def test_postgres_tree_downgrades_to_empty(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'd.db'}"
    pg_migrations.upgrade_to_head(url)
    command.downgrade(pg_migrations.alembic_config(url), "base")
    tables = set(sa.inspect(sa.create_engine(url)).get_table_names())
    assert tables <= {"alembic_version"}


# ---------------------------------------------------------------------------- store contract


def _ev(namespace: str, content: str) -> MemoryEvent:
    return MemoryEvent(kind=EventKind.WRITE, namespace=namespace, payload={"content": content})


def _rec(record_id: str, namespace: str, content: str, **extra: Any) -> MemoryRecord:
    return MemoryRecord(
        record_id=record_id,
        namespace=namespace,
        memory_type="semantic",
        content=content,
        source=SourceInfo(role="user"),
        **extra,
    )


def _pg_url() -> str:
    url = os.environ.get("MEMSPINE_TEST_PG_URL") or os.environ.get("MEMSPINE_TEST_POSTGRES_URL")
    if url:
        return url
    try:
        from testcontainers.postgres import PostgresContainer  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip(
            "postgres records store: set MEMSPINE_TEST_PG_URL to a scratch Postgres DSN "
            "(or install testcontainers with Docker); no server available"
        )
    container = PostgresContainer("postgres:16")  # pragma: no cover - needs Docker
    try:  # pragma: no cover
        container.start()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"postgres records store: testcontainers could not start Postgres: {exc}")
    return container.get_connection_url()  # pragma: no cover


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    pytest.importorskip("psycopg")
    url = _pg_url()
    from memspine.clients.postgres import normalize_pg_url

    engine = sa.create_engine(normalize_pg_url(url))
    try:
        with engine.connect():
            pass
    except Exception as exc:
        engine.dispose()
        # M-LIVE: the URL is either an explicit opt-in (MEMSPINE_TEST_PG_URL /
        # MEMSPINE_TEST_POSTGRES_URL) or a container that already started, so an
        # unreachable server is a failure of the requested gate, never a skip.
        pytest.fail(f"postgres records store: requested postgres is not reachable: {exc}")
    engine.dispose()
    yield url


def _ensure(url: str) -> None:
    """Run the schema bring-up the way ``PostgresStorage.start`` does: over one lent
    connection, under the migration advisory lock."""
    from memspine.clients.postgres import normalize_pg_url

    engine = sa.create_engine(normalize_pg_url(url))
    try:
        with engine.connect() as connection:
            pg_migrations.ensure_schema(connection)
    finally:
        engine.dispose()


def _wipe(url: str) -> None:
    from memspine.clients.postgres import normalize_pg_url

    engine = sa.create_engine(normalize_pg_url(url))
    meta = sa.MetaData()
    meta.reflect(bind=engine)
    meta.drop_all(bind=engine)
    engine.dispose()


async def _sqlite(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Any, Any, sa.Engine]:
    path = tmp_path / "contract.db"
    client = SQLiteClient(SQLiteSpec(str(path)))
    await client.open()
    storage = SQLiteStorage(client, mode=EventLogMode.ROLLING)
    await storage.start()
    return client, storage, sa.create_engine(f"sqlite:///{path}")


async def _postgres(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Any, Any, sa.Engine]:
    from memspine.clients.postgres import PostgresClient, PostgresSpec, normalize_pg_url
    from memspine.services.storage.postgres.engine import PostgresStorage

    url = request.getfixturevalue("pg_url")
    _wipe(url)
    client = PostgresClient(PostgresSpec(url))
    await client.open()
    storage = PostgresStorage(client, mode=EventLogMode.ROLLING)
    await storage.start()
    return client, storage, sa.create_engine(normalize_pg_url(url))


_RECORDS = {"sqlite": _sqlite, "postgres": _postgres}


@pytest.mark.parametrize("provider", list(_RECORDS))
async def test_store_contract(
    provider: str, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    client, storage, engine = await _RECORDS[provider](tmp_path, request)
    try:
        # event log: the single write door, monotonic gap-free seq.
        e1 = await storage.append_event(_ev("ns/a", "alpha"))
        e2 = await storage.append_event(_ev("ns/a", "beta"))
        assert (e1.seq, e2.seq) == (1, 2)
        assert [e.seq for e in await storage.read_events(after_seq=0)] == [1, 2]

        # record store: round trip, upsert in place, fact key, namespaces.
        await storage.upsert_record(_rec("r1", "ns/a", "one", entity="e", attribute="a"))
        await storage.upsert_record(_rec("r1", "ns/a", "two", entity="e", attribute="a"))
        got = await storage.get_record("r1")
        assert got is not None and got.content == "two"
        active = await storage.find_active_fact("ns/a", "e", "a")
        assert active is not None and active.record_id == "r1"
        assert await storage.list_namespaces() == ["ns/a"]
        assert [r.record_id for r in await storage.list_records("ns/a")] == ["r1"]

        # offsets: advance-only, resettable.
        await storage.set_offset("proj", 5)
        await storage.set_offset("proj", 3)
        assert await storage.get_offset("proj") == 5
        await storage.reset_offset("proj")
        assert await storage.get_offset("proj") == 0

        # lineage table: unique edge, insertion order.
        lineage = sa.Table("lineage", sa.MetaData(), autoload_with=engine)
        with engine.begin() as conn:
            conn.execute(lineage.insert(), {"child_id": "c", "parent_id": "p", "role": "support"})
            # a SAVEPOINT: Postgres aborts the whole transaction on an error (SQLite does
            # not), which would roll the first insert back with it.
            with pytest.raises(sa.exc.IntegrityError), conn.begin_nested():
                conn.execute(
                    lineage.insert(), {"child_id": "c", "parent_id": "p", "role": "support"}
                )
        with engine.connect() as conn:
            assert conn.execute(sa.select(sa.func.count()).select_from(lineage)).scalar() == 1

        # outbox: the F-04 job store runs over this database.
        from memspine.runtime.outbox import SqlJobStore

        assert SqlJobStore(engine) is not None
        assert set(outbox_metadata.tables) <= set(sa.inspect(engine).get_table_names())
        # ADR-081: a durable SQL database is always Alembic-managed.
        assert "alembic_version" in sa.inspect(engine).get_table_names()

        # hard delete drops the read-model row.
        await storage.delete_record("r1")
        assert await storage.get_record("r1") is None
    finally:
        await client.close()
        engine.dispose()


def test_server_tree_matches_sqlite_tree_on_a_real_postgres(pg_url: str, tmp_path: Path) -> None:
    from memspine.clients.postgres import normalize_pg_url

    _wipe(pg_url)
    pg_migrations.upgrade_to_head(normalize_pg_url(pg_url))
    engine = sa.create_engine(normalize_pg_url(pg_url))
    try:
        a = _logical(_sqlite_tree(tmp_path / "s.db"))
        b = _logical(engine)
        assert set(a) == set(b)
        for table in a:
            assert a[table]["columns"] == b[table]["columns"], table
    finally:
        engine.dispose()
        _wipe(pg_url)


def test_concurrent_schema_upgrades_finish_at_head_and_release_the_lock(pg_url: str) -> None:
    """Two starts against one empty database both succeed (the second finds the database at
    head) and no advisory lock is left behind. Cross-session blocking is the next test."""
    from memspine.clients.postgres import normalize_pg_url

    _wipe(pg_url)
    errors: list[BaseException] = []

    def start() -> None:
        try:
            _ensure(pg_url)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not errors
    engine = sa.create_engine(normalize_pg_url(pg_url))
    try:
        with engine.connect() as conn:
            version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            held = conn.execute(
                sa.text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
            ).scalar()
        assert version == pg_migrations.heads()[0]
        assert held == 0  # released after the upgrade
    finally:
        engine.dispose()
        _wipe(pg_url)


def test_advisory_lock_blocks_a_migration_held_elsewhere(pg_url: str) -> None:
    """While another session holds the schema lock, ``ensure_schema`` waits for it."""
    from memspine.clients.postgres import normalize_pg_url

    _wipe(pg_url)
    holder = sa.create_engine(normalize_pg_url(pg_url))
    done = threading.Event()
    try:
        with holder.connect() as conn:
            conn.execute(sa.text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY})
            conn.commit()
            worker = threading.Thread(target=lambda: (_ensure(pg_url), done.set()))
            worker.start()
            assert not done.wait(1.5)  # blocked on the advisory lock
            conn.execute(sa.text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY})
            conn.commit()
            worker.join(timeout=120)
        assert done.is_set()
    finally:
        holder.dispose()
        _wipe(pg_url)


def test_unversioned_database_is_not_adopted(tmp_path: Path) -> None:
    path = tmp_path / "unversioned.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE memory_events (sentinel INTEGER)")
        connection.exec_driver_sql("INSERT INTO memory_events VALUES (7)")
    with pytest.raises(sa.exc.OperationalError, match="already exists"):
        sqlite_migrations.upgrade_to_head(path)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT sentinel FROM memory_events").scalar_one() == 7
    engine.dispose()
