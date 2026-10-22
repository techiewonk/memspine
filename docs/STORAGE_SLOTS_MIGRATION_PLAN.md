# Storage slots and deployment block — migration plan (ADR-081, RM-108 / RM-111)

Plan for owner review. Status lives in `docs/PROVIDER_TODO.md`. Background is in
`docs/STORAGE_TOPOLOGY_REVIEW_2026-10-22.md`.

## 1. Target shape

```yaml
deployment:
  id: prod-eu-1                 # was runtime.deployment_id
  locality: multi_node          # single_process (default) | multi_process | multi_node
  data_dir: /var/lib/memspine   # only required when a host-local slot is chosen

storage:                        # data providers: where memory lives
  structured: {provider: postgres, settings: {url: ${PG_URL}, connect_timeout_s: 5, statement_timeout_s: 30}}
  vector:     {provider: pgvector, settings: {url: ${PG_URL}, connect_timeout_s: 5, statement_timeout_s: 30}}
  lexical:    {provider: opensearch, settings: {url: ${OS_URL}}}
  graph:      {provider: neo4j, settings: {url_env: MEMSPINE_NEO4J_URL}}
  graph_mirror: null
  cache:      {provider: redis, settings: {url: ${REDIS_URL}}}

embedding: {provider: fastembed, settings: {...}}   # service provider (model), unchanged
llm: {...}                                          # service providers, unchanged
models: {...}                                       # profiles/bindings, unchanged
```

**Embedded template** (the default, `single_process`; `data_dir` defaults to the
database file's directory):
```yaml
storage:
  structured: {provider: sqlite, settings: {path: ./memspine.db, busy_timeout_s: 5}}
  vector:  {provider: lancedb}
  lexical: {provider: tantivy}
  graph:   {provider: auto}     # resolved once, then pinned
  cache:   {provider: memory}
```

**Sharing rule.** Each slot keeps its own settings (owner choice). Two slots with the
same target, for example `structured` and `vector` on one Postgres URL, share one
client and pool through the ADR-079 registry. A slot whose provider needs a SQL
connection (pgvector) may omit `settings.url` to mean "the structured slot's database".
That is the one implicit link, and it is validated.

## 2. Old key to new key

| Today | New | Notes |
|---|---|---|
| `storage.profile` | removed | Alembic always for durable SQL; templates replace profiles |
| `storage.provider` / `storage.settings` | `storage.structured.provider` / `.settings` | `settings.data_dir` moves to `deployment.data_dir` |
| `vector.provider` / `vector.settings` | `storage.vector.*` | |
| `read.lexical_provider` / `read.lexical_settings` | `storage.lexical.*` | `read.lexical_scorer`, `lexical_language`, `lexical_strip_names`, `lexical_namespace_languages`, `short_query_lexical_weight` stay in `read` |
| `graph.provider` / `graph.settings` | `storage.graph.*` | `graph.edge_compiler_v2`, `key_v2`, `entity_embeddings` stay in `graph` |
| `graph.mirror.{enabled,provider,settings}` | `storage.graph_mirror` (null = off) | gated by the `mirror` capability |
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
   - `graph_mirror` needs `mirror`.
   - pgvector needs a Postgres target.
   - `sqlite_adjacency` is renamed `sql_adjacency` and supports SQLite and Postgres (no
     alias).
6. **Side files.** Sleep leases, the cost ledger and dead letters move to tables in
   `structured`. A one-time import reads an existing `.sleep`, `.cost` or `.deadletters`
   file and then renames it to `*.imported`.

## 4. Work packages (after approval)

| WP | Content | Tests |
|---|---|---|
| 1 | Config schema: `deployment` and `storage.<slot>`; remove the old keys with errors naming the new key; templates; config baseline; USAGE rows | config schema tests, docs-config surface, baseline regeneration |
| 2 | Container wiring: build each category from its slot; locality traits on descriptors; `data_dir` placement | provider and wiring suites, combinations |
| 3 | Schema: Alembic always; advisory lock; legacy adoption test; delete `services/storage/profile.py` (or replace it with a read-only topology report) | storage, migration and Postgres e2e (disposable container) |
| 4 | Pinning `auto` in projector bindings; `sql_adjacency` on Postgres; capability-based mirror gate | graph contract on SQLite, LadybugDB and Postgres; binding drift tests |
| 5 | Side files into `structured`, with one-time import; encryption covered | SQLCipher test, import test, leases on SQLite and Postgres |
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

## 6. Questions for the owner before building

1. Should the embedded template's `graph` be `auto` (pinned on first start), or fixed to
   `sql_adjacency` so it does not depend on optional extras?
2. Should `multi_process` hard-fail single-writer stores outside the writer process, or
   open them read-only?
3. Should `storage.vector` and `storage.lexical` be allowed to be `null` (no vector or
   lexical store)? Today the vector store is mandatory (RM-44).
4. Is `sqlite_adjacency` → `sql_adjacency` acceptable as a breaking rename?
