"""P7/D-25: memory_fts — lexical BM25 projection (FTS5 virtual table).

Rebuildable projection: the LexicalProjector re-indexes record content from the
event log, so this migration only builds the (empty) index structure. On a
build without the FTS5 module it falls back to a plain table so an in-place
upgrade never hard-fails (the store's LIKE path matches, D-25); the store's own
probe agrees on which path this build takes.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-08
"""

from __future__ import annotations

from alembic import op
from sqlalchemy.exc import OperationalError

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_TABLE = "memory_fts"


def upgrade() -> None:
    try:
        op.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {_TABLE} "
            "USING fts5(record_id UNINDEXED, namespace UNINDEXED, content)"
        )
    except OperationalError:
        # FTS5 not compiled into this build — plain-table fallback (D-25 ILIKE).
        op.execute(
            f"CREATE TABLE IF NOT EXISTS {_TABLE} "
            "(record_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, content TEXT NOT NULL)"
        )


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
