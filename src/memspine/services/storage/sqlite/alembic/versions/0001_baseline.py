"""Single v0.1 baseline schema (D-36, ADR-025 squash).

Pre-alpha: the incremental ``0001``-``0009`` migration chain was collapsed into
this one baseline — no production database exists, so there is no upgrade path
to preserve. This builds the **current** substrate from the single source of
truth (``schema.py``'s ``metadata``): the append-only event log, projector
offsets, the relational read model (with every E1/D-27/D-42 column), and the
zero-dep graph adjacency tables. It deliberately does NOT create
``memory_embeddings`` — LanceDB is the sole vector store (ADR-021) — nor any
lexical table: the BM25 leg is the standalone core Tantivy index (D-25, v0.2),
independent of the transactional store.

Because there is exactly one baseline, building from live ``metadata`` keeps the
migration and the schema permanently in lock-step — it can never drift.

Revision ID: 0001
Revises:
Create Date: 2026-07-10
"""

from __future__ import annotations

from alembic import op

from memspine.services.storage.sqlite.schema import metadata

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Every modeled table (event log + read model + graph adjacency), straight
    # from schema.py — includes sqlite_autoincrement on memory_events.seq and
    # all server_defaults (tier='hot', corroborations=0, reflection_depth=0).
    # v0.2: no lexical table here — the BM25 leg is the standalone core Tantivy
    # index (D-25), independent of the transactional store.
    metadata.create_all(bind)


def downgrade() -> None:
    metadata.drop_all(op.get_bind())
