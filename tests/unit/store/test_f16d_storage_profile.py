"""F-16d (M-04 C): the storage-profile contract, the Postgres Alembic tree and its parity.

The store contract runs on both profiles. ``embedded`` (SQLite file) runs everywhere. ``server``
(Postgres through the Alembic tree) runs only when a server is reachable: set
``MEMSPINE_TEST_PG_URL`` (or the older ``MEMSPINE_TEST_POSTGRES_URL``) to a DSN of a scratch
database, or install ``testcontainers`` (with a Docker daemon). Otherwise it skips with the reason.
The tree-parity test needs no server: the Postgres revisions are dialect-neutral DDL, so they are
replayed on SQLite and compared with the SQLite tree; with a server the same comparison also runs
against the reflected Postgres schema.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory

from memspine.clients.sqlite import SQLiteClient
from memspine.config.schema import MemspineConfig
from memspine.config.schema.storage import StorageConfig
from memspine.exceptions import ConfigError
from memspine.observability import tap
from memspine.runtime.outbox import outbox_metadata
from memspine.services.storage import profile as profile_mod
from memspine.services.storage.postgres import migrations as pg_migrations
from memspine.services.storage.profile import EMBEDDED, SERVER, resolve_profile
from memspine.services.storage.sqlite import migrations as sqlite_migrations
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord, SourceInfo

# ---------------------------------------------------------------------------- the trees


def test_both_trees_have_a_single_head() -> None:
    sqlite_dir = Path(sqlite_migrations.__file__).parent / "alembic"
    assert len(ScriptDirectory(str(sqlite_dir)).get_heads()) == 1
    assert pg_migrations.heads() == ["pg_w202_stamping"]


def test_postgres_tree_mirrors_the_sqlite_revision_count() -> None:
    def count(tree: Path) -> int:
        return len(list(ScriptDirectory(str(tree)).walk_revisions()))

    assert count(EMBEDDED.migration_tree) == count(SERVER.migration_tree) == 10


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


def _logical(engine: sa.Engine) -> dict[str, Any]:
    insp = sa.inspect(engine)
    out: dict[str, Any] = {}
    for table in sorted(insp.get_table_names()):
        if table == "alembic_version":
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


def _pg_tree_on_sqlite(path: Path) -> sa.Engine:
    pg_migrations.upgrade_to_head(f"sqlite:///{path}")
    return sa.create_engine(f"sqlite:///{path}")


def test_trees_produce_the_same_logical_schema(tmp_path: Path) -> None:
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
    } <= got


def test_postgres_tree_downgrades_to_empty(tmp_path: Path) -> None:
    from alembic import command

    url = f"sqlite:///{tmp_path / 'd.db'}"
    pg_migrations.upgrade_to_head(url)
    command.downgrade(pg_migrations.alembic_config(url), "base")
    tables = set(sa.inspect(sa.create_engine(url)).get_table_names())
    assert tables <= {"alembic_version"}


# ---------------------------------------------------------------------------- profile + config


def test_default_profile_is_embedded_and_unchanged() -> None:
    cfg = MemspineConfig()
    assert cfg.storage.profile == "embedded"
    assert cfg.storage.backend == "sqlite"
    assert resolve_profile(cfg.storage) is EMBEDDED
    assert EMBEDDED.sql_backend == "sqlite" and EMBEDDED.vector == "lancedb"


def test_server_profile_selects_postgres_backend() -> None:
    storage = StorageConfig(profile="server", url="postgresql://x/y")
    assert storage.backend == "postgres"
    assert resolve_profile(storage) is SERVER
    assert set(SERVER.provides()) == set(EMBEDDED.provides())
    assert SERVER.migration_tree.name == "alembic" and SERVER.migration_tree.is_dir()


def test_server_profile_rejects_a_foreign_backend() -> None:
    with pytest.raises(ConfigError):
        StorageConfig(profile="server", backend="mysql")


def test_legacy_postgres_backend_resolves_to_server_stores() -> None:
    storage = StorageConfig(backend="postgres", url="postgresql://x/y")
    assert storage.profile == "embedded"
    assert resolve_profile(storage) is SERVER


def test_profile_events_are_tapped(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(tap, "enabled", lambda: True)
    monkeypatch.setattr(tap, "event", lambda stage, /, **f: seen.append((stage, f)))
    resolve_profile(StorageConfig(profile="server"))
    assert seen[-1][0] == "store.profile.resolved"
    assert seen[-1][1]["profile"] == "server"
    assert seen[-1][1]["sql_backend"] == "postgres"


def test_migrate_is_tapped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[str] = []
    monkeypatch.setattr(tap, "event", lambda stage, /, **f: seen.append(stage))
    monkeypatch.setattr(pg_migrations, "create_engine", lambda url: sa.create_engine(url))
    pg_migrations.ensure_schema(f"sqlite:///{tmp_path / 'm.db'}")
    assert seen == ["store.profile.migrate"]
    assert profile_mod.pgvector_available() in (True, False)


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
            "server profile: set MEMSPINE_TEST_PG_URL to a scratch Postgres DSN "
            "(or install testcontainers with Docker); no server available"
        )
    container = PostgresContainer("postgres:16")  # pragma: no cover - needs Docker
    try:  # pragma: no cover
        container.start()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"server profile: testcontainers could not start Postgres: {exc}")
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
        pytest.skip(f"server profile: postgres not reachable: {exc}")
    engine.dispose()
    yield url


def _wipe(url: str) -> None:
    from memspine.clients.postgres import normalize_pg_url

    engine = sa.create_engine(normalize_pg_url(url))
    meta = sa.MetaData()
    meta.reflect(bind=engine)
    meta.drop_all(bind=engine)
    engine.dispose()


async def _embedded(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Any, Any, sa.Engine]:
    path = tmp_path / "contract.db"
    client = SQLiteClient(str(path))
    await client.connect()
    storage = SQLiteStorage(client, mode=EventLogMode.ROLLING)
    await storage.start()
    return client, storage, sa.create_engine(f"sqlite:///{path}")


async def _server(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Any, Any, sa.Engine]:
    from memspine.clients.postgres import PostgresClient, normalize_pg_url
    from memspine.services.storage.postgres.engine import PostgresStorage

    url = request.getfixturevalue("pg_url")
    _wipe(url)
    client = PostgresClient(url)
    await client.connect()
    storage = PostgresStorage(client, mode=EventLogMode.ROLLING, use_alembic=True)
    await storage.start()
    return client, storage, sa.create_engine(normalize_pg_url(url))


_PROFILES = {"embedded": _embedded, "server": _server}


@pytest.mark.parametrize("profile", list(_PROFILES))
async def test_store_contract(profile: str, tmp_path: Path, request: pytest.FixtureRequest) -> None:
    client, storage, engine = await _PROFILES[profile](tmp_path, request)
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

        # outbox: the F-04 job store runs over this profile's database.
        from memspine.runtime.outbox import SqlJobStore

        assert SqlJobStore(engine) is not None
        assert set(outbox_metadata.tables) <= set(sa.inspect(engine).get_table_names())

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
