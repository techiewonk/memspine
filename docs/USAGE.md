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
`community` (graspologic-native), `rest` (FastAPI), `dbos`/`taskiq` (durable/brokered workers). See the
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
| `base` (profile `simple`) | working + episodic + semantic, **with the measured read-path advantages on** (ADR-033): replay reads, relative dates resolved, time order for ordering questions, relative floor, relevance-first scoring, no access recording, dated answer prompt |
| `core` | the bare configuration (pre-ADR-033 `base`): auto read mode, composite scoring, access recording, base chat prompt |
| `coding` | + procedural (`conflict_bias: newest`) |
| `personal` | + reflective + prospective |
| `voice` | rolling+zstd event log; tighter working window (`page_size: 8`) |
| `multi_agent` | + shared; secure defaults: `integrity.enabled`, `untrusted_wrap_below: 0.5`, `redact_secrets`, `pii: redact` |
| `regulated_financial` | full audit log, strict PII, no forgetting; the same secure defaults as `multi_agent` |
| `assistant` (**default**, ADR-032) | = `base` (since ADR-033 the advantages live in `base`); the name for chat workloads (LoCoMo 70.7 → 78.3%, measured) |

> **Default template (ADR-032).** `Engine()` with no `template` loads `assistant`. Pass `template="base"` for the plain `simple` profile (the previous behaviour). A caller that names a `profile` but no template (`Engine(profile="simple")`, or `profile:` in the user config) gets `base`, never the assistant settings. The CLI `audit taint` / `forget` commands always run on `base`.

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

With `memories.associative.policies.entity_nodes: true`, records also link to the entities
they name, and `read.graph_leg` / `read.cards_include_edges` read the graph from the
entities a query names (see the config-key reference). `related()` ignores entity edges.

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
await engine.approve_quarantined(held_id, namespace="ops", actor="ops:lee", principal="ops:lee")
```

A hard forget cascades to every record derived from the forgotten one: mined
facts, cues, reflections, and the consolidation and reorganize summaries whose
members include it (a summary of an erased member is erased, never re-derived).
It also rewrites the LanceDB vector table and drops its older versions, so the
erased vector cannot be checked out again; `verify_forget` reports
`vector_history_absent` and stays unproven (`clean: False`) when that purge did
not run. `residual_risks` names what the proof cannot cover: a Tantivy segment
keeps a deleted document's terms until it is merged.

Caller tags never include the engine-only tags (`constants.RESERVED_TAGS`:
lead-section and header tags, the cue tag, `taint_archived`,
`quarantine_rejected`); the write door drops them. `approve_quarantined` refuses
a reviewer whose `principal` or `actor` is the held record's `source.principal`.

```python
await engine.sleep()      # run consolidate -> decay -> compress -> prune now
await engine.rebuild()    # replay every projector from seq 0
```

### Data-subject rights and audit (#46-#51, all opt-in)
```python
lines = await engine.export("user/ana", subject="ana", include_events=True)  # JSONL lines
new = await engine.correct(("ana", "city"), "Ana lives in Nice",
                           actor="ana", reason="moved", namespace="user/ana")
await engine.forget(record_id, namespace="user/ana", hard=True, actor="ana", reason="art. 17")
await engine.write("ticket #42 ...", namespace="user/ana", purposes=["support"])
hits = await engine.search("ticket", namespace="user/ana", purpose="support")
await engine.expire_retention()          # retention.classes, also a sleep-cycle stage
assert await engine.audit_chain_ok()     # audit.reads / audit.actions hash chain
```

- **Export** (#46): a header line, then one line per live, archived or held record
  (soft-forgotten ones are left out) in `(recorded_at, record_id)` order, with
  provenance and, by default, archived versions; `include_events=True` adds the
  namespace's log events as stored (hard-erased content is already redacted; the
  content of soft-forgotten records is scrubbed). Same data, same bytes.
- **Correct** (#47): user-direct supersession by record id or `(entity, attribute)`.
  The conflict ladder does not run, so a correction is never CONTESTed under
  `contest_lower_trust` (the `base` template); the firewall still screens the new
  value. The new record's WRITE event carries `correction: {supersedes, actor, reason}`.
- **Read audit / hash chain** (#49), **purposes / remote-LLM gate** (#50) and
  **retention classes** (#48): see `audit.*`, `consent.*` and `retention.classes`
  in the config-key reference. `memspine.core.privacy.principal_scope(name)` binds
  the principal recorded by the read audit outside REST.
- The FORGET event itself still records `actor: user`; under `audit.actions` the
  chained `memory.audit` event carries the real actor and reason.

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
memspine export --db ./memspine.db -n user/ana --out ana.jsonl   # subject-access export (#46)
memspine export -n user/ana --subject ana --events --no-history --out ana.jsonl
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
| `DELETE /records/{id}?hard=&reason=` · `GET /describe` | forget · introspect |
| `POST /correct` | #47 correction by `record_id` or `entity` + `attribute` (never contested) |
| `GET /export?subject=&include_history=&include_events=` | #46 subject-access export, `application/x-ndjson` (admin under auth) |
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

### Reference auth middleware (#51, ADR-041)

`rest.auth.mode: api_key` or `oidc_jwt` turns on a reference middleware that binds
an authenticated principal and its allowed namespaces to each request:

```yaml
rest:
  auth:
    mode: api_key
    api_keys:
      - {key_env: MEMSPINE_KEY_SUPPORT, principal: support-bot, namespaces: ["tenant-a/*"]}
      - {key_env: MEMSPINE_KEY_OPS, principal: ops, namespaces: ["*"], admin: true}
  rate_limit: {requests_per_second: 5, burst: 20}
