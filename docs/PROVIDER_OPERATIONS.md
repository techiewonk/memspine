# Provider configuration and controlled operations

Current implementation reference for ADR-068, ADR-069 and the accepted provider plan.
This is a breaking schema: old keys, registry modules, aliases and retirement
maps are not accepted.

Owner scope clarification (2026-10-17): database copying and re-embedding are
excluded from the current work. Existing generic contracts remain optional
extension points; they are not pending implementation commitments. Selecting a
provider does not silently migrate data or start model re-embedding calls.

## Configure services

| Service | Selector | Native settings |
|---|---|---|
| Authoritative SQL | `storage.provider` | `storage.settings` |
| Vector | `vector.provider` | `vector.settings` |
| Lexical | `read.lexical_provider` | `read.lexical_settings` |
| Primary graph | `graph.provider` | `graph.settings` |
| Optional graph mirror | `graph.mirror.enabled/provider` | `graph.mirror.settings` |
| Embedding | `embedding.provider` | `embedding.settings` |
| LLM role | `llm.roles.<role>.provider` | `llm.roles.<role>.settings` |
| Reranking | `read.rerank` (selected provider or explicit chain policy) | `read.rerank_settings` |
| Cache | `cache.provider` | `cache.settings` |

Optional remote adapters use these same paths, with no Engine-specific vendor
branch or alternative ingestion API. For example, this selects a Weaviate vector
leg and an OpenSearch lexical leg (endpoints come from the named environment
variables):

```python
engine = Engine(
    template="core",
    runtime={"deployment_id": "my-unique-application-deployment"},
    vector={"provider": "weaviate", "settings": {
        "url_env": "MEMSPINE_WEAVIATE_URL",
        "connect_timeout_s": 10.0, "request_timeout_s": 30.0,
    }},
    read={"hybrid": True, "lexical_provider": "opensearch", "lexical_settings": {
        "url_env": "MEMSPINE_OPENSEARCH_URL",
        "connect_timeout_s": 10.0, "request_timeout_s": 30.0,
    }},
)
```

For Weaviate BM25 set `read.lexical_provider="weaviate"` with its own
`read.lexical_settings`. For pgvector select PostgreSQL storage and
`vector.provider="pgvector"`; provision its owned projection explicitly with
`vector.settings.provision_projection=true`, then disable that flag for normal
serving. `create_extension=true` additionally requires provisioning opt-in and
appropriate administrator permissions. Vector dimensions/identity come from the
selected embedder, not a second independent dimension setting.

The adapters reject unsupported explicit quantization, Matryoshka or multi-space
modes rather than silently ignoring them. Native hybrid fusion and a shared
Weaviate client pool are not advertised. PostgreSQL DELETE does not prove removal
from WAL/MVCC/backups; physical history absence remains unproven.

