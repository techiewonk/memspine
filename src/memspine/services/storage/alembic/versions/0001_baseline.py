"""Complete fresh SQL schema for the first development cycle."""

from __future__ import annotations

from alembic import op

from memspine.services.storage.baseline import create_schema, drop_schema

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# ── RM-130 (ADR-089) entity name index ──────────────────────────────────────────────
# The ``entity_index`` table (services/storage/entity_index_tables.py) is created by
# ``create_schema`` through ``schema_metadata()``: a rebuildable projection of the entity,
# alias, link and merge events, cleared and replayed by the ``entity_index`` projector.

# ── RM-159 outbox job indexes ───────────────────────────────────────────────────────
# ``outbox`` (services/storage/outbox_tables.py) carries ``ix_outbox_state_seq``
# (claim / live jobs / expired leases), ``ix_outbox_scope_stage_state`` (live jobs per
# scope, finished-job prune) and the partial unique ``ux_outbox_running_key`` (one running
# job per (scope, stage), WHERE state = 'running'). They replace ``ix_outbox_state`` and
# ``ix_outbox_scope_stage``; first cycle, so the baseline is edited in place.


def upgrade() -> None:
    create_schema(op.get_bind())


def downgrade() -> None:
    drop_schema(op.get_bind())
