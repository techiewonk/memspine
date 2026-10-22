"""Programmatic Alembic runner for the Postgres storage profile (F-16d, M-04).

The tree under ``postgres/alembic`` mirrors the SQLite tree revision for revision (0001..0009) and
has its own single head. It is a separate tree because the SQLite revisions carry SQLite-only
backfills (``json_extract``) and a baseline built from live metadata; the Postgres revisions are
explicit DDL, frozen per revision. A parity test keeps the two trees' logical schema equal.

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
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

from memspine.clients.postgres.sync import advisory_lock
from memspine.observability import tap

__all__ = ["LEGACY_STAMP_REVISION", "alembic_config", "ensure_schema", "heads", "upgrade_to_head"]

_HERE = Path(__file__).parent / "alembic"
_LOCK = threading.Lock()  # Alembic keeps module-global context: never overlap two runs.
#: The last metadata-only revision: a database built by the pre-ADR-081 ``create_all`` path is
#: stamped here, then upgraded (pinned by tests/unit/store/test_f16d_storage_profile.py).
LEGACY_STAMP_REVISION = "pg_lf_e_layer_backfill"


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
    """Bring the database behind ``connection`` (lent by the connector) to head, under the
    migration advisory lock. A database that already has the tables but no
    ``alembic_version`` (made by the embedded ``create_all`` path) is stamped at the last
    metadata-only revision, then upgraded so migration-only provider tables are created.
    An empty database is migrated from the baseline."""
    with _LOCK, advisory_lock(connection):
        inspector = inspect(connection)
        legacy = inspector.has_table("memory_events") and not inspector.has_table("alembic_version")
        if connection.in_transaction():
            connection.commit()  # end the inspection's implicit transaction
        cfg = Config()
        cfg.set_main_option("script_location", str(_HERE))
        cfg.attributes["connection"] = connection
        if legacy:
            command.stamp(cfg, LEGACY_STAMP_REVISION)
            command.upgrade(cfg, "head")
            tap.event("store.profile.migrate", tree="postgres", action="stamped_legacy")
        else:
            command.upgrade(cfg, "head")
            tap.event("store.profile.migrate", tree="postgres", action="upgraded")
        if connection.in_transaction():
            connection.commit()
