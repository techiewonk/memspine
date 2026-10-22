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

## 2a. Live status by provider (updated after each merge; RM-113 row holds the evidence)

Last update: 2026-10-23, after enforcement (`26068983`); import contracts 17 kept / 1 broken (the sqlite sync-engine chain, closing with G).
✅ done · 🔧 in progress (agent) · ⏳ queued · ⚠️ known gap

| Slot | Provider | Connector | Adapter | Factory | Status | Work |
|---|---|---|---|---|---|---|
| records | sqlite | 🔧 sync engine → `clients/sqlite` | ✅ records port + conformance | 🔧 slot wiring | 🔧 | G (connector, Alembic over connector), A (slot) |
| records | postgres | 🔧 sync engine, advisory lock | ✅ records port + conformance | 🔧 slot wiring | 🔧 | G, A |
| records | mongodb | ✅ `clients/mongodb` (replica-set check, `hello` health, driver vocabulary in `driver.py`) | ✅ records port, 14/14 conformance on a replica set | ⏳ registration after the slot rewrite; uv.lock refresh (RM-02) | 🔧 b41eadaf | I (RM-112) |
| vector | lancedb | 🔧 Connector + real health | ✅ | 🔧 slot wiring | 🔧 | G, A |
| vector | pgvector | ⏳ borrows the postgres connector | ⏳ DDL into the adapter | 🔧 slot wiring | ⏳ | after G/A |
| vector | weaviate | 🔧 Connector + single identity (shared with lexical/documents) | ⏳ collection binding instead of native handle | 🔧 `acquire()` | 🔧 | G |
| lexical | tantivy | 🔧 new `clients/tantivy` | 🔧 uses the connector | 🔧 slot wiring | 🔧 | G, A |
| lexical | opensearch | 🔧 Connector + single identity | ✅ | 🔧 `acquire()` | 🔧 | G |
| lexical | weaviate | 🔧 same shared Weaviate connector | ⏳ collection binding instead of native handle | 🔧 `acquire()` | 🔧 | G |
| graph | ladybug | 🔧 Connector + real health | ✅ one port | 🔧 slot wiring | 🔧 | G, A |
| graph | sql_adjacency | ✅ borrows the SQL connector | ✅ one port, SQLite + Postgres | 🔧 slot wiring | 🔧 | A |
| graph | neo4j | 🔧 Connector, no providers import, shared | ✅ one port | 🔧 slot wiring | 🔧 | G, A |
| graph_mirror | neo4j | 🔧 same Neo4j connector | ✅ one port | 🔧 mirror slot + capability gate | 🔧 | G, A |
| cache | memory | n/a (process) | 🔧 move to `services/cache` | 🔧 slot wiring | 🔧 | F, A |
| cache | disk | 🔧 cashews connector, placement under `data_dir` | 🔧 move to `services/cache` | 🔧 slot wiring | 🔧 | G, F, A |
| cache | redis | 🔧 cashews connector, shared | 🔧 move to `services/cache` | 🔧 slot wiring | 🔧 | G, F, A |
| cache | valkey | 🔧 cashews connector, shared | 🔧 move to `services/cache` | 🔧 slot wiring | 🔧 | G, F, A |
| documents | filesystem | ⏳ `clients/fs` | ⏳ | ⏳ | ⏳ | after A (slot) |
| documents | s3 | ⏳ `clients/s3` | ⏳ | ⏳ | ⏳ | RM-112 |
| documents | mongodb | ✅ `clients/mongodb` (shared with records) | ⏳ GridFS adapter | ⏳ | ⏳ | RM-112 |
| documents | weaviate | ⏳ shared Weaviate connector | ⏳ | ⏳ | ⏳ | RM-112 |
| model.embedding | fastembed | 🔧 `clients/weights/fastembed` | 🔧 → `services/embedding` | 🔧 | 🔧 | F |
| model.embedding | sentence_transformers | 🔧 `clients/weights/sentence_transformers` | 🔧 → `services/embedding` | 🔧 | 🔧 | F |
| model.embedding | static (model2vec) | 🔧 `clients/weights/model2vec` | 🔧 → `services/embedding` | 🔧 | 🔧 | F |
| model.embedding | litellm | 🔧 `clients/litellm` | 🔧 → `services/embedding` | 🔧 | 🔧 | F |
| model.embedding | hash | n/a | 🔧 → `services/embedding` | ✅ | 🔧 | F |
| model.llm | litellm | 🔧 `clients/litellm` | 🔧 → `services/llm` | 🔧 | 🔧 | F |
| model.llm | ollama | 🔧 `clients/litellm` | 🔧 → `services/llm` | 🔧 own descriptor (was a model prefix) | 🔧 | F |
| model.llm | vllm | 🔧 `clients/litellm` | 🔧 → `services/llm` | 🔧 own descriptor | 🔧 | F |
| model.llm | bedrock | 🔧 `clients/litellm` | 🔧 → `services/llm` | 🔧 own descriptor | 🔧 | F |
| model.llm | llamacpp | 🔧 `clients/weights/llama_cpp` | 🔧 → `services/llm` | 🔧 | 🔧 | F |
| model.rerank | fastembed | 🔧 `clients/weights/fastembed` | 🔧 → `services/rerank` | 🔧 one factory | 🔧 | F |
| model.rerank | flashrank | 🔧 `clients/weights/flashrank` | 🔧 → `services/rerank` | 🔧 one factory | 🔧 | F |
| model.rerank | qwen3 / jina | 🔧 `clients/weights/transformers` | 🔧 → `services/rerank` | 🔧 one factory | 🔧 | F |
| model.rerank | litellm | 🔧 `clients/litellm` | 🔧 → `services/rerank` | 🔧 one factory | 🔧 | F |
| model.extraction | gliner2 | 🔧 `clients/weights/gliner2` | 🔧 → `services/extraction` | 🔧 new descriptor | 🔧 | F |
| model.decider | opendecider | 🔧 `clients/weights/opendecider` | 🔧 → `services/decision` | 🔧 new descriptor | 🔧 | F |
| model.decider | gliner2 | 🔧 `clients/weights/gliner2` | 🔧 → `services/decision` | 🔧 new descriptor | 🔧 | F |
| model.decider | jepa (JEPA-style, joint-embedding predictive) | ⏳ future `clients/weights/<lib>` | ⏳ future `services/decider/jepa.py` | ⏳ future descriptor | ⏳ future | not scheduled |
| model.decider | yolo-style (fast single-pass classifier) | ⏳ future `clients/weights/<lib>` | ⏳ future `services/decider/<name>.py` | ⏳ future descriptor | ⏳ future | not scheduled |
| assets | fetch / vision | 🔧 `clients/http` | 🔧 | n/a | 🔧 | G |
| external | provider / wikipedia | 🔧 `clients/http` | 🔧 | n/a | 🔧 | G |
| secrets | aws | 🔧 `clients/aws` | 🔧 | n/a | 🔧 | G |
| — | enforcement (import rules, constructor denylist) | — | — | — | ✅ 26068983 (37 import + 25 constructor baseline entries, shrink-only) | E |