```

- No or bad credentials → 401; a namespace outside the principal's globs → 403;
  `/sleep`, `/rebuild`, `/export`, `/quarantine…` without the admin role → 403;
  over the rate limit → 429.
- The principal replaces the caller-claimed `actor` on `/correct` and forget, and is
  the `actor` of `memory.read_audit` events.
- `oidc_jwt` needs `pyjwt` (a clear `ConfigError` otherwise) and reads the principal,
  namespaces and roles from configurable claims; keys come from `jwks_url` or an env var.
- `create_app(engine, rest=RestConfig(...))` overrides the engine's `rest` block.

It is a **reference, not a production auth plane**: no key rotation, revocation,
per-route scopes or cross-replica rate limiting. With the default `mode: none` the
rest of this section applies unchanged.

### ⚠️ Authentication is the deployer's job (ADR-017 / ADR-018)

**The REST app has no authentication by default.** The caller's namespace comes from
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
- `/quarantine/{id}/approve` and `/quarantine/{id}/reject` are operator-only:
  they return 403 until you override the operator seam, and the identity it
  returns is the decision's actor (the request body cannot name the reviewer):
  ```python
  from memspine.protocols.rest.app import resolve_operator
  app.dependency_overrides[resolve_operator] = my_authenticated_operator
  ```

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
  rerank: fastembed            # off (default) | fastembed | flashrank [rerank] | litellm | qwen3 [st]
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
| `profile` | `simple` | Behavior profile; templates set it (base→simple/core/coding/personal/voice/multi_agent/regulated_financial/assistant). |
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
| `embedding.request_dimensions` | `false` | litellm only: request exactly `dim` dimensions (Matryoshka models: Cohere embed-v4 256/512/1024/1536, Titan v2, OpenAI v3). |
| `embedding.query_input_type` | `null` | litellm only: input type for retrieval queries (Cohere: `search_query`). |
| `embedding.document_input_type` | `null` | litellm only: input type for stored content (Cohere: `search_document`). |
| `embedding.batch_size` | `32` | G9: most texts per embedding call when `write_messages` embeds its turns up front (Cohere on Bedrock: up to 96). |
| `vector.backend` | `lance` | `lance` is the sole store (ADR-021); `weaviate` reserved (raises). |
| `vector.quantization` | `auto` | `auto` (manifest-driven) \| `none` \| `int8` \| `binary` — E4 native rescore (ADR-020). |
| `cache.backend` | `memory` | `memory` \| `lmdb` `[lmdb]` \| `redis` `[redis]` \| `valkey` `[valkey]` (D-09). |
| `cache.path` | `./memspine.cache` | LMDB env directory. |
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
| `llm.roles.*.no_think` | `null` | Qwen3 thinking switch: `true` appends ` /no_think` to the last user message; `null` = on for model ids containing `qwen3`, off otherwise. `<think>…</think>` blocks are always stripped from replies. |
| `read.scoring` | `{}` | Options for `ScoringPolicy.bind` (M1 composite). |
| `read.assembly` | `{}` | Options for `AssemblyPolicy.bind` (E2 placement / MMR). |
| `read.rerank` | `off` | `off` \| `fastembed` \| `flashrank` `[rerank]` \| `litellm` \| `qwen3` `[st]` (Qwen3-Reranker, `rerank_model` defaults to `Qwen/Qwen3-Reranker-0.6B`) — E8 cross-encoder (D-51). |
| `read.rerank_model` | `null` | LiteLLM rerank model id; required when `rerank: litellm`. For `fastembed` / `qwen3` it overrides the default local model. |
| `read.static_prefilter` | `false` | E8 cheap lexical-overlap gate (post-vector). |
| `read.static_embedding_prefilter` | `false` | E4 model2vec static-cosine gate `[static]`. |
| `read.hybrid` | `true` | Fuse the lexical BM25 leg via RRF (D-25; on by default since the v0.2 flip, ADR-019); `false` = vector-only. |
| `read.lexical_provider` | `tantivy` | `sqlite_fts5` (FTS5/BM25) \| `tantivy` `[tantivy]`; only when `hybrid` is on. |
| `read.compression` | `{}` | Options for the E5 assembly-stage `CompressionPolicy` (`memspine[compress]`). |
| `read.record_access` | `true` | Append a RETRIEVE event per search (reinforcement stats). `false` makes reads side-effect free (e.g. benchmark isolation). |
| `read.current_state_view` | `false` | C4': render each retrieved keyed fact as `CURRENT (since date)` plus its superseded `HISTORY` (deterministic, from the bi-temporal chain). |
| `read.temporal_leg` | `false` | C3': fuse a temporal RRF leg: records whose event time lies in an absolute date span named in the query ("7 May 2023", "May 2023", "in 2023"); relative dates are not resolved. |
| `read.rrf_k` | `null` | RRF rank constant k in 1/(k+rank). `null` = 60; Graphiti uses 1 (ablation knob). |
| `read.reply_reserve_tokens` | `0` | ContextPipe: tokens kept free for the reply inside the assembly budget. |
| `read.default_mode` | `auto` | The mode `Engine.read()` uses when the caller passes none (`auto` / `full` / `replay` / `retrieve` / `compose`). The `assistant` template pins `replay`. |
| `read.rerank_date_prefix` | `false` | Hindsight: prefix reranker inputs with `[Date: YYYY-MM-DD]`, so the cross-encoder sees when each candidate happened. |
| `read.skip_rerank_for_ordering` | `false` | Agent Zero: skip the reranker for ordering questions (first / latest / most recent). |
| `decision.provider` | `off` | H24: optional decision provider for calibrated choices among described options without generation. `gliner2` uses the `[ner]` extra (GLiNER2, Apache-2.0). |
| `decision.model` | `fastino/gliner2-base-v1` | H24: the GLiNER2 checkpoint (Hugging Face id; `-large-v1` and `-multi-v1` also exist). |
| `read.planner` | `rules` | H24/G2a: how `read(mode="auto")` picks a mode once full context does not fit. `rules` = deterministic cues; `decision` = the decision provider chooses (falls back to rules on any failure); `llm` = one call to the `plan` LLM role returns a `ReadPlan` (`lookup` / `replay` read by replay, `aggregate` by compose with the plan's up to three subqueries as extra probes, rank-fused with `read.rrf_k`). An unbound role, a failed call or an invalid plan falls back to the rules with a warning. One counted call per auto read. |
| `read.planner_min_confidence` | `0.0` | G2b: with `planner: decision`, a choice below this confidence does not route the read; it keeps the default `replay` (retrieve when no hit is episodic). A choice of unknown confidence (a bare GLiNER2 label) is below any gate above 0. `0.0` = every choice routes. |
| `read.planner_version` | `v1` | #35: with `planner: llm`, `v2` selects the `plan@v2` prompt, which also writes one or two evidence-seeking subqueries for lookup questions ("Is X religious?" → "X church", "X faith"); the lookup read fuses each as an extra vector (+ BM25 under hybrid) leg by RRF. `v1`: unchanged. |
| `read.compose_replay` | `false` | G2c: compose results get the same ±`replay_window` neighbour expansion as replay mode (nearest first, gated and decorated like replayed turns, within the budget), so routing to compose no longer loses the surrounding turns. |
| `read.aggregate_top_k` | `null` | G11: the `top_k` of a read that `read(mode="auto")` routes to compose (the LLM planner's `aggregate`, the decision planner's or the rules' compose), so list and count questions whose evidence spans sessions pool more candidates; the budget still caps the context. An explicit `mode="compose"` keeps the caller's `top_k`. `null` = unchanged. |
| `read.compose_rewrites` | `false` | P4 (JustMem COMPOSE): `read(mode="compose")` adds up to two answer-free query rewrites from the `query_rewrite` LLM role (`@compose` prompt variant). |
| `read.gap_markers` | `false` | H22 (Mastra): with `render: dated`, prefix records after long silences with `[3 weeks later]`. |
| `read.relevance_filter` | `false` | H17: the `relevance` LLM role labels search candidates relevant / related / irrelevant; only "irrelevant" ones are dropped (Hindsight-style; a yes/no filter loses gold evidence). |
| `read.relevance_safety_net` | `10` | H17: the best-scored candidates always kept, whatever their label. |
| `read.replay_topic_segments` | `false` | H15: replay windows stay inside the hit's topic segment. Boundaries are found by TextTiling-style lexical cohesion within a session (no model), so neighbouring turns from another topic are not replayed. |
| `read.rerank_max_top_k` | `null` | H18: run the configured reranker only when `top_k` is at most this. Reranking pays most when few of many candidates are kept. `null` = always. |
| `read.rerank_keep` | `null` | G5b: with a reranker and `candidate_pool > 1`, keep only the best `rerank_keep` candidates after reranking, before assembly fills the budget, so a wider pool sharpens the ranking instead of doubling the context. `null` = keep the whole pool. A failed rerank keeps the pool. |
| `read.core_terms_leg` | `false` | H13: an extra BM25 leg over the question's core terms (interrogative and function words removed), fused by RRF. Needs the lexical store (`read.hybrid`). |
| `read.metadata_leg` | `false` | C3': fuse a metadata RRF leg: records whose `entity` is named in the query, newest first. |
| `read.order_by_time_for_ordering` | `false` | H16: for ordering questions ("first", "latest", "most recent", …) the assembled records are presented in event-time order rather than score order. |
| `read.render` | `plain` | H5: `dated` prefixes each episodic or semantic record with its event date, `[2023-05-08 Mon] …`, after the stable cache prefix. |
| `read.candidate_pool` | `1` | H11: assembly draws from `candidate_pool × top_k` search candidates, so the token budget rather than a fixed K decides how much evidence enters (LoCoMo: top-10 used ~400 of 4,096 tokens; top-30 lifted multi-hop all-evidence coverage from 5% to 19%). Pair with `read.assembly.relative_floor` (H4: drop candidates below that fraction of the best score; 0 = off). |
| `read.resolve_relative_dates` | `false` | H1: annotate relative-time phrases ("yesterday", "last Friday", "two weeks ago", "last month") in assembled/read records with the absolute date, resolved against each record's event time: `last Friday [= Fri 2023-07-14]`. Rules only, no model. Vague phrases ("recently") are left alone. Stored content is unchanged. On LoCoMo temporal evidence the resolver agrees with the gold date in 126/129 resolvable cases. |
| `read.relative_dates_anchored` | `false` | G13: with `resolve_relative_dates`, state week-level phrases relative to the record's own day, LoCoMo's gold convention ("The week before 9 June 2023"): `last week [= the week before 2023-06-09 (2023-06-02..2023-06-08)]` instead of the previous calendar week; `last/this/next weekend` → `the weekend before / of / after <day>`, `last Friday` → `the Friday before <day> (Fri …)`, `two weeks ago` → `two weeks before <day> (≈ …)`, and `a few days ago` → `a few days before <day>` (no span). Months, years and single days are unchanged. Off: byte-identical. |
| `read.relative_week` | `calendar` | #58: with `resolve_relative_dates`, the span of "last/past week" and "next week": `calendar` (the previous / next Monday-to-Sunday week, unchanged) or `preceding_7_days` (the seven days before / after the record's own day, LoCoMo's "the week before <session date>"; the label stays an absolute span). Also used by `consolidation.mine_event_dates` and `read.count_dedupe`. |
| `read.anticipatory_cues` | `false` | C8': a search hit on a cue written with `Engine.add_cues` resolves to its target record; cues are keys, never content (off: invisible). |
| `read.cue_min_trust` | `0.5` | C8': cues below this trust are ignored, so a low-trust source cannot plant cues that redirect retrieval. Cue trust is capped at the target's. |
| `read.topic_timelines` | `false` | H22 (Mnemon): open the volatile context with one dated timeline per topic entity of the retrieved keyed facts: every live fact on that entity (any attribute) plus its superseded history, oldest first, `(until …)` on ended facts. Built at read time from the stored facts (no model, nothing stored). Entries pass the context gates and must be clean (not instruction-flagged, not below `integrity.untrusted_wrap_below`); an entity with a single entry gets none. Applies to `assemble` and `read` (retrieve / replay), not to shared reads. Pair with mined facts (H2): without keyed facts there is nothing to index. |
| `read.timeline_entities` | `3` | H22: how many entities get a timeline, best-scored first. |
| `read.timeline_items` | `8` | H22: the newest entries kept per timeline. |
| `read.standing_instructions` | `false` | H22: right after the pinned persona, a `USER-STATED PREFERENCES` block of the requests and preferences the user stated ("from now on …", "please always …", "I'd prefer …"), newest five, dated. Only `user`-role records at or above `read.standing_min_trust` that pass the context gates; external documents and tool output never qualify. The block is labelled as the user's words, not system instructions. **Risk:** in the trust-horizon threat model a query-only attacker writes as a user (MINJA); leave this off for agents that serve untrusted users. |
| `read.standing_min_trust` | `0.7` | H22: the least (view) trust of a record shown in the standing block. |
| `read.lead_budget_tokens` | `400` | H22: token sub-budget of the lead section (standing block first, then timelines), taken out of the assembly budget and capped at half of it. A block that does not fit is skipped. |
| `read.cards` | `off` | G1b (JustMem cards): `header` opens the volatile context of every read mode and `assemble` with a `FACTS` block of the mined atomic facts relevant to the query, one `[said YYYY-MM-DD] Entity: fact` line each (the date of the fact's earliest source turn; no date without one), fitted as rendered and shown in said order. Facts come from the same hybrid search restricted to `atomic_fact` records and pass every search gate and M12 abstention on their own scores; the rest of the budget goes to the normal read, which then leaves mined facts out (no fact twice) inside its search, before the rerank and `rerank_keep` cut, widening until enough visible records survive. A `full` read leaves out only the facts the header shows. Under `integrity.claims_only_below` a card whose source turn is below the threshold carries the `[CLAIM ...]` marker, and B9 does not put it back as a claim. A header that passed abstention is attached even when the routed read abstains (the read then no longer abstains). Needs `consolidation.mine_facts`. |
| `read.cards_budget_share` | `0.25` | G1b: the share of the budget the cards header may use. With both headers on, `cards_budget_share + profile_budget_share` must be below 1 (config error otherwise). |
| `read.cards_top_k` | `10` | G1b: how many mined facts the header search fetches (best first, kept while they fit). |
| `read.cards_skip_temporal` | `false` | Skip the cards header for date/time questions (`when ...`, `how long ago ...`), so the H1-resolved raw turns answer them; the miner's own dates on cards cost temporal accuracy in the 2026-10-05 smoke test. |
| `read.cards_event_date` | `false` | #29: a card whose mined fact carries a happened date (`consolidation.mine_event_dates`) different from the day it was said renders `[said d1 · happened d2]`. Off: byte-identical. |
| `read.profile_header` | `false` | G3b: after the cards header, a `PROFILE NOTES` block of the H14 profile insights (records `consolidation.reflect_profile` deposits) on the people the query names, or the most relevant insights when none matches, one dated line each. They come from the search restricted to reflective records (every gate applies), and the routed read leaves the shown insights out. Shown only when its own hits pass M12 abstention; names are the capitalised words of the query other than a sentence-initial word (unless repeated), question words and common imperatives (`Tell`, `Give`, `Yes`, `I'm`, ...), matched case-sensitively. |
| `read.profile_budget_share` | `0.15` | G3b: the share of the budget the profile header may use. |
| `read.count_timeline` | `false` | E3: for count questions (`how many times …`, `how many <things> …`, `how often …`; not durations such as `how many days ago`, see `query_shape.is_count`), every read mode and `assemble` lead the volatile context with an `Occurrences (dated):` block: one `- [said YYYY-MM-DD] <mention>` line per distinct occurrence of the counted event among the episodic records the read retrieved, oldest first. The event is the query's core terms without names and count words; a record mentions it when it shares at least half of them. Same-day mentions in one session, or same-day mentions sharing at least half their words, count once. Mined facts, lead blocks and wrapped (instruction-flagged or untrusted) records are left out; the mentions stay in the context. Off: byte-identical. |
| `read.count_budget_share` | `0.1` | E3: the share of the budget kept for the occurrences block (the read gets the rest; lines are kept oldest first while the block fits). Counts toward the header-share check: active `cards_budget_share + profile_budget_share + count_budget_share` must be below 1. |
| `read.count_dedupe` | `false` | #60: with `count_timeline`, a mention's event day is the single day its relative phrase names ("yesterday", "last Friday"), else the day it was said; two mentions on the same event day with at least half their words shared are one occurrence, even when said on different days. Off: byte-identical. |
| `read.graph_leg` | `false` | GP-3 (#14): fuse a graph leg into the RRF ranking. Seeds are the entity nodes the query names (its 1-4-word n-grams matched against the namespace's entity names; the decision provider's optional `entities` hook, GLiNER2, adds names when configured, never required), else the entities of the best 3 hits of the other legs. A walk of `graph_depth` entity hops yields fact records, each followed by its source turns; the first `graph_leg_k` form the leg, and its hits pass every search gate. Needs `memories.associative` with `policies.entity_nodes`. Off: byte-identical (golden `tests/unit/golden/graph_leg_off_read.json`). |
| `read.graph_depth` | `2` | GP-3: walk depth in entity hops (entity -> record -> entity is one), 1-3. |
| `read.graph_leg_k` | `10` | GP-3: the most records (facts and their source turns) the graph leg contributes. |
| `read.graph_min_trust` | `0.25` | GP-10 (#16): a graph walk (the leg and the facts block) never enters a record below this trust, nor a quarantined, erased or taint-rolled-back one; a refused node is a dead end. Default: the firewall's quarantine threshold. |
| `read.cards_include_edges` | `false` | GP-5 (#15): after the cards header, a `GRAPH FACTS` block of the edge facts (`rel:`-tagged records) the graph walk reaches from the entities the query names, live and superseded, one `[2023-05-01 → present] Melanie read "X" (sources: 2)` line each (a superseded fact shows its end date; `sources` counts the live episodes stating it). Shares `cards_budget_share` with the cards header and counts toward the header-share check; the shown facts are left out of the read below. Off: byte-identical. |
| `firewall.enabled` | `true` | `false` keeps trust scoring but disables flagging, anomaly checks and quarantine: the N1 ablation arm only. |
| `firewall.skip_message_roles` | `[]` | H21: `write_messages` never deposits turns with these roles (e.g. `["system", "tool"]`). |
| `firewall.skip_injected_recall` | `false` | H21: never re-deposit a turn carrying memspine's own assembly markers (recalled memory echoed back into the conversation). |
| `firewall.tag_assistant_claims` | `false` | H21: tag assistant turns `assistant_claim`, a proposal rather than an observed fact. |
| `firewall.redact_secrets` | `false` | Replace cloud keys, VCS/chat tokens, JWTs, private keys, `key=value` credentials and emails with `[REDACTED:<kind>]` at write, in content, entity, attribute and tags. |
| `firewall.pii` | `off` | PII pack (phone, Luhn-checked card, US SSN, mod-97 IBAN, IPv4/IPv6) over content, entity, attribute and tags: `redact` masks each match as `[REDACTED:<kind>]`; `tag` keeps the text, adds `pii:<kind>` tags and raises `pii_tier` to at least `high`; `off` does neither. |
| `firewall.max_content_chars` | `null` | Quarantine non-privileged writes longer than this (size anomaly). |
| `firewall.protected_keys` | `[]` | Fact keys (`entity` or `entity.attribute`) only operator/system sources may write; others are quarantined. |
| `integrity.principal_reputation` | `false` | B7: multiply a principal's later write trust by min(1, 2 × Beta mean of its good vs bad records), where bad = quarantined, or the seed of a taint rollback. It only lowers trust, so the trust horizon still holds. `Engine.principal_reputation(p)` returns the factor. |
| `integrity.enabled` | `false` | Trust-horizon invariant (THI) for shared memory (opt-in); off = pre-MTI behaviour, byte-identical. |
| `integrity.attenuation` | `product` | `product` (trust × κ per grant hop) \| `min` (min(trust, κ)). |
| `integrity.kappa` | `0.5` | Default per-grant attenuation κ ∈ (0, 1]. |
| `integrity.edge_kappa` | `{}` | Per-edge κ overrides keyed `"grantor->grantee"`. |
| `integrity.derivation_decay` | `1.0` | Extra factor applied to every `derived_from` deposit (1.0 = none). |
| `integrity.verification_bonus` | `1.0` | Baseline emulation only (MAP-Graph-style bonus, 1.0–2.0): >1 lets derived trust exceed its parents and breaks the invariant on purpose. |
| `integrity.admission_threshold` | `0.0` | Records whose view trust is below θ are never returned by `search`/`shared_search`. |
| `integrity.trust_weighted_ranking` | `true` | Rank by composite score × view trust. |
| `integrity.principal_bound_corroboration` | `true` | Promotion needs corroborators with distinct `source.principal`s (never the held record's). |
| `integrity.merge_reinforcement_gate` | `true` | A less-trusted duplicate never reinforces the record it merges into. |
| `integrity.implicit_parents` | `off` | B0: `turn` / `session`: records each session's reads and makes them implicit parents of its next write (write trust ≤ lowest view trust read), so omitting `derived_from` cannot launder. `turn` keeps the parents for every write until the session's next read; `session` keeps them until `end_session()`. Reads with no `session_id` share one fail-closed ledger per namespace that only `end_session(ns)` clears. |
| `integrity.untrusted_wrap_below` | `0.0` | B6: `assemble` renders records whose view trust is below this inside an untrusted-data wrapper (data, not instructions). 0.0 = off. |
| `integrity.claims_only_below` | `0.0` | B9 facts-only: a context record whose view trust is below this never enters a context window as raw text. Each live, unflagged atomic fact mined from it (H2) stands in once, prefixed `[CLAIM from a low-trust source, unverified]`; with none it is left out. Mined facts, the persona and lead blocks are exempt. Applies to assemble, retrieve, replay, full and compose reads. Needs `integrity.enabled`. Trades recall on low-trust sources for containment of their framing. `0.0` = off. |
| `integrity.live_reevaluation` | `false` | B4': search/shared_search re-check each candidate's trust against its current parents (ancestor quarantine/rollback propagates, and so does grant revocation: a parent behind a revoked grant counts 0, see ADR-029 edge cases; radii only shrink). |
| `workers.runner` | `inline` | `inline` \| `dbos` `[dbos]` \| `taskiq` `[taskiq]` (D-16). |
| `workers.broker_url` | `redis://localhost:6379/0` | taskiq broker endpoint (ignored by other runners). |
| `workers.dbos_system_database_url` | `null` | DBOS system db; `null` derives a SQLite file beside `storage.path`. |
| `retention.classes` | `[]` | #48: retention classes, checked in order, first match wins: `{namespace: <glob>, memory_type: <type or null>, ttl_days: <days>}`. A sleep-cycle stage (`retention_expire`, run first, only when this list is non-empty) hard-forgets records whose `recorded_at` is older than the TTL through the ordinary forget path (cascading to derived records). Records whose type's `retention` policy refuses deletion (legal hold, `regulated` PII) are kept, and so are records with such a descendant. Empty: nothing expires and the sleep cycle is unchanged. `Engine.expire_retention(now=None)` runs it on demand. |
| `audit.reads` | `false` | #49: every `search` / `assemble` / `read` / `retrieve` / `shared_search` / `export` appends one `memory.read_audit` event: principal (`event.actor`, from the REST auth binding or `principal_scope`, else `anonymous`), namespace, returned record ids, purpose and time. A verb that calls another public read verb audits once. Hash-chained (see `audit.actions`). No projector reads it. |
| `audit.actions` | `false` | #49: `forget` (with `actor=` / `reason=`), `correct`, retention expiry and `export` append a `memory.audit` event with the actor, principal, reason and record ids. Both audit kinds share one SHA-256 hash chain (`payload.chain.prev` / `.hash`); `Engine.verify_audit_chain()` / `audit_chain_ok()` validate it (a rolling log anchors at its oldest surviving audit event). |
| `consent.enforce` | `false` | #50 purpose limitation: records carry purposes (`write(..., purposes=[...])`, stored as `consent_tags`; `*` = any purpose) and reads pass `purpose=`. On, a read returns only records whose purposes include the read's purpose; a read without a purpose sees only untagged and `*` records. Applied in the search gates and to every returned list or assembled context. |
| `consent.untagged` | `allow` | #50: with `enforce`, whether records with no purpose are visible to every read (`allow`) or to none (`deny`). |
| `consent.remote_llm_max_tier` | `null` | #50 remote-LLM gate: `none` \| `low` \| `high` \| `regulated`. Every LLM role bound to a remote provider is wrapped so that, before each call, the text of any record whose `pii_tier` is above this tier (content, archived versions, the 400-char relevance-note prefix and their JSON-escaped forms) is replaced by `[WITHHELD: above the remote-LLM PII tier]`. Local = `llamacpp/…`, an `ollama/…` model without `api_base`, or an `api_base` on `localhost`/`127.0.0.1`/`::1`/`*.local`/`consent.local_hosts`. Textual gate: a paraphrase of the content is not caught. `null`: off. |
| `consent.local_hosts` | `[]` | #50: extra `api_base` host names that count as local for the remote-LLM gate. |
| `rest.auth.mode` | `none` | #51 reference auth middleware (ADR-041; not a production auth plane): `none` (unauthenticated, v0.1) \| `api_key` \| `oidc_jwt` (needs `pyjwt`). Binds a principal and its namespaces to each request: another namespace gets 403; `/sleep`, `/rebuild`, `/export`, `/quarantine…` need the admin role. |
| `rest.auth.api_keys` | `[]` | #51 `api_key` mode: list of `{key_env: <ENV VAR>, principal, namespaces: [<glob>], admin: false}` (or `key:` instead of `key_env`). Sent as `Authorization: Bearer <key>` or `X-API-Key`. Only SHA-256 digests are kept; keys are never logged or echoed. |
| `rest.auth.jwt.issuer` | `null` | #51 `oidc_jwt`: required `iss` (null = not checked). |
| `rest.auth.jwt.audience` | `null` | #51 `oidc_jwt`: required `aud` (null = not checked). |
| `rest.auth.jwt.algorithms` | `["RS256"]` | #51 `oidc_jwt`: accepted signing algorithms. |
| `rest.auth.jwt.jwks_url` | `null` | #51 `oidc_jwt`: JWKS endpoint for the signing keys (PyJWT `PyJWKClient`). |
| `rest.auth.jwt.key_env` | `null` | #51 `oidc_jwt`: env var holding a PEM public key or HMAC secret (when no `jwks_url`). |
| `rest.auth.jwt.principal_claim` | `sub` | #51 `oidc_jwt`: claim naming the principal. |
| `rest.auth.jwt.namespaces_claim` | `memspine_namespaces` | #51 `oidc_jwt`: claim listing the allowed namespace globs (list or space-separated). |
| `rest.auth.jwt.roles_claim` | `roles` | #51 `oidc_jwt`: claim listing roles (list or space-separated). |
| `rest.auth.jwt.admin_role` | `memspine-admin` | #51 `oidc_jwt`: the role that unlocks the admin routes. |
| `rest.rate_limit` | `null` | #51: `{requests_per_second, burst: 10}` in-memory token bucket per principal (per client address without auth); over the limit → 429. One process only. |
| `workers.sleep_interval_seconds` | `null` | D1: when set (seconds), the engine runs the full sleep cycle on that interval autonomously; `null` keeps v0.1 behavior (cycle runs only on `Engine.sleep()`). |
| `prompts.overrides` | `{}` | Per-prompt overrides (body/system/format/version/output_model/token_budget) (D-43). |
| `prompts.partials` | `{}` | Override fragments for shared Jinja `{% include %}` partials (anti-injection block, output footer); `<name>` → replacement text, consulted before the shipped `_partials/` dir (B1). |
| `prompts.selection` | `{}` | Per-role default scenario selectors: `<role>` → map of optional `memory_type`/`condition`, merged into every `select(role)` query so a deployment can pin a prompt variant without code (B2). Shipped `chat` conditions: `dated` (H12, `chat@dated`), `dated2` (G12, `chat@dated2`: `chat@dated` plus "a line's leading date is when it was said, `[= …]` is when the event happened; answer *when* questions with the happened date"), `infer` (G10, `chat@infer`), `dated3` (#34, `chat@dated3`: brief reasoning then a final `Answer:` line, which `Engine.final_answer()` extracts; quote the specific detail; dates in the granularity asked; "a date in brackets is when it was said; the event may be earlier"; merge repeated mentions before counting; "Not mentioned" only when nothing bears on the question). `plan` condition `v2` is selected by `read.planner_version`; `extract` conditions `session3` / `dates` by the consolidation options. |
| `memories.*.enabled` | `false` | Enable a memory type (`working`/`episodic`/`semantic`/…); C1b auto-enables prerequisites. |
| `memories.*.policies` | `{}` | Per-type policy overrides (conflict/dedup/trust/entity_extraction/page_size/…). `semantic.policies.extract_graph` (`{max_rounds, min_confidence}`) opts into C2 graphiti-style writes: with an `extract_edges` LLM role, the background `extract_graph` sleep stage writes edge facts + `asserted` links. `semantic.policies.write_pipeline: graph` opts into the C3 synchronous variant — edges extracted at write time and written through the M4/M5 ladder (ADR-026). |
| `namespaces.*.policies` | `{}` | Per-namespace policy overrides (D-14). |
<!-- CONFIG-KEYS-TABLE:END -->

Secrets backend selection is **not** a config key: `MEMSPINE_SECRETS_BACKEND`
(`env` default \| `aws`) is an environment variable, because secrets resolve before
`MemspineConfig` is built (ADR-023).

### Policy options (nested under the dict-valued keys)

The dict-valued keys above take the options of a bindable policy
(`src/memspine/core/policies/`). These are the opt-in read and write options that the
table above does not list one by one. All are off by default, so `profile="simple"` is
unchanged; `tests/unit/test_simple_profile_golden.py` pins their defaults.

| Option path | Default | Notes |
|-----|---------|-------|
| `memories.semantic.policies.conflict.contest_ties` | `false` | H9: when neither event time nor trust decides between two values of one fact key, keep both (verdict CONTEST). The contender is tagged `disputed`; the current fact stays the single active one. |
| `memories.semantic.policies.conflict.contest_window_seconds` | `0.0` | H9: two event times at most this many seconds apart count as a tie. `0.0` = only identical event times. |
| `memories.semantic.policies.conflict.contest_trust_margin` | `0.05` | H9: two trusts at most this far apart count as a tie. |
| `memories.semantic.policies.write.reflexion` | `true` | #32 (GR-16): run the extra edge-extraction rounds (`extract_graph.max_rounds` > 1) of the C2/C3 graph write paths. `false` makes every extraction a single call (one call per source fewer for each extra round), the ablation Graphiti ran before removing reflexion. With the default `max_rounds: 1` there is no extra round, so nothing changes. |
| `memories.semantic.policies.conflict.contest_lower_trust` | `false` | A same-key write less trusted than the current fact (but within `trust_margin`) CONTESTs it instead of superseding or retracting it, so a lower-trust source cannot archive a higher-trust fact (ADR-029, supersession). |
| `read.assembly.latest_slots` | `0` | H19: reserve up to this many of the most recent candidates (by event time) in the selection, so the latest evidence is not crowded out. `0` = off. |
| `read.assembly.dedupe_jaccard` | `1.0` | H23: drop a candidate whose word set overlaps an already selected one at or above this Jaccard ratio. `1.0` = off. |
| `memories.episodic.policies.consolidation.mine_facts` | `false` | C6′: a sleep stage mines dated atomic facts once per consolidated session through the write door. Needs an `extract` LLM role. |
| `memories.episodic.policies.consolidation.mine_by_topic` | `false` | H15: with `mine_facts`, mine each topic segment of a session in its own call (lexical-cohesion boundaries, no model), so the miner reads one topic at a time. A fact's parents are its segment's turns (the call saw nothing else). One call per segment. |
| `memories.episodic.policies.consolidation.mine_prompt` | `session` | #27: the `extract` prompt condition the miner selects. `session3` = `extract@session3` (a worked relative-date example, a complete-coverage rule for list items and countable events, evidence-line citations, and a 4096-token output cap sent as `max_tokens`). |
| `memories.episodic.policies.consolidation.mine_evidence_turns` | `false` | #29: number the miner's transcript lines (`[n] [YYYY-MM-DD] …`); a fact that cites valid lines in `turns` gets those turns as parents instead of the whole session (segment). |
| `memories.episodic.policies.consolidation.mine_event_dates` | `false` | #29: tag each mined fact `happened:<date>`: the H1 resolution of a relative phrase in the fact or its cited turns (each against its own date; `read.relative_week` applies), else the miner's `date`. A rule-resolved date also becomes the fact's `valid_from`. |
| `memories.episodic.policies.consolidation.mine_event_dates_llm` | `false` | #29: with `mine_event_dates`, one batched `extract@dates` call per mined batch dates the facts still undated. Needs the `extract` role. |
| `memories.episodic.policies.consolidation.mine_multiview` | `false` | #28: store each mined fact's multi-view fields as tags, `person:<name>`, `loc:<place>` and `topic:<class>` (NFKC, case-folded, whitespace collapsed), so read legs can prefilter; the statement is stored as mined. The fields pass the #31 guards (placeholders and reasoning dropped, at most 8 persons, 80 chars each). With the default `mine_prompt` the miner uses `extract@session4`, which asks for them; `mine_prompt: session4` selects that prompt without the tags. |
| `memories.episodic.policies.consolidation.list_cards` | `false` | #30: after `mine_facts`, keep one person-level list card per (person, class) of live event facts, e.g. `Melanie — activities: pottery class (2023-05), camping (2023-07)`. Person = `person:` tags, else the entity; class = `topic:` tag, else a non-generic miner attribute, else one `extract@classes` call per person batch (cached in the log). Groups of at least 2 facts; at most 25 items, newest kept. Parents = the facts (a hard forget cascades), trust capped at the least trusted fact; re-derived (old card archived) only when its members or text change. Cards are `atomic_fact` records, so `read.cards: header` shows them whole within `cards_budget_share`. |
| `memories.episodic.policies.consolidation.anticipate` | `false` | H8: a sleep stage asks the `anticipate` role (falls back to `extract`) once per session for likely future questions and stores them as cues via `add_cues`. |
| `memories.associative.policies.community.algorithm` | `auto` | KB-12 (ADR-043): `auto` and `leiden` run graspologic-native Leiden (canonical edge order, seeded, warm-started from the previous partition) then LPA refinement, and stay a no-op without the `[community]` extra; `lpa` runs the built-in label propagation without the extra, with a collapse guard (largest community > 50% of >= 100 nodes keeps the previous partition and logs a warning). |
| `memories.associative.policies.community.refine_passes` | `10` | LPA passes that refine a Leiden result (stops early when nothing moves). `0` = pure Leiden. |
| `memories.associative.policies.community.incremental` | `false` | KB-12/#84: per sleep, new nodes take their neighbours' majority community and at most `incremental_passes` LPA passes run over the touched nodes; a warm full rebuild runs on the refresh triggers. State is kept as `community_partition` MARKER events. |
| `memories.associative.policies.community.incremental_passes` | `3` | LPA passes per incremental sleep. |
| `memories.associative.policies.community.refresh_fraction` | `0.1` | With `incremental`: a full rebuild runs once incrementally placed nodes exceed this share of the graph. |
| `memories.associative.policies.community.refresh_every` | `5` | With `incremental`: a full rebuild runs at least every this many sleeps. |
| `memories.associative.policies.community.summary_keep_jaccard` | `1.0` | #84: a community whose membership Jaccard against the member set its summary was written from is at least this keeps that summary (its membership links follow the community) instead of a rewrite, unless a newcomer is less trusted than the summary. `1.0` = off; `0.8` is the KB-12 recommendation. |
| `memories.episodic.policies.consolidation.reflect_profile` | `false` | H14: a sleep stage asks the `reflect` role (generic `reflect.yaml` prompt) once per session for profile insights, stored through `Engine.reflect`. Needs reflective memory enabled. |
| `memories.associative.policies.entity_nodes` | `false` | GP-2 (#13): the graph projector adds an `ent:<namespace>:<canonical>` node per entity a record names (its `entity` field and `dst:` tags; canonical = NFKC, casefolded, whitespace collapsed) and a `mentions` edge record -> entity weighted by the record's trust. `true` uses the default blocklist (pronouns, day words, "luck"); a map takes `blocklist` (replaces it) and `allowed` (only these names). Rebuild == incremental; forgetting a record removes its mentions and any entity left without one. `mentions` is a reserved rel. Change it, then `engine.rebuild()`. |

---

## Where to go next

- [`FEATURES.md`](./FEATURES.md) — the feature catalog (types, firewall, E2–E9).
- [`examples/01_quickstart.py`](../examples/01_quickstart.py) → `04_prospective_shared_rest.py`.
- [`memspine-structure-plan.md`](./memspine-structure-plan.md) — the authoritative blueprint.
- [`adr/`](./adr/) — architecture decision records (ADR-001 … ADR-031); the newest cover
  the multi-call write pipeline (ADR-026), record group tags (ADR-027), Leiden community
  detection (ADR-028, amended by ADR-043), the trust-horizon invariant (ADR-029, proposed), relevance-first
  scoring (ADR-030, proposed), and the decision port with call accounting (ADR-031,
  proposed).
