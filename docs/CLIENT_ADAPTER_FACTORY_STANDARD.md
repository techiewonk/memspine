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

As implemented (G, 2026-10-23): the protocol is `clients/base.py` `Connector[H]` with
`ConnectorIdentity(kind, target, auth_digest, knobs, canonical)`; `key_target()` is the
registry target and `descriptor_target()` the provider descriptor's `target_identity`
(byte-identical to the factory functions it replaces, so fingerprints do not move). A
private target (`:memory:` SQLite or Ladybug, `memory://` LanceDB, an in-RAM Tantivy index,
`mem://` cache) gets a unique identity and is never shared. Shared helpers live in
`clients/_common/` (`endpoints`, `secrets` with `auth_digest` and `Secret`, `deadlines.probe`).
Transport constants (SQLite busy timeout, the psycopg connect-timeout floor, the LanceDB
memory metadata cache, Tantivy writer heap and commit retries) moved from
`config/constants.py` into their connector modules; `config/remote.py` moved to
`clients/_common/endpoints.py` (no alias). Factories acquire a shared connector with
`providers/connectors.py` `acquire(ctx, ConnectorCls, spec, resource_id=...)`.

## 2a. Live status by provider (updated after each merge; RM-113 row holds the evidence)

Last update: 2026-10-23, after the storage-slot merge (A, `28c54d8e`..`41e66f51`); import contracts 18 kept / 0 broken. In progress: WP6 locality + documents slot; RM-113 leftovers (items 6, 7, 11, 12).
✅ done · 🔧 in progress (agent) · ⏳ queued · ⚠️ known gap

