# Client / adapter / factory standard — audit and plan (2026-10-23, RM-113)

Context document for the owner request: "client connector and factory separate, structured and
standard, for isolation". Status lives in `docs/PROVIDER_TODO.md` (RM-113). Pairs with ADR-081
(storage slots). Paths are relative to `src/memspine/`.

## 1. The standard

| Layer | Location | Job | May import | Must not |
|---|---|---|---|---|
| Port | `services/<slot>/base.py` | What the engine needs from a slot | stdlib, pydantic, memspine types | name a technology |
| Connector (client) | `clients/<tech>/` | Connection or pool, auth, deadlines, health round trip, close, identity, locality | driver packages, `memspine.exceptions`, `clients._common` | import services, providers, runtime, memories, config schema |
| Adapter | `services/<slot>/<tech>.py` | Implements the port with an injected connector | its port, memspine types, connector types, query-building parts of the driver | construct or open connections, files or weights; import providers or runtime |
| Factory | `providers/builtin/<slot>.py` | Descriptor (slot, capabilities, locality, extra) and builder: settings → spec → shared connector (ADR-079) → adapter | clients, services, config | hold query or business logic |

Every connector implements one protocol:

| Member | Purpose |
|---|---|
| `kind`, `scope` | registry category; loop- or thread-bound |
| `identity(spec)` | credential-free key: target, auth digest, settings |
| `locality(spec)` | `process`, `host` or `cluster`, used by the ADR-081 `deployment.locality` checks |
| `open()` | connect within a deadline; closes itself on failure |
| `health(deadline_s)` | a real round trip (`SELECT 1`, `RETURN 1`, `hello`, `HeadBucket`), never a paid call |
| `close()` | idempotent |
| `handle` | the native object, given only to the adapter |

The connector receives a transport **spec** (frozen dataclass), never a pydantic config model.
Factories map slot settings to the spec.

## 2. Audit result (summary)

| Provider | Connector | Adapter | Factory | Main deviations |
|---|---|---|---|---|
| storage sqlite | `clients/sqlite.py` | `services/storage/sqlite` | `providers/builtin/storage.py` | sync engines built in `runtime/outbox.py`; migrations open their own engine; health only checks the handle; `store/migrate.py` builds the client directly |
| storage postgres | `clients/postgres.py` | `services/storage/postgres` | `storage.py` | factory builds the sync engine inline (duplicate URL/timeout logic); migrations call `create_engine` themselves; handle-only health |
| vector lancedb | `clients/lancedb.py` | `services/vector/lancedb_store.py` | `providers/builtin/vector.py` | factory owns spaces-state file I/O; handle-only health |
| vector pgvector | none (borrows the SQL engine) | `services/vector/pgvector_store.py` | `providers/builtin/pgvector.py` | DDL in the factory; adapter gets a raw engine; adapter imports providers |
| vector/lexical weaviate | `clients/weaviate.py` | `services/{vector,lexical}/weaviate_store.py` | `providers/builtin/weaviate.py` | not a `Client`; identity parsing duplicated in the factory; native handle passed to the adapter |
| lexical tantivy | **none** | `services/lexical/tantivy.py` | `providers/builtin/lexical.py` | adapter opens index files itself; registry shares the adapter, not a connector |
| lexical opensearch | `clients/opensearch.py` | `services/lexical/opensearch.py` | `providers/builtin/opensearch.py` | not a `Client`; duplicated identity; untyped client |
| graph ladybug | `clients/ladybug.py` | `services/graph/ladybug.py` | `providers/builtin/graph.py` | the registry caches a (client, adapter) pair; handle-only health |
| graph sql_adjacency | none (borrows SQL) | `services/graph/sql_adjacency.py` | `graph.py` | adapter typed to the raw engine |
| graph neo4j | `clients/neo4j.py` | `services/graph/neo4j.py` | `graph.py` | client imports providers and builds the domain executor; not shared; service has its own driver factory; adapter closes a driver it does not own |
| cache memory/disk/redis/valkey | `clients/cashews.py` | `models/cache/*` (wrong package) | `providers/builtin/cache.py` | adapters live in `models`; not shared through the registry (one Redis connection per Engine) |
| embedding (fastembed/st/static/litellm) | **none** | `models/encoders/embedding/*` | `providers/builtin/models.py` | adapters load weights, download or hold transport themselves |
| llm (litellm, llamacpp) | **none** | `models/providers/llm/*` | `models.py` | transport in the adapter; ollama/vllm/bedrock are model prefixes, not descriptors |
| rerank | **none** | `models/providers/rerank/*` | `models.py` and a second `rerank/factory.py` | duplicate factory; circular dependency |
| gliner (extraction) | **none** | `models/runtime/gliner_runtime.py` | **no descriptor** (built in the container) | consumers import the concrete pool |
| decider | **none** | `models/providers/decision/*` | **no descriptor** (built in `api/engine.py`) | the engine builds the adapter |

