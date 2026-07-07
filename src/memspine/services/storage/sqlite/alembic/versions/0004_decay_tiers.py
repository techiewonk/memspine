"""P3: decay tier (M3) + cold-tier compressed content (M6/D-32) on memory_records.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_records",
        sa.Column("tier", sa.String, nullable=False, server_default="hot"),
    )
    op.add_column("memory_records", sa.Column("content_zstd", sa.LargeBinary))
    op.create_index("ix_memory_records_tier", "memory_records", ["tier"])


def downgrade() -> None:
    op.drop_index("ix_memory_records_tier", table_name="memory_records")
    op.drop_column("memory_records", "content_zstd")
    op.drop_column("memory_records", "tier")
