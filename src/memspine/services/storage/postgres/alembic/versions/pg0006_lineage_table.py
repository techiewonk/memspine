"""Postgres mirror of SQLite 0006_lineage (F-16a, M-09): the ``lineage`` table.

Revision ID: pg0006_lineage
Revises: pg0005
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "pg0006_lineage"
down_revision = "pg0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lineage",
        sa.Column("created_seq", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("child_id", sa.String(), nullable=False),
        sa.Column("parent_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("namespace", sa.String()),
        sa.Column("span", sa.String()),
        sa.Column("source_version", sa.Integer()),
        sa.Column("source_family", sa.String()),
        sa.Column("derivation", sa.String()),
    )
    op.create_index("uq_lineage_edge", "lineage", ["child_id", "parent_id", "role"], unique=True)
    op.create_index("ix_lineage_parent", "lineage", ["parent_id"])


def downgrade() -> None:
    op.drop_table("lineage")