Graph port (2026-10-23, one port): the three graph rows implement one port,
`services/graph/base.py` `GraphStore`. Qualifier-filtered traversal is part of it
(`neighbors` / `subgraph` take `qualifier_filter`, and `frontier_edges` is a port method);
there is no separate qualified protocol and no `qualifier_filter` descriptor capability
(ADR-080 item 10 note).

Connections are also opened outside providers:
- **In services:**
  - `services/assets/registry.py`: sqlite3;
  - `services/assets/fetch.py`, `vision.py`, `external/provider.py`, `external/wikipedia.py`: urllib;
  - `services/secrets/aws.py`: boto3.
- **Elsewhere:** `derive/job.py`, `store/side_tables.py`, `observability/tap_sinks.py`, `workers/step_journal.py` and `workers/taskiq_runner.py` (Redis).

**Nothing enforces layering.** `.importlinter` has `include_external_packages = False`, and
the CI script filters out every non-memspine module, so no contract can name a driver
package. No contract covers `memspine.providers`.

## 3. Refactor plan (prioritised)

**P0, low risk:**
1. Connector base for OpenSearch and Weaviate, with `identity()`; delete the factory-side identity copies.
2. Neo4j spec; drop the client's providers import; executor built by the adapter.
3. Real `health()` round trips for sqlite, postgres, lancedb, ladybug and cashews.
4. `providers/connectors.py` `acquire()` helper, replacing six copy-pasted open/close closures.
5. Delete `executor_from_env`; adapters stop closing drivers they do not own.
6. Share Neo4j and cache clients through the registry.
7. Route `store/migrate.py` through the storage descriptor.

**P1, medium risk:**

8. SQLite and Postgres sync engines move into `clients/{sqlite,postgres}/sync.py`.
9. Alembic runs over a connector-lent connection; the ADR-081 advisory lock sits there.
10. `clients/tantivy` connector (index open, mkdir, reload, writer lock).
11. pgvector DDL moves into the adapter's `ensure_table()`; the adapter takes the Postgres connector.
12. Weaviate adapters take the connector plus a collection binding.
13. Cache adapters move to `services/cache/`.
14. A `clients/http` shared httpx connector for vision, external search and asset fetch; a `clients/aws` connector.

**P2, high risk (wide import churn, done with a codemod):**

15. Compute ports and adapters move from `models/...` to `services/{embedding,llm,rerank,extraction,decision}/`, and weight loading to `clients/weights/<lib>.py`.
16. Descriptors and categories for `extraction` (GLiNER) and `decision`; no ad hoc construction.
17. A `clients/litellm` connector; ollama, vllm and bedrock become their own descriptors; collapse the second rerank factory.
18. ADR-081 alignment (`records`, `documents` and `locality` on descriptors); the assets registry moves to records/documents.

## 4. Enforcement

**Prerequisites:**
- `include_external_packages = True`.
- The CI script and AST mirror keep external edges.
- A symbol-denylist AST test for connection constructors in `services/**` and `models/**`:
  `create_engine`, `sqlite3.connect`, `lancedb.connect`, `tantivy.Index`, `GraphDatabase.driver`,
  `AsyncOpenSearch`, weaviate `connect_to_*`, `Redis(`, `httpx.Client(`, `urlopen`, `boto3.client`,
  `MongoClient`, `snapshot_download`.

**New import-linter contracts:**

| Contract | Rule |
|---|---|
| `clients-isolated` | clients import only stdlib, drivers, `memspine.exceptions` and `memspine.clients` |
| `services-no-transport` | services and models never import transport packages (psycopg, aiosqlite, redis, cashews, httpx, boto3, pymongo, neo4j, litellm, huggingface_hub) |
| `services-not-composition` | services and models never import providers, runtime or api |
| `consumers-never-see-clients` | engine, memories, derive, read, ingest, compose, store and trust never import clients or provider builders |
| `providers-composition-only` | factories never import consumers |

The current violations go into the existing BASELINE so the tree stays green. The baseline
shrinks as each refactor item lands.

## 5. How it composes with ADR-081

- **Sharing.** Two slots on the same target produce the same `identity(spec)`, so they
  share one connector. Postgres records and pgvector share one pool; Weaviate vector,
  lexical and documents share one client.
- **Locality.** `locality(spec)` feeds the `deployment.locality` checks before any `open()`.
  Only factories resolve `data_dir` paths.
- **Future providers:**
  - MongoDB: `clients/mongodb` with `services/records/mongodb.py` (conformance suite)
    and `services/documents/mongodb.py` (GridFS).
  - S3: `clients/s3` with `services/documents/s3.py`.
  - Filesystem: `clients/fs` with `services/documents/filesystem.py`.
  - Weaviate documents: `services/documents/weaviate.py`.
