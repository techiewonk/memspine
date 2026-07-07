"""P5: procedural stage (M13.4) + reflective depth (M13.7) on memory_records.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_records", sa.Column("skill_stage", sa.String))
    op.add_column(
        "memory_records",
        sa.Column("reflection_depth", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("memory_records", "reflection_depth")
    op.drop_column("memory_records", "skill_stage")
