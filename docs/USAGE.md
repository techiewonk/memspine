# memspine — usage guide

How to install, construct an `Engine`, and drive every memory type with the real
API. All snippets are grounded in `src/memspine/engine.py`,
`src/memspine/cli.py`, and `src/memspine/protocols/rest/`. Runnable end-to-end
tours are in [`examples/`](../examples).

For *what* each feature is, see [`FEATURES.md`](./FEATURES.md).

---

## Install

The **core** install is slim — SQLite storage + FTS5, **LanceDB vector** (ADR-021),
fastembed embeddings, inline workers. Add capabilities as extras.

```bash
# Users:
pip install memspine
pip install "memspine[kuzu,ingest,rest]"   # pick the extras you need

# Developers (all extras + test/lint/docs tooling):
uv sync --all-extras
just check        # ruff + mypy --strict + pytest
```

Common extras: `kuzu` (graph), `ingest` (markitdown+chonkie), `ner` (gliner2),
`structured` (instructor), `compress` (llmlingua, E5), `rerank` (flashrank, E8),
`community` (graspologic), `rest` (FastAPI), `dbos`/`taskiq` (durable/brokered workers). See the
[README extras table](../README.md#-install--extras) for the full set.

> A feature that needs a missing extra raises `MissingServiceError` naming the
> extra (unless `strict_services: false`).

---

## Constructing an Engine

The constructor layers config: **defaults → template → user YAML → env → kwargs**
(D-11). Nothing runs until you `await engine.start()`.

```python
from memspine import Engine

Engine(
    template=None,                 # a shipped template name, e.g. "base", "coding"
    user_config=None,              # path to a YAML file, or a dict
    dotenv_path=".env",            # None to skip .env loading
    **overrides,                   # highest-precedence config, e.g. storage={"path": ...}
)
```

**From a template** (partial overlay on `base`):
```python
engine = Engine(template="personal")           # working+episodic+semantic+reflective+prospective
await engine.start()
```

**Explicit config via kwargs** (override any block):
```python
engine = Engine(
    template="base",
    storage={"backend": "sqlite", "path": "./memspine.db"},   # ":memory:" for ephemeral
    embedding={"provider": "hash"},             # deterministic, offline; default is "fastembed"
    vector={"backend": "lance"},                # lance is the sole store (ADR-021); weaviate reserved
    memories={
        "semantic": {"enabled": True, "policies": {"entity_extraction": "off"}},
        "prospective": {"enabled": True},
    },
    read={"rerank": "off", "hybrid": False},
    strict_services=True,
)
await engine.start()
...
await engine.stop()
```

**From a YAML file:**
```python
engine = Engine(user_config="memspine.yaml")
```

Inspect the effective world any time:
```python
print(engine.describe())   # enabled types, services, event-log mode, projectors, runner, ...
```

### Templates

| Template | Enables |
|----------|---------|
| `base` | working + episodic + semantic |
| `coding` | + procedural (`conflict_bias: newest`) |
| `personal` | + reflective + prospective |
| `voice` | rolling+zstd event log; tighter working window (`page_size: 8`) |
| `multi_agent` | + shared |
| `regulated_financial` | full audit log, strict PII, no forgetting |

---

## The API is async-first

Every verb is a coroutine — `await` it inside an async context, or wrap a
top-level call in `asyncio.run(...)` as the examples do. Thin **sync wrappers**
exist for a small subset only: `start_sync`, `write_sync`, `retrieve_sync`,
`stop_sync`. Calling a sync wrapper from inside a running loop raises.

```python
engine = Engine(template="base", embedding={"provider": "hash"})
engine.start_sync()
engine.write_sync("hello", namespace="demo")
print(engine.retrieve_sync(namespace="demo"))
engine.stop_sync()
```

---

## Worked examples per memory type

Assume `engine` is started. See [`examples/`](../examples) for full versions.

### Semantic — write + search
```python
await engine.write(
    "primary region is eu-west-1",
    namespace="ops", entity="deploy", attribute="region",   # keys the conflict ladder
)
for record, score in await engine.search("where do we deploy?", namespace="ops", top_k=8):
    print(f"{score:0.3f}  {record.content}")
```
`write` returns the materialized `MemoryRecord`; `search` returns
`(record, score)` pairs sorted by the M1 composite score (recency · relevance ·
importance · utility), not raw cosine.

### Working — persona + assembly
```python
await engine.set_persona("agent/demo", "You are a concise coding assistant.")
await engine.write("user likes type hints", namespace="agent/demo", memory_type="working")

ctx = await engine.assemble("what does the user like?", namespace="agent/demo", budget_tokens=500)
for i, record in enumerate(ctx.records):
    boundary = "  <-- cache boundary" if i == ctx.boundary_index else ""
    print(i, record.memory_type, record.content, boundary)
print("abstained:", ctx.abstained, "tokens:", ctx.tokens_used)
```
Records before `boundary_index` are the stable prefix (persona, facts) to feed
provider prefix caching; volatile episodic/working content follows.

### Episodic — timeline + sessions
```python
await engine.write("build started", namespace="dev", memory_type="episodic")
events = await engine.timeline(namespace="dev")
sessions = await engine.sessions(namespace="dev", gap_minutes=30)
```

### Resource — ingest *(needs `memspine[ingest]`)*
```python
chunks = await engine.ingest("docs/runbook.md", namespace="ops")
```

### Procedural — skill ladder + plans
```python
s = await engine.add_skill("run pytest -q then ruff check", name="verify", namespace="dev")
s = await engine.promote_skill(s.record_id, namespace="dev")                       # draft -> staged
s = await engine.promote_skill(s.record_id, namespace="dev")                       # staged -> verified
s = await engine.promote_skill(s.record_id, namespace="dev", dry_run_passed=True)  # -> active
active = await engine.skills(namespace="dev")        # ACTIVE only by default

await engine.record_plan("ship a release", "1. bump version 2. tag 3. push", namespace="dev")
plan = await engine.recall_plan("cut a release", namespace="dev")   # None if nothing clears the floor
```

### Reflective — derive from records
```python
a = await engine.write("build failed on windows", namespace="dev", memory_type="episodic")
b = await engine.write("build failed on windows again", namespace="dev", memory_type="episodic")
note = await engine.reflect("windows builds are flaky", [a.record_id, b.record_id], namespace="dev")
```

### Associative — links + graph recall
```python
await engine.associate(a.record_id, b.record_id, namespace="dev", rel="related", weight=1.0)
neighbours = await engine.related(a.record_id, namespace="dev", k=10)   # personalized PageRank
```

### Prospective — watches
```python
from datetime import UTC, datetime, timedelta
now = datetime.now(UTC)

w = await engine.watch("rotate the API key", due_at=now + timedelta(minutes=30), namespace="ops")
fired = await engine.due(namespace="ops", now=now + timedelta(hours=1))
await engine.acknowledge_watch(w.record_id, namespace="ops")

# invalidation watch: fires when the watched fact is superseded
await engine.write("region is eu-west-1", namespace="ops", entity="deploy", attribute="region")
await engine.watch("recheck runbooks", namespace="ops", entity="deploy", attribute="region")
await engine.write("region is us-east-2", namespace="ops", entity="deploy", attribute="region")
fired = await engine.due(namespace="ops", now=datetime.now(UTC))
```

### Shared — grants + cross-namespace search
```python
await engine.grant("analyst", namespace="ops", memory_types=["semantic"])
results = await engine.shared_search("primary region", namespace="analyst")
for record, _ in results:
    if record.namespace != "analyst":
        print("foreign hit:", record.namespace, "trust:", record.trust)   # capped
issued = await engine.grants_from(namespace="ops")
await engine.revoke("analyst", namespace="ops")
```

### Governance
```python
report = await engine.audit_taint(record_id, namespace="ops")   # origin + blast radius
await engine.forget(record_id, namespace="ops")                 # soft: status=DELETED
await engine.forget(record_id, namespace="ops", hard=True)      # hard: row + log payloads redacted
proof = await engine.verify_forget(record_id, namespace="ops")  # {"clean": True, ...}
await engine.sleep()      # run consolidate -> decay -> compress -> prune now
await engine.rebuild()    # replay every projector from seq 0
```

---

## CLI

Installed as `memspine` (see `pyproject.toml [project.scripts]`).

```bash
# Config (D-11/D-12)
memspine config validate -t personal              # dependency closure + effective combination
memspine config validate -c ./memspine.yaml
memspine config resolve                            # merged config, "# source:" per key

# Prompts (D-43)
memspine prompts list                              # id, version, role, format, source layer
memspine prompts show extract                      # full frontmatter + body of one prompt
memspine prompts resolve                           # id@version # source: defaults|override

# Governance (E1 / M7)
memspine audit taint <record_id> --db ./memspine.db -n <namespace>
memspine forget <record_id> --db ./memspine.db -n <namespace>
memspine forget <record_id> --hard --verify        # provable erasure; exits 1 if not clean
```

`config`/`prompts` commands accept `-t/--template` and `-c/--config`.
`forget`/`audit` boot a throwaway engine on `--db` (hash embedder, deterministic).

---

## REST protocol *(needs `memspine[rest]`)*

One FastAPI app wraps one `Engine`; **you own the engine lifecycle** — start it
before serving, stop it after.

```python
from memspine import Engine
from memspine.protocols.rest import create_app

engine = Engine(template="personal")
await engine.start()
app = create_app(engine)         # serve with: uvicorn my_module:app
```

`create_app` is the only import safe without fastapi installed — it raises
`MissingServiceError("protocols.rest", extra="rest")` if the `rest` extra is
missing.

### Routes

| Method & path | Verb |
|---------------|------|
| `POST /write` · `POST /search` · `POST /assemble` · `POST /retrieve` | core read/write |
| `DELETE /records/{id}?hard=` · `GET /describe` | forget · introspect |
| `POST /skills` · `POST /skills/{id}/promote` · `DELETE /skills/{id}` | procedural |
| `POST /plans` · `GET /plans/recall` | plan cache (E6) |
| `POST /reflect` | reflective |
| `POST /watches` · `GET /watches/due` · `POST /watches/{id}/ack` | prospective |
| `POST /grants` · `DELETE /grants` · `GET /grants` · `GET /shared_search` | shared |
| `POST /subscriptions` · `GET /subscriptions` | standing queries |
| `POST /sleep` · `POST /rebuild` · `GET /audit/taint/{id}` | maintenance / governance |

Errors map cleanly: `ConflictError`→409, `MissingServiceError`→501,
`MemspineError`→400, anything else →500 with a generic body (no stack traces
leak). Request bodies over 1 MiB are rejected with 413.

### ⚠️ Authentication is the deployer's job (ADR-017 / ADR-018)

**The v0.1 REST app has no authentication.** The caller's namespace comes from
the `X-Memspine-Namespace` header (default `"default"`) and is **trusted
verbatim** — whoever can reach the app can read and write every namespace.

- Put the app behind a reverse proxy / auth middleware and override the
  namespace seam:
  ```python
  from memspine.protocols.rest.app import resolve_namespace
  app.dependency_overrides[resolve_namespace] = my_authenticated_namespace
  ```
- REST writes are forced onto the low-trust `rest` channel: a caller cannot
  claim `role="operator"` to escalate trust or dodge the firewall (SEC-C1). The
  role is preserved for provenance only.
- `/sleep`, `/rebuild`, `/audit/taint` are engine-global or cross-cutting —
  keep them on an internal-only network boundary, never exposed to tenant
  callers.

Never expose this app to an untrusted network without filling the auth seam.

---

## Swap a backend (config alone)

Every store is a port; you pick the adapter by config, and the event-sourced core
stays the single source of truth. Nothing below changes the API you call — only
which backend the same verbs run against. Each swap is a small config diff.

**Storage: SQLite → PostgreSQL** *(needs `memspine[postgres]`)* — one dialect-neutral
schema serves both (ADR-025). `data_dir` is where the file-backed projections
(LanceDB vectors, Tantivy lexical) live, since a DSN is not a filesystem path.
```yaml
storage:
  backend: postgres
  url: postgresql+psycopg://user:pass@host:5432/memspine   # secrets-resolved
  data_dir: /var/lib/memspine                               # base dir for derived files
read:
  lexical_provider: tantivy   # FTS5 is SQLite-only; use tantivy when hybrid is on with postgres
graph:
  provider: kuzu              # sqlite_adjacency is SQLite-only; use kuzu/ladybug with postgres
```

**Cache: in-process → Redis** *(needs `memspine[redis]`; `valkey` is wire-compatible)* —
also `lmdb` for a persistent single-process cache (`memspine[lmdb]`).
```yaml
cache:
  backend: redis            # memory (default) | lmdb | redis | valkey
  url: redis://localhost:6379/0
  namespace: memspine       # key prefix so instances can share one server
  default_ttl_seconds: 3600
```

**LLM: local → cloud/Bedrock** — LiteLLM routes by the model-id **prefix** (ADR-024).
The special `llamacpp/<path>` prefix runs the in-process llama.cpp adapter
(`memspine[llmlocal]`); every other prefix (`openai/`, `ollama/`, `bedrock/`,
`vertex_ai/`, `azure/`, …) goes through LiteLLM.
```yaml
llm:
  roles:
    extract:                       # local Ollama
      model: ollama/llama3
      api_base: http://localhost:11434
    judge:                         # AWS Bedrock
      model: bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0
      aws_region: us-east-1
```
> **⚠ Breaking (ADR-024):** `llm.roles.<role>` dropped `provider` and `base_url`.
> Migrate `provider: openai` + `base_url: …` → `model: openai/<name>` + `api_base: …`.
> `ollama`/`vllm`/`lmstudio`/OpenAI-compatible all become a `model` prefix + `api_base`.

**Embedding: local → cloud (LiteLLM)** — a cloud embedder's output dimension is not
locally discoverable, so `dim` is **required** when `provider: litellm`.
```yaml
embedding:
  provider: litellm
  model: openai/text-embedding-3-small
  dim: 1536                 # REQUIRED for litellm; the vector store needs it up front
  # api_base / api_key / aws_region as your provider needs
```

**Secrets: env → AWS Secrets Manager** *(needs `memspine[aws]`)* — secrets resolve
*before* the config exists, so this is an **environment variable**, not a config key
(ADR-023). `aws` chains env/`.env` first, then AWS, so local values always win.
```bash
export MEMSPINE_SECRETS_BACKEND=aws     # env (default) | aws
```

**Retrieval: vector-only → hybrid + rerank** — fuse a lexical BM25 leg via RRF, then
optionally cross-encoder rerank. Both default OFF (results stay bit-identical to the
vector-only pipeline until you opt in).
```yaml
read:
  hybrid: true                 # fuse vector + lexical BM25 via RRF (D-25)
  lexical_provider: sqlite_fts5   # sqlite_fts5 (default) | tantivy [tantivy]
  rerank: fastembed            # off (default) | fastembed | flashrank [rerank] | litellm
  rerank_model: cohere/rerank-english-v3.0   # required only when rerank: litellm
```

---

## Config-key reference

Every key of `MemspineConfig` (`src/memspine/config/schema.py`) and its sub-blocks.
`*` marks an open map keyed by name (a role, memory type, or namespace). Dict-valued
keys (`read.scoring`, `*.policies`, `prompts.overrides`) take nested option maps
validated by their own policy/registry. This table is kept honest by
`tests/unit/test_docs_config_surface.py`, which fails if a key here does not exist
in the schema — or if the schema gains a key not documented here.

<!-- CONFIG-KEYS-TABLE:START -->
| Key | Default | Notes |
|-----|---------|-------|
| `profile` | `simple` | Behavior profile; templates set it (base/coding/personal/voice/multi_agent/regulated_financial). |
| `strict_services` | `true` | Missing service hard-fails naming the extra (D-10); `false` starts degraded. |
| `event_log.mode` | `full` | `full` \| `rolling` (bounded window) \| `ephemeral` (nothing persisted — no rebuild/audit) (D-45). |
| `event_log.retention_days` | `30` | Rolling-window retention floor; never prunes past a projector high-water mark. |
| `event_log.compress` | `false` | zstd-compress event payloads at rest. |
| `storage.backend` | `sqlite` | `sqlite` \| `postgres` (ADR-025). |
| `storage.path` | `./memspine.db` | SQLite db file, or `:memory:` for ephemeral. |
| `storage.url` | `null` | Postgres DSN (secrets-resolved); required when `backend: postgres`. |
| `storage.data_dir` | `null` | Base dir for file-backed projections (LanceDB/Tantivy); required for postgres. |
| `embedding.provider` | `fastembed` | `fastembed` (ONNX/CPU) \| `hash` (deterministic, tests) \| `static` (model2vec `[static]`) \| `litellm` (cloud). |
| `embedding.model` | `BAAI/bge-small-en-v1.5` | Embedder model id. |
| `embedding.dim` | `null` | **Required** when `provider: litellm` — a cloud embedder's output dim. |
| `embedding.api_base` | `null` | Endpoint override (litellm). |
| `embedding.api_key` | `null` | API key (litellm; secrets-resolved). |
| `embedding.aws_region` | `null` | Bedrock region (litellm). |
| `vector.backend` | `lance` | `lance` is the sole store (ADR-021); `weaviate` reserved (raises). |
| `vector.quantization` | `auto` | `auto` (manifest-driven) \| `none` \| `int8` \| `binary` — E4 native rescore (ADR-020). |
| `cache.backend` | `memory` | `memory` \| `lmdb` `[lmdb]` \| `redis` `[redis]` \| `valkey` `[valkey]` (D-09). |
| `cache.path` | `./memspine.cache.lmdb` | LMDB env directory. |
| `cache.url` | `redis://localhost:6379/0` | Redis/Valkey DSN (secrets-resolved). |
| `cache.namespace` | `memspine` | Key prefix so instances can share one store. |
| `cache.default_ttl_seconds` | `null` | Default TTL when a caller passes none (`null` = no expiry). |
| `cache.max_entries` | *(constant)* | In-memory backend entry cap. |
| `graph.provider` | `sqlite_adjacency` | `sqlite_adjacency` \| `kuzu` `[kuzu]` \| `ladybug` `[graph]` \| `neo4j` (reserved) (D-26). |
| `llm.roles.*.model` | `""` | LiteLLM model id; **prefix routes** (`openai/`, `ollama/`, `bedrock/`, `vertex_ai/`, `llamacpp/<path>`) (ADR-024). |
| `llm.roles.*.api_base` | `null` | Local endpoint override (Ollama, vLLM, …). |
| `llm.roles.*.api_key` | `null` | API key (secrets-resolved). |
| `llm.roles.*.aws_region` | `null` | Bedrock region. |
| `llm.roles.*.timeout_seconds` | `60.0` | Per-call timeout. |
| `read.scoring` | `{}` | Options for `ScoringPolicy.bind` (M1 composite). |
| `read.assembly` | `{}` | Options for `AssemblyPolicy.bind` (E2 placement / MMR). |
| `read.rerank` | `off` | `off` \| `fastembed` \| `flashrank` `[rerank]` \| `litellm` — E8 cross-encoder (D-51). |
| `read.rerank_model` | `null` | LiteLLM rerank model id; required when `rerank: litellm`. |
| `read.static_prefilter` | `false` | E8 cheap lexical-overlap gate (post-vector). |
| `read.static_embedding_prefilter` | `false` | E4 model2vec static-cosine gate `[static]`. |
| `read.hybrid` | `false` | Fuse the lexical BM25 leg via RRF (D-25); off = vector-only, bit-identical. |
| `read.lexical_provider` | `sqlite_fts5` | `sqlite_fts5` (FTS5/BM25) \| `tantivy` `[tantivy]`; only when `hybrid` is on. |
| `read.compression` | `{}` | Options for the E5 assembly-stage `CompressionPolicy` (`memspine[compress]`). |
| `workers.runner` | `inline` | `inline` \| `dbos` `[dbos]` \| `taskiq` `[taskiq]` (D-16). |
| `workers.broker_url` | `redis://localhost:6379/0` | taskiq broker endpoint (ignored by other runners). |
| `workers.dbos_system_database_url` | `null` | DBOS system db; `null` derives a SQLite file beside `storage.path`. |
| `prompts.overrides` | `{}` | Per-prompt overrides (body/system/format/version/output_model/token_budget) (D-43). |
| `prompts.partials` | `{}` | Override fragments for shared Jinja `{% include %}` partials (anti-injection block, output footer); `<name>` → replacement text, consulted before the shipped `_partials/` dir (B1). |
| `memories.*.enabled` | `false` | Enable a memory type (`working`/`episodic`/`semantic`/…); C1b auto-enables prerequisites. |
| `memories.*.policies` | `{}` | Per-type policy overrides (conflict/dedup/trust/entity_extraction/page_size/…). |
| `namespaces.*.policies` | `{}` | Per-namespace policy overrides (D-14). |
<!-- CONFIG-KEYS-TABLE:END -->

Secrets backend selection is **not** a config key: `MEMSPINE_SECRETS_BACKEND`
(`env` default \| `aws`) is an environment variable, because secrets resolve before
`MemspineConfig` is built (ADR-023).

---

## Where to go next

- [`FEATURES.md`](./FEATURES.md) — the feature catalog (types, firewall, E2–E9).
- [`examples/01_quickstart.py`](../examples/01_quickstart.py) → `04_prospective_shared_rest.py`.
- [`memspine-structure-plan.md`](./memspine-structure-plan.md) — the authoritative blueprint.
- [`adr/`](./adr/) — architecture decision records (ADR-001 … ADR-025); the newest cover
  LanceDB core vector (ADR-021), shared cache backends (ADR-022), pluggable secrets +
  AWS (ADR-023), the LiteLLM LLM/embedding/rerank gateway (ADR-024), and PostgreSQL
  storage (ADR-025).
