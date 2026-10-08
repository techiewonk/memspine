"""I6: ``session_key`` and ``source_role`` columns on memory_records, indexed by namespace.

A record's conversation id (``write_messages(session_id=...)``, kept in
``source.message_id``) and its speaker role (``source.role``) become real columns, so a
database can list one conversation of one user through an index instead of decoding
every source blob. Guarded add-column like 0002/0004: a fresh database built from the
live ``schema.py`` already has them. Existing rows are backfilled from their source
blob, so no rebuild is needed.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-08
"""

from __future__ import annotations

import orjson
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_TABLE = "memory_records"
_INDEX = "ix_memory_records_ns_session"


def _columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {col["name"] for col in inspector.get_columns(_TABLE)}


def _indexes() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {str(ix["name"]) for ix in inspector.get_indexes(_TABLE) if ix["name"]}


def upgrade() -> None:
    columns = _columns()
    added = False
    for name in ("session_key", "source_role"):
        if name not in columns:
            op.add_column(_TABLE, sa.Column(name, sa.String(), nullable=True))
            added = True
    if _INDEX not in _indexes():
        op.create_index(_INDEX, _TABLE, ["namespace", "session_key"])
    if not added:
        return
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT record_id, source FROM {_TABLE}")).all()
    for record_id, blob in rows:
        try:
            source = orjson.loads(blob) if blob else {}
        except orjson.JSONDecodeError:
            continue
        bind.execute(
            sa.text(
                f"UPDATE {_TABLE} SET session_key = :s, source_role = :r WHERE record_id = :id"
            ),
            {"s": source.get("message_id"), "r": source.get("role"), "id": record_id},
        )


def downgrade() -> None:
    if _INDEX in _indexes():
        op.drop_index(_INDEX, table_name=_TABLE)
    columns = _columns()
    for name in ("source_role", "session_key"):
        if name in columns:
            op.drop_column(_TABLE, name)
