# memspine — usage guide

How to install, construct an `Engine`, and drive every memory type with the real
API. All snippets are grounded in `src/memspine/engine.py`,
`src/memspine/cli.py`, and `src/memspine/protocols/rest/`. Runnable end-to-end
tours are in [`examples/`](../examples).

For *what* each feature is, see [`FEATURES.md`](./FEATURES.md).

---

## Install

The **core** install is slim — SQLite storage, fastembed embeddings, a zero-dep
SQLite vector store, inline workers. Add capabilities as extras.

```bash
# Users:
pip install memspine
pip install "memspine[lance,kuzu,ingest,rest]"   # pick the extras you need

# Developers (all extras + test/lint/docs tooling):
uv sync --all-extras
just check        # ruff + mypy --strict + pytest
```

Common extras: `lance` (LanceDB vector), `kuzu` (graph), `ingest`
(markitdown+chonkie), `ner` (gliner2), `structured` (instructor), `compress`
(llmlingua, E5), `rerank` (flashrank, E8), `community` (graspologic), `rest`
(FastAPI), `dbos`/`taskiq` (durable/brokered workers). See the
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
    storage={"path": "./memspine.db"},          # ":memory:" for ephemeral
    embedding={"provider": "hash"},             # deterministic, offline; default is "fastembed"
    vector={"backend": "auto"},                 # auto | lance | sqlite
    memories={
        "semantic": {"enabled": True, "policies": {"entity_extraction": "off"}},
        "prospective": {"enabled": True},
    },
    read={"rerank": "off", "static_prefilter": False},
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
| `voice` | rolling+zstd event log; larger working window |
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

## Where to go next

- [`FEATURES.md`](./FEATURES.md) — the feature catalog (types, firewall, E2–E9).
- [`examples/01_quickstart.py`](../examples/01_quickstart.py) → `04_prospective_shared_rest.py`.
- [`memspine-structure-plan.md`](./memspine-structure-plan.md) — the authoritative blueprint.
- [`adr/`](./adr/) — architecture decision records (ADR-001 … ADR-018).
