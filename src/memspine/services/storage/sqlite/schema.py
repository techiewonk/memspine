"""Phase-0 DDL as SQLAlchemy Core metadata (D-36).

This is the schema contract for v0.1: every column later phases need already
exists here (E1 firewall, D-27 dedup, D-42 provenance/lifecycle), so migrations
stay additive. The initial Alembic migration mirrors this metadata exactly.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
)

__all__ = [
    "memory_embeddings",
    "memory_events",
    "memory_records",
    "metadata",
    "projector_offsets",
]

metadata = MetaData()

# The append-only source of truth (D0.1). Payload is canonical orjson, optionally
# zstd-compressed at rest (D-45); timestamps are ISO-8601 UTC strings.
# sqlite_autoincrement is load-bearing: without the AUTOINCREMENT keyword SQLite
# reuses rowids after rolling-mode pruning, and reused seqs would fall below
# projector high-water marks — events silently never projected.
memory_events = Table(
    "memory_events",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String, nullable=False, unique=True),
    Column("kind", String, nullable=False),
    Column("namespace", String, nullable=False),
    Column("ts", String, nullable=False),
    Column("actor", String, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("payload", LargeBinary, nullable=False),
    Column("compressed", Boolean, nullable=False, default=False),
    Column("fingerprint", String, nullable=False),
    Index("ix_memory_events_namespace", "namespace"),
    Index("ix_memory_events_kind", "kind"),
    sqlite_autoincrement=True,
)

# Durable high-water marks: one row per projector.
projector_offsets = Table(
    "projector_offsets",
    metadata,
    Column("projector_name", String, primary_key=True),
    Column("last_seq", Integer, nullable=False),
    Column("updated_at", String, nullable=False),
)

# Relational read model of the universal record (M1). JSON-shaped sub-objects
# (source/history/consent_tags/scoring) are canonical orjson blobs (D-38).
memory_records = Table(
    "memory_records",
    metadata,
    Column("record_id", String, primary_key=True),
    Column("namespace", String, nullable=False),
    Column("memory_type", String, nullable=False),
    Column("content", String, nullable=False),
    Column("content_fingerprint", String, nullable=False),
    Column("entity", String),
    Column("attribute", String),
    Column("valid_from", String, nullable=False),
    Column("valid_to", String),
    Column("recorded_at", String, nullable=False),
    Column("superseded_at", String),
    Column("source", LargeBinary, nullable=False),
    Column("status", String, nullable=False),
    Column("version", Integer, nullable=False),
    Column("history", LargeBinary, nullable=False),
    Column("evolve_to", String),
    Column("pii_tier", String, nullable=False),
    Column("consent_tags", LargeBinary, nullable=False),
    Column("scoring", LargeBinary, nullable=False),
    Column("trust", Float, nullable=False),
    Column("quarantined", Boolean, nullable=False),
    Column("instruction_flag", Boolean, nullable=False),
    # E1 quarantine promotion counter (migration 0005).
    Column("corroborations", Integer, nullable=False, server_default="0"),
    Column("simhash", Integer),
    Column("minhash_sig", LargeBinary),
    # M3 decay tier + M6/D-32 cold-tier compressed content (migration 0004).
    Column("tier", String, nullable=False, server_default="hot"),
    Column("content_zstd", LargeBinary),
    Index("ix_memory_records_ns_type", "namespace", "memory_type"),
    Index("ix_memory_records_fingerprint", "content_fingerprint"),
    Index("ix_memory_records_fact_key", "namespace", "entity", "attribute"),
    Index("ix_memory_records_tier", "tier"),
)

# Zero-dep vector fallback (P1): float32 little-endian vectors, brute-force
# cosine at query time. LanceDB [lance] is the scalable default; this table is
# a rebuildable projection like every other derived store (D0.1).
memory_embeddings = Table(
    "memory_embeddings",
    metadata,
    Column("record_id", String, primary_key=True),
    Column("namespace", String, nullable=False),
    Column("embedder_id", String, nullable=False),
    Column("dim", Integer, nullable=False),
    Column("vector", LargeBinary, nullable=False),
    Index("ix_memory_embeddings_ns", "namespace"),
)
