"""Programmatic Alembic runner (D-36): `upgrade_to_head(db_path)`.

File-backed databases migrate through Alembic; ``:memory:`` databases (tests,
throwaway engines) fall back to ``metadata.create_all`` because a migration
process cannot share an in-memory connection.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

__all__ = ["alembic_config", "upgrade_to_head"]

_HERE = Path(__file__).parent / "alembic"


def alembic_config(db_path: str | Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_HERE))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def upgrade_to_head(db_path: str | Path) -> None:
    command.upgrade(alembic_config(db_path), "head")
