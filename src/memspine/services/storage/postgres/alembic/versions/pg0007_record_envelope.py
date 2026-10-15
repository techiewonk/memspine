"""Postgres mirror of SQLite 0007 (F-02a): the record envelope columns on memory_records.

Revision ID: pg0007
Revises: pg0006_lineage
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "pg0007"
down_revision = "pg0006_lineage"
branch_labels = None
depends_on = None

_TEXT = ("layer", "type_tag", "provenance_class", "spk", "addr", "ask")
_BLOB = ("sub", "fingerprint")


def upgrade() -> None:
    for name in _TEXT:
        op.add_column("memory_records", sa.Column(name, sa.String(), nullable=True))
    for name in _BLOB:
        op.add_column("memory_records", sa.Column(name, sa.LargeBinary(), nullable=True))
    op.add_column(
        "memory_records",
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_memory_records_ns_layer", "memory_records", ["namespace", "layer"])


def downgrade() -> None:
    op.drop_index("ix_memory_records_ns_layer", table_name="memory_records")
    for name in ("schema_version", *_BLOB, *reversed(_TEXT)):
        op.drop_column("memory_records", name)
