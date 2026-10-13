"""Postgres mirror of SQLite 0004 (#19): ``invalid_at`` on memory_records.

Revision ID: pg0004
Revises: pg0003
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "pg0004"
down_revision = "pg0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_records", sa.Column("invalid_at", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("memory_records", "invalid_at")
