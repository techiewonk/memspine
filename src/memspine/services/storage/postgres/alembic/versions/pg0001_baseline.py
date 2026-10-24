"""Complete fresh postgres schema for the first development cycle."""

from __future__ import annotations

from alembic import op

from memspine.services.storage.baseline import create_schema, drop_schema

revision = "pg0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    create_schema(op.get_bind())


def downgrade() -> None:
    drop_schema(op.get_bind())
