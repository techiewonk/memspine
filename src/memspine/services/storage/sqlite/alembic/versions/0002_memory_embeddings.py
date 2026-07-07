"""P1: memory_embeddings — zero-dep vector fallback projection.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_embeddings",
        sa.Column("record_id", sa.String, primary_key=True),
        sa.Column("namespace", sa.String, nullable=False),
        sa.Column("embedder_id", sa.String, nullable=False),
        sa.Column("dim", sa.Integer, nullable=False),
        sa.Column("vector", sa.LargeBinary, nullable=False),
    )
    op.create_index("ix_memory_embeddings_ns", "memory_embeddings", ["namespace"])


def downgrade() -> None:
    op.drop_table("memory_embeddings")
