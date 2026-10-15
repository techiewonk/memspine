# ruff: noqa: E501
"""LF-E (MF-08): backfill the record envelope on Postgres rows written before pg0007.

pg0007 added ``layer``, ``type_tag``, ``provenance_class``, ``spk``, ``addr``, ``ask`` and ``sub`` without
filling them (SQLite 0007 does). This revision fills every one that is still NULL, from the row's own
data, with the same classification new records and ``layer_counts`` use
(``memspine.store.layer_classify``). Idempotent: only NULL cells are written and nothing else is touched,
so a second run, or a database that never had NULLs, changes nothing. ``fingerprint`` stays NULL (a record
derives it from its content fingerprint and prompt version).

Downgrade is a no-op: the backfilled values equal what the record derives from a NULL, so removing them
would not restore anything that was lost.

Revision ID: pg_lf_e_layer_backfill
Revises: pg_w202_stamping
Create Date: 2026-10-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from memspine.store.layer_classify import envelope_backfill_values

revision = "pg_lf_e_layer_backfill"
down_revision = "pg_w202_stamping"
branch_labels = None
depends_on = None

_TABLE = "memory_records"
_CELLS = ("layer", "type_tag", "provenance_class", "spk", "addr", "ask", "sub")


def upgrade() -> None:
    bind = op.get_bind()
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
