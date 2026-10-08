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
    "graph_edges",
    "graph_nodes",
    "memory_events",
    "memory_records",
    "metadata",
    "projector_offsets",
]

metadata = MetaData()

# NOTE (v0.2): the transactional-DB FTS5 lexical projection was removed with the
# ``sqlite_fts5`` provider — the lexical/BM25 leg is now the standalone core
# Tantivy index (D-25), independent of the storage backend, so no lexical table
# lives in this schema anymore.

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
    # M13.4 procedural stage + M13.7 reflective depth (migration 0006).
    Column("skill_stage", String),
    Column("reflection_depth", Integer, nullable=False, server_default="0"),
    # D2 sub-scoping facet (migration 0002): group_id + orjson tags blob. Both
    # nullable — a NULL tags column reads back as an empty list.
    Column("group_id", String),
    Column("tags", LargeBinary),
    # #19 interval arithmetic (migration 0004): when the fact stopped being true
    # in the world; NULL = not known (always, unless conflict.interval_order).
    Column("invalid_at", String),
    # I6 (migration 0005): the conversation id (``source.message_id``) and speaker
    # role (``source.role``) as indexed columns, so one user's conversation is listed
    # through an index.
    Column("session_key", String),
    Column("source_role", String),
    Index("ix_memory_records_ns_type", "namespace", "memory_type"),
    Index("ix_memory_records_fingerprint", "content_fingerprint"),
    Index("ix_memory_records_fact_key", "namespace", "entity", "attribute"),
    Index("ix_memory_records_tier", "tier"),
    Index("ix_memory_records_ns_group", "namespace", "group_id"),
    Index("ix_memory_records_ns_session", "namespace", "session_key"),
)

# Zero-dep graph fallback (P6, D-26): adjacency lists for associative memory,
# the zero-dep default (ladybug is the embedded graph engine, ADR-034). Labels/
# properties are canonical orjson blobs (D-38). A rebuildable projection like
# every other derived store (D0.1) — never a second source of truth.
#
# KB-1 (migration 0003): ``namespace`` on nodes and edges (tenant isolation:
# PPR, BFS and Leiden filter on it); ``weight`` as a REAL column (the walk
# weight, mirrored from the properties blob so the recursive-CTE BFS can rank
# and drop tombstones in SQL); ``kind`` (node: its memory type; edge: the
# optional ``kind`` property, e.g. state/event).
graph_nodes = Table(
    "graph_nodes",
    metadata,
    Column("node_id", String, primary_key=True),
    Column("labels", LargeBinary, nullable=False),
    Column("properties", LargeBinary, nullable=False),
    Column("namespace", String, nullable=False, server_default=""),
    Column("kind", String),
    Index("ix_graph_nodes_namespace", "namespace"),
)

graph_edges = Table(
    "graph_edges",
    metadata,
    Column("src", String, primary_key=True),
    Column("dst", String, primary_key=True),
    Column("rel_type", String, primary_key=True),
    Column("properties", LargeBinary, nullable=False),
    Column("namespace", String, nullable=False, server_default=""),
    Column("weight", Float, nullable=False, server_default="1.0"),
    Column("kind", String),
    # src lookups ride the composite PK; dst lookups need their own index for
    # the undirected traversal neighbors()/edges_of() perform. The namespace
    # indexes serve the per-tenant walk, ranked by weight (KB-1).
    Index("ix_graph_edges_dst", "dst"),
    Index("ix_graph_edges_ns_src_weight", "namespace", "src", "weight"),
    Index("ix_graph_edges_ns_dst_weight", "namespace", "dst", "weight"),
)

# NOTE(ADR-021/ADR-025): ``memory_embeddings`` (the removed SQLite brute-force
# vector fallback) is intentionally absent — LanceDB is the sole vector store,
# so ``create_all`` never materializes it. The ADR-025 migration squash
# collapsed the old incremental 0001-0009 chain into a single ``0001_baseline``
# that builds exactly this ``metadata`` — no upgrade path to preserve in a
# pre-alpha project. (v0.2 also removed the FTS5 lexical virtual table with the
# ``sqlite_fts5`` provider; the lexical leg is the standalone core Tantivy index.)
