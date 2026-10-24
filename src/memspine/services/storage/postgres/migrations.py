"""Programmatic Alembic runner for the Postgres storage profile (F-16d, M-04).

Both SQL dialects have one complete fresh baseline. Their table definitions are
shared, and a parity test verifies the logical schema. Historical databases are
not adopted or transformed.

Alembic runs over a connection the Postgres connector lends
(:meth:`~memspine.clients.postgres.PostgresClient.migration_connection`) and receives it through
``config.attributes['connection']`` (RM-113 P1 #9). ADR-081: the run holds the Postgres session
advisory lock :data:`~memspine.clients.postgres.sync.MIGRATION_LOCK_KEY`, so two processes
starting on one database never migrate at once; the second one waits, then finds head.
"""

from __future__ import annotations

import threading
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection

from memspine.clients.postgres.sync import advisory_lock
from memspine.observability import tap

__all__ = ["alembic_config", "ensure_schema", "heads", "upgrade_to_head"]

_HERE = Path(__file__).parent / "alembic"
_LOCK = threading.Lock()  # Alembic keeps module-global context: never overlap two runs.


def alembic_config(url: str) -> Config:
    """The Alembic config of the Postgres tree, URL-addressed (offline/CLI use and tests)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(_HERE))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def heads() -> list[str]:
    """The head revision ids of the Postgres tree (exactly one, asserted by a test)."""
    return list(ScriptDirectory(str(_HERE)).get_heads())


def upgrade_to_head(url: str) -> None:
    command.upgrade(alembic_config(url), "head")


def ensure_schema(connection: Connection) -> None:
    """Initialize an empty database or open the current schema under the advisory lock."""
    with _LOCK, advisory_lock(connection):
        cfg = Config()
        cfg.set_main_option("script_location", str(_HERE))
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
        tap.event("store.profile.migrate", tree="postgres", action="upgraded")
        if connection.in_transaction():
            connection.commit()
