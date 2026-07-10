# ADR-025 — PostgreSQL storage backend + dialect-neutral SqlStorage base

**Status:** accepted · **Date:** 2026-07-10 · **Register:** amends D-36 (storage/migrations) · **Phase:** services/* completion — Phase 6 (last)

## Context

Storage was SQLite-only end-to-end: `SQLiteStorage` hard-wired
`sqlalchemy.dialects.sqlite.insert` for its upserts and `func.max(a, b)` for the
advance-only offset high-water mark, and the engine constructed a `SQLiteClient`
unconditionally. There was no way to run the event log + relational read model
on **PostgreSQL** — the standard production database — even though the schema
itself is almost entirely portable SQLAlchemy Core.

## Decision

1. **Extract a dialect-neutral `SqlStorage` base** (`services/storage/sql_base.py`)
   holding every event-log/read-model method (the D-45 modes, zstd payloads, M7
   erasure). The only dialect-specific pieces are dispatched at runtime from the
   bound engine's dialect:
   - `_insert(table)` → `sqlalchemy.dialects.{sqlite,postgresql}.insert` (both
     expose `on_conflict_do_update` with the same shape).
   - `_greatest(a, b)` → Postgres `greatest(a, b)` vs SQLite scalar `max(a, b)`.
2. **`SQLiteStorage` and `PostgresStorage` are thin subclasses** supplying only
   `start()` (schema bring-up) and `manifest` (backend name). Behavior on SQLite
   is unchanged (the full test suite is byte-identical green).
3. **`clients/postgres.py` `PostgresClient`** owns the async engine over
   **psycopg3** (`postgresql+psycopg://`). psycopg3 is chosen over asyncpg
   because it gives a sync driver (Alembic has no async API) and an async driver
   from one package. It pings at `connect()` so a bad DSN fails at start (D-10).
4. **One dialect-neutral schema, materialized two ways.** Both backends use the
   SAME `metadata`: SQLite via Alembic (files) / `create_all` (`:memory:`),
   Postgres via `create_all`. `memory_events.seq` (INTEGER PK, autoincrement)
   compiles to `SERIAL` on Postgres (monotonic identity) — verified by compiling
   the DDL + upserts against the Postgres dialect. `sqlite_autoincrement` is a
   SQLite-only table option Postgres ignores.
5. **Config** (`StorageConfig`): `backend: sqlite|postgres`; `url` (DSN,
   secrets-resolved by the config loader) required for postgres; `data_dir` as
   the base directory for the file-backed projections (LanceDB vectors, Tantivy
   lexical) that live OUTSIDE the SQL database. `engine._build_storage()`
   dispatches; `describe()["storage"]["backend"]` reports the real backend.
6. **SQLite-specific projections are guarded.** `read.lexical_provider =
   sqlite_fts5` (a SQLite FTS5 virtual table) and `graph.provider =
   sqlite_adjacency` require `backend=sqlite`; with postgres they raise a
   `ConfigError` pointing to `tantivy` / `kuzu`/`ladybug`. The default profile
   (hybrid off, associative off) needs neither, so a basic Postgres deployment
   works unchanged. Vectors always live in LanceDB regardless of SQL backend
   (`pgvector` stays dormant — the vector store is not the SQL database).

## Scope note (honest deviation from the plan)

The plan called for collapsing the SQLite Alembic tree into a single
dialect-neutral baseline and a shared Postgres Alembic `env.py`. That squash is
**deferred**: the SQLite Alembic tree is kept as-is (the real migration path for
existing SQLite deployments), and Postgres uses `create_all` from the shared
metadata. For pre-alpha this yields the same schema deterministically without a
history rewrite; the Postgres Alembic tree + squash is a documented follow-up.
The dialect-neutral schema *content* and the shared `SqlStorage` base — the
load-bearing parts of "full parity" — are delivered.

## Consequences

- Positive: the event log + read model run identically on SQLite and Postgres
  (proven by a storage-contract test parametrized over both). One storage code
  path; one schema.
- Testing: Postgres integration is skip-if-unavailable (`MEMSPINE_TEST_POSTGRES_URL`);
  the dialect correctness is additionally proven offline by compiling DDL +
  upserts against the Postgres dialect.
- Limitation: FTS5 lexical and SQLite-adjacency graph are SQLite-only; Postgres
  deployments use Tantivy / Kuzu / Ladybug for those (guarded with a clear error).
- Dependency: `[postgres]` is `psycopg[binary]` + `pgvector` (dormant); `asyncpg`
  dropped (psycopg3 covers both sync-Alembic and async-runtime).

## Alternatives rejected

- **asyncpg** — no sync driver, so Alembic (sync-only) would need a *second*
  Postgres driver; psycopg3 serves both from one package.
- **pgvector as the Postgres vector store** — vectors live in LanceDB for every
  backend (ADR-021); duplicating them into pgvector splits the vector story.
- **Duplicate the storage class per backend** — the logic is identical bar two
  dialect calls; a shared base is the DRY, less-drift design.
