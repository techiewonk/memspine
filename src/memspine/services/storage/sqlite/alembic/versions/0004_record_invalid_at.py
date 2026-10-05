"""#19: ``invalid_at`` on memory_records (interval arithmetic for contradicting facts).

Guarded add-column, like 0002: the 0001 baseline builds ``memory_records`` from
the live ``schema.py`` metadata, which already has the column, so a fresh
database reaches this revision with it present and only the guard runs. A
database stamped before #19 gets a nullable column and no backfill: a NULL
``invalid_at`` means "not known", which is what every record written without
``conflict.interval_order`` carries, and what a rebuild gives those records.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_TABLE = "memory_records"


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {col["name"] for col in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    if "invalid_at" not in _columns():
        op.add_column(_TABLE, sa.Column("invalid_at", sa.String(), nullable=True))


def downgrade() -> None:
    if "invalid_at" in _columns():
        op.drop_column(_TABLE, "invalid_at")
