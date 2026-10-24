"""PostgreSQL migration entry points for the shared SQL baseline.

The connector lends a connection. PostgreSQL's advisory lock serializes startup
across processes; the shared runner serializes Alembic context across dialects.
"""

from __future__ import annotations

from alembic.config import Config
from sqlalchemy.engine import Connection

from memspine.clients.postgres.sync import advisory_lock
from memspine.observability import tap
from memspine.services.storage import migrations as shared

__all__ = ["alembic_config", "ensure_schema", "heads", "upgrade_to_head"]


def alembic_config(url: str) -> Config:
    return shared.alembic_config(url)


def heads() -> list[str]:
    return shared.heads()


def upgrade_to_head(url: str) -> None:
    shared.upgrade_to_head(alembic_config(url))


def ensure_schema(connection: Connection) -> None:
    """Initialize/open the shared schema under the PostgreSQL advisory lock."""
    with advisory_lock(connection):
        shared.ensure_schema(connection)
        tap.event("store.profile.migrate", tree="shared", action="upgraded")