| Slot | Provider | Connector | Adapter | Factory | Status | Work |
|---|---|---|---|---|---|---|
| records | sqlite | ✅ `clients/sqlite` (spec, `SELECT 1`, sync engines, migration connection) | ✅ records port + conformance; Alembic over the lent connection | ✅ slot wiring (A) | ✅ | — |
| records | postgres | ✅ `clients/postgres` (spec, `SELECT 1`, sync engines, advisory lock) | ✅ records port + conformance; Alembic over the lent connection | ✅ slot wiring (A) | ✅ | A |
| records | mongodb | ✅ `clients/mongodb` (replica-set check, `hello` health, driver vocabulary in `driver.py`) | ✅ records port, 14/14 conformance on a replica set | ⏳ registration after the slot rewrite; uv.lock refresh (RM-02) | 🔧 b41eadaf | I (RM-112) |
| vector | lancedb | ✅ Connector + `list_tables` health | ✅ | ✅ slot wiring (A) | ✅ | A |
| vector | pgvector | ⏳ borrows the postgres connector | ⏳ DDL into the adapter | ✅ slot wiring (A) | ⏳ | after G/A |
| vector | weaviate | ✅ Connector + single identity, `is_ready` health (shared with lexical/documents) | ⏳ collection binding instead of native handle (item 12) | ✅ `acquire()`, identity from the connector | ⏳ | item 12 |
| lexical | tantivy | ✅ `clients/tantivy` (index open, mkdir, writer lock, commit+reload, searcher health) | ✅ uses the connector; RM-72 holds unchanged | ✅ slot wiring (A) (still shares the adapter) | ✅ | A |
| lexical | opensearch | ✅ Connector + single identity, cluster-health round trip | ✅ | ✅ `acquire()`, identity from the connector | ✅ | G (7a7493b3) |
| lexical | weaviate | ✅ same shared Weaviate connector | ⏳ collection binding instead of native handle (item 12) | ✅ `acquire()` | ⏳ | item 12 |
| graph | ladybug | ✅ Connector + `RETURN 1` health, shared statement lock | ✅ one port | ✅ slot wiring (A) | ✅ | A |
| graph | sql_adjacency | ✅ borrows the SQL connector | ✅ one port, SQLite + Postgres | ✅ slot wiring (A) | ✅ | A |
| graph | neo4j | ✅ Connector (spec, `RETURN 1`), no providers import; executor built adapter-side, never closes the driver | ✅ one port | ✅ slot wiring (A); not yet shared (item 6) | 🔧 item 6 (agent L) | A |
| graph_mirror | neo4j | ✅ same Neo4j connector | ✅ one port | ✅ mirror slot + capability gate (A) | ✅ | A |
| cache | memory | n/a (process) | ✅ `services/cache` (F) | ✅ slot wiring (A) | ✅ | F, A |
| cache | disk | ✅ cashews Connector (spec, `get` health) | ✅ `services/cache` (F) | ✅ slot wiring (A); placement under `data_dir`; not yet shared (item 6) | 🔧 item 6 (agent L) | A |
| cache | redis | ✅ cashews Connector (spec, `get` health) | ✅ `services/cache` (F) | ✅ slot wiring (A); not yet shared (item 6) | 🔧 item 6 (agent L) | A |
| cache | valkey | ✅ cashews Connector (spec, `get` health) | ✅ `services/cache` (F) | ✅ slot wiring (A); not yet shared (item 6) | 🔧 item 6 (agent L) | A |
| documents | filesystem | ✅ `clients/fs` (writable-root open and probe health, atomic staged writes, confined paths, locality host) | ✅ `services/documents/filesystem.py` on the `DocumentStore` port; conformance suite | ✅ `providers/builtin/documents.py` (`acquire()`, single-writer, locality host) | ✅ | RM-111 (documents) |
| documents | s3 | ⏳ `clients/s3` | ⏳ (joins the documents conformance suite) | ⏳ | ⏳ | RM-112 |
| documents | mongodb | ✅ `clients/mongodb` (shared with records) | ⏳ GridFS adapter | ⏳ | ⏳ | RM-112 |
| documents | weaviate | ⏳ shared Weaviate connector | ⏳ | ⏳ | ⏳ | RM-112 |
| model.embedding | fastembed | ✅ `clients/weights/fastembed` | ✅ `services/embedding/fastembed.py` | ✅ `providers/builtin/models.py` | ✅ | F (fa543ba0) |
| model.embedding | sentence_transformers | ✅ `clients/weights/sentence_transformers` | ✅ `services/embedding/st.py` | ✅ | ✅ | F (fa543ba0) |
| model.embedding | static (model2vec) | ✅ `clients/weights/model2vec` | ✅ `services/embedding/static.py` | ✅ | ✅ | F (fa543ba0) |
| model.embedding | litellm | ✅ `clients/litellm` | ✅ `services/embedding/litellm.py` | ✅ | ✅ | F (fa543ba0) |
| model.embedding | hash | n/a | ✅ `services/embedding/hash.py` | ✅ | ✅ | F (fa543ba0) |
| model.llm | litellm | ✅ `clients/litellm` | ✅ `services/llm/litellm.py` | ✅ | ✅ | F (fa543ba0) |
| model.llm | ollama | ✅ `clients/litellm` | ✅ `services/llm/litellm.py` | ✅ own descriptor (route fixed by the descriptor) | ✅ | F (fa543ba0) |
| model.llm | vllm | ✅ `clients/litellm` | ✅ `services/llm/litellm.py` | ✅ own descriptor | ✅ | F (fa543ba0) |
| model.llm | bedrock | ✅ `clients/litellm` | ✅ `services/llm/litellm.py` | ✅ own descriptor | ✅ | F (fa543ba0) |
| model.llm | llamacpp | ✅ `clients/weights/llama_cpp` | ✅ `services/llm/llamacpp.py` | ✅ | ✅ | F (fa543ba0) |
| model.rerank | fastembed | ✅ `clients/weights/fastembed` | ✅ `services/rerank/fastembed.py` | ✅ one factory `providers/builtin/rerank.py` | ✅ | F (fa543ba0) |
| model.rerank | flashrank | ✅ `clients/weights/flashrank` | ✅ `services/rerank/flashrank.py` | ✅ one factory | ✅ | F (fa543ba0) |
| model.rerank | qwen3 / jina | ✅ `clients/weights/transformers` | ✅ `services/rerank/{qwen3,jina}.py` | ✅ one factory | ✅ | F (fa543ba0) |
| model.rerank | litellm | ✅ `clients/litellm` | ✅ `services/rerank/litellm.py` | ✅ one factory | ✅ | F (fa543ba0) |
| model.extraction | gliner2 | ✅ `clients/weights/gliner2` (the only `snapshot_download`) | ✅ `services/extraction` (port `base.py`, pool `gliner2.py`) | ✅ `extraction` descriptor, container builds through it | ✅ | F (fa543ba0) |
| model.decider | opendecider | ✅ `clients/weights/opendecider` | ✅ `services/decider/opendecider_nano.py` | ✅ `decider` descriptor via `providers/decider.py` | ✅ | F (fa543ba0) |
| model.decider | gliner2 | ✅ leases the extraction pool | ✅ `services/decider/gliner2_decision.py` | ✅ `decider` descriptor | ✅ | F (fa543ba0) |
| model.decider | jepa (JEPA-style, joint-embedding predictive) | ⏳ future `clients/weights/<lib>` | ⏳ future `services/decider/jepa.py` | ⏳ future descriptor | ⏳ future | not scheduled |
| model.decider | yolo-style (fast single-pass classifier) | ⏳ future `clients/weights/<lib>` | ⏳ future `services/decider/<name>.py` | ⏳ future descriptor | ⏳ future | not scheduled |
| assets | fetch / vision | ✅ `clients/http` (one pooled httpx client per Engine, no redirect following) | ✅ takes the connector; the asset registry's sqlite3 stays (item 18) | n/a | ✅ | G (7a7493b3) |
| external | provider / wikipedia | ✅ `clients/http` | ✅ takes the connector | n/a | ✅ | G (7a7493b3) |
| secrets | aws | ✅ `clients/aws` (boto3 session, STS health) | ✅ takes the connector | n/a | ✅ | G (7a7493b3) |
| — | enforcement (import rules, constructor denylist) | — | — | — | ✅ 26068983; baselines shrinking (F and G removed lines) | E |

