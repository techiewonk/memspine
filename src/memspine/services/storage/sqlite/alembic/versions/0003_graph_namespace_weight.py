"""KB-1: namespace, weight and kind columns on the graph adjacency tables.

Guarded add-column, like 0002: the 0001 baseline builds from the live
``schema.py`` metadata, which already has these columns, so a fresh database
reaches this revision with them present and only the guards run. A database
stamped before KB-1 gets the columns, the indexes and a backfill:

- ``graph_edges.weight`` from the ``weight`` property in the orjson blob
  (absent or non-numeric => 1.0, the port's default);
- ``graph_edges.kind`` from the ``kind`` property;
- ``graph_nodes.namespace`` from the ``namespace`` property the projector has
  always written on record nodes, ``graph_nodes.kind`` from the first label;
- ``graph_edges.namespace`` from the source node's namespace (else the
  destination's), since links never cross namespaces.

The graph is a rebuildable projection (D0.1), so ``engine.rebuild()`` reaches
the same state; the backfill only spares existing databases that replay.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {col["name"] for col in inspector.get_columns(table)}


def _indexes(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {str(ix["name"]) for ix in inspector.get_indexes(table)}


def upgrade() -> None:
    nodes = _columns("graph_nodes")
    if "namespace" not in nodes:
        op.add_column(
            "graph_nodes", sa.Column("namespace", sa.String(), nullable=False, server_default="")
        )
        op.execute(
            "UPDATE graph_nodes SET namespace = COALESCE("
            "json_extract(CAST(properties AS TEXT), '$.namespace'), '')"
        )
    if "kind" not in nodes:
        op.add_column("graph_nodes", sa.Column("kind", sa.String(), nullable=True))
        op.execute("UPDATE graph_nodes SET kind = json_extract(CAST(labels AS TEXT), '$[0]')")
    if "ix_graph_nodes_namespace" not in _indexes("graph_nodes"):
        op.create_index("ix_graph_nodes_namespace", "graph_nodes", ["namespace"])

    edges = _columns("graph_edges")
    if "weight" not in edges:
        op.add_column(
            "graph_edges", sa.Column("weight", sa.Float(), nullable=False, server_default="1.0")
        )
        op.execute(
            "UPDATE graph_edges SET weight = CASE "
            "WHEN json_type(CAST(properties AS TEXT), '$.weight') IN ('integer', 'real') "
            "THEN json_extract(CAST(properties AS TEXT), '$.weight') ELSE 1.0 END"
        )
    if "kind" not in edges:
        op.add_column("graph_edges", sa.Column("kind", sa.String(), nullable=True))
        op.execute(
            "UPDATE graph_edges SET kind = CASE "
            "WHEN json_type(CAST(properties AS TEXT), '$.kind') = 'text' "
            "THEN json_extract(CAST(properties AS TEXT), '$.kind') END"
        )
    if "namespace" not in edges:
        op.add_column(
            "graph_edges", sa.Column("namespace", sa.String(), nullable=False, server_default="")
        )
        op.execute(
            "UPDATE graph_edges SET namespace = COALESCE("
            "NULLIF((SELECT n.namespace FROM graph_nodes n WHERE n.node_id = graph_edges.src), ''),"
            "(SELECT n.namespace FROM graph_nodes n WHERE n.node_id = graph_edges.dst), '')"
        )
    existing = _indexes("graph_edges")
    if "ix_graph_edges_ns_src_weight" not in existing:
        op.create_index(
            "ix_graph_edges_ns_src_weight", "graph_edges", ["namespace", "src", "weight"]
        )
    if "ix_graph_edges_ns_dst_weight" not in existing:
        op.create_index(
            "ix_graph_edges_ns_dst_weight", "graph_edges", ["namespace", "dst", "weight"]
        )


def downgrade() -> None:
    existing = _indexes("graph_edges")
    for name in ("ix_graph_edges_ns_dst_weight", "ix_graph_edges_ns_src_weight"):
        if name in existing:
            op.drop_index(name, table_name="graph_edges")
    for column in ("namespace", "kind", "weight"):
        if column in _columns("graph_edges"):
            op.drop_column("graph_edges", column)
    if "ix_graph_nodes_namespace" in _indexes("graph_nodes"):
        op.drop_index("ix_graph_nodes_namespace", table_name="graph_nodes")
    for column in ("kind", "namespace"):
        if column in _columns("graph_nodes"):
            op.drop_column("graph_nodes", column)
