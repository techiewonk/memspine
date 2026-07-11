# ADR-003 — Store defaults: SQLite · LanceDB · LadybugDB · LMDB

- **Status:** accepted
- **Date:** 2026-07-07
- **Decision id:** D-09 / D-25 / D-26
- **Phase:** P0 (SQLite) → P1/P6 (vector/graph) · **Tier:** DF

## Context

The 2026-07-07 dependency scan (`DEPENDENCY_ANALYSIS.md`) shows the field converging on embedded, zero-server stores: LanceDB for vectors (+Tantivy FTS), kuzu-lineage embedded Cypher graphs (graphiti, cognee), LMDB for hot KV.

## Decision

SQLite (relational/event-log/FTS5) in core; **LanceDB in core** as the sole vector store (ADR-021, amends D-09); LadybugDB `[graph]` graph adapter with kuzu `[kuzu]` first-class alternative and `sqlite_adjacency` zero-dep fallback; LMDB `[lmdb]` hot cache. Lexical is a first-class port with FTS5/BM25 core default and RRF implemented once (D-25). Prod swap-ins (Postgres/Weaviate/Neo4j/Valkey) are stubs in v0.1.

**Amendment (ADR-021, 2026-07-09):** the P1 SQLite brute-force vector fallback and `[lance]` extra are removed. E4 rescore is LanceDB-native only.

## Consequences

- Positive: no servers to run; graph-adapter parity test (ladybug vs kuzu vs adjacency) keeps the port honest.
- Negative / cost: three embedded engines to version-pin; LadybugDB fork must be pinned at build time.
- Follow-up: P6 lands the graph adapters; parity tests in `tests/combinations/`.

## Alternatives rejected

- **Server-first (Postgres+pgvector+Neo4j)** — heavy OOTB; they remain swap-ins, not defaults.
- **igraph / community detection** — `leidenalg` over `igraph` covers Leiden community detection under `[community]` (D-40, per ADR-028; supersedes the earlier graspologic choice).
