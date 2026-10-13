"""Postgres mirror of SQLite w202_stamping (W2-02): stamping columns on memory_records.

``source_tz`` and ``participants`` are added.

``participants`` is bytea (an orjson list), as ``sub`` and ``fingerprint`` are.

Revision ID: pg_w202_stamping
Revises: pg_f02b_tri_temporal
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "pg_w202_stamping"
down_revision = "pg_f02b_tri_temporal"
branch_labels = None
depends_on = None

_COLUMNS = (("source_tz", sa.String), ("participants", sa.LargeBinary))


def upgrade() -> None:
    for name, kind in _COLUMNS:
        op.add_column("memory_records", sa.Column(name, kind(), nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("memory_records", name)
