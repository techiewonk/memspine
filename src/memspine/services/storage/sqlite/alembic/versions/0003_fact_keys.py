"""P2: (entity, attribute) fact keying on memory_records (M13.3/M4).

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_records", sa.Column("entity", sa.String))
    op.add_column("memory_records", sa.Column("attribute", sa.String))
    op.create_index(
        "ix_memory_records_fact_key", "memory_records", ["namespace", "entity", "attribute"]
    )


def downgrade() -> None:
    op.drop_index("ix_memory_records_fact_key", table_name="memory_records")
    op.drop_column("memory_records", "attribute")
    op.drop_column("memory_records", "entity")
