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

Last update: 2026-10-23, after the storage-slot merge (A, `28c54d8e`..`41e66f51`) and the RM-113 leftovers (items 6, 7, 11, 12, `8226befe`); import contracts 18 kept / 0 broken. WP6 locality + documents slot landed (`72855bb5`); pgvector live-tested (`d9a87db6`). RM-117 model slots (ADR-082) landed (`80490893`..`778ccaca`): every model.* row is configured through its `model.<slot>`; regression green, import contracts 18 kept / 0 broken. RM-118 follow-ups (`28236a18`) and the RM-02 dependency refresh (`5d132e89`, audit 0 outdated) landed. S3 + Weaviate documents (`79394028`), `storage.graph_mirror` removed: one graph client at a time (`655ac7f9`, ADR-081 amendment), asset registry on records + documents (`d8540742`). In progress: MongoDB records + GridFS (RM-112), single-query walks LadybugDB then Neo4j (RM-96); then the managed local mongod (RM-119, ADR-083).
✅ done · 🔧 in progress (agent) · ⏳ queued · ⚠️ known gap

| Slot | Provider | Connector | Adapter | Factory | Status | Work |
|---|---|---|---|---|---|---|
| records | sqlite | ✅ `clients/sqlite` (spec, `SELECT 1`, sync engines, migration connection) | ✅ records port + conformance; Alembic over the lent connection | ✅ slot wiring (A) | ✅ | — |
| records | postgres | ✅ `clients/postgres` (spec, `SELECT 1`, sync engines, advisory lock) | ✅ records port + conformance; Alembic over the lent connection | ✅ slot wiring (A) | ✅ | A |
| records | mongodb | ✅ `clients/mongodb` (replica-set check, `hello` health, driver vocabulary in `driver.py`) | ✅ records port incl. the RM-112 read-model, scrub and provider-metadata methods; conformance 20/20 on a replica set | ✅ registered in `storage.records` (`acquire()`, locality cluster, `deployment.id` required); SQL-only features refused before open (`providers/records_features.py`) | ✅ | RM-112 (473c3f48); managed local mongod RM-119 |
| vector | lancedb | ✅ Connector + `list_tables` health | ✅ | ✅ slot wiring (A); `acquire()` | ✅ | A |
| vector | pgvector | ✅ borrows the records slot's Postgres connector (`ManagedSqlAccess.connector`) | ✅ `PgVectorStore(connector, PgVectorProjection)`; `ensure_table()` in `start()` honours `provision_projection` / `create_extension`; async over the connector pool; no providers import | ✅ no DDL, no sync lease | ✅ live-tested 2026-10-23 on pgvector/pgvector:pg16 with the locked pgvector 0.5.1 (provider contract + adapter + multi_node e2e: 10 passed, 0 skipped) | 8226befe |
| vector | weaviate | ✅ Connector + single identity, `is_ready` health (shared with lexical/documents) | ✅ connector + `WeaviateCollectionBinding` (name, properties); `ensure_collection` in `start()`; helper in private `services/_weaviate/` | ✅ `acquire()`, identity from the connector | ✅ | 8226befe |
| lexical | tantivy | ✅ `clients/tantivy` (index open, mkdir, writer lock, commit+reload, searcher health) | ✅ uses the connector; RM-72 holds unchanged | ✅ slot wiring (A); shared connector via `acquire(lazy=True)`, an adapter per Engine | ✅ | A |
| lexical | opensearch | ✅ Connector + single identity, cluster-health round trip | ✅ | ✅ `acquire()`, identity from the connector | ✅ | G (7a7493b3) |
| lexical | weaviate | ✅ same shared Weaviate connector | ✅ connector + collection binding (with `content`); `ensure_collection` in `start()` | ✅ `acquire()` | ✅ | 8226befe |
| graph | ladybug | ✅ Connector + `RETURN 1` health, shared statement lock | ✅ one port | ✅ slot wiring (A); shared connector via `acquire()`, a store per Engine | ✅ | A |
| graph | sql_adjacency | ✅ borrows the SQL connector | ✅ one port, SQLite + Postgres | ✅ slot wiring (A) | ✅ | A |
| graph | neo4j | ✅ Connector (spec, `RETURN 1`), no providers import; executor built adapter-side, never closes the driver | ✅ one port | ✅ slot wiring (A); ✅ shared via `acquire()` (item 6, 8226befe), identity from the connector | ✅ | A, 8226befe |
| cache | memory | n/a (process) | ✅ `services/cache` (F) | ✅ slot wiring (A) | ✅ | F, A |
| cache | disk | ✅ cashews Connector (spec, `get` health) | ✅ `services/cache` (F) | ✅ slot wiring (A); placement under `data_dir`; ✅ shared via `acquire()` (item 6, 8226befe) per directory | ✅ | A, 8226befe |
| cache | redis | ✅ cashews Connector (spec, `get` health) | ✅ `services/cache` (F) | ✅ slot wiring (A); ✅ shared via `acquire()` (item 6, 8226befe) per server, database and credentials | ✅ | A, 8226befe |
| cache | valkey | ✅ cashews Connector (spec, `get` health) | ✅ `services/cache` (F) | ✅ slot wiring (A); ✅ shared via `acquire()` (item 6, 8226befe) per server, database and credentials | ✅ | A, 8226befe |
| documents | filesystem | ✅ `clients/fs` (writable-root open and probe health, atomic staged writes, confined paths, locality host) | ✅ `services/documents/filesystem.py` on the `DocumentStore` port; conformance suite | ✅ `providers/builtin/documents.py` (`acquire()`, single-writer, locality host) | ✅ | RM-111 (documents) |
| documents | s3 | ✅ `clients/s3` (boto3 in worker threads from `[aws]`; `HeadBucket` open/health; credentials by env-var name, digest-only identity; keys confined to the prefix; driver errors mapped to `StorageError`) | ✅ `services/documents/s3.py` (content-addressed blobs + heads + per-record ref markers; multipart above 8 MiB under the 64 MiB cap; conditional-write head merges); conformance 19/19 live on MinIO | ✅ `providers/builtin/documents.py` (`acquire()`, locality cluster) | ✅ | RM-112 (79394028) |
| documents | mongodb | ✅ `clients/mongodb` (one connector with the records slot) | ✅ `services/documents/mongodb.py` (GridFS bucket + `<bucket>.heads`; streamed hashing, cap and digest abort; revision CAS head merge); conformance 19/19 | ✅ `providers/builtin/documents.py`, locality cluster | ✅ | RM-112 (473c3f48) |
| documents | weaviate | ✅ shared Weaviate connector (one client with the vector/lexical slots) | ✅ `services/documents/weaviate.py` (non-vectorized collection; head, BLOB chunk (1 MiB default, 4 MiB max) and per-record reference objects); conformance 19/19 live | ✅ `acquire()`, locality cluster, needs `deployment.id` | ✅ | RM-112 (79394028) |
| assets (registry) | records + documents | ✅ storage's own async engine; bytes through the `DocumentStore` port | ✅ `services/assets/registry.py` (`asset_registry` table, Alembic SQLite + Postgres); no sqlite3 sidecar; `ingest.asset_dir` removed | ✅ built by the container | ✅ | RM-113 item 18 (d8540742) |
| model.embedding | onnx | ✅ `clients/weights/onnx` | ✅ `services/embedding/onnx.py` | ✅ `providers/builtin/models.py` | ✅ | ADR-084; pinned catalogue, reference fixtures, current identities |
| model.embedding | sentence_transformers | ✅ `clients/weights/sentence_transformers` | ✅ `services/embedding/st.py` | ✅ | ✅ | F (fa543ba0) |
| model.embedding | static (model2vec) | ✅ `clients/weights/model2vec` | ✅ `services/embedding/static.py` | ✅ | ✅ | F (fa543ba0) |
| model.embedding | openai_compatible | ✅ `clients/openai_compat` | ✅ `services/embedding/openai_compat.py` | ✅ | ✅ | ADR-085; HTTP transport tests |
| model.embedding | anyllm | yes `clients/anyllm` | yes `services/embedding/openai_compat.py` | yes optional `[anyllm]` | yes | ADR-085; fake SDK, bounded calls, endpoint identities |
| model.embedding | ollama | ✅ `clients/openai_compat` | ✅ `services/embedding/openai_compat.py` | ✅ | ✅ | ADR-085; HTTP transport tests |
| model.embedding | vllm | ✅ `clients/openai_compat` | ✅ `services/embedding/openai_compat.py` | ✅ | ✅ | ADR-085; HTTP transport tests |
| model.embedding | lmstudio | ✅ `clients/openai_compat` | ✅ `services/embedding/openai_compat.py` | ✅ | ✅ | ADR-085; HTTP transport tests |
| model.embedding | llamacpp_server | ✅ `clients/openai_compat` | ✅ `services/embedding/openai_compat.py` | ✅ | ✅ | ADR-085; HTTP transport tests |
| model.embedding | hash | n/a | ✅ `services/embedding/hash.py` | ✅ | ✅ | F (fa543ba0) |
| model.llm | openai_compatible | ✅ `clients/openai_compat` | ✅ `services/llm/openai_compat.py` | ✅ | ✅ | ADR-085; HTTP transport tests |
| model.llm | ollama | ✅ `clients/openai_compat` | ✅ `services/llm/openai_compat.py` | ✅ own descriptor (route fixed by the descriptor) | ✅ | ADR-085; HTTP transport tests |
| model.llm | vllm | ✅ `clients/openai_compat` | ✅ `services/llm/openai_compat.py` | ✅ own descriptor | ✅ | ADR-085; HTTP transport tests |
| model.llm | bedrock | ✅ `clients/bedrock` | ✅ `services/llm/openai_compat.py` | ✅ own descriptor | ✅ | ADR-085; boto3 [aws] |
| model.llm | lmstudio | yes `clients/openai_compat` | yes `services/llm/openai_compat.py` | yes presets | yes | ADR-085 |
| model.llm | llamacpp_server | yes `clients/openai_compat` | yes `services/llm/openai_compat.py` | yes presets | yes | ADR-085 |
| model.llm | anyllm | yes `clients/anyllm` | yes `services/llm/openai_compat.py` | yes optional `[anyllm]` | yes | ADR-085; fake provider and missing-extra tests |
| model.embedding | bedrock | yes `clients/bedrock` | yes `services/embedding/openai_compat.py` | yes | yes | ADR-085; Stubber |
| model.rerank | bedrock | yes `clients/bedrock` | yes `services/rerank/openai_compat.py` | yes | yes | ADR-085; Stubber |
| model.llm | llamacpp | ✅ `clients/weights/llama_cpp` | ✅ `services/llm/llamacpp.py` | ✅ | ✅ | F (fa543ba0) |
| model.rerank | onnx | ✅ `clients/weights/onnx` | ✅ `services/rerank/onnx.py` | ✅ one factory `providers/builtin/rerank.py` | ✅ | ADR-084; pinned catalogue, reference fixtures, current identities |
| model.rerank | flashrank | ✅ `clients/weights/flashrank` | ✅ `services/rerank/flashrank.py` | ✅ one factory | ✅ | F (fa543ba0) |
| model.rerank | qwen3 / jina | ✅ `clients/weights/transformers` | ✅ `services/rerank/{qwen3,jina}.py` | ✅ one factory | ✅ | F (fa543ba0) |
| model.rerank | openai_compatible | ✅ `clients/openai_compat` | ✅ `services/rerank/openai_compat.py` | ✅ one factory | ✅ | ADR-085; HTTP transport tests |
| model.rerank | vllm | ✅ `clients/openai_compat` | ✅ `services/rerank/openai_compat.py` | ✅ one factory | ✅ | ADR-085; HTTP transport tests |
| model.extraction | gliner2 | ✅ `clients/weights/gliner2` (the only `snapshot_download`) | ✅ `services/extraction` (port `base.py`, pool `gliner2.py`) | ✅ `extraction` descriptor, container builds through it | ✅ | F (fa543ba0) |
| model.decider | opendecider | ✅ `clients/weights/opendecider` | ✅ `services/decider/opendecider_nano.py` | ✅ `decider` descriptor via `providers/decider.py`; slot `model.decider.read` | ✅ | F (fa543ba0), RM-117 |
| model.decider | gliner2 | ✅ leases the extraction pool (one load shared with the extraction consumers) | ✅ `services/decider/gliner2_decision.py` | ✅ `decider` descriptor; slot `model.decider.route` (own `min_confidence` floor; `workers` must stay 1, concurrency is the pool's `inference_concurrency`); read slot not implemented | ✅ | F (fa543ba0), RM-117 |
| model.decider | jepa (JEPA-style, joint-embedding predictive) | ⏳ future `clients/weights/<lib>` | ⏳ future `services/decider/jepa.py` | ⏳ future descriptor | ⏳ future | not scheduled |
| model.decider | yolo-style (fast single-pass classifier) | ⏳ future `clients/weights/<lib>` | ⏳ future `services/decider/<name>.py` | ⏳ future descriptor | ⏳ future | not scheduled |
| model.vision | ollama | ✅ `clients/http` | ✅ `services/vision/` (port `base`, `noop`, `ollama`; no re-export from `services.assets`) | ⚠️ no descriptor (built from `model.vision`) | ✅ | RM-117, RM-118 (28236a18) |

Config keys: the model.* rows are the ADR-082 slot names and the configuration uses them (RM-117, [plan](MODEL_SLOTS_MIGRATION_PLAN.md)); the old keys (`embedding`, `llm.roles`, `models.*`, `decision.provider`, `read.rerank*`, `read.decider*`, `ingest.asset_vision*`) are rejected naming their new key. The rerank rows are entries of `model.rerank.chain`; the extraction row serves `model.extraction.{semantic,prepass,topic,sensitivity}`.
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
| vector/lexical weaviate | `clients/weaviate.py` | `services/{vector,lexical}/weaviate_store.py` | `providers/builtin/weaviate.py` | not a `Client`; identity parsing duplicated in the factory; native handle passed to the adapter (all fixed: items 1, 12) |
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
is unchanged), 10 and 14. Open at the time: 6, 7, 11, 12, 13 (6, 7, 11 and 12 are
closed by 8226befe below). Behaviour notes: `TantivyLexical.close()` only commits held documents; the
connector's owner closes the index. `DriverExecutor.close()` never closes the driver. Every
connector opens with `open()` (no `connect()`), and every constructor takes its spec.

