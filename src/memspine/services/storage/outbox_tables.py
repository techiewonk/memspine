"""Outbox / job-store table definitions (the storage layer owns the schema).

Moved from ``runtime/outbox.py`` so the records port (``services/storage/sql_base.py``,
ADR-081) can write ``outbox_item`` rows atomically with an event without importing the
runtime layer. ``runtime/outbox.py`` (the job book) imports these definitions.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, Index, Integer, MetaData, String, Table, Text

__all__ = [
    "OUTBOX_TABLES",
    "outbox",
    "outbox_item",
    "outbox_log",
    "outbox_metadata",
    "outbox_progress",
]
outbox_metadata = MetaData()

outbox = Table(
    "outbox",
    outbox_metadata,
    Column("job_id", String, primary_key=True),
    Column("seq", Integer, nullable=False, unique=True),
    Column("tenant", String, nullable=False),
    Column("owner", String, nullable=False),
    Column("owner_kind", String, nullable=False),
    Column("stage", String, nullable=False),
    Column("target_offset", Integer, nullable=False),
    Column("state", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("payload", Text, nullable=True),
    Column("frozen", Boolean, nullable=False, default=False),
    Column("epoch", Integer, nullable=False, default=0),
    Column("lease_owner", String, nullable=True),
    Column("lease_acquired_at", String, nullable=True),
    Column("lease_heartbeat_at", String, nullable=True),
    Column("lease_until", String, nullable=True),
    Column("attempts", Integer, nullable=False, default=0),
    Column("not_before", String, nullable=True),
    Column("error", Text, nullable=True),
    Column("failure_class", String, nullable=True),
    Column("coalesced_into", String, nullable=True),
    Index("ix_outbox_state", "state"),
    Index("ix_outbox_scope_stage", "tenant", "owner", "owner_kind", "stage"),
)

outbox_progress = Table(
    "outbox_progress",
    outbox_metadata,
    Column("tenant", String, primary_key=True),
    Column("owner", String, primary_key=True),
    Column("owner_kind", String, primary_key=True),
    Column("job_kind", String, primary_key=True),
    Column("watermark", Integer, nullable=False),
    Column("target_offset", Integer, nullable=True),
    Column("holes", Text, nullable=False, default="[]"),
)

outbox_item = Table(
    "outbox_item",
    outbox_metadata,
    Column("tenant", String, primary_key=True),
    Column("owner", String, primary_key=True),
    Column("owner_kind", String, primary_key=True),
    Column("stage", String, primary_key=True),
    Column("item_offset", Integer, primary_key=True),
    Column("payload", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

outbox_log = Table(
    "outbox_log",
    outbox_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", String, nullable=False),
    Column("job_id", String, nullable=False),
    Column("op", String, nullable=False),
    Column("presented_epoch", Integer, nullable=False),
    Column("current_epoch", Integer, nullable=True),
    Column("state", String, nullable=True),
    Column("reason", String, nullable=False),
)

OUTBOX_TABLES: tuple[str, ...] = ("outbox", "outbox_progress", "outbox_item", "outbox_log")
