"""Postgres baseline (F-16d, M-04): event log, projector offsets, read model, graph adjacency.

Explicit DDL, the columns the SQLite 0001 baseline held before 0002..0009 added theirs. Frozen:
later revisions add columns, this one is never edited.

Revision ID: pg0001
Revises:
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "pg0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_events",
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(), nullable=False, unique=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("ts", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("compressed", sa.Boolean(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
    )
    op.create_index("ix_memory_events_namespace", "memory_events", ["namespace"])
    op.create_index("ix_memory_events_kind", "memory_events", ["kind"])
    op.create_table(
        "projector_offsets",
        sa.Column("projector_name", sa.String(), primary_key=True),
        sa.Column("last_seq", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
    )
    op.create_table(
        "memory_records",
        sa.Column("record_id", sa.String(), primary_key=True),
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("memory_type", sa.String(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("content_fingerprint", sa.String(), nullable=False),
        sa.Column("entity", sa.String()),
        sa.Column("attribute", sa.String()),
        sa.Column("valid_from", sa.String(), nullable=False),
        sa.Column("valid_to", sa.String()),
        sa.Column("recorded_at", sa.String(), nullable=False),
        sa.Column("superseded_at", sa.String()),
        sa.Column("source", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("history", sa.LargeBinary(), nullable=False),
        sa.Column("evolve_to", sa.String()),
        sa.Column("pii_tier", sa.String(), nullable=False),
        sa.Column("consent_tags", sa.LargeBinary(), nullable=False),
        sa.Column("scoring", sa.LargeBinary(), nullable=False),
        sa.Column("trust", sa.Float(), nullable=False),
        sa.Column("quarantined", sa.Boolean(), nullable=False),
        sa.Column("instruction_flag", sa.Boolean(), nullable=False),
        sa.Column("corroborations", sa.Integer(), nullable=False, server_default="0"),
        # 64-bit simhash: BIGINT here (the SQLite INTEGER is 64-bit, a Postgres INTEGER is not).
        sa.Column("simhash", sa.BigInteger()),
        sa.Column("minhash_sig", sa.LargeBinary()),
        sa.Column("tier", sa.String(), nullable=False, server_default="hot"),
        sa.Column("content_zstd", sa.LargeBinary()),
        sa.Column("skill_stage", sa.String()),
        sa.Column("reflection_depth", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_memory_records_ns_type", "memory_records", ["namespace", "memory_type"])
    op.create_index("ix_memory_records_fingerprint", "memory_records", ["content_fingerprint"])
    op.create_index(
        "ix_memory_records_fact_key", "memory_records", ["namespace", "entity", "attribute"]
    )
    op.create_index("ix_memory_records_tier", "memory_records", ["tier"])
    op.create_table(
        "graph_nodes",
        sa.Column("node_id", sa.String(), primary_key=True),
        sa.Column("labels", sa.LargeBinary(), nullable=False),
        sa.Column("properties", sa.LargeBinary(), nullable=False),
    )
    op.create_table(
        "graph_edges",
        sa.Column("src", sa.String(), primary_key=True),
        sa.Column("dst", sa.String(), primary_key=True),
        sa.Column("rel_type", sa.String(), primary_key=True),
        sa.Column("properties", sa.LargeBinary(), nullable=False),
    )
    op.create_index("ix_graph_edges_dst", "graph_edges", ["dst"])


def downgrade() -> None:
    for name in (
        "graph_edges",
        "graph_nodes",
        "memory_records",
        "projector_offsets",
        "memory_events",
    ):
        op.drop_table(name)
