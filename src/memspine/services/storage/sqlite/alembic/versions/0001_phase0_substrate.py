"""Phase-0 substrate DDL: memory_events, projector_offsets, memory_records.

Mirrors ``schema.py`` exactly — includes the E1 firewall columns, D-27 dedup
fields, and D-42 provenance/lifecycle columns so later phases stay additive.

Revision ID: 0001
Revises:
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_events",
        sa.Column("seq", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String, nullable=False, unique=True),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("namespace", sa.String, nullable=False),
        sa.Column("ts", sa.String, nullable=False),
        sa.Column("actor", sa.String, nullable=False),
        sa.Column("schema_version", sa.Integer, nullable=False),
        sa.Column("payload", sa.LargeBinary, nullable=False),
        sa.Column("compressed", sa.Boolean, nullable=False),
        sa.Column("fingerprint", sa.String, nullable=False),
        # AUTOINCREMENT prevents rowid reuse after rolling-mode pruning (D-45):
        # a reused seq would fall below projector high-water marks.
        sqlite_autoincrement=True,
    )
    op.create_index("ix_memory_events_namespace", "memory_events", ["namespace"])
    op.create_index("ix_memory_events_kind", "memory_events", ["kind"])

    op.create_table(
        "projector_offsets",
        sa.Column("projector_name", sa.String, primary_key=True),
        sa.Column("last_seq", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.String, nullable=False),
    )

    op.create_table(
        "memory_records",
        sa.Column("record_id", sa.String, primary_key=True),
        sa.Column("namespace", sa.String, nullable=False),
        sa.Column("memory_type", sa.String, nullable=False),
        sa.Column("content", sa.String, nullable=False),
        sa.Column("content_fingerprint", sa.String, nullable=False),
        sa.Column("valid_from", sa.String, nullable=False),
        sa.Column("valid_to", sa.String),
        sa.Column("recorded_at", sa.String, nullable=False),
        sa.Column("superseded_at", sa.String),
        sa.Column("source", sa.LargeBinary, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("history", sa.LargeBinary, nullable=False),
        sa.Column("evolve_to", sa.String),
        sa.Column("pii_tier", sa.String, nullable=False),
        sa.Column("consent_tags", sa.LargeBinary, nullable=False),
        sa.Column("scoring", sa.LargeBinary, nullable=False),
        sa.Column("trust", sa.Float, nullable=False),
        sa.Column("quarantined", sa.Boolean, nullable=False),
        sa.Column("instruction_flag", sa.Boolean, nullable=False),
        sa.Column("simhash", sa.Integer),
        sa.Column("minhash_sig", sa.LargeBinary),
    )
    op.create_index("ix_memory_records_ns_type", "memory_records", ["namespace", "memory_type"])
    op.create_index("ix_memory_records_fingerprint", "memory_records", ["content_fingerprint"])


def downgrade() -> None:
    op.drop_table("memory_records")
    op.drop_table("projector_offsets")
    op.drop_table("memory_events")