**RM-113 leftovers (8226befe), 2026-10-23.** Done: 6, 7, 11, 12 and the factory `acquire()`
list below. Not in this pass: 13 (cache adapters, F) and 18 (ADR-081 alignment).
- Item 6: `Neo4jClient` (primary graph and mirror of every Engine share one driver per target,
  credentials and transport knobs) and `CashewsClient` (disk per directory; redis/valkey per
  server, database and credentials; each Engine keeps its own key namespace) go through
  `acquire()`; the registry closes them on the last release.
- Item 7: `store/migrate.py` builds both stores through the `sqlite` storage descriptor
  (`get_registry("storage_backends")`), each under its own `ProviderLifecycleManager`; no
  client construction, no `memspine.clients` import.
- Item 11: `PgVectorStore(connector, PgVectorProjection(...), embedder_id=...)` takes the
  records slot's Postgres connector (`ManagedSqlAccess.connector`, a borrowed reference) and
  owns its DDL in `ensure_table()` (run by `start()`): the extension is created only with
  `create_extension`, schema/table/indexes only with `provision_projection`, otherwise the
  operator-provisioned table is validated. Queries run async on the connector's pool; the
  factory no longer takes a sync lease. The owned table name and the column/index layout are
  unchanged.
- Item 12: `WeaviateVectorStore` / `WeaviateLexicalStore` take the shared connector plus a
  `WeaviateCollectionBinding(name, properties)` and run `ensure_collection` in `start()`;
  `services/weaviate_projection.py` moved to the private `services/_weaviate/` (no alias).
  Collection names and property schemas are unchanged.
- Factories: `storage.py` (SQLite file and Postgres connectors via `acquire()`;
  `sqlite_shared_identity` deleted; `postgres_target_identity` is
  `PostgresClient.identity(spec).target`; the shared sync engine stays a second lease keyed
  `<kind>.sync` by the connector identity), `graph.py` (`_neo4j_target_identity` is
  `Neo4jClient.identity(spec).descriptor_target()`; Ladybug shares the connector, a store per
  Engine on the connector's statement lock), `vector.py` (`LanceDBClient`), `lexical.py`
  (shared `TantivyConnector`, an adapter per Engine; `acquire(..., lazy=True)` keeps the
  index opening lazily). Descriptor identities are pinned byte-identical to the deleted
  parsers (`test_descriptor_identities_from_connectors_are_byte_identical`). `:memory:` /
  in-RAM targets stay private and unregistered.

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