Model slots (owner 2026-10-23): service providers are named `model.<slot>`: `model.embedding`, `model.llm`, `model.rerank`, `model.extraction` (GLiNER entity/relation spans, source-grounded) and `model.decider` (choose among described options). Extraction and decider stay separate slots with separate ports; the same GLiNER weights are loaded once through the shared registry when both are bound to it. Future `model.decider` providers: a JEPA-style decider and a YOLO-style fast classifier (reserved names, not scheduled).

**Import contracts:** 17 kept (5 new), 1 broken. The one remaining chain is
`store.migrate → services.storage.sqlite.engine → runtime.outbox.sqlite_engine`, and it
closes when G moves the sync engines into `clients/sqlite`. Two other chains are already
fixed in `8368c356`: the outbox tables moved to `services/storage/outbox_tables.py`, and
the lease store moved to `store/leases.py`.

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

**P2, high risk (wide import churn, done with a codemod):**

15. Compute ports and adapters move from `models/...` to `services/{embedding,llm,rerank,extraction,decision}/`, and weight loading to `clients/weights/<lib>.py`.
16. Descriptors and categories for `extraction` (GLiNER) and `decision`; no ad hoc construction.
17. A `clients/litellm` connector; ollama, vllm and bedrock become their own descriptors; collapse the second rerank factory.
18. ADR-081 alignment (`records`, `documents` and `locality` on descriptors); the assets registry moves to records/documents.

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

`target-layers` was already broken under `lint-imports` before this change. It fails through
indirect chains such as `services.storage.sql_base -> runtime.outbox`, which item 8 removes. The
AST mirror checks direct imports only and passes. This baseline does not cover it.

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
