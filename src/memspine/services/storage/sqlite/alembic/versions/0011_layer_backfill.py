"""LF-E (MF-08): backfill NULL envelope cells, mirroring ``pg_lf_e_layer_backfill``.

0007 filled the envelope only when it added a column, so a row written by a build that left
``layer`` (or another envelope cell) NULL stayed NULL. This revision fills every cell that is still
NULL, from the row's own data, with the shared classification (``memspine.store.layer_classify``).
Idempotent and a no-op on a store the 0007 backfill already covered: only NULL cells are written.
``fingerprint`` stays NULL.
Downgrade is a no-op (the values equal what a record derives from a NULL).

Revision ID: lf_e_layer_backfill
Revises: w202_stamping
Create Date: 2026-10-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from memspine.store.layer_classify import envelope_backfill_values

revision = "lf_e_layer_backfill"
down_revision = "w202_stamping"
branch_labels = None
depends_on = None

_TABLE = "memory_records"
_CELLS = ("layer", "type_tag", "provenance_class", "spk", "addr", "ask", "sub")


def upgrade() -> None:
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns(_TABLE)}
    if not set(_CELLS) <= columns:
        return
    rows = bind.execute(
        sa.text(
            f"SELECT record_id, memory_type, source, tags FROM {_TABLE} "
            "WHERE layer IS NULL OR type_tag IS NULL OR provenance_class IS NULL OR sub IS NULL"
        )
    ).all()
    for record_id, memory_type, source_blob, tags_blob in rows:
        values = envelope_backfill_values(str(memory_type or ""), source_blob, tags_blob)
        sets = ", ".join(f"{name} = COALESCE({name}, :{name})" for name in _CELLS)
        bind.execute(
            sa.text(f"UPDATE {_TABLE} SET {sets} WHERE record_id = :id"),
            {**values, "id": record_id},
        )


def downgrade() -> None:
    """No-op: see the module docstring."""
