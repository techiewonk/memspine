"""Postgres mirror of SQLite 0002: ``group_id`` and ``tags`` on memory_records (D2 sub-scoping).

Revision ID: pg0002
Revises: pg0001
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "pg0002"
down_revision = "pg0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_records", sa.Column("group_id", sa.String(), nullable=True))
    op.add_column("memory_records", sa.Column("tags", sa.LargeBinary(), nullable=True))
    op.create_index("ix_memory_records_ns_group", "memory_records", ["namespace", "group_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_records_ns_group", table_name="memory_records")
    op.drop_column("memory_records", "tags")
    op.drop_column("memory_records", "group_id")
