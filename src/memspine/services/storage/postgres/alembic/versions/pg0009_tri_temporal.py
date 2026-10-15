"""Postgres mirror of SQLite f02b_tri_temporal (F-02b): tri-temporal columns on memory_records.

``event_approximate`` is an integer flag (0/1), as in SQLite, not a boolean, so the two trees
agree on the logical type.

Revision ID: pg_f02b_tri_temporal
Revises: pg_f04_outbox
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "pg_f02b_tri_temporal"
down_revision = "pg_f04_outbox"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("source_at", sa.String),
    ("source_precision", sa.String),
    ("event_precision", sa.String),
    ("event_approximate", sa.Integer),
    ("event_basis", sa.String),
)


def upgrade() -> None:
    for name, kind in _COLUMNS:
        op.add_column("memory_records", sa.Column(name, kind(), nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("memory_records", name)
