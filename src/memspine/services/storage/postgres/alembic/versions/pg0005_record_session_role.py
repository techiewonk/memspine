"""Postgres mirror of SQLite 0005 (I6): ``session_key`` and ``source_role`` on memory_records.

Revision ID: pg0005
Revises: pg0004
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "pg0005"
down_revision = "pg0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_records", sa.Column("session_key", sa.String(), nullable=True))
    op.add_column("memory_records", sa.Column("source_role", sa.String(), nullable=True))
    op.create_index("ix_memory_records_ns_session", "memory_records", ["namespace", "session_key"])


def downgrade() -> None:
    op.drop_index("ix_memory_records_ns_session", table_name="memory_records")
    op.drop_column("memory_records", "source_role")
    op.drop_column("memory_records", "session_key")
