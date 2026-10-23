# Storage slots and deployment block — migration plan (ADR-081, RM-108 / RM-111)

Plan for owner review. Status lives in `docs/PROVIDER_TODO.md`. Background is in
`docs/STORAGE_TOPOLOGY_REVIEW_2026-10-22.md`.

**Amendment 2026-10-23 (owner decision): graph mirror removed.** Exactly one graph client
runs at a time (`storage.graph`); the optional `storage.graph_mirror` slot, the `mirror`
capability and the outbox-fed Neo4j mirror are gone, with no alias. A config naming
`storage.graph_mirror` (any value, null included) is rejected naming `storage.graph`; Neo4j
stays available as `storage.graph.provider`. See the ADR-081 amendment section.

## 1. Target shape

```yaml
deployment:
  id: prod-eu-1                 # was runtime.deployment_id
  locality: multi_node          # single_process (default) | multi_process | multi_node
  data_dir: /var/lib/memspine   # only required when a host-local slot is chosen

storage:                        # data providers: where memory lives
  records:    {provider: postgres, settings: {url: ${PG_URL}, connect_timeout_s: 5, statement_timeout_s: 30}}
  vector:     {provider: pgvector, settings: {url: ${PG_URL}, connect_timeout_s: 5, statement_timeout_s: 30}}
  lexical:    {provider: opensearch, settings: {url: ${OS_URL}}}
  graph:      {provider: neo4j, settings: {url_env: MEMSPINE_NEO4J_URL}}
  cache:      {provider: redis, settings: {url: ${REDIS_URL}}}

embedding: {provider: fastembed, settings: {...}}   # service provider (model), unchanged
llm: {...}                                          # service providers, unchanged
models: {...}                                       # profiles/bindings, unchanged
```

