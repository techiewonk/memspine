"""Postgres mirror of SQLite 0003 (KB-1): namespace, weight and kind on the graph tables.

No backfill: a Postgres database built from this tree has no rows at this revision.

Revision ID: pg0003
Revises: pg0002
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "pg0003"
down_revision = "pg0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "graph_nodes", sa.Column("namespace", sa.String(), nullable=False, server_default="")
    )
    op.add_column("graph_nodes", sa.Column("kind", sa.String(), nullable=True))
    op.create_index("ix_graph_nodes_namespace", "graph_nodes", ["namespace"])
    op.add_column(
        "graph_edges", sa.Column("weight", sa.Float(), nullable=False, server_default="1.0")
    )
    op.add_column("graph_edges", sa.Column("kind", sa.String(), nullable=True))
    op.add_column(
        "graph_edges", sa.Column("namespace", sa.String(), nullable=False, server_default="")
    )
    op.create_index("ix_graph_edges_ns_src_weight", "graph_edges", ["namespace", "src", "weight"])
    op.create_index("ix_graph_edges_ns_dst_weight", "graph_edges", ["namespace", "dst", "weight"])


def downgrade() -> None:
    op.drop_index("ix_graph_edges_ns_dst_weight", table_name="graph_edges")
    op.drop_index("ix_graph_edges_ns_src_weight", table_name="graph_edges")
    for column in ("namespace", "kind", "weight"):
        op.drop_column("graph_edges", column)
    op.drop_index("ix_graph_nodes_namespace", table_name="graph_nodes")
    for column in ("kind", "namespace"):
        op.drop_column("graph_nodes", column)