Model slots (owner 2026-10-23): service providers are named `model.<slot>`: `model.embedding`, `model.llm`, `model.rerank`, `model.extraction` (GLiNER entity/relation spans, source-grounded) and `model.decider` (choose among described options). Extraction and decider stay separate slots with separate ports; the same GLiNER weights are loaded once through the shared registry when both are bound to it. Future `model.decider` providers: a JEPA-style decider and a YOLO-style fast classifier (reserved names, not scheduled).

**Import contracts:** 18 kept, 0 broken (after G). The last chain
(`store.migrate → services.storage.sqlite.engine → runtime.outbox.sqlite_engine`) closed
when the sync engines moved into `clients/sqlite/sync.py`; the lease engine is lent by the
connector. Earlier fixes: outbox tables in `services/storage/outbox_tables.py`, lease store in
`store/leases.py` (`8368c356`).

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
| cache memory/disk/redis/valkey | `clients/cashews.py` | `services/cache/*` (wrong package) | `providers/builtin/cache.py` | adapters live in `models`; not shared through the registry (one Redis connection per Engine) |
| embedding (fastembed/st/static/litellm) | **none** | `services/embedding/*` | `providers/builtin/models.py` | adapters load weights, download or hold transport themselves |
| llm (litellm, llamacpp) | **none** | `services/llm/*` | `models.py` | transport in the adapter; ollama/vllm/bedrock are model prefixes, not descriptors |
| rerank | **none** | `services/rerank/*` | `models.py` and a second `rerank/factory.py` | duplicate factory; circular dependency |
| gliner (extraction) | **none** | `services/extraction/gliner2.py` | **no descriptor** (built in the container) | consumers import the concrete pool |
| decider | **none** | `services/decider/*` | **no descriptor** (built in `api/engine.py`) | the engine builds the adapter |

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

