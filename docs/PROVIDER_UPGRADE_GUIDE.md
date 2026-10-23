# Breaking provider configuration upgrade

This guide describes only the current schema. There is no translation layer,
legacy registry alias, retired-provider map, database copying or automatic
re-embedding. Review callers and YAML directly before the next Engine startup.

## 1. Select a template and supply native settings

Use the selected provider's `settings` object. Defaults and required fields are
owned by that provider; they are not generic top-level settings. An offline
plumbing configuration, with no model downloads, is:

```yaml
storage:
  records:
    provider: sqlite
    settings:
      path: ./memory.db
      busy_timeout_s: 5.0
  vector:
    provider: lancedb
    settings:
      isolation: shared
  lexical:
    provider: tantivy
    settings: {}
  cache:
    provider: memory
    settings: {}
model:
  embedding:
    provider: hash
    settings:
      dim: 64
read:
  hybrid: true
```

Data providers live in the `storage` slots (ADR-081); model providers live in the
`model` slots (ADR-082). Each slot is `{provider, <typed common keys>, settings}`.

Pass this YAML through `Engine(template="core", user_config="providers.yaml")`.
The shipped template supplies explicit runtime health/close/startup deadlines.
The hash embedder is for deterministic plumbing checks; it is not a model-quality
recommendation or a fair retrieval-quality baseline.

| Caller area | Current configuration path | Responsibility |
|---|---|---|
| Authoritative storage | `storage.records.provider`, `storage.records.settings` | SQL path/URL and native deadlines (`deployment.data_dir` for host-local slots) |
| Embedding | `model.embedding.provider`, `model.embedding.settings` | Model, dimension, device and provider-specific inference settings |
| Vector projection | `storage.vector.provider`, `storage.vector.settings` | Native index and collection/table options; dimensions come from embedder |
| Lexical retrieval | `storage.lexical.provider`, `storage.lexical.settings` | Native analyzer, index and transport options; `read.hybrid` enables the leg |
| Graph | `storage.graph.provider`, `storage.graph.settings` | The one graph client (Neo4j included); associative memory must be enabled. `storage.graph_mirror` was removed (ADR-081 amendment 2026-10-23) and is rejected; a store that ran it has its leftover mirror offset, binding and outbox rows deleted at start |
| LLM role | `model.llm.<role>.provider/timeout_s/settings` | Explicit adapter, deadline, model and generation defaults |
| Reranking | `model.rerank.chain` (`[{provider, settings}]`, `[]` = off) | Native rerankers tried in order; behaviour keys (`pool`, `keep`, ...) on `model.rerank` |
| Extraction / decider / vision | `model.extraction.<consumer>`, `model.decider.{route,read}`, `model.vision` | One inline slot per consumer; no named profiles |
| Cache | `storage.cache.provider`, `storage.cache.settings` | Memory/disk/remote cache native settings |
| Durable maintenance runner | `workers.runner`, `workers.temporal`, `workers.resonate` | External Temporal and embedded Resonate; separate runner contracts |

Inspect exact defaults without opening a service:

```python
from memspine.providers import provider_settings_reference
print(provider_settings_reference("embedding", "fastembed"))
print(provider_settings_reference("llm", "litellm"))
print(provider_settings_reference("vector", "weaviate"))
```

See [generated defaults](PROVIDER_DEFAULTS.md) for the authoritative native schema.
Model-dependent dimensions must match actual runtime output. Changing the model
ID alone does not authorize historical vector migration.

## 2. Bind a model explicitly

This is an optional binding example, not an endpoint probe:

```yaml
model:
  llm:
    extract:
      provider: litellm
      timeout_s: 30.0
      settings:
        model: ollama/your-local-model
        temperature: 0.0
        max_tokens: 2048
```

The role name chooses the task; `provider` chooses the adapter; `settings.model`
chooses the model handled by that adapter. Zero temperature is a valid explicit
value. Required timeouts are not invented by model-name inference. An absent role
is disabled. Use your secret mechanism for credentials and inspect only sanitized
provider catalogs; do not dump effective secret settings into logs.

## 3. Configure server-backed services deliberately

```yaml
storage:
  vector:
    provider: weaviate
    settings:
      url_env: MEMSPINE_WEAVIATE_URL
      connect_timeout_s: 10.0
      request_timeout_s: 30.0
  lexical:
    provider: opensearch
    settings:
      url_env: MEMSPINE_OPENSEARCH_URL
      connect_timeout_s: 10.0
      request_timeout_s: 30.0
read:
  hybrid: true
```

Install the corresponding `[weaviate]` and `[opensearch]` extras. Their endpoints,
TLS policy, auth and backend provisioning are explicit deployment concerns.
Weaviate lexical and vector adapters are separate portable retrieval legs; native
hybrid fusion is not implied by selecting them.

For Ladybug/Neo4j, select `storage.graph.provider` and the matching settings/extra.
Controlled switches require their accepted readiness and handover gates; selecting
a new provider does not itself copy the projection or change a running Engine.
PostgreSQL storage and pgvector require the SQL/provider fields documented in
[provider operations](PROVIDER_OPERATIONS.md). Projection bootstrap permissions
and normal serving permissions are intentionally separate.

## 4. Replace durable-worker configuration

```yaml
workers:
  runner: temporal
  temporal:
    deployment_id: my-memory-deployment
    address: localhost:7233
    namespace: default
  resonate:
    journal_path: ./state/micro-steps.sqlite
```

Install `[temporal]` and provide an external Temporal Server. The current durable
runner always combines Temporal with local embedded Resonate. Inline remains the
embedded default; taskiq is a separate brokered option. The former DBOS runner and
system database configuration are removed and rejected, with no compatibility
aliases. Resolve any previously deployed old jobs operationally before rollout.

The SQLite result journal is application-owned, not native Resonate promise
persistence. Retry placement must reach the same persistent journal and application
store. See [orchestration operations](TEMPORAL_RESONATE_OPERATIONS.md) for all
bounded defaults, explicit operation/input fingerprint reuse and deployment limits.
The ingestion outbox retains its independent `workers.transport` seam.

## 5. Validate and inspect

1. Parse configuration with `load_config(template="core", user_config=...)` or
   construct the Engine with that explicit template/user file; resolve native
   descriptor validation at Engine startup.
2. Reject unknown settings and invalid ranges before selecting a release. Do not
   mutate the immutable running Engine configuration to switch a provider.
3. Run a synthetic start/ingest/search/shutdown check, then inspect
   `await engine.provider_catalog()` and `await engine.readiness()`.
4. For native integration, use only task-owned scratch services and the strict
   [native gate](PROVIDER_CI_COVERAGE.md). A health probe is not workload acceptance.
5. Record source/lock identities, commands, executed/skipped outcomes and artifacts
   through the [verification ledger](provider_verification/LEDGER_USAGE.md).

See the [platform/install evidence](PROVIDER_PLATFORM_MATRIX.md) for measured
combinations. Production rollout and unavailable model/native environments remain
separate gates. The historical evaluation schema migration remains deferred.
