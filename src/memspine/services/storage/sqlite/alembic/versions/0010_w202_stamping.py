"""W2-02: stamping columns on memory_records (W-04, W-05).

Adds ``source_tz`` (the zone the source time was said in; ``unknown`` for a naive time nobody gave a
zone for) and ``participants`` (orjson list, speaker first). Both nullable: NULL = not stamped, and
nothing is back-filled (a zone or a participant list is never invented for an old row; the v1
``participant:`` tags stay readable). Guarded add-column like 0007/0009: a fresh database built from
the live ``schema.py`` already has them. Downgrade drops them.

Revision ID: w202_stamping
Revises: f02b_tri_temporal
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "w202_stamping"
down_revision = "f02b_tri_temporal"
branch_labels = None
depends_on = None

_TABLE = "memory_records"
_COLUMNS = (("source_tz", sa.String), ("participants", sa.LargeBinary))


def _columns() -> set[str]:
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    columns = _columns()
    for name, kind in _COLUMNS:
        if name not in columns:
            op.add_column(_TABLE, sa.Column(name, kind(), nullable=True))


def downgrade() -> None:
    columns = _columns()
    for name, _kind in reversed(_COLUMNS):
        if name in columns:
            op.drop_column(_TABLE, name)
