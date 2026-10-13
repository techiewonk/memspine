"""Programmatic Alembic runner for the Postgres storage profile (F-16d, M-04).

The tree under ``postgres/alembic`` mirrors the SQLite tree revision for revision (0001..0009) and
has its own single head. It is a separate tree because the SQLite revisions carry SQLite-only
backfills (``json_extract``) and a baseline built from live metadata; the Postgres revisions are
explicit DDL, frozen per revision. A parity test keeps the two trees' logical schema equal.
"""

from __future__ import annotations

import threading
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from memspine.clients.postgres import normalize_pg_url
from memspine.observability import tap

__all__ = ["alembic_config", "ensure_schema", "heads", "upgrade_to_head"]

_HERE = Path(__file__).parent / "alembic"
_LOCK = threading.Lock()  # Alembic keeps module-global context: never overlap two runs.


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_HERE))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def heads() -> list[str]:
    """The head revision ids of the Postgres tree (exactly one, asserted by a test)."""
    return list(ScriptDirectory(str(_HERE)).get_heads())


def upgrade_to_head(url: str) -> None:
    command.upgrade(alembic_config(url), "head")


def ensure_schema(url: str) -> None:
    """Bring a Postgres database to head. A database that already has the tables but no
    ``alembic_version`` (made by the legacy ``create_all`` path) is stamped at head, because
    ``create_all`` builds the current schema; an empty database is migrated from the baseline."""
    sync_url = normalize_pg_url(url)
    engine = create_engine(sync_url)
    try:
        inspector = inspect(engine)
        legacy = inspector.has_table("memory_events") and not inspector.has_table("alembic_version")
    finally:
        engine.dispose()
    with _LOCK:
        cfg = alembic_config(sync_url)
        if legacy:
            command.stamp(cfg, "head")
            tap.event("store.profile.migrate", tree="postgres", action="stamped_legacy")
        else:
            command.upgrade(cfg, "head")
            tap.event("store.profile.migrate", tree="postgres", action="upgraded")
