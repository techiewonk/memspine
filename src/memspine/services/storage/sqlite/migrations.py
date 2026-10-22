"""Programmatic Alembic runner (D-36) for file-backed SQLite databases.

Alembic runs over a connection the SQLite connector lends
(:meth:`~memspine.clients.sqlite.SQLiteClient.migration_connection`, keyed under SQLCipher)
and receives it through ``config.attributes['connection']`` (RM-113 P1 #9); this module never
opens a connection itself. ``:memory:`` databases cannot be migrated — a separate migration
connection cannot reach another connection's in-memory database — so callers
(``SQLiteStorage.start``) use ``metadata.create_all`` for those, and the connector refuses
``:memory:`` loudly instead of "succeeding" against a throwaway database.
"""

from __future__ import annotations

import threading
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from memspine.exceptions import StorageError

__all__ = ["alembic_config", "ensure_schema", "upgrade_to_head"]

_HERE = Path(__file__).parent / "alembic"

#: #88: Alembic keeps its migration context in module globals (``alembic.op``,
#: ``alembic.context``), so two migrations in one process must not overlap. Two
#: engines started concurrently on one file each run :func:`ensure_schema` in a
#: worker thread; unserialised, the process crashed (a native access violation).
_MIGRATION_LOCK = threading.Lock()


def _reject_memory(db_path: str | Path) -> None:
    if str(db_path) == ":memory:":
        raise StorageError(
            "cannot run Alembic against ':memory:' — a migration connection cannot "
            "reach an in-memory database; use metadata.create_all instead"
        )


def alembic_config(db_path: str | Path) -> Config:
    """The Alembic config of the SQLite tree, URL-addressed (offline/CLI use and tests)."""
    _reject_memory(db_path)
    cfg = Config()
    cfg.set_main_option("script_location", str(_HERE))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def upgrade_to_head(db_path: str | Path) -> None:
    command.upgrade(alembic_config(db_path), "head")


def ensure_schema(connection: Connection) -> None:
    """Bring the database behind ``connection`` (lent by the connector) to schema head.

    Handles the pre-Alembic case: a database whose tables exist (created by an
    older ``create_all`` run) but that has no ``alembic_version`` table is
    stamped at the baseline revision first, so ``upgrade`` applies only what is
    actually missing instead of failing on existing tables. Each Alembic command
    commits its own transaction on the lent connection.
    """
    with _MIGRATION_LOCK:
        inspector = inspect(connection)
        has_version = inspector.has_table("alembic_version")
        has_tables = inspector.has_table("memory_events")
        if connection.in_transaction():
            connection.commit()  # end the inspection's implicit transaction
        cfg = Config()
        cfg.set_main_option("script_location", str(_HERE))
        cfg.attributes["connection"] = connection
        if has_tables and not has_version:
            command.stamp(cfg, "0001")
        command.upgrade(cfg, "head")
        if connection.in_transaction():
            connection.commit()
