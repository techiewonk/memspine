"""Persist proof-bearing recovery of abandoned provider cleanup fences.

Revision ID: pg_cleanup_fence_recovery
Revises: pg_projector_bindings
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "pg_cleanup_fence_recovery"
down_revision = "pg_projector_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "provider_cleanup_fence_recoveries" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "provider_cleanup_fence_recoveries",
            sa.Column("recovery_id", sa.String(128), primary_key=True),
            sa.Column("deployment_id", sa.String(256), nullable=False, index=True),
            sa.Column("operation_id", sa.String(128), nullable=False, index=True),
            sa.Column("proof_id", sa.String(128), nullable=False, unique=True),
            sa.Column("recovered_at", sa.String(64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        )


def downgrade() -> None:
    if "provider_cleanup_fence_recoveries" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("provider_cleanup_fence_recoveries")