**Nothing enforced layering at audit time.** `.importlinter` had
`include_external_packages = False`, and the CI script filtered out every non-memspine
module, so no contract could name a driver package. No contract covered `memspine.providers`.
Section 4 records the enforcement added on 2026-10-23.

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

**Connector layer (G) status, 2026-10-23.** Done: 1 (OpenSearch and Weaviate factories
now take their identity from the connector), 2, 3 (plus Tantivy, HTTP and AWS), 4, 5, 8, 9
(Postgres runs hold the `MIGRATION_LOCK_KEY` session advisory lock; the legacy stamp adoption
is unchanged), 10 and 14. Open: 6 (Neo4j and cache are connectors but still per-Engine),
7, 11, 12, 13. Behaviour notes: `TantivyLexical.close()` only commits held documents; the
connector's owner closes the index. `DriverExecutor.close()` never closes the driver. Every
connector opens with `open()` (no `connect()`), and every constructor takes its spec.

**Factories still to adopt `acquire()`** (left to the storage-slot rewrite, which owns these
files; each one drops a hand-written `own_shared` closure and, where noted, a duplicate
identity parser):
- `providers/builtin/storage.py`: SQLite and Postgres async connectors; drop
  `sqlite_shared_identity` and `postgres_target_identity` in favour of
  `SQLiteClient.identity` / `PostgresClient.identity` (`.target` equals the old dict). The
  shared sync engine stays a second lease on `client.sync_engine(pool)`.
- `providers/builtin/vector.py`: `LanceDBClient` (path identity already matches).
- `providers/builtin/lexical.py`: share the `TantivyConnector` (`lexical.tantivy`) and build an
  adapter per Engine; the RM-72 hold is per adapter token and the uncommitted flag lives on the
  connector, so this is safe, but `test_tantivy_one_writer_per_path_and_memory_is_private`
  asserts the shared adapter and must change with it.
- `providers/builtin/graph.py`: `LadybugClient` (today a shared (client, adapter) pair) and
  `Neo4jClient` (item 6; drop `_neo4j_target_identity` for
  `Neo4jClient.identity(spec).descriptor_target()`).
- `providers/builtin/cache.py`: `CashewsClient` for disk/redis/valkey (item 6).
- `providers/builtin/pgvector.py`: no connector of its own; item 11 moves the DDL into the
  adapter and lends it the Postgres connector.

**P2, high risk (wide import churn, done with a codemod):**

15. ✅ Compute ports and adapters move from `models/...` to `services/{embedding,llm,rerank,extraction,decider}/`, and weight loading to `clients/weights/<lib>.py` (codemod `scripts/move_models_to_services_rm113.py`).
16. ✅ Descriptors and categories for `extraction` (GLiNER) and `decider` (owner naming 2026-10-23: slots `model.embedding`, `model.llm`, `model.rerank`, `model.extraction`, `model.decider`; the `reranker` category is now `rerank`); no ad hoc construction.
17. ✅ A `clients/litellm` connector; ollama, vllm and bedrock become their own descriptors; the second rerank factory is collapsed.

What stays in `models/`: pure model metadata and planning (`profiles.py`, `embedding_profiles.py`,
`tiers.py`, `resolve.py`) and the rule-based query analysis `encoders/query` (no model; `core`
depends on it and may not import `services`). Known limits of the P2 pass: adapters built
standalone (scripts, tests) still make a private connector when none is injected; the
OpenDecider adapter takes the weight registry (it builds its connector lazily per worker
setting); retry policy stays in `services/_retry.py` (it logs through observability, which
clients may not import); the config keys `decision.provider` and `read.decider*` are unchanged
in this pass.
18. ADR-081 alignment (`records`, `documents` and `locality` on descriptors); the assets registry moves to records/documents. Status 2026-10-23: `locality`, `resolve_locality`, `single_writer` and `needs_placement` are on the descriptors and checked by `providers/locality.py` (RM-111 WP6); the `documents` slot ships with the filesystem provider; the asset registry move is still open (it keeps its sqlite3 sidecar and blob directory, and `multi_node` rejects `ingest.asset_dir` until it moves).

