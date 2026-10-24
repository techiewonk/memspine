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

from pathlib import Path

from alembic.config import Config
from sqlalchemy.engine import Connection

from memspine.exceptions import StorageError
from memspine.services.storage import migrations as shared

__all__ = ["alembic_config", "ensure_schema", "upgrade_to_head"]


def _reject_memory(db_path: str | Path) -> None:
    if str(db_path) == ":memory:":
        raise StorageError(
            "cannot run Alembic against ':memory:' — a migration connection cannot "
            "reach an in-memory database; use metadata.create_all instead"
        )


def alembic_config(db_path: str | Path) -> Config:
    """The shared Alembic config with a SQLite URL (offline/CLI use and tests)."""
    _reject_memory(db_path)
    return shared.alembic_config(f"sqlite:///{db_path}")


def upgrade_to_head(db_path: str | Path) -> None:
    shared.upgrade_to_head(alembic_config(db_path))


def ensure_schema(connection: Connection) -> None:
    """Initialize/open SQLite using the shared SQL baseline and process lock."""
    shared.ensure_schema(connection)