The selected descriptor validates settings into a strict immutable model before
its builder runs. Descriptors share one registry/catalog, version checks,
capability checks and engine-local ownership. Category contexts carry the
different dependencies each service actually needs. Pipeline policies such as
retrieval fusion, token budgets, role selection and embedding-space migration
remain outside native settings.
Every descriptor owns its native defaults and validation schema. See the generated
[per-provider defaults table](PROVIDER_DEFAULTS.md), or inspect one with
`provider_settings_reference(category, provider)` without starting an Engine.
Vector table dimensions come from the selected embedding service; they are not a
second independent vector-database default that can drift from the encoder.
The `pgvector` vector provider requires `storage.provider: postgres` and
`memspine[postgres]`. It borrows the storage provider's managed SQL lease and
stores vectors in deployment-, generation-, profile-, and dimension-scoped
tables. The PostgreSQL `vector` extension must already be installed in that
database; `vector.settings.create_extension: true` explicitly asks startup to
install it when the database role has permission. Its default HNSW settings
follow pgvector's documented `m=16`, `ef_construction=64`, and
`ef_search=40` values. HNSW supports indexed `vector` values through 2,000
dimensions; for larger dimensions set `vector.settings.index: none` to use
exact SQL ranking. Namespace predicates are always applied. Because pgvector
applies filters after approximate index scans, exact ranking (`index: none`)
may be preferable for highly selective namespace filters. See the
[pgvector index and filtering guidance](https://github.com/pgvector/pgvector#filtering).
Projection DDL is opt-in: set `vector.settings.provision_projection: true` for
an operator-controlled bootstrap, then set it back to `false` for normal
production startup. This setting creates only the deterministic table for the
selected deployment, generation, embedding identity, dimension and profile;
it does not create or migrate application tables. Installing the extension
also requires `provision_projection: true` and `create_extension: true`.
The adapter deletes rows from the current owned table, but it cannot prove
physical erasure from PostgreSQL MVCC history, WAL, replicas or backups. Its
erasure history verdict therefore remains unproven after a row is deleted.
The Engine uses a detached, recursively immutable effective configuration;
changing the caller's original model cannot change a running Engine. Configure
policy variants before startup rather than assigning to `engine.config` afterward.
Native provider settings and pipeline policy configuration use strict scalar
validation. The resolved Engine configuration is recursively immutable; invalid
or unknown fields fail validation before providers are built.

Adapter authors register a stateless `ProviderDescriptor` with a strict settings
model, typed builder, capabilities and lazy requirements. The builder must
register owned cleanup before connecting and return only registered or explicitly
borrowed resources. Storage also supplies the full `SqlAccess` capability,
including `sync_lease` and `acquire_sync` for workers.

Projection target identity must be computable without opening the target. A
non-versioned vector projection additionally requires the embedding descriptor's
no-load identity to contain its exact `embedder_id` and `dimension`. After build,
the runtime encoder must match that identity. Graph providers supporting a primary
and mirror destination can declare `canonical_target` inside target identity;
that part excludes credentials and transport policy so different credentials
cannot hide a duplicate physical target.
Embedding dimension defaults belong to each provider's typed settings:

| Provider | Default model | Default dimension | Selecting another model |
|---|---|---:|---|
| `fastembed` | `BAAI/bge-small-en-v1.5` | 384 | Known models use exact [small](https://huggingface.co/BAAI/bge-small-en-v1.5/blob/main/config.json) and [base](https://huggingface.co/BAAI/bge-base-en-v1.5/blob/main/config.json) model metadata; other models require `settings.dim`. |
| `hash` | deterministic hash encoder | `HASH_EMBEDDING_DIM` (64) | Set `settings.dim` to choose another hash-vector size. |
| `static` | `minishlab/potion-base-8M` | 256 | Default model [config](https://huggingface.co/minishlab/potion-base-8M/blob/main/config.json); other models require `settings.dim`. |
| `st` | `sentence-transformers/all-MiniLM-L6-v2` | 384 | Known model [config](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/blob/main/config.json) and [Qwen config](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/blob/main/config.json); other models require `settings.dim`. |
| `litellm` | `openai/text-embedding-3-small` | 1536 | OpenAI's [embedding guide](https://developers.openai.com/api/docs/guides/embeddings) lists default 3-small/3-large sizes; [ada-002](https://openai.com/index/new-and-improved-embedding-model/) is 1536; other models require `settings.dim`. |

Known-model dimension conflicts are rejected unless a provider explicitly supports
a requested size (LiteLLM `request_dimensions`). Local adapters also validate
their actual output against the resolved dimension. These checks prevent a model
selection from silently changing an existing vector table's identity.

An offline configuration using shipped template deadlines:

```python
from memspine import Engine

engine = Engine(
    template="core",
    dotenv_path=None,
    storage={"provider": "sqlite", "settings": {
        "path": "./memory.db", "busy_timeout_s": 5.0,
    }},
    embedding={"provider": "hash", "settings": {}},
    vector={"provider": "lancedb", "settings": {}},
    read={"lexical_provider": "tantivy", "lexical_settings": {}},
    cache={"provider": "memory", "settings": {}},
)
await engine.start()
print(await engine.provider_catalog())
print(await engine.provider_catalog(probe_health=True))
print(await engine.readiness())
await engine.stop()
```

Shipped templates explicitly state health/close/startup deadlines. Standalone
configurations must supply `runtime.provider_services` themselves. Health and
close deadlines are finite positive numbers; startup may explicitly be null.
Enabled remote services also require their applicable native request/connect
deadlines and an explicit `runtime.deployment_id`. Local development persists
an automatically generated deployment identity once in SQL.

LLM bindings explicitly select an adapter, independently of its model ID:

Generation precedence is explicit call options, then the selected role's validated
settings, then that provider's own defaults. A deliberately configured thinking
or structured-output budget still caps its call. Catalogue bindings forward only
declared generation overrides, so omission does not replace a native default.

```yaml
runtime:
  deployment_id: example-deployment
llm:
  roles:
    extract:
      provider: litellm
      settings:
        model: ollama/llama3
        api_base: http://localhost:11434
        timeout_seconds: 30.0
        # Defaults: temperature 0.2, top_p 1.0, max_tokens 2048.
        # Per-call options override these provider defaults.
```

For `llamacpp`, the native defaults are `temperature: 0.8`, `top_p: 0.95`,
`max_tokens: 2048`, `n_ctx: 8192`, `n_gpu_layers: 0`, and `n_batch: 512`.
Set them in that role's `settings` when needed; `timeout_seconds` remains
required. Explicit generation options passed to `chat()` take precedence over
the adapter's configured defaults.

```yaml
llm:
  roles:
    chat:
      provider: llamacpp
      settings:
        model: llamacpp/models/chat.gguf
        timeout_seconds: 30.0
        n_ctx: 4096
        n_gpu_layers: 20
        temperature: 0.4
```

Plugins may define different fields entirely. Unknown plugin schemas are treated
as remote for consent checks. Engine-bound structured retry policy is isolated
from other engines. Tiered LLM catalogue entries require an explicit transport
timeout, which is honored without automatic widening.

## Plan and execute changes

`engine.provider_changes(revision_guard=..., strategies=..., plan_ttl_s=...)`
constructs an administrative controller over that engine's SQL and deployment.
It does not run a migration. Its explicit async methods are:

| Method | Effect |
|---|---|
| `plan_change(request, steps=..., required_capabilities=...)` | Persist an immutable expiring plan; perform no migration/model work |
| `execute_change(plan_id)` | Check source/config/dependency revisions, claim the SQL fence, run the registered strategy |
| `resume_change(operation_id)` | Resume from persisted checkpoints under a renewed fence |
| `verify_change(operation_id)` | Require typed verification evidence, then invoke activation and persist the active result |
| `cleanup_change(operation_id)` | Apply retention, ownership and category guards before scoped cleanup |

Controllers reject another deployment's plans and become invalid when their
Engine stops or restarts. Unsupported category/strategy pairs produce unsupported
plans; they do not silently choose another strategy. Paid model quality probes
and historical re-derivation never run implicitly.

| Change | Strategy | Required execution implementation |
|---|---|---|
| Graph/vector/lexical | `projection_rebuild` | `ProjectionSwitchStrategy` with an event-log backend, explicit admission fence, config resolver and generation policy |
| Embedding space | `reembed` | Injected `ServiceChangeBackend` proving new-space indexing, encoder revisions and zero pending records |
| LLM/reranker | `forward_revision` | Injected backend proving the forward-only configuration/producer/prompt revision and in-flight policy |
| Authoritative database | `verified_migration` | Injected backend implementing source fence, copy/schema proof and target receipt/acknowledgement |
| Cache | `coldstart_reset` | Injected backend proving the owned old/new key generation and TTL/purge policy |
| Worker | `handover` | Injected backend proving ownership epochs, lease transfer and acknowledgement reconciliation |

Use `build_service_change_strategies(backends)` to register the non-projection
strategies for which a real backend is supplied. The strategy layer validates
evidence and orchestrates execution; it is not itself a database-copy tool,
remote queue migration adapter or automatic model re-embedding implementation.
These deployment-specific implementations must be supplied and tested before
their operations are supported. Fake tests prove orchestration, not native
backend migration support.

## Projection generations and restart

SQL persists provider/target/generation bindings for projector offsets. A config
edit cannot reuse an unrelated index's completed offset. Verified activated
routes must match the supplied startup config, source binding, checkpoints and
operation evidence before rebinding is permitted. Credentials are not stored in
switch manifests. Resolved target identity contributes only through a digest.

`BuiltinProjectionGenerationOpener` uses descriptor-built stores and canonical
projectors for replay. Its explicit config source supplies settings in memory;
its generation policy supplies parity verification, activation, reference checks
and owned cleanup. Activation callbacks must not retain the temporary store
handle: the supported cutover requires stopped/fenced admission and a fresh
Engine using the verified target configuration. This is not an unbounded live
hot-swap facility.

File-backed LanceDB, Tantivy and Ladybug use isolated generation paths. Vector
space state and profile pins use the same isolation. Neo4j uses deployment and
generation-scoped labels. SQLite adjacency shares authoritative SQL tables and
does not support independent generation switches. In-memory targets cannot be
used for durable projection cutover.

Development cleanup may remove an old owned projection after verification;
production defaults to seven days when retention is omitted. Authoritative SQL,
model weights and shared deployments are never automatically deleted. Retained
projections must continue receiving erasures before rollback can be safe.
Cleanup state belongs to the retired source generation, not to the active target.
Restart routing still validates the activated target and its durable verification
evidence while source cleanup is pending or complete; cleanup does not reset the
Engine to its initial projection path.
Projection cleanup can run through the opt-in cleanup worker: it performs one
bounded startup batch followed by bounded periodic batches. Engine shutdown
stops the worker and waits for an in-flight cleanup/backend call before closing
its dependencies. A normally returned cleanup failure remains
`CLEANUP_PENDING` and can be retried by a later sweep. A process crash after a
cleanup claim may leave its durable fence held; the sweeper reports the claim as
failed and does not take it over based on age. Operators must quiesce workers
and explicitly recover an abandoned fence before retrying. No automatic stale
fence takeover is implemented. Recovery is exposed only when the application
injects a `CleanupQuiescenceGuard` that can prove every process which might hold
that deployment fence is stopped. The library has no built-in process inventory
and does not accept a caller-supplied boolean or age threshold as proof.

Before recovery, stop every writer, worker and Engine that can touch the
deployment, back up authoritative SQL, and verify from operational evidence
whether the interrupted cleanup completed. The following queries are read-only
and help compare the operation with its deployment fence (bind the parameters
using your database client's safe parameter mechanism):

```sql
SELECT deployment_id, epoch, active_operation_id
FROM provider_change_fences
WHERE deployment_id = :deployment_id;

SELECT operation_id, deployment_id, status, operation_revision, fence_epoch
FROM provider_change_operations
WHERE operation_id = :operation_id;

SELECT recovery_id, deployment_id, operation_id, proof_id, recovered_at
FROM provider_cleanup_fence_recoveries
WHERE deployment_id = :deployment_id AND operation_id = :operation_id;
```

Start one recovery Engine with automatic cleanup disabled and the deployment's
real quiescence guard and idempotent projection cleanup strategy injected.
Supply the guard as
`ProviderChangeRuntime(..., cleanup_quiescence_guard=verified_worker_inventory)`
on `Engine(provider_change_runtime=...)`; YAML configuration cannot instantiate
or replace this trusted application dependency.
Inspect the exact current operation state through the Engine-bound controller,
then submit that revision and fence epoch. Recovery atomically advances the
fence epoch and operation revision, clears the abandoned owner and writes an
audit receipt. The request and quiescence proof bind to the pre-recovery
revision; `receipt.operation_revision` reports the new revision written by the
recovery transaction. It does not perform target cleanup itself; call
`cleanup_change` after recovery if the interrupted cleanup still needs to run.
Never edit the fence table directly.

```python
from memspine.providers.recovery import CleanupFenceRecoveryRequest

changes = engine.provider_changes()
operation = await changes.get_operation(operation_id)
receipt = await changes.recover_abandoned_cleanup_fence(
    CleanupFenceRecoveryRequest(
        deployment_id=deployment_id,
        operation_id=operation.operation_id,
        expected_operation_revision=operation.operation_revision,
        expected_fence_epoch=operation.fence_epoch,
        evidence_reference=incident_evidence_id,
        actor=authenticated_operator_id,
    )
)
# After confirming the receipt and that cleanup is still required:
await changes.cleanup_change(operation_id)
```

`evidence_reference` is passed to the injected guard, which must independently
validate it and issue a time-bounded proof bound to the exact deployment,
operation, revision and fence epoch. Inspect recorded recoveries with
`await changes.cleanup_fence_recoveries(operation_id)`. Recovery refuses stale
state and cannot revoke a live owner unless the application guard is defective;
implement the guard from the deployment's actual process/lease control plane.

Automatic sweeps are disabled by default (`cleanup_interval_s: null`); the
default batch limit is 50 operations. Enable them only with the application's
actual revision guard and registered projection strategies:

```python
from memspine import Engine
from memspine.providers.control import ProviderChangeRuntime

# revision_guard and projection_strategies are application-owned implementations
# of the contracts above, not names loaded from YAML.
engine = Engine(
    template="core",
    provider_change_runtime=ProviderChangeRuntime(
        revision_guard=revision_guard,
        strategies=projection_strategies,
        plan_ttl_s=900.0,
    ),
    runtime={"service_switches": {"projections": {
        "cleanup_interval_s": 60.0,
        "cleanup_batch_size": 50,
    }}},
)
await engine.start()
changes = engine.provider_changes()
# changes.last_cleanup_sweep and changes.last_cleanup_sweep_error expose
# bounded counts/operation IDs and sanitized error types.
await engine.stop()
```

Enabling the interval without an injected projection strategy fails startup
before provider resources open. The sweep only considers verified, owned,
expired graph/vector/lexical generations; it never initiates a new cutover.

Disk/remote cache namespaces include a hash of the persisted deployment identity.
No previous unscoped cache keys are read or translated.

## Verification limits

Local behavioural tests and fake backend contracts are separate from live
PostgreSQL, Neo4j, Redis/Valkey and local model gates. A skipped live check is
unverified. OpenSearch lexical, Weaviate lexical/vector and pgvector vector are
implemented optional adapters with separate native contract evidence. A dependency
extra alone is never support evidence. Portable hybrid
fusion is read policy; future native hybrid capability must avoid duplicate
contributions from the same backend.

| Area | Verified implementation or remaining boundary |
|---|---|
| Native backend matrix | Latest combined run: eight passed, no skips, covering PostgreSQL, Neo4j, Redis, Valkey, OpenSearch, Weaviate and embedded providers; see [the report](provider_verification/live-provider-core-latest.json) and [the test guide](v2/PROVIDER_LIVE_CONTRACTS.md) |
| pgvector | A separate Docker-backed contract verifies native cosine ranking, namespace filtering, dimension validation, deletion, health and persistence across Engine restart; see [the sanitized run record](provider_verification/pgvector-live-contract.json) and `tests/integration/providers/test_pgvector_contract.py` |
| Remote model/worker/cache cutover | Typed HTTP execution adapter is implemented; the actual deployment controller and native handover evidence are supplied by the application; see [the deployment runbook](PROVIDER_DEPLOYMENT_RUNBOOK.md) |
| Automatic expired-generation deletion | Opt-in bounded startup/periodic sweeps are implemented; abandoned crash-held fences require an injected quiescence guard and an audited explicit recovery call |
| Strict parsing for every pipeline policy | Implemented with strict scalar validation and recursively immutable resolved configuration |
| OpenSearch and Weaviate | Implemented and native-container-tested; independent lexical/vector ports, explicit settings and scoped projection ownership |

See [the provider plan](PROVIDER_ARCHITECTURE_PLAN.md) for phase evidence and
[ADR-068](adr/ADR-068-provider-contracts-and-controlled-changes.md) for decisions.
The [final verification report](provider_verification/README.md) includes full
test counts, live-backend evidence and repeated performance measurements. The
snapshot optimization is measured separately; cold startup remains 12.1% slower after import, while combined import/startup is
2.8% faster and warm-start p95 5.8% faster in the final local sample. Investigation and
profiling evidence are recorded alongside the raw samples.

## Run the local gates

From the repository root in PowerShell, using the project's Python environment:

```powershell
$env:PYTHONPATH = 'src;evals'
$py = 'C:/Users/hembad/Documents/GitHub/memspine/.venv/Scripts/python.exe'
& $py -m pytest tests/unit/providers tests/unit/config tests/unit/runtime tests/unit/store `
    -p no:deepeval -o addopts='' -q --tb=short
& $py -m pytest tests/combinations tests/integration `
    -p no:deepeval -o addopts='' -q --tb=short
& $py -m pytest tests/unit -p no:deepeval -o addopts='' -q --tb=short
& $py -m ruff check src
& $py -m mypy src/memspine
```

The interactive ingest check remains available after the config migration:

```powershell
& $py docs/v2/flow_checks.py ingest F1.8 --events --lines --only derive/ --limit 400 `
    2>&1 | Tee-Object -FilePath './extraction.log'
```

These checks use temporary/offline fixtures where applicable. Configure live
backend gates separately; do not interpret a skipped PostgreSQL or remote-service
test as verified support.