## 4. Enforcement

In place since 2026-10-23. Two checks, each with a **shrink-only baseline** under
`tests/unit/baselines/`.

**1. Import contracts** (`.importlinter`, `include_external_packages = True`):

| Contract | Rule | Baseline at seeding |
|---|---|---|
| `clients-isolated` | clients import only stdlib, drivers, `memspine.exceptions` and `memspine.clients` (replaces `v1-clients-low`) | 6 |
| `services-no-transport` | services and models never import psycopg, aiosqlite, sqlcipher3, pysqlcipher3, redis, cashews, httpx, aiohttp, boto3, botocore, aioboto3, pymongo, motor, neo4j, litellm, huggingface_hub. Alembic `env.py` and `versions/*` are exempt through `ignore_imports` | 8 |
| `services-not-composition` | services and models never import providers, runtime or api | 20 |
| `consumers-never-see-clients` | `api.engine`, memories, memory, derive, read, ingest, compose, store and trust never import clients or `providers.builtin` | 1 |
| `providers-composition-only` | providers never import api, memories, derive, read, ingest or compose | 2 |

- `tests/unit/test_import_contracts.py` checks them with `ast`. Third-party imports are
  recorded by their top-level package. A violation is keyed `(file, forbidden module)`.
- `scripts/ci_importlinter.py` keeps external modules in the filtered config. It turns each
  baseline line into extra `ignore_imports` for `lint-imports`, with
  `unmatched_ignore_imports_alerting = none`, because the AST test owns the vanished-entry check.
- Baseline: `tests/unit/baselines/import_contracts.txt`.
  Format: `<contract> <file under src/> <imported> -- <reason citing RM-113 item N>`.
- A permanent, reviewed exemption goes into the contract's own `ignore_imports` in
  `.importlinter`. Never put one in the baseline.

**2. Connection constructors** (`tests/unit/test_connection_constructors.py`):
- Flags calls of `create_engine`, `create_async_engine`, `sqlite3.connect`, `psycopg.connect`,
  `lancedb.connect`, `tantivy.Index`, `*GraphDatabase.driver`, `AsyncOpenSearch`, weaviate
  `connect_to_*`/`use_async_with_*`, `Redis(`/`Redis.from_url`, `httpx.Client`/`AsyncClient`,
  `urlopen`/`build_opener`, `boto3.client`/`Session`, `MongoClient`/`AsyncIOMotorClient`,
  `snapshot_download`/`hf_hub_download` and temporalio `Client.connect`.
- Import aliases are resolved.
- These calls are allowed only in `src/memspine/clients/**` and Alembic `env.py`. All other
  packages are scanned: services, models, providers, runtime, derive, store, observability and
  workers.
- Baseline: `tests/unit/baselines/connection_constructors.txt`, 25 entries at seeding.
  Format: `<file under src/> <symbol> -- <reason citing RM-113>`.

**Run:**

```bash
python -m pytest tests/unit/test_import_contracts.py tests/unit/test_ci_importlinter.py \
  tests/unit/test_connection_constructors.py -o addopts="" -q
python scripts/ci_importlinter.py          # lint-imports on the filtered config + baseline
python scripts/ci_importlinter.py --print  # show the filtered config with injected ignores
```

**Shrink rules:**
- A new violation fails the test. Fix the import or inject a connector. Never add a
  baseline line.
- When a refactor item removes a violation, the test fails with "no longer occur" and names
  the line. Delete it in the same PR.
- A move such as `models/` to `services/` changes the file path, so the old line goes and a new
  one appears. Re-key the line only if the violation really remains, and keep its reason.
- The baseline must reach zero when section 3 is done. A contract with no baseline lines and no
  `ignore_imports` is fully enforced.

`target-layers` was already broken under `lint-imports` before this change, through indirect
chains such as `services.storage.sql_base -> runtime.outbox`. Item 8 removed the last one;
`scripts/ci_importlinter.py` reports 18 kept, 0 broken. The AST mirror checks direct imports
only.

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
