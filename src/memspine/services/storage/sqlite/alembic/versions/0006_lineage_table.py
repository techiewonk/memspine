"""F-16a (M-09): the ``lineage`` table, backfilled from ``source.parents``.

One row per (child, parent, role) edge of the lineage DAG. Guarded create like 0002-0005: a fresh
database built from the live ``schema.py`` already has the table. Existing derived records get one
``support`` edge per parent id in their ``source`` blob (the only link that existed before).
The table is only written by the engine when ``write.lineage_table`` is on.

Revision ID: 0006_lineage
Revises: 0005
Create Date: 2026-10-13
"""

from __future__ import annotations

import orjson
import sqlalchemy as sa
from alembic import op

revision = "0006_lineage"
down_revision = "0005"
branch_labels = None
depends_on = None

_TABLE = "lineage"


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        _TABLE,
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
    op.create_index("uq_lineage_edge", _TABLE, ["child_id", "parent_id", "role"], unique=True)
    op.create_index("ix_lineage_parent", _TABLE, ["parent_id"])
    rows = bind.execute(
        sa.text("SELECT record_id, namespace, source FROM memory_records ORDER BY recorded_at")
    ).all()
    for record_id, namespace, blob in rows:
        try:
            source = orjson.loads(blob) if blob else {}
        except orjson.JSONDecodeError:
            continue
        for parent in dict.fromkeys(source.get("parents") or ()):
            if parent == record_id:
                continue
            bind.execute(
                sa.text(
                    "INSERT INTO lineage (child_id, parent_id, role, namespace) "
                    "VALUES (:c, :p, 'support', :ns)"
                ),
                {"c": record_id, "p": parent, "ns": namespace},
            )


def downgrade() -> None:
    if _TABLE in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(_TABLE)
