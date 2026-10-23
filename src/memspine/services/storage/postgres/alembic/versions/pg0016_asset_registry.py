"""Postgres mirror of SQLite rm113_asset_registry (RM-113 item 18, ADR-081): asset registry table.

Before this revision the E04 asset registry was a raw-sqlite3 sidecar (``registry.sqlite`` under
``ingest.asset_dir``) private to one host. The table ``asset_registry`` holds one row per
``(namespace, asset_id)``: identity (source turn, original URI, caption), availability, content
hash, mime, size and the description result, shared by every process on the database. Created
only when missing, so a table the runtime already made
(``memspine.services.assets.sql_registry``) upgrades cleanly. Self-contained: the DDL is
repeated here, not imported.

Revision ID: pg_rm113_asset_registry
Revises: pg_rm111_side_tables
Create Date: 2026-10-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "pg_rm113_asset_registry"
down_revision = "pg_rm111_side_tables"
branch_labels = None
depends_on = None

_INDEXES = (
    ("asset_registry_hash", ["namespace", "content_hash"]),
    ("asset_registry_record", ["namespace", "record_id"]),
)


def upgrade() -> None:
    text, integer, real = sa.Text(), sa.Integer(), sa.Float()
    if "asset_registry" not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            "asset_registry",
            sa.Column("namespace", text, primary_key=True),
            sa.Column("asset_id", text, primary_key=True),
            sa.Column("source_turn_id", text, nullable=False),
            sa.Column("uri", text, nullable=False),
            sa.Column("kind", text, nullable=False),
            sa.Column("caption", text, nullable=True),
            sa.Column("search_hint", text, nullable=True),
            sa.Column("record_id", text, nullable=True),
            sa.Column("availability", text, nullable=False),
            sa.Column("content_hash", text, nullable=True),
            sa.Column("mime", text, nullable=True),
            sa.Column("size", integer, nullable=True),
            sa.Column("evidence_status", text, nullable=False),
            sa.Column("evidence", text, nullable=True),
            sa.Column("evidence_backend", text, nullable=True),
            sa.Column("error", text, nullable=True),
            sa.Column("updated_at", real, nullable=False),
        )
    have = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes("asset_registry")}
    for name, columns in _INDEXES:
        if name not in have:
            op.create_index(name, "asset_registry", columns)


def downgrade() -> None:
    if "asset_registry" in set(sa.inspect(op.get_bind()).get_table_names()):
        op.drop_table("asset_registry")
