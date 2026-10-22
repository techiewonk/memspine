# Storage profile and provider topology review — 2026-10-22 (RM-108)

Context document for the RM-108 discussion. Status lives in `docs/PROVIDER_TODO.md`.
File references are relative to `src/memspine/`.

## 1. What the settings actually do

**`storage.profile` (embedded | server) switches exactly one behaviour:**

| Behaviour | Where |
|---|---|
| Postgres schema setup: `server` runs Alembic (`ensure_schema`); `embedded` runs `metadata.create_all` | `providers/builtin/storage.py:282`, `services/storage/postgres/engine.py:51-58` |
| Default provider when `storage.provider` is omitted (`server` gives postgres; otherwise sqlite) | `config/schema/storage.py:66-68` |
| Rejects `server` with any provider other than postgres | `config/schema/storage.py:73-76` |
| Mirror gate, which is effectively dead logic: only Neo4j declares the `mirror` capability | `runtime/container.py:1588` |

**What the profile does not change:**
- It changes no defaults: vector `lancedb`, graph `auto`, cache `memory` and lexical `tantivy` are the same under both profiles.
- The descriptions in `services/storage/profile.py` are wrong:
  - **pgvector:** described as "pgvector when installed", but it is only used when selected explicitly.
  - **Graph on Postgres:** described as "SQL adjacency on Postgres", but that combination is refused.

**`storage.provider` controls almost everything else:**
- which SQL builder runs;
- the SQL capabilities;
- placement of every file-backed projection: next to the SQLite file, or under `<data_dir>/memspine` on Postgres;
- whether graph `auto` and pgvector are eligible;
- where the sleep leases live.

## 2. Where everything lives

| Category | How the provider is chosen | Data location | Schema mechanism |
|---|---|---|---|
| Events, records, offsets, lineage, deployment identity, change/switch tables | `storage.provider` | SQL DB | Alembic or `create_all`. Some tables join the shared metadata **at import time**. |
| Outbox and job store | follows storage | SQL DB (`:memory:` gets a private DB) | creates its own tables on construction |
| Sleep leases | SQL capability | SQLite: **separate `<path>.sleep` file, raw sqlite3, not SQLCipher-encrypted**. Postgres: in the DB | `CREATE TABLE IF NOT EXISTS` |
| Vector lancedb (default) | explicit | `<base>.lance` files | Lance |
| Vector pgvector | explicit, needs Postgres | same Postgres, schema `memspine_vectors` | builder-provisioned, not Alembic |
| Vector weaviate | explicit | remote | `ensure_collection` |
| Lexical tantivy (default) | `read.lexical_provider` (under `read.*`) | `<base>.tantivy` files, single writer | on open |
| Lexical opensearch / weaviate | explicit | remote | `ensure_index` |
| Graph `auto` | Ladybug if installed, else `sqlite_adjacency` (SQLite only) | Ladybug: `<base>.ladybug` file. Adjacency: tables in the SQL DB | client DDL / metadata |
| Graph neo4j (primary or mirror) | explicit | remote | n/a |
| Cache | explicit | memory; disk at `./memspine.cache` **relative to the working directory (ignores placement)**; redis/valkey remote | n/a |
| Embedding pin, cost ledger, dead letters | features | `<base>.embedding-profile.json`, `<base>.cost`, `<base>.deadletters.sqlite` (raw sqlite3) | ad hoc |

## 3. Problems found

1. **RM-108, Postgres without the server profile.**
   - Schema comes from `create_all`, so there is no `alembic_version`.
   - Later column-adding migrations and data backfills never apply. The schema drifts silently.
   - Some tables exist only because of module import order.
   - The USAGE Postgres example omits `profile`, so it leads users onto this path.
2. **Multi-node divergence (severe).**
   - Projector offsets live in shared Postgres, keyed only by projector name.
   - LanceDB, Tantivy and Ladybug indexes live on each node's local `data_dir`.
   - Two nodes sharing one Postgres share offsets but not indexes, so node B silently skips events. Nothing validates against this.
3. **Concurrent migrations.** `ensure_schema` serialises with a thread lock only. Nodes starting together can race the Alembic upgrade; there is no advisory lock.
4. **Single-writer stores across processes.** Tantivy and Ladybug allow one writer per process. Separate worker processes on one `data_dir` contend for the writer.
5. **Graph `auto` depends on what is installed.**
   - Installing `[graph]` on SQLite switches the graph from adjacency tables to a Ladybug file. The projector-binding check then blocks startup with no hint why.
   - On Postgres, `auto` can only mean Ladybug.
   - SQL adjacency would port to Postgres with small changes (insert dialect).
6. **Unencrypted side files.** The `.sleep`, `.cost` and `.deadletters` side files use raw sqlite3, so they bypass SQLCipher encryption.
7. **`data_dir` is required on Postgres** even when every selected store is remote (pgvector, OpenSearch, Neo4j).
8. **The disk cache ignores placement** and writes relative to the working directory.
9. **Doc drift:**
   - the structure plan D-09/D-26/D-49 still say the graph default is `sqlite_adjacency`;
   - USAGE calls OpenSearch/Weaviate "future work" and Neo4j "reserved";
   - the encryption docs omit the side files.

## 4. Design options

**A. Profile as a named bundle of defaults.** `server` would mean pgvector, OpenSearch, SQL graph and redis.
- Gives one-line setups.
- Hidden conditional defaults go against ADR-068.
- Bundled packages may be missing.

**B. Drop `storage.profile`; derive everything from providers.**
- Every durable SQL DB always uses Alembic; `create_all` only for `:memory:`.
- The mirror gate becomes a capability check.
- SQL adjacency is ported to Postgres.
- Existing `create_all` Postgres DBs upgrade through the existing legacy stamp.
- This is a breaking key removal with no alias, and it reverses plan Q11, so it needs an ADR.

**C. Explicit deployment topology block.**
- `deployment.locality: single_process | multi_process | multi_node` and `deployment.data_dir`.
- Providers declare a locality trait: process, host or cluster.
- `multi_node` rejects host-local stores; `multi_process` rejects single-writer stores outside the writer process.
- `data_dir` is required only when a host-local store is selected.
- `lexical:` is promoted to a top-level block.
- Fixes problems 2, 4, 7 and 8. It is the largest change; `data_dir` defaults to the old base so fingerprints stay identical.

**D (recommended): B now, C next.**
- **Phase 1 (RM-108):**
  - Remove `storage.profile`.
  - Alembic for all durable SQL, plus an advisory lock (problem 3).
  - Capability-based mirror gate.
  - Port SQL adjacency to Postgres.
  - Fix the docs.
- **Phase 2:** the topology and locality validation from C, plus encrypted or relocated side files.
