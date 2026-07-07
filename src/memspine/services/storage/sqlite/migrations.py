"""Programmatic Alembic runner (D-36) for file-backed SQLite databases.

``:memory:`` databases cannot be migrated here — a separate migration
connection cannot reach another connection's in-memory database — so callers
(``SQLiteStorage.start``) use ``metadata.create_all`` for those, and this
module refuses ``:memory:`` loudly instead of "succeeding" against a throwaway
database.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from memspine.exceptions import StorageError

__all__ = ["alembic_config", "ensure_schema", "upgrade_to_head"]

_HERE = Path(__file__).parent / "alembic"


def _reject_memory(db_path: str | Path) -> None:
    if str(db_path) == ":memory:":
        raise StorageError(
            "cannot run Alembic against ':memory:' — a migration connection cannot "
            "reach an in-memory database; use metadata.create_all instead"
        )


def alembic_config(db_path: str | Path) -> Config:
    _reject_memory(db_path)
    cfg = Config()
    cfg.set_main_option("script_location", str(_HERE))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def upgrade_to_head(db_path: str | Path) -> None:
    command.upgrade(alembic_config(db_path), "head")


def ensure_schema(db_path: str | Path) -> None:
    """Bring a file-backed database to schema head.

    Handles the pre-Alembic case: a database whose tables exist (created by an
    older ``create_all`` run) but that has no ``alembic_version`` table is
    stamped at the baseline revision first, so ``upgrade`` applies only what is
    actually missing instead of failing on existing tables.
    """
    _reject_memory(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        has_version = inspector.has_table("alembic_version")
        has_tables = inspector.has_table("memory_events")
    finally:
        engine.dispose()
    cfg = alembic_config(db_path)
    if has_tables and not has_version:
        command.stamp(cfg, "0001")
    command.upgrade(cfg, "head")