**Embedded template** (the default, `single_process`; `data_dir` defaults to the
database file's directory):
```yaml
storage:
  records:    {provider: sqlite, settings: {path: ./memspine.db, busy_timeout_s: 5}}
  vector:  {provider: lancedb}
  lexical: {provider: tantivy}
  graph:   {provider: auto}     # resolved once, then pinned
  cache:   {provider: memory}
```

**Sharing rule.** Each slot keeps its own settings (owner choice). Two slots with the
same target, for example `records` and `vector` on one Postgres URL, share one
client and pool through the ADR-079 registry. A slot whose provider needs a SQL
connection (pgvector) may omit `settings.url` to mean "the records slot's database".
That is the one implicit link, and it is validated.

## 2. Old key to new key

| Today | New | Notes |
|---|---|---|
| `storage.profile` | removed | Alembic always for durable SQL; templates replace profiles |
| `storage.provider` / `storage.settings` | `storage.records.provider` / `.settings` | `settings.data_dir` moves to `deployment.data_dir` |
| `vector.provider` / `vector.settings` | `storage.vector.*` | |
| `read.lexical_provider` / `read.lexical_settings` | `storage.lexical.*` | `read.lexical_scorer`, `lexical_language`, `lexical_strip_names`, `lexical_namespace_languages`, `short_query_lexical_weight` stay in `read` |
| `graph.provider` / `graph.settings` | `storage.graph.*` | `graph.edge_compiler_v2`, `key_v2`, `entity_embeddings` stay in `graph` |
| `graph.mirror.{enabled,provider,settings}` | removed (amendment 2026-10-23) | was `storage.graph_mirror`; one graph client runs at a time, so select Neo4j as `storage.graph` |
| `cache.provider` / `cache.settings` | `storage.cache.*` | the disk cache path defaults under `deployment.data_dir` |
| `runtime.deployment_id` | `deployment.id` | |
| `workers.resonate.journal_path` | unchanged | declared host-local, so `multi_node` checks it |
| `event_log.*`, `embedding.*`, `llm.*`, `models.*` | unchanged | policy and service settings, not data placement |

There are no aliases (ADR-068). An old key is rejected with an error that names its
new location.

## 3. Validation rules

1. **Locality.** Every descriptor declares `locality: process | host | cluster`.
   - `multi_node`: any `host` slot (or host-local feature) is a configuration error.
     That covers sqlite, lancedb, tantivy, ladybug, disk cache, the Resonate journal and
     the embedding pin file.
   - `multi_process`: tantivy and ladybug are only allowed in the designated writer
     process; other processes open them read-only or fail clearly.
2. **`data_dir`.** It is required only when a host-local slot is selected. It defaults to
   today's placement base, so file paths and projection fingerprints stay byte-identical.
3. **Schema.** Alembic runs for every durable SQL database, with a Postgres advisory lock
   around upgrades. `create_all` is used only for `:memory:`. Existing `create_all`
   Postgres databases are adopted by the existing legacy stamp. A test pins that the
   `create_all` schema equals the stamped revision.
4. **Pinning.** `auto` resolves once; the resolved provider is recorded in the projector
   binding. A later difference is reported as an explicit provider change and never
   applied silently.
5. **Capabilities.**
   - pgvector needs a Postgres target.
   - `sqlite_adjacency` is renamed `sql_adjacency` and supports SQLite and Postgres (no
     alias).
6. **Side files.** Sleep leases, the cost ledger and dead letters move to tables in
   `records`. A one-time import reads an existing `.sleep`, `.cost` or `.deadletters`
   file and then renames it to `*.imported`.

## 4. Work packages (after approval)

| WP | Content | Tests |
|---|---|---|
| 1 | Config schema: `deployment` and `storage.<slot>`; remove the old keys with errors naming the new key; templates; config baseline; USAGE rows | config schema tests, docs-config surface, baseline regeneration |
| 2 | Container wiring: build each category from its slot; locality traits on descriptors; `data_dir` placement | provider and wiring suites, combinations |
| 3 | Schema: Alembic always; advisory lock; legacy adoption test; delete `services/storage/profile.py` (or replace it with a read-only topology report) | storage, migration and Postgres e2e (disposable container) |
| 4 | Pinning `auto` in projector bindings; `sql_adjacency` on Postgres; capability-based mirror gate | graph contract on SQLite, LadybugDB and Postgres; binding drift tests |
| 5 | Side files into `records`, with one-time import; encryption covered | SQLCipher test, import test, leases on SQLite and Postgres |
| 6 | Locality validation (`multi_node` / `multi_process`) | config-rule unit tests; e2e with two engines on one Postgres |
| 7 | A codemod moves the old keys in tests, examples, docs and eval configs (about 300+ sites); full regression plus e2e on Postgres | full chunked regression, e2e, combinations |

Order: WP1–WP3 first, as one breaking change. WP4–WP6 can run in parallel after that.
WP7 runs with WP1 so the tree stays green.

## 5. Risks

- **Breaking change everywhere at once.** Mitigation: the codemod plus a clear rejection
  message for each old key.
- **Projection fingerprints change if `data_dir` moves.** Mitigation: the default equals
  today's base.
- **The eval harness still uses old flat configs (PA-107).** Migrate in WP7 or keep PA-107
  as the tracked follow-up.

## 6. Owner answers (2026-10-23)

1. Embedded template `storage.graph`: `auto`, resolved once on first start and pinned.
2. `multi_process`: single-writer host-local stores (tantivy, ladybug) hard-fail outside
   the designated writer process.
3. `storage.vector` and `storage.lexical` are both required (never null).
4. `sqlite_adjacency` is renamed `sql_adjacency` (SQLite and Postgres), with no alias.
5. Non-structured data stores (for example MongoDB) must be possible. The design
   question is open, see section 7.

## 7. Non-structured (document) stores

**Owner decision (2026-10-23): both readings, contract now and provider later.**
- The authoritative slot is `storage.records`, with a SQL-independent storage port and a
  conformance suite in RM-111.
- An optional `storage.documents` slot is added for non-structured payloads.
- The MongoDB provider is built later, as RM-112.
- Weaviate becomes a `documents` provider (objects with properties) next to its vector
  and lexical roles. It cannot be the `records` store: it has no ordered append-only log
  and no multi-object transactions.

**Contract shipped (RM-111, 2026-10-23).** The port is
`services/storage/records_port.py` (`RecordsStore`). The conformance suite is
`tests/unit/services/storage/test_records_conformance.py`, which runs on SQLite (memory and
file) and on Postgres (Alembic; WP3 removed the Postgres `create_all` path, and a legacy
`create_all` database is adopted by the stamp, pinned in
`tests/unit/store/test_f16d_storage_profile.py`). The contract text for future providers
is `docs/RECORDS_STORE_CONTRACT.md`.

Original analysis:

The owner wants non-SQL data stores such as MongoDB. Two readings, possibly both:

- **(a) The authoritative store on a document database.** The slot is renamed from
  `storage.records` to `storage.records`, so the name does not assume SQL. The
  provider may be `sqlite`, `postgres` or `mongodb`. Every provider in this slot must meet
  the event-sourcing contract:
  - append-only event log with a monotonic sequence;
  - atomic append-plus-outbox (MongoDB needs a replica set for multi-document
    transactions);
  - projector offsets;
  - leases;
  - erasure and lineage queries;
  - migrations, which become versioned collection and index setup, not Alembic.
- **(b) A separate optional slot for non-structured payloads.** For example
  `storage.documents` for raw attachments, assets, large payloads and ingest blobs. Its
  providers could be `filesystem`, `s3`, `mongodb` (GridFS) or others. The event log
  keeps only references and content hashes.

Today the record and event model is SQL-only (SQLAlchemy Core). Reading (a) needs a
storage-port contract and conformance suite that are independent of SQL. Reading (b) is
additive.

## 8. Implementation notes (WP1-WP3, WP7; 2026-10-23)

- **Rejections.** Each removed key raises `ConfigError: config key '<old>' was removed by
  ADR-081; use <new>` (`config/schema/storage.py::MOVED_KEYS`). A null `vector`, `lexical`,
  `records`, `graph` or `cache` slot is rejected. (`documents`: see section 9.)
- **`data_dir`.** The records descriptor's placement receives `deployment.data_dir`. SQLite with
  it unset keeps `<path>` as the base; Postgres uses `<data_dir>/memspine` (the old
  `settings.data_dir` base). A host-local slot with no base raises naming `deployment.data_dir`.
  An unset disk-cache `path` defaults to `<data_dir>/memspine.cache` when `data_dir` is set.
  Projection fingerprints are pinned byte-identical in `tests/unit/config/test_storage_slots.py`.
- **`profile.py`.** Deleted rather than replaced: the resolved topology is already the validated
  config (`storage.*`, `deployment.*`) plus `describe()` and `provider_inspection()`; a second
  description module would drift, as the old one had.
- **Template chain.** `extends:` now resets a slot's settings when a child template changes its
  provider (`server` over `base`), as the user/env/kwargs layers already did.
- **Locality.** `sql_adjacency` is declared `host` statically; its effective locality is the
  records slot's, resolved in WP6 (section 9).
- **Codemod.** `scripts/migrate_config_keys_adr081.py` is idempotent; a file containing the
  marker `migrate_config_keys_adr081: skip` is left alone.

## 9. Implementation notes (WP6 and the documents slot; 2026-10-23)

**Locality validation (WP6).** `memspine/providers/locality.py`
(`validate_deployment_locality`) runs inside `check_capabilities`, before the records store or
any other slot is built, so a refused configuration opens no connection and creates no file. It
reads only the config and the descriptors:

- Descriptor fields: `locality` (static trait), `resolve_locality(settings, records_locality)`
  (`:memory:` SQLite is `process`; LanceDB, Tantivy and LadybugDB placed beside an in-memory
  store are `process`, else `host`; `sql_adjacency` takes the records slot's locality, so it is
  `cluster` on Postgres, `host` on a SQLite file and `process` for `:memory:`),
  `single_writer` (Tantivy, LadybugDB, filesystem documents) and `needs_placement` (the slot
  builds its files from the records placement base).
- The declared trait wins over the endpoint: a cluster provider reached on a loopback address
  (a sidecar proxy such as cloud-sql-proxy) stays `cluster`. The connector's `locality(spec)`
  is endpoint information, not the rule input.
- Only slots that are built are checked: `lexical` with `read.hybrid`, `graph` with
  associative memory and `documents` when set.
- `multi_node`: every built data slot must be `cluster`, except a `process` cache (`memory`
  only memoises deterministic embeddings and extractions). Host-local features are rejected:
  `workers.runner: temporal` (Resonate journal) and `embedding.pin_profile` (pin file);
  `ingest.assets: on` is rejected only with a records store that has no SQL tables (the asset
  registry is a table of the records database since RM-113 item 18). The error lists the
  cluster providers registered for the slot.
- `multi_process`: `:memory:` records are rejected. A single-writer host-local store is allowed
  only with `deployment.writer: true`; every other process fails at start (read-only opening
  is not offered). **Writer designation:** `deployment.writer` (default false), normally set
  per process through the environment layer (`MEMSPINE_DEPLOYMENT__WRITER=true`). Exactly one
  writer per host cannot be proven from one process's configuration; it is the operator's
  responsibility (documented in USAGE).
- Outside `single_process` an `auto` slot must be explicit, because two processes or nodes with
  different extras installed would resolve it differently (the pin is only readable after the
  records store opens).
- `data_dir`: required only when a built `needs_placement` slot (LanceDB, Tantivy, LadybugDB,
  filesystem documents without `settings.root`) or `embedding.pin_profile` sits on a non-file
  records store. The disk cache has its own `settings.path` default. The build-time check in
  `providers/placement.py::host_base_path` stays as a safety net.
- All violations are reported in one `DeploymentLocalityError` (a `ConfigError`).
- Tests: `tests/unit/providers/test_locality.py` (one test per rule, plus two Engine tests that
  prove nothing is opened) and `tests/e2e/test_multi_node_locality.py` (two `multi_node`
  Engines on one Postgres are refused with no table created; the same config starts in
  `single_process`; with the `pgvector` package two cluster-only Engines share the database
  and read each other's writes).

**Documents slot.** `storage.documents: {provider: filesystem, settings: {root, max_bytes,
fsync}}`.

- Port `services/documents/base.py` (`DocumentStore`): `put` (bytes or a sync/async chunk
  stream, size cap `max_bytes`, optional `expected_digest`, metadata, `record_ids`), `get`,
  `stream`, `head`, `delete`, `list`, and the erasure hooks `erase_records` and
  `erase_namespace`. Addressed by `(namespace, sha256)`; namespaces never share documents.
- Connector `clients/fs` (`FilesystemConnector`, locality `host`, writable-root open and health
  probe, staging with a running hash and a size cap, atomic promote with fsync, confined
  paths, an exclusive section shared by every Engine on the root). Adapter
  `services/documents/filesystem.py`; factory `providers/builtin/documents.py` (`acquire()`
  shares the connector per root). An unset `root` is `<records placement base>.documents`.
- `mongodb` is rejected by name until RM-112 ships it; other unknown names fail at start with
  the registry's valid list. `s3` and `weaviate` ship (RM-112, below).
- Erasure: the hard path of `trust/pipeline/erasure.py::_forget_many` (hard forget, cascade,
  `erase_subject`, erasure v2 and re-derive all go through it) calls `erase_records(ns, ids)`,
  which drops those references and deletes documents left with none; `erase_namespace` deletes
  every document of the namespace. The E04 asset adapter writes downloaded attachment bytes
  (RM-113 item 18); other callers use `engine.documents`. Removing a file is a logical delete,
  not physical-erasure proof.
- Conformance: `tests/unit/services/documents/test_documents_conformance.py`, parametrized by
  provider name; the `mongodb` case skips until its descriptor registers, `s3` and `weaviate`
  run live when `MEMSPINE_TEST_S3_URL` / `MEMSPINE_TEST_WEAVIATE_URL` are set.

**S3 documents (RM-112).** `storage.documents: {provider: s3}`, `[aws]` (boto3; no new
dependency), locality `cluster`, multi-writer.

- Connector `clients/s3` (`S3Connector`, `S3Spec`): one boto3 client per endpoint, region,
  bucket, prefix, principal and transport knobs (`acquire()` shares it). Sync boto3 runs in
  worker threads (`asyncio.to_thread`, `scope = "thread"`); aioboto3/aiobotocore were rejected
  because they pin botocore to an exact version (DEPENDENCY_ANALYSIS A14). `open()` builds the
  client and runs `HeadBucket` within `connect_timeout_s` (`create_bucket` creates a missing
  one); `health()` is `HeadBucket`. Credentials are named environment variables resolved by
  the factory into `Secret` values, or the boto3 chain; the identity holds only an auth
  digest. The connector owns every object call (keys relative to the prefix, confined; driver
  errors become `StorageError` with the operation and S3 error code only), so the adapter never
  imports boto3/botocore.
- Adapter `services/documents/s3.py`. Keys: `ns/<sha256(ns)[:32]>/blobs/<digest>`,
  `.../heads/<digest>.json`, `.../refs/<sha256(record)[:32]>/<digest>` (empty markers) and
  `.../tmp/<uuid>` (multipart staging). A payload up to one part (`part_size_bytes`, default
  8 MiB) is hashed in memory and written with one `PutObject`; a larger one streams as a
  multipart upload to `tmp/` (one part in memory, cap enforced while streaming, aborted on
  failure) and is moved into place by `CopyObject`. Blob first, reference markers next, head
  last. The head is created with `If-None-Match: *` and merged with `If-Match: <etag>`,
  retried on a lost race, so concurrent puts from any process merge every reference;
  `conditional_writes: false` falls back to an in-process exclusive section.
- Erasure without scanning the bucket: `erase_records` lists `refs/<record>/` per erased record
  and reads only those heads; `erase_namespace` lists and deletes only the namespace prefix
  (heads first). Not linearizable against a concurrent put of the same bytes; a delete is
  logical (versioned buckets keep versions; use lifecycle rules, also
  `AbortIncompleteMultipartUpload`).

**Weaviate documents (RM-112).** `storage.documents: {provider: weaviate}`, `[weaviate]`,
locality `cluster`, needs `deployment.id`.

- Shares the `WeaviateClient` connector with the vector and lexical slots (same settings, same
  identity, one `acquire()` key); the descriptor's target identity equals the vector slot's.
- Adapter `services/documents/weaviate.py` with a dedicated collection
  `<collection_prefix>Documents<owner digest>` (`services/_weaviate/documents.py`; the name
  does not depend on a projection generation: documents are source data). Objects: `head`,
  `chunk` (BLOB, `chunk_bytes` default 1 MiB, at most 4 MiB) and `ref` (one per record
  reference), all exact-match `FIELD` properties, not BM25-indexed, a self-provided `flat`
  vector index and no vectors (nothing is embedded).
- `max_bytes` is capped at 64 MiB in the settings (refused above at config time); chunking keeps
  every insert far below Weaviate's message limits (gRPC default about 100 MB).
- A put streams chunks under a fresh upload id, inserts references, then creates the head
  (insert-if-absent on its deterministic UUID); an existing head keeps its chunks and the new
  ones are deleted. References are separate objects, so concurrent puts never lose one; a
  metadata merge across processes is last-writer-wins. `erase_records` finds a record's
  documents with one filtered query; listing uses keyset pagination on a `key` property, so no
  result-window limit applies.
- Live evidence (2026-10-23): conformance 19/19 for each provider on MinIO
  (`cgr.dev/chainguard/minio`) and Weaviate (`cr.weaviate.io/semitechnologies/weaviate`).
- Asset registry (RM-113 item 18, done 2026-10-23): the rows are the `asset_registry` table of
  the records database (Alembic `rm113_asset_registry` / `pg_rm113_asset_registry`; key
  `(namespace, asset_id)`; encrypted with the database under SQLCipher, shared on Postgres),
  reached through the `AssetRegistry` port (`services/assets/registry.py`; SQL adapter
  `services/assets/sql_registry.py` over the storage's async engine; an in-process adapter for
  a records store without SQL). Downloaded bytes go to `storage.documents` by content hash with
  the carrying record as reference; `read.asset_evidence: fetch` is refused at config time
  without that slot. Erasure: `_forget_many` (hard) deletes the erased records' rows and any
  stored bytes no remaining row references, after `erase_records`; `erase_namespace` deletes
  every row of the namespace. `ingest.asset_dir` is removed and rejected naming
  `storage.records` and `storage.documents` (ADR-068, no alias); because the old directory
  path is no longer in the config, the one-time import is explicit:
  `Engine.import_asset_dir(path, namespace=None)` copies `registry.sqlite` rows and `blobs/`
  bytes, records a content digest in `side_file_imports` (kind `assets`) in the rows'
  transaction and renames both to `*.imported` (bytes stay in place without a documents slot).
  The legacy file is read through `clients/sqlite/files.py`; no `sqlite3.connect` remains in
  `services/`.
