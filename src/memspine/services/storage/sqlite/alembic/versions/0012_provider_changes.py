"""Durable provider change plans, fences and projection generations.

Revision ID: provider_changes
Revises: lf_e_layer_backfill
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from memspine.providers.identity import DEPLOYMENT_METADATA

revision = "provider_changes"
down_revision = "lf_e_layer_backfill"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "provider_change_plans" not in tables:
        op.create_table(
            "provider_change_plans",
            sa.Column("plan_id", sa.String(128), primary_key=True),
            sa.Column("deployment_id", sa.String(256), nullable=False, index=True),
            sa.Column("payload", sa.JSON(), nullable=False),
        )
    if "provider_change_operations" not in tables:
        op.create_table(
            "provider_change_operations",
            sa.Column("operation_id", sa.String(128), primary_key=True),
            sa.Column("plan_id", sa.String(128), nullable=False, unique=True),
            sa.Column("deployment_id", sa.String(256), nullable=False, index=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("operation_revision", sa.Integer(), nullable=False),
            sa.Column("fence_epoch", sa.Integer(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        )
    if "provider_change_fences" not in tables:
        op.create_table(
            "provider_change_fences",
            sa.Column("deployment_id", sa.String(256), primary_key=True),
            sa.Column("epoch", sa.Integer(), nullable=False),
            sa.Column("active_operation_id", sa.String(128), nullable=True),
        )
    if "provider_projection_generations" not in tables:
        op.create_table(
            "provider_projection_generations",
            sa.Column("deployment_id", sa.String(256), primary_key=True),
            sa.Column("logical_service_id", sa.String(256), primary_key=True),
            sa.Column("generation_id", sa.String(256), primary_key=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        )
    if "provider_projection_switches" not in tables:
        op.create_table(
            "provider_projection_switches",
            sa.Column("switch_id", sa.String(128), primary_key=True),
            sa.Column("deployment_id", sa.String(256), nullable=False, index=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        )

    # Identity is created by the shared provider metadata; production upgrades
    # include it here when that table has not already been installed.
    DEPLOYMENT_METADATA.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    tables = _tables()
    for name in (
        "provider_projection_switches",
        "provider_projection_generations",
        "provider_change_fences",
        "provider_change_operations",
        "provider_change_plans",
    ):
        if name in tables:
            op.drop_table(name)
    if "provider_deployment_identity" in _tables():
        op.drop_table("provider_deployment_identity")
