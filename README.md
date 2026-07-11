<div align="center">

# 🧠 memspine

### The memory spine for AI agents

*One clean API · composable stores · real learning dynamics*

<br/>

[![status](https://img.shields.io/badge/status-pre--alpha-orange?style=flat-square)](#-roadmap)
[![phase](https://img.shields.io/badge/phase-P0–P7_·_v0.2-7c3aed?style=flat-square)](#-roadmap)
[![tests](https://img.shields.io/badge/tests-773_collected-2ea44f?style=flat-square)](#-roadmap)
[![python](https://img.shields.io/badge/python-3.13+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-Apache--2.0-2ea44f?style=flat-square)](./LICENSE)
[![built with uv](https://img.shields.io/badge/built%20with-uv-DE5FE9?style=flat-square)](https://github.com/astral-sh/uv)
[![lint: ruff](https://img.shields.io/badge/lint-ruff-000000?style=flat-square&logo=ruff&logoColor=white)](https://github.com/astral-sh/ruff)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square)](./CONTRIBUTING.md)

<br/>

**memspine is the memory engine where every store — vector, graph, cache, SQL storage, LLM, embedding, and secrets — is swappable by config alone, over an event-sourced, rebuildable core.** It turns raw agent interactions into a **structured, typed, self-maintaining memory** — a real write pipeline, hybrid + graph retrieval, the Memory Firewall, and background learning dynamics — behind a small, stable API. It's the *engine*, not a product: the backbone something else plugs into.

[**Quickstart**](#-quickstart) · [**Features**](#-features) · [**Install**](#-install--extras) · [**How to use**](#-how-to-use) · [**Architecture**](#-architecture) · [**Roadmap**](#-roadmap)

</div>

---

> [!NOTE]
> **Status: pre-alpha, under active construction.** Phases **P0–P7 are implemented and review-passed** — event-sourced substrate, working memory + retrieval, semantic memory + prompts, episodic + lifecycle, the Memory Firewall, procedural + reflective, associative graph, and prospective + shared + REST — plus the **v0.2 enhancement program** (hybrid-retrieval default-on, Tantivy-core lexical, graphiti-style graph extraction, an autonomous learning scheduler, `group_id`/`tags` sub-scoping, configurable graph traversal, and deeper compression). **773 collected tests**, `ruff` + `mypy --strict` clean, **28 ADRs**, decision register through D-54. The blueprint in [`docs/`](./docs) is the single source of truth; see [`docs/FEATURES.md`](./docs/FEATURES.md), [`docs/USAGE.md`](./docs/USAGE.md), and the ecosystem comparison ([`ECOSYSTEM_COMPARISON.md`](./docs/ECOSYSTEM_COMPARISON.md) · [`ARCHITECTURE_FLOWS.md`](./docs/ARCHITECTURE_FLOWS.md)). Some capabilities need extras (below) and a few production swap-ins are reserved — see the [honest caveats](#-honest-caveats).

---

## ✨ Why memspine?

Most "memory" libraries are a thin `add / search` wrapper over a vector store. memspine is a **memory *system***.

<table>
<tr>
<th align="left">🗄️ A storage facade…</th>
<th align="left">🧠 …vs a memory system</th>
</tr>
<tr>
<td valign="top">

- dumps raw text into one backend
- one flavour of search, pick it up front
- flat strings + metadata
- no lifecycle — memories never change
- no provenance, no audit
- no defense against poisoned context

</td>
<td valign="top">

- **write pipeline**: firewall gate → dedup → classify → embed → link
- **9 typed memory types**, each with its own lifecycle
- **dynamics**: decay · reinforce · consolidate · forget
- **provenance + bitemporal facts** on every record
- **Memory Firewall**: trust, quarantine, poisoning defense
- **event-sourced** — one append-only log, rebuildable projectors

</td>
</tr>
</table>

---

## 🚀 Quickstart

```bash
pip install memspine          # slim core: a working brain, zero heavy deps
```

```python
import asyncio
from memspine import Engine


async def main() -> None:
    engine = Engine(
        template="base",
        storage={"path": "./quickstart.db"},
        embedding={"provider": "hash"},   # offline demo; omit for fastembed (ONNX, CPU)
    )
    await engine.start()

    await engine.write("Alice prefers her coffee black", namespace="agent/alice")
    await engine.write("Alice's timezone is Europe/Berlin", namespace="agent/alice")

    for record, score in await engine.search(
        "how does alice take her coffee?", namespace="agent/alice", top_k=2
    ):
        print(f"{score:0.3f}  {record.content}")

    await engine.stop()


asyncio.run(main())
```

The API is **async-first**; thin sync wrappers (`start_sync`, `write_sync`, `retrieve_sync`, `stop_sync`) exist for scripts that can't `await`. Runnable tours live in [`examples/`](./examples) — start with [`01_quickstart.py`](./examples/01_quickstart.py).

---

## 🧩 Features

### The nine memory types

Each type is **opt-in** (`memories.<type>.enabled: true`); the `simple` profile collapses to flat semantic memory. Templates (below) turn on sensible sets.

| Type | What it's for | Primary verbs |
|------|---------------|---------------|
| 🧾 **semantic** | durable facts about the world, with a conflict ladder + bitemporal validity | `write` · `search` · `assemble` |
| 💬 **working** | the bounded hot window of the current session; a pinned persona; overflow pages out to episodic | `write(memory_type="working")` · `set_persona` · `assemble` |
| 🕰️ **episodic** | the raw chronological event stream; derives session boundaries | `timeline` · `sessions` |
| 📚 **resource** | ingested documents — extract → chunk → firewall-gated resource records | `ingest` |
| 🔧 **procedural** | reusable skills + validated plans on a promotion ladder (draft → staged → verified → active) | `add_skill` · `promote_skill` · `skills` · `record_plan` · `recall_plan` |
| 🪞 **reflective** | higher-order notes derived from other records (depth-capped, no laundering) | `reflect` |
| 🔗 **associative** | a link graph over memories; recall by personalized PageRank | `associate` · `related` |
| ⏰ **prospective** | watches that fire at a due time or when a watched fact is invalidated | `watch` · `due` · `acknowledge_watch` |
| 👥 **shared** | cross-namespace grants + standing queries; foreign hits are trust-capped live views | `grant` · `shared_search` · `subscribe` |

### 🛡️ Memory Firewall (E1)

Every write of every type passes a deterministic gate before the door (OWASP **ASI06** — memory & context poisoning):

- **Trust scoring** by source class × channel — retrieved/web/tool/REST content is capped low (`0.3`) so it can never masquerade as operator input.
- **Quarantine** — low-trust or instruction-shaped writes are stored inert: no dedup, no conflict ladder, never retrievable.
- **Corroboration** — independent trusted writes can promote an honest quarantined record out of hold.
- **Anomaly detection** — embedding-outlier + MINJA progressive-injection heuristics.
- **Instruction-flag** — imperative-shaped content is wrapped ("treat as data") when it enters a context window.
- **Audit taint** — `audit_taint` walks the log for a record's blast radius; `forget --hard --verify` proves erasure.

### ⚡ Optimization program (E2–E9)

| | On by default | Opt-in (config / extra) |
|---|---|---|
| **E2** cache-friendly assembly | ✅ `assemble()` returns a `boundary_index` — stable prefix (persona/facts) before it, volatile content after, for provider prefix caching | |
| **E3** semantic + embedding cache | ✅ content-hash embedding cache; prompt-versioned extraction cache | |
| **E5** assembly-time compression | | `read.compression: {assembly: true}` + `memspine[compress]` |
| **E6** plan cache | ✅ when procedural is on: `record_plan` / `recall_plan` by task-embedding similarity | |
| **E7** sleep-time compute | reserved hook in the sleep cycle (no-op default) | |
| **E4** embedding quantization + static prefilter | | `vector.quantization` (LanceDB-native int8/binary rescore, ADR-020); `read.static_embedding_prefilter: true` + `memspine[static]` (model2vec pre-rerank gate) |
| **E8** retrieval-quality stages | | `read.rerank: fastembed`\|`flashrank`\|`litellm` (+ `rerank_model`); `read.static_prefilter: true` |
| **E9** token micro-opts | ✅ YAML/CoD prompt formats + always-on `json-repair` | |

> **Hybrid retrieval is on by default (v0.2):** every `search` fuses a lexical **BM25** leg — a standalone **Tantivy** index (core dependency, backend-independent) — into the vector ranking via reciprocal-rank fusion (D-25). Set `read.hybrid: false` to restore the bit-identical vector-only pipeline; server-scale deployments swap the lexical leg to OpenSearch/Elasticsearch with `memspine[opensearch]`.

### 🆕 New in v0.2

The engine now *learns on its own* and retrieves more sharply out of the box:

| | What changed | How to reach it |
|---|---|---|
| 🔎 **Hybrid by default** | vector + Tantivy BM25 fused via RRF on every backend, no extra | on by default · `read.hybrid: false` to opt out |
| 🌐 **Graphiti-style graph writes** | async `extract_graph` pipeline mines entities/edges into `LINK`/`WRITE` events; bitemporal edge invalidation | `memories.semantic.policies.extract_graph` (opt-in) |
| ⏱️ **Autonomous learning scheduler** | a background loop drives the full sleep cycle (consolidate → extract → reorganize → decay → prune) — dynamics run without an explicit `sleep()` | `workers.sleep_interval_seconds: 3600` |
| 💬 **Chat-transcript ingest** | `write_messages()` / `write_episode()` turn a message list into episodic records with session detection | core verbs |
| 🏷️ **Sub-scoping facets** | `group_id` + `tags` on every record, threaded through write / filter / REST (ADR-027) | `write(..., group_id=..., tags=[...])` |
| 🧭 **Configurable graph recall** | `related()` traversal strategy — `ppr` (PageRank) · `bfs` · `rrf`-blended | `related(..., strategy="ppr\|bfs\|rrf")` |
| 🗜️ **Deeper compression** | llmlingua-2 model, per-placement-band token budgets, entity `preserve`, and a zstd-vs-llmlingua decision rule | `memspine[compress]` |
| 🧠 **Reinforcement on read** | retrieving a record additively bumps its salience (`utility += step`) — used memories decay slower | on by default |
| 🧩 **Prompt partials + scenarios** | Jinja `_partials/` includes + `(role, scenario)` variant selection, fingerprinted into `prompt_version` | prompt pack |
| 🗃️ **Unified cache** | cashews backs `cache.backend = memory\|disk\|redis\|valkey` (LMDB retired) | `memspine[cache]` for disk/redis |

---

## 📦 Install / extras

The **core** install is slim (D-03) but already a full brain — SQLite event log, **LanceDB vector** (ADR-021), **Tantivy BM25** for default-on hybrid (D-25), the **LiteLLM** LLM/embedding/rerank gateway (ADR-024), fastembed embeddings, and the inline worker runner. Everything heavier is an extra:

```bash
pip install memspine                              # core: SQLite + LanceDB + Tantivy + LiteLLM + fastembed
pip install "memspine[kuzu]"                   # embedded-Cypher graph for associative memory
pip install "memspine[ingest]"                 # markitdown + chonkie — document ingest()
pip install "memspine[ner]"                    # gliner2 CPU entity extraction
pip install "memspine[structured]"             # instructor — schema-validated LLM output
pip install "memspine[compress]"               # llmlingua-2 — assembly compression
pip install "memspine[rerank]"                 # flashrank — E8 cross-encoder rerank
pip install "memspine[community]"              # leidenalg — Leiden graph community detection
pip install "memspine[cache]"                  # cashews — disk / redis / valkey cache backends
pip install "memspine[rest]"                   # FastAPI + uvicorn — REST protocol
pip install "memspine[opensearch]"             # server-scale lexical (OpenSearch / Elasticsearch)
pip install "memspine[dbos]" / "[taskiq]"      # durable / brokered background workers
pip install "memspine[llmlocal]"               # llama-cpp-python in-proc inference
```

| Extra | Unlocks |
|-------|---------|
| *(core)* | SQLite event log, **LanceDB vector**, **Tantivy BM25**, LiteLLM gateway, fastembed, inline runner (ADR-021/024/D-25) |
| `graph` | LadybugDB embedded graph adapter (D-26) |
| `kuzu` | embedded-Cypher graph store for associative memory (D-26) |
| `cache` | cashews-backed disk / redis / valkey cache (`cache.backend`, ADR-022) |
| `ingest` | multi-format document ingest + chunking (D-29) |
| `ner` | local CPU entity extraction (D-28) |
| `structured` | schema-validated LLM output via instructor (D-31) |
| `compress` | assembly-time context compression (llmlingua-2, E5/D-32) |
| `rerank` | E8 cross-encoder rerank alternative (flashrank) |
| `static` | E4 model2vec static-embedding prefilter (ADR-020) |
| `community` | Leiden graph community detection (leidenalg, D-40/ADR-028 — no numpy pin) |
| `opensearch` | server-scale lexical backend for hybrid (OpenSearch / Elasticsearch, D-25) |
| `dbos` / `taskiq` | durable / brokered worker runners (D-16) |
| `rest` | REST protocol (FastAPI, D-06) |
| `llmlocal` | in-proc open-weight inference (llama-cpp-python) |
| `postgres` | PostgreSQL storage backend (ADR-025) |
| `aws` | Bedrock LLM/embeddings + AWS Secrets Manager (ADR-023/024) |
| `promptopt` | prompt self-optimization hook (langmem, D-43) |
| `weaviate`, `neo4j` | reserved production swap-ins (raise if selected) |

> Both the **LiteLLM** gateway (cloud + local LLM/embedding/rerank, ADR-024) and the **Tantivy** BM25 leg (default-on hybrid, D-25) are **core** dependencies — no extra needed.

Convenience bundles: `local` (`graph,cache,dbos,ingest,ner`), `prod-aws`, `all`. Dev: `uv sync --all-extras`.

---

## 🎛️ Templates

Construct with `Engine(template="...")`. Each template is a partial overlay on the `base` policy pack — it enables memory types and tunes policies.

| Template | Enables | Tuned for |
|----------|---------|-----------|
| `base` | working + episodic + semantic | the `simple` working brain (every other template extends it) |
| `coding` | + procedural | coding agents with reusable skills; `conflict_bias: newest` |
| `personal` | + reflective + prospective | personal assistants (reflective auto-enables episodic) |
| `voice` | rolling+zstd event log; larger working window | high-volume voice transcripts |
| `multi_agent` | + shared; **DBOS** durable workers | namespace grants across agents (R2), server-grade durability |
| `regulated_financial` | full audit log, strict PII, no forgetting; **DBOS** durable workers | audited / compliant deployments |

> Server profiles (`multi_agent`, `regulated_financial`) pin `workers.runner: dbos` at the **template** layer (A4/ADR-005) — DBOS defaults to a colocated SQLite system database, so this needs only `memspine[dbos]`, no external infra. Embedded profiles stay `inline`.

---

## 🛠️ How to use

All snippets assume a started engine (`await engine.start()`), the hash embedder for offline demos, and the relevant memory type enabled. Full runnable versions are in [`examples/`](./examples).

**Semantic — write + search** *(hybrid vector + BM25 by default)*
```python
await engine.write("primary region is eu-west-1", namespace="ops",
                   entity="deploy", attribute="region",   # entity/attribute key the conflict ladder
                   group_id="infra", tags=["prod"])        # v0.2 sub-scoping facets
for record, score in await engine.search("where do we deploy?", namespace="ops", tags=["prod"]):
    print(score, record.content)
```

**Chat transcript → episodic records** *(v0.2)*
```python
await engine.write_messages(
    [{"role": "user", "content": "let's ship on friday"},
     {"role": "assistant", "content": "ok, I'll prep the release notes"}],
    namespace="agent/demo",
)   # one episodic record per turn; write_episode() stamps a shared session_id across turns
```

**Working memory — persona + cache-aware assembly**
```python
await engine.set_persona("agent/demo", "You are a concise coding assistant.")
await engine.write("turn 1: user likes type hints", namespace="agent/demo", memory_type="working")
ctx = await engine.assemble("what does the user like?", namespace="agent/demo", budget_tokens=500)
print(ctx.records, ctx.boundary_index)   # everything before boundary_index is the cacheable prefix
```

**Procedural — the skill ladder**
```python
skill = await engine.add_skill("run pytest -q then ruff check", name="verify", namespace="dev")
skill = await engine.promote_skill(skill.record_id, namespace="dev")                       # draft → staged
skill = await engine.promote_skill(skill.record_id, namespace="dev")                       # staged → verified
skill = await engine.promote_skill(skill.record_id, namespace="dev", dry_run_passed=True)  # → active
usable = await engine.skills(namespace="dev")   # only ACTIVE skills by default
```

**Reflective — derive from existing records**
```python
a = await engine.write("build failed on windows", namespace="dev", memory_type="episodic")
b = await engine.write("build failed on windows again", namespace="dev", memory_type="episodic")
note = await engine.reflect("windows builds are flaky", [a.record_id, b.record_id], namespace="dev")
```

**Resource — ingest a document** *(needs `memspine[ingest]`)*
```python
chunks = await engine.ingest("docs/runbook.md", namespace="ops")
```

**Associative — link + graph recall** *(needs a graph store)*
```python
await engine.associate(a.record_id, b.record_id, namespace="dev", rel="related")
neighbours = await engine.related(a.record_id, namespace="dev", k=10)   # personalized PageRank
```

**Prospective — watches**
```python
from datetime import UTC, datetime, timedelta
now = datetime.now(UTC)
w = await engine.watch("rotate the API key", due_at=now + timedelta(minutes=30), namespace="ops")
fired = await engine.due(namespace="ops", now=now + timedelta(hours=1))
await engine.acknowledge_watch(w.record_id, namespace="ops")
```

**Shared — grant + cross-namespace search**
```python
await engine.grant("analyst", namespace="ops", memory_types=["semantic"])
results = await engine.shared_search("primary region", namespace="analyst")   # foreign hits are trust-capped
await engine.revoke("analyst", namespace="ops")
```

**CLI** (`memspine ...`)
```bash
memspine config validate -t personal        # dependency closure + effective combination
memspine config resolve                      # merged config with a # source: per key
memspine prompts list                        # every prompt, version, source layer
memspine audit taint <record_id> --db ./memspine.db
memspine forget <record_id> --hard --verify  # provable erasure (exit 1 if not clean)
```

**REST** *(needs `memspine[rest]`)* — see [`docs/USAGE.md`](./docs/USAGE.md) for routes and the auth caveat.
```python
from memspine.protocols.rest import create_app
app = create_app(engine)     # you own engine.start()/stop(); serve with uvicorn
```

---

## 🏗️ Architecture

A **four-layer engine** over a pluggable store abstraction. The API surface stays tiny; the internals are a real pipeline. Everything is **event-sourced**: an append-only log is the source of truth, and vector / graph / FTS / cache are rebuildable projectors — `rebuild()` replays them from seq 0.

```mermaid
flowchart TB
    subgraph API["🎯 API facade — Engine"]
        A["write · search · assemble · forget · reflect · associate · watch · grant · sleep"]
    end
    subgraph COG["🧠 Cognitive layer"]
        W["**Write** &nbsp; firewall gate → dedup → classify → embed → link"]
        R["**Read** &nbsp; recall(vector · graph) → [rerank?] → score → MMR → assemble"]
        D["**Dynamics** &nbsp; decay · reinforce · consolidate · forget"]
    end
    subgraph STORE["🧩 Store layer — composable capabilities"]
        V["VectorStore"]
        DOC["Storage / event log"]
        G["GraphStore"]
        KV["KV cache"]
    end
    subgraph BK["🔌 Backends"]
        B["LanceDB · Tantivy · kuzu / sqlite_adjacency · SQLite · cashews"]
    end
    API --> COG --> STORE --> BK

    classDef api fill:#4f46e5,stroke:#4f46e5,color:#fff;
    classDef cog fill:#0ea5e9,stroke:#0ea5e9,color:#fff;
    classDef store fill:#10b981,stroke:#10b981,color:#fff;
    classDef bk fill:#334155,stroke:#334155,color:#fff;
    class A api;
    class W,R,D cog;
    class V,DOC,G,KV store;
    class B bk;
```

---

## 🔌 Defaults & swap-ins

Every capability is a port. The engine plans against capabilities, degrades gracefully, and hard-fails with a clear *"install `memspine[…]`"* when a required service is missing (D-10).

| Capability | 🟢 Default *(embedded, offline)* | 🚀 Swap-in *(config alone)* |
|------------|----------------------------------|-----------|
| **Vector** | **LanceDB** *(core, ADR-021)* | Weaviate *(reserved)* |
| **Graph** | **`sqlite_adjacency`** *(zero-dep)* · **kuzu** with `[kuzu]` · **ladybug** with `[graph]` | Neo4j *(reserved)* |
| **Cache / KV** | in-process KV *(core)* · cashews **disk** with `[cache]` | Redis / Valkey via `cache.backend` + `[cache]` *(ADR-022)* |
| **Relational / event log** | **SQLite** (SQLAlchemy Core + Alembic) | **PostgreSQL** `[postgres]` *(ADR-025)* |
| **Embeddings** | **fastembed** (ONNX, CPU) · `hash` for tests · model2vec `[static]` | **LiteLLM** cloud (OpenAI / Bedrock / …) via `embedding.provider: litellm` *(ADR-024)* |
| **LLM** | **local**: Ollama · vLLM · llama.cpp `[llmlocal]` · LM Studio · any OpenAI-compatible | **LiteLLM** prefix routing: `openai/` · `bedrock/` · `vertex_ai/` · `azure/` *(ADR-024)* |
| **Lexical** | **Tantivy BM25** *(core, hybrid on by default)* | **OpenSearch / Elasticsearch** `[opensearch]` |
| **Secrets** | env / `.env` *(core, ADR-023)* | **AWS Secrets Manager** `[aws]` via `MEMSPINE_SECRETS_BACKEND=aws` |
| **Workers** | **inline** | DBOS `[dbos]` · taskiq `[taskiq]` |

---

## ⚠️ Honest caveats

memspine is **pre-alpha**. What's shipped vs. deferred, so you're not surprised:

- **Retrieval is hybrid *by default* (v0.2).** `search` runs `[static_prefilter?] → vector + Tantivy BM25 fused by RRF → [rerank?] → score`. Set `read.hybrid: false` for the bit-identical vector-only pipeline. The embedded Tantivy index is **single-writer per directory**, so two live Engine instances can't share one file-backed lexical index — use one engine per process or the `[opensearch]` provider.
- **Graph default is `sqlite_adjacency`.** `sqlite_adjacency` (zero-dep) is the shipped default; `ladybug` (`[graph]`) and `kuzu` (`[kuzu]`) are working embedded-Cypher opt-ins; Neo4j is a reserved stub. Graphiti-style `extract_graph` is **opt-in** (`memories.semantic.policies.extract_graph`).
- **The autonomous scheduler is off by default and skipped on `:memory:`.** Set `workers.sleep_interval_seconds` to drive dynamics on a file-backed DB; production autonomous scheduling should pair with a durable runner (`[dbos]`/`[taskiq]`).
- **Some backends are still reserved stubs.** `weaviate` vector, `neo4j` graph, and the `opensearch` lexical provider raise a clear error if selected; everything else in the swap-in table is wired.
- **Extras gate features.** `ingest`, `compress`, `rerank`, `static`, `community`, `cache` (disk/redis/valkey), `postgres`, `opensearch`, `aws`, and durable/brokered workers each need their extra installed.
- **REST has no authentication in v0.1.** The namespace comes from the `X-Memspine-Namespace` header verbatim — binding caller → namespace is the deployer's job (ADR-017/018). REST writes are forced onto the low-trust `rest` channel.
- **Sync wrappers cover only** `start`/`write`/`retrieve`/`stop`; everything else is async.

---

## 🤖 Working with Claude Code

Building on memspine with Claude Code? [`CLAUDE.md`](./CLAUDE.md) + `.claude/` ship the project context, slash commands (`/phase`, `/decision`, `/scaffold`, `/check`) and subagents.

---

## 🗺️ Roadmap

Every phase ships independently and keeps `profile="simple"` behavior stable. Every landed phase went through a multi-agent review pass.

```mermaid
flowchart LR
    P0["✅ P0<br/>Substrate"] --> P1["✅ P1<br/>Working +<br/>retrieval"] --> P2["✅ P2<br/>Semantic +<br/>prompts"] --> P3["✅ P3<br/>Episodic +<br/>lifecycle"] --> P4["✅ P4<br/>Firewall"] --> P5["✅ P5<br/>Procedural +<br/>reflective"] --> P6["✅ P6<br/>Associative<br/>graph"] --> P7["✅ P7<br/>Prospective +<br/>shared + REST"]
    classDef done fill:#065f46,stroke:#10b981,color:#ecfdf5,rx:6,ry:6;
    class P0,P1,P2,P3,P4,P5,P6,P7 done;
```

- [x] **P0** substrate — event log · records · policies · SQLite
- [x] **P1** working memory + retrieval
- [x] **P2** semantic + write pipeline + prompts
- [x] **P3** episodic + lifecycle dynamics
- [x] **P4** governance + Memory Firewall
- [x] **P5** procedural + reflective
- [x] **P6** associative graph
- [x] **P7** prospective + shared + REST

**Since P7 (v0.1 tail + v0.2 program, all landed):** the LiteLLM LLM/embedding/rerank gateway (ADR-024), cashews cache backends (ADR-022), AWS secrets (ADR-023), PostgreSQL storage (ADR-025) — then the **v0.2 enhancement program**: hybrid retrieval **default-on** with Tantivy-core BM25 (D-25), graphiti-style `extract_graph` writes (ADR-026), the autonomous learning scheduler (ADR-005), `group_id`/`tags` sub-scoping (ADR-027), configurable graph traversal (E1), and deeper llmlingua-2 compression (E5). **Next:** live-backend contract verification (needs Postgres/Redis infra), then REST auth and WS/gRPC/MCP protocol adapters (v2) — see the [structure plan](./docs/memspine-structure-plan.md).

---

## 🤝 Contributing

Contributions welcome — start with [`CONTRIBUTING.md`](./CONTRIBUTING.md) and the golden rules in [`CLAUDE.md`](./CLAUDE.md).

```bash
uv sync --all-extras
just check        # ruff + mypy + pytest
```

<div align="center">

---

**Built as an engine, not a black box.** 🧠

Apache-2.0 · [techiewonk/memspine](https://github.com/techiewonk/memspine)

</div>
