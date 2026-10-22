"""RM-111 WP5 (ADR-081): sleep leases, the cost ledger and dead letters move into the database.

Before this revision they lived in raw-sqlite3 side files (``<path>.sleep``, ``<path>.cost``,
``<path>.deadletters.sqlite``) that bypassed SQLCipher. The tables: ``sleep_lease``,
``sleep_cycle``, ``sleep_watermark``, ``sleep_state``, ``sleep_deferred``, ``sleep_fence_log``,
``cost_entry``, ``dead_letters`` and ``side_file_imports`` (the one-time import record). Each is
created only when missing, so a table the runtime already made (``memspine.store.side_tables``)
upgrades cleanly. Self-contained: the DDL is repeated here, not imported.

Revision ID: rm111_side_tables
Revises: cleanup_fence_recovery
Create Date: 2026-10-23
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "rm111_side_tables"
down_revision = "cleanup_fence_recovery"
branch_labels = None
depends_on = None

_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

_ORDER = (
    "sleep_lease",
    "sleep_cycle",
    "sleep_watermark",
    "sleep_state",
    "sleep_deferred",
    "sleep_fence_log",
    "cost_entry",
    "dead_letters",
    "side_file_imports",
)


def _columns(name: str) -> list[sa.Column[Any]]:
    text, integer, real = sa.Text(), sa.Integer(), sa.Float()
    tables: dict[str, list[sa.Column[Any]]] = {
        "sleep_lease": [
            sa.Column("ns", text, primary_key=True),
            sa.Column("owner", text, nullable=False),
            sa.Column("epoch", integer, nullable=False),
            sa.Column("job_id", text, nullable=False),
            sa.Column("lease_until", real, nullable=False),
            sa.Column("heartbeat_at", real, nullable=False),
        ],
        "sleep_cycle": [
            sa.Column("ns", text, primary_key=True),
            sa.Column("kind", text, primary_key=True),
            sa.Column("cycle", integer, nullable=False),
            sa.Column("status", text, nullable=False),
        ],
        "sleep_watermark": [
            sa.Column("ns", text, primary_key=True),
            sa.Column("kind", text, primary_key=True),
            sa.Column("cycle", integer, primary_key=True),
            sa.Column("stage", text, primary_key=True),
            sa.Column("seq", integer, nullable=False),
            sa.Column("done_at", real, nullable=False),
        ],
        "sleep_state": [
            sa.Column("ns", text, primary_key=True),
            sa.Column("deposits_since_run", integer, nullable=False, server_default="0"),
            sa.Column("last_deposit_at", real, nullable=True),
            sa.Column("last_run_at", real, nullable=True),
            sa.Column("context_pressure", real, nullable=False, server_default="0"),
            sa.Column("last_maintenance_at", real, nullable=True),
        ],
        "sleep_deferred": [
            sa.Column("ns", text, primary_key=True),
            sa.Column("kind", text, primary_key=True),
            sa.Column("stage", text, primary_key=True),
            sa.Column("pending", integer, nullable=False),
            sa.Column("total", integer, nullable=False),
            sa.Column("last_cycle", integer, nullable=False),
        ],
        "sleep_fence_log": [
            sa.Column("id", _ID, primary_key=True, autoincrement=True),
            sa.Column("at", real, nullable=False),
            sa.Column("ns", text, nullable=False),
            sa.Column("op", text, nullable=False),
            sa.Column("presented_epoch", integer, nullable=False),
            sa.Column("current_epoch", integer, nullable=True),
        ],
        "cost_entry": [
            sa.Column("id", _ID, primary_key=True, autoincrement=True),
            sa.Column("run", text, nullable=False),
            sa.Column("ns_hash", text, nullable=False),
            sa.Column("layer", text, nullable=False),
            sa.Column("stage", text, nullable=False),
            sa.Column("kind", text, nullable=False),
            sa.Column("model", text, nullable=True),
            sa.Column("calls", integer, nullable=False),
            sa.Column("tokens_in", integer, nullable=False),
            sa.Column("tokens_out", integer, nullable=False),
            sa.Column("latency_ms", real, nullable=False),
            sa.Column("outcome", text, nullable=False),
        ],
        "dead_letters": [
            sa.Column("id", _ID, primary_key=True, autoincrement=True),
            sa.Column("at", real, nullable=False),
            sa.Column("ns", text, nullable=False),
            sa.Column("window_id", text, nullable=False),
            sa.Column("kind", text, nullable=False),
            sa.Column("item_index", integer, nullable=False),
            sa.Column("code", text, nullable=False),
            sa.Column("detail", text, nullable=False),
            sa.Column("attempts", integer, nullable=False),
            sa.Column("retry_allowed", integer, nullable=False),
            sa.Column("item_hash", text, nullable=True),
            sa.Column("content", text, nullable=True),
        ],
        "side_file_imports": [
            sa.Column("kind", text, primary_key=True),
            sa.Column("digest", text, primary_key=True),
            sa.Column("source", text, nullable=False),
            sa.Column("rows", integer, nullable=False),
            sa.Column("imported_at", real, nullable=False),
        ],
    }
    return tables[name]


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    have = set(inspector.get_table_names())
    for name in _ORDER:
        if name not in have:
            op.create_table(name, *_columns(name))
    indexes = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes("dead_letters")}
    if "dead_letters_ns" not in indexes:
        op.create_index("dead_letters_ns", "dead_letters", ["ns", "id"])


def downgrade() -> None:
    have = set(sa.inspect(op.get_bind()).get_table_names())
    for name in reversed(_ORDER):
        if name in have:
            op.drop_table(name)
