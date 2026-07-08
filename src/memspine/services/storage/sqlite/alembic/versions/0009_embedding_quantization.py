"""E4: memory_embeddings gains the quantized-prefilter columns (ADR-020).

``quantized_vec`` holds the int8/binary codes of the (optionally Matryoshka-
truncated) vector and ``quantization`` records the scheme that built them. Both
are nullable.

This migration is SCHEMA-ONLY — it adds the two columns and nothing more; it
does NOT backfill codes. Every existing row keeps its float32 ``vector`` and
carries NULL prefilter columns. Enabling ``vector.quantization`` on a populated
database therefore does NOT retroactively code the old rows: they stay NULL
until a projector ``rebuild()`` replays the WRITE events and the VectorProjector
re-encodes each vector with the current settings (``upsert`` → ``_encode``).
Until that rebuild the store is a mixed table, and ``search_rescore``'s
float-fallback path scores the still-NULL rows so none are dropped in the
interim.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_embeddings", sa.Column("quantized_vec", sa.LargeBinary, nullable=True))
    op.add_column("memory_embeddings", sa.Column("quantization", sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column("memory_embeddings", "quantization")
    op.drop_column("memory_embeddings", "quantized_vec")
