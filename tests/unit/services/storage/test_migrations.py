"""``memspine.services.storage.migrations``: the shared Alembic tree and runner."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Connection, create_engine, inspect, text

from memspine.services.storage import migrations
from memspine.services.storage.baseline import schema_metadata


def _tables() -> set[str]:
    return {name for md in schema_metadata() for name in md.tables} | {"alembic_version"}


def _versions(conn: Connection) -> list[str]:
    return [row[0] for row in conn.execute(text("SELECT version_num FROM alembic_version"))]


def test_the_tree_has_one_head() -> None:
    assert migrations.heads() == ["0001"]


def test_alembic_config_points_at_the_shared_tree_and_escapes_percent() -> None:
    cfg = migrations.alembic_config("postgresql://u:p%40ss@h/db")
    assert Path(cfg.get_main_option("script_location") or "").name == "alembic"
    assert cfg.get_main_option("sqlalchemy.url") == "postgresql://u:p%40ss@h/db"
    assert migrations.alembic_config().get_main_option("sqlalchemy.url") is None


def test_upgrade_to_head_builds_the_baseline_from_a_url(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'url.db'}"
    migrations.upgrade_to_head(migrations.alembic_config(url))
    engine = create_engine(url)
    assert set(inspect(engine).get_table_names()) == _tables()
    with engine.connect() as conn:
        assert _versions(conn) == migrations.heads()


def test_ensure_schema_creates_head_and_is_idempotent(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'lent.db'}")
    with engine.connect() as conn:
        migrations.ensure_schema(conn)
        assert not conn.in_transaction()  # the caller's connection is committed
        migrations.ensure_schema(conn)
        migrations.ensure_schema(conn)
        assert _versions(conn) == migrations.heads()
    assert set(inspect(engine).get_table_names()) == _tables()


def test_ensure_schema_keeps_existing_rows(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'rows.db'}")
    with engine.connect() as conn:
        migrations.ensure_schema(conn)
        conn.execute(
            text("INSERT INTO provider_change_fences (deployment_id, epoch) VALUES ('d', 3)")
        )
        conn.commit()
        migrations.ensure_schema(conn)
        rows = conn.execute(text("SELECT deployment_id, epoch FROM provider_change_fences"))
        assert [tuple(r) for r in rows] == [("d", 3)]
