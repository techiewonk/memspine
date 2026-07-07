"""P4: Memory Firewall corroboration counter (E1/M17) on memory_records.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_records",
        sa.Column("corroborations", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("memory_records", "corroborations")
