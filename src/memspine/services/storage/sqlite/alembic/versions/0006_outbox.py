"""F-04: the durable outbox tables: outbox, outbox_progress, outbox_item, outbox_log.

The tables live in their own metadata (``memspine.runtime.outbox``), not in ``schema.py``, so the
0001 baseline does not build them: this revision does, guarded by ``has_table`` so a database whose
outbox was already created (``SqlJobStore.ensure_schema``) upgrades cleanly. Self-contained: it
repeats the DDL instead of importing the runtime module, so a later refactor cannot change what
this revision means. To rebase onto another revision, change ``down_revision`` only.

Revision ID: f04_outbox
Revises: 0006_lineage
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f04_outbox"
down_revision = "0006_lineage"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    have = _tables()
    if "outbox" not in have:
        op.create_table(
            "outbox",
            sa.Column("job_id", sa.String(), primary_key=True),
            sa.Column("seq", sa.Integer(), nullable=False, unique=True),
            sa.Column("tenant", sa.String(), nullable=False),
            sa.Column("owner", sa.String(), nullable=False),
            sa.Column("owner_kind", sa.String(), nullable=False),
            sa.Column("stage", sa.String(), nullable=False),
            sa.Column("target_offset", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("payload", sa.Text(), nullable=True),
            sa.Column("frozen", sa.Boolean(), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False),
            sa.Column("lease_owner", sa.String(), nullable=True),
            sa.Column("lease_acquired_at", sa.String(), nullable=True),
            sa.Column("lease_heartbeat_at", sa.String(), nullable=True),
            sa.Column("lease_until", sa.String(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("not_before", sa.String(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("failure_class", sa.String(), nullable=True),
            sa.Column("coalesced_into", sa.String(), nullable=True),
        )
        op.create_index("ix_outbox_state", "outbox", ["state"])
        op.create_index(
            "ix_outbox_scope_stage", "outbox", ["tenant", "owner", "owner_kind", "stage"]
        )
    if "outbox_progress" not in have:
        op.create_table(
            "outbox_progress",
            sa.Column("tenant", sa.String(), primary_key=True),
            sa.Column("owner", sa.String(), primary_key=True),
            sa.Column("owner_kind", sa.String(), primary_key=True),
            sa.Column("job_kind", sa.String(), primary_key=True),
            sa.Column("watermark", sa.Integer(), nullable=False),
            sa.Column("target_offset", sa.Integer(), nullable=True),
            sa.Column("holes", sa.Text(), nullable=False),
        )
    if "outbox_item" not in have:
        op.create_table(
            "outbox_item",
            sa.Column("tenant", sa.String(), primary_key=True),
            sa.Column("owner", sa.String(), primary_key=True),
            sa.Column("owner_kind", sa.String(), primary_key=True),
            sa.Column("stage", sa.String(), primary_key=True),
            sa.Column("item_offset", sa.Integer(), primary_key=True),
            sa.Column("payload", sa.Text(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
        )
    if "outbox_log" not in have:
        op.create_table(
            "outbox_log",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("at", sa.String(), nullable=False),
            sa.Column("job_id", sa.String(), nullable=False),
            sa.Column("op", sa.String(), nullable=False),
            sa.Column("presented_epoch", sa.Integer(), nullable=False),
            sa.Column("current_epoch", sa.Integer(), nullable=True),
            sa.Column("state", sa.String(), nullable=True),
            sa.Column("reason", sa.String(), nullable=False),
        )


def downgrade() -> None:
    have = _tables()
    for name in ("outbox_log", "outbox_item", "outbox_progress", "outbox"):
        if name in have:
            op.drop_table(name)
