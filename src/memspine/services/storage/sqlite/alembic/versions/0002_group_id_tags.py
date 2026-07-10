"""D2: add group_id + tags sub-scoping columns to memory_records.

Guarded add-column: the 0001 baseline builds ``memory_records`` from the live
``schema.py`` metadata, which now already includes ``group_id``/``tags``, so a
*fresh* database reaches this revision with the columns present — this migration
then skips them. An existing file database stamped at 0001 *before* D2 lacks the
columns, so this adds them. Both paths converge on the same schema. Both columns
are nullable (no backfill): a pre-D2 row reads back ``group_id=None`` and
``tags=[]``.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_TABLE = "memory_records"


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {col["name"] for col in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    existing = _columns()
    if "group_id" not in existing:
        op.add_column(_TABLE, sa.Column("group_id", sa.String(), nullable=True))
        op.create_index("ix_memory_records_ns_group", _TABLE, ["namespace", "group_id"])
    if "tags" not in existing:
        op.add_column(_TABLE, sa.Column("tags", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    existing = _columns()
    if "tags" in existing:
        op.drop_column(_TABLE, "tags")
    if "group_id" in existing:
        op.drop_index("ix_memory_records_ns_group", table_name=_TABLE)
        op.drop_column(_TABLE, "group_id")
