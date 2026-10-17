"""Bind durable projector offsets to their provider target.

Revision ID: projector_bindings
Revises: provider_changes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "projector_bindings"
down_revision = "provider_changes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "provider_projector_bindings" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "provider_projector_bindings",
            sa.Column("deployment_id", sa.String(256), primary_key=True),
            sa.Column("projector_name", sa.String(256), primary_key=True),
            sa.Column("provider", sa.String(256), nullable=False),
            sa.Column("target_fingerprint", sa.String(64), nullable=False),
            sa.Column("generation_id", sa.String(128), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
        )


def downgrade() -> None:
    if "provider_projector_bindings" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("provider_projector_bindings")
