"""P6: graph_nodes + graph_edges — zero-dep adjacency graph projection (D-26).

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "graph_nodes",
        sa.Column("node_id", sa.String, primary_key=True),
        sa.Column("labels", sa.LargeBinary, nullable=False),
        sa.Column("properties", sa.LargeBinary, nullable=False),
    )
    op.create_table(
        "graph_edges",
        sa.Column("src", sa.String, primary_key=True),
        sa.Column("dst", sa.String, primary_key=True),
        sa.Column("rel_type", sa.String, primary_key=True),
        sa.Column("properties", sa.LargeBinary, nullable=False),
    )
    op.create_index("ix_graph_edges_dst", "graph_edges", ["dst"])


def downgrade() -> None:
    op.drop_table("graph_edges")
    op.drop_table("graph_nodes")
