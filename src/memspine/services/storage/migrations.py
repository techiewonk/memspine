"""Shared SQL Alembic tree and process-wide migration serialization."""

from __future__ import annotations

import threading
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection

_TREE = Path(__file__).parent / "alembic"
# Alembic uses module-global context, even when databases/dialects differ.
_LOCK = threading.Lock()


def alembic_config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_TREE))
    if url is not None:
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def heads() -> list[str]:
    return list(ScriptDirectory(str(_TREE)).get_heads())


def upgrade_to_head(config: Config) -> None:
    with _LOCK:
        command.upgrade(config, "head")


def ensure_schema(connection: Connection) -> None:
    """Initialize/open the current schema over the caller's connection."""
    cfg = alembic_config()
    cfg.attributes["connection"] = connection
    upgrade_to_head(cfg)
    if connection.in_transaction():
        connection.commit()
