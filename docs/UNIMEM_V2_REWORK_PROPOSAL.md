# Drastic Rework Proposal: unimem v2 — from storage facade to cognitive memory engine

**Status:** Draft v3 for review · **Standard:** brought up to the `memspine-structure-plan.md` bar (locked decision register, code-level evidence, tier tags, phase mapping).
**Positioning:** this document is the **architecture rationale**; `memspine-structure-plan.md` is the **buildable blueprint**. unimem v2 is the design case that feeds memspine — decisions here are numbered to slot into that plan's register (D-01…D-25 there; **D-26…D-35 introduced here**, backed by `DEPENDENCY_ANALYSIS.md`).
**Last re-verified:** 2026-07-07 — every code claim re-checked against current repo code; adoption decisions come from a code-level dependency scan of all repos in `D:\mem`.
**Tier legend:** **QW** quick win · **DF** differentiator · **RG** research-grade (hook only).

---

## 0. TL;DR

unimem v1 is a **clean multi-backend CRUD facade** (~2,020 LOC, 45 tests): `add / search / get / get_all / update / delete / delete_all` over five interchangeable stores, plus optional recency decay, LLM fact-extraction (`infer`), an audit callback, and LangChain glue. It is essentially "mem0 with pluggable backends" — correct and legible, but a **storage abstraction, not a memory system**: no real write pipeline, no memory taxonomy, no graph reasoning wired into retrieval, no learning dynamics.

The **drastic change:** a **layered cognitive memory engine** that keeps what v1 got right (one clean API, pluggable backends, LangChain-first) and adds what it lacks (a real write pipeline, a memory-type model, hybrid + graph retrieval with reranking, and learning dynamics), everything gated by **profiles** so the simple mem0-style mode still exists. Concretely this converges on the `memspine` engine; this doc supplies the evidence and the decisions.

**What changed in v3 (this pass):**

1. **Decision register added (D-26…D-35)** — the code-level adoption calls, locked with the user, in memspine style (§1).
2. **Code-level feature adoption** — specific libraries pulled from the ecosystem into named modules: `datasketch` (dedup), `gliner2` (local NER), `chonkie`+`markitdown` (ingest), `instructor`+`json-repair` (structured output), `zstandard` (cold compression). Evidence base: `DEPENDENCY_ANALYSIS.md` (§7).
3. **Graph store decision locked:** stay with the **LadybugDB** default; `kuzu` becomes a first-class *alternative* adapter (it is what graphiti and cognee both ship), not the default.
4. **File-native profile dropped** (decision D-30 = skip): unimem v2 stays vector/graph/SQLite-first; the ReMe/EverOS Markdown direction is noted as a trend but explicitly out of scope.
5. **Verification corrections retained** (OpenMemory rewrite is on an unmerged branch; reinforcement boost is 0.1 not 0.15; unimem's Neo4j backend is dead code; Redis drops partial-scope queries) — all still in §3/§4.

---

## 1. Locked decision register (D-26…D-35) **[new — memspine standard]**

Continues the numbering of `memspine-structure-plan.md` (which locks D-01…D-25). These ten come from the 2026-07-07 code-level scan; evidence per row is in `DEPENDENCY_ANALYSIS.md §3/§5`.

| # | Decision | Locked value | Tier | Slot |
|---|----------|--------------|------|------|
| **D-26** | Embedded graph store | **LadybugDB default** (pinned fork); **kuzu** first-class alt adapter; `sqlite_adjacency` zero-dep fallback | DF | `services/graph/` |
| **D-27** | Dedup engine | **datasketch MinHash-LSH** stage-1 candidate gen → embedding-cosine stage-2 confirm | QW | `policies/dedup.py` (M5) |
| **D-28** | Local entity extraction | **gliner2** CPU NER behind a config flag; LLM extraction is the default fallback | DF | `memories/semantic/entities.py` (M13.3) |
| **D-29** | Multi-format ingest / chunking | **markitdown** (doc→text) + **chonkie** (chunking) under a `[ingest]` extra | QW | write-path `extract→chunk` (P1) |
| **D-30** | File-native profile | **Skipped for v0.1** — not a goal; vector/graph/SQLite-first. Interface not reserved. | — | (out of scope) |
| **D-31** | Structured LLM output | **instructor** for extract/judge roles; **json-repair** always-on safety net | QW | `services/llm` (E9) |
| **D-32** | Cold-tier compression | **zstandard** for dormant/cold memory compression + fingerprinting | QW | `policies/compression.py` (M6) |
| **D-33** | LLM gateway | Keep `services/llm` per-role providers (D-07/D-22); **litellm only as an optional adapter**, never core | — | `services/llm/` |
| **D-34** | Multilingual lexical | **jieba/rjieba config-gated**, off by default | — | `services/lexical/` |
| **D-35** | Eval assertions | **deepeval** in `evals/` only, not shipped in the wheel (respects D-19) | 🧪 | `evals/` |

Anti-decisions (recorded for traceability): **litellm as core LLM layer — rejected** (hides per-role provider control); **sqlmodel for storage — rejected** (blurs the event-log boundary, D0.1); **celery/apscheduler workers — rejected** (D-16 already picks inline/DBOS/taskiq); **baml for structured output — rejected** (heavy DSL vs instructor).

---

## 2. Current state — unimem v1 (from code) **[re-verified 2026-07-07]**

| Layer | What exists today | File |
|-------|-------------------|------|
| API facade | `Memory.add/search/get/get_all/update/delete/delete_all`; scoping via `user_id/agent_id/run_id`; `search` returns `{results:[{id, content, score, metadata}]}` (only `add` renames the field to `memory`) | `unimem/unimem/memory.py` |
| Backends | `MemoryBackend` ABC + `InMemory`, `Redis`, `Postgres` (ILIKE or pgvector), `Weaviate` (near_vector or substring), `Neo4j` (one node/memory, substring) | `unimem/unimem/backends/*` |
| Scoring | `apply_recency_decay` = `exp(-ln2·elapsed/halflife)`; `apply_combined_score` = α·sim + β·recency + γ·importance (defaults 0.5/0.3/0.2) | `unimem/unimem/scoring.py` |
| Infer | Optional LLM extractor: `add(infer=True)` → split into facts → store each | `unimem/unimem/infer.py` |
| Audit | `audit_callback(event, id, **details)` on add/update/delete/delete_all (not search/get) | `unimem/unimem/audit.py` |
| LangChain | Retriever, chat message history, add/search tools | `unimem/unimem/integrations/*` |

**Six structural limits — all CONFIRMED against current code:**

1. **No unified write pipeline.** `add` (`memory.py:38-102`) concatenates messages, stamps `_ts`, stores raw; `infer=True` stores each fact independently. No dedup, classification, linking, consolidation.
2. **Retrieval is whatever the backend does.** One `backend.search`; vector k-NN *or* substring, never both. Re-weighting only *after* the backend returns; **substring backends return `score=1.0` for every hit — nothing real to re-rank.**
3. **The graph backend isn't a graph — and is broken.** Neo4j stores one isolated node per memory with substring search, and has a hard **`IndentationError` at `neo4j_backend.py:81-82`** → the module fails to import. Dead code.
4. **No memory model.** Item = `{id, content:str, metadata:dict}`; `importance` is just a metadata key.
5. **No dynamics.** Decay is opt-in read-time re-ranking only; no reinforcement, no forgetting, no consolidation.
6. **Backends siloed.** Single `backend`; can't compose vector+graph+kv. **Redis bakes scope into a composite key at write time → partial-scope queries silently miss memories.**

---

## 3. Reference engine — CaviraOSS OpenMemory (condensed, from code) **[re-verified 2026-07-07]**

> **Branch caveat.** The README's "being rewritten" banner points to an **unmerged `rewrite` branch**. Checked-out `main` (commit `9af0f95`, 2026-06-27) is the same engine reviewed in Feb — treat OpenMemory as a **design reference, not a dependency**.

- **Write** (`memory/hsg.py:add_hsg_memory`, 389-478): extract → chunk (`ops/ingest.py`) → **simhash dedup** (`hamming ≤ 3` → salience **+0.15**, line 395) → **regex classify** into sectors → **multi-sector embed** → mean vector → **single waypoint** → user-summary update.
- **Sectors** (`core/constants.py:10-78`, exact λ/weight): episodic (0.015/1.2), semantic (0.005/1.0), procedural (0.008/1.1), emotional (0.02/1.3), reflective (0.001/0.8).
- **Retrieval** (`hsg_query`): hybrid score (`SCORING_WEIGHTS`, 28-34) = **0.35·sim + 0.20·overlap + 0.15·waypoint + 0.10·recency + 0.20·tag** (sigmoid-wrapped) → low-confidence **waypoint BFS** (`expand_via_waypoints`, avg_top < 0.55) → **reinforce** on recall (salience **+0.1** — corrected; the +0.15 is the write-path dedup boost) → 60s cache.
- **Temporal graph** (`temporal_graph/*`): bitemporal SPO facts + typed edges — **still NOT wired into `hsg_query`** (zero imports; reachable only via `ai/mcp.py`). The biggest missed opportunity, unchanged.
- **Storage**: SQLite default; Postgres/pgvector + Valkey/Redis selectable via `OPENMEMORY_VECTOR_STORE`.

**Take / leave**

| Take | Leave / improve |
|------|-----------------|
| Sector taxonomy as an explicit model | Regex classify → embedding/NER classifier (D-28) |
| Salience decay + reinforcement-on-recall | Single-waypoint graph → k-links + real relations |
| Hybrid **explainable** scoring (return *why*) | Temporal graph decoupled → wire it in; use **graphiti's** bitemporal model, not OpenMemory's unwired one |
| Ingestion pipeline (chunk, dedup) | Node/SQLite coupling → stay backend-agnostic (D-24/M14) |
| Bitemporal fact model | Parameter sprawl → centralize in config/profiles (D-11) |

---

## 4. Proposed v2 architecture

A **four-layer engine** over the backend abstraction; surface API unchanged, internals become a real pipeline. This is the architecture `memspine` builds.

```
        ┌────────────────────────────────────────────────────────┐
        │  API facade  (Memory/Engine: add/search/get/update...)  │  ← unchanged surface
        ├────────────────────────────────────────────────────────┤
        │  COGNITIVE LAYER                                         │
        │   Write:  extract → chunk → dedup → classify → embed     │
        │           → link → (consolidate)                        │
        │   Read:   rewrite → recall(vector|lexical|graph) → fuse  │
        │           → rerank → decay/salience → explain            │
        │   Dynamics: decay · reinforce · consolidate · forget     │
        ├────────────────────────────────────────────────────────┤
        │  STORE LAYER (composable capabilities, not one-of)      │
        │   VectorStore │ DocStore │ GraphStore │ KVStore          │
        ├────────────────────────────────────────────────────────┤
        │  BACKENDS  Postgres(+pgvector) · Weaviate · LadybugDB/kuzu(graph) · Redis · InMemory · LanceDB │
        └────────────────────────────────────────────────────────┘
```

### 4.1 Store layer — compose capabilities (fixes limits #3, #6)

Replace the single `MemoryBackend` with **capability interfaces** (memspine M14 / capability manifests):

- `VectorStore` — `upsert / knn(filter)` · default **LanceDB** (independently chosen by EverMemOS, cognee, honcho)
- `DocStore` — canonical record + lexical/full-text (BM25/FTS5/tsvector)
- `GraphStore` — nodes, typed edges, traversal, temporal validity · default **LadybugDB** (D-26), **kuzu** alt
- `KVStore` — cache, counters, working buffers · default **LMDB**

A `MemoryStore` composite wires these together — turning the broken Neo4j backend into a real graph and letting one config use "LanceDB for recall + graph for relations + LMDB for cache." **No FileStore** (D-30).

### 4.2 Memory model

```
MemoryRecord{
  id, content, type∈{episodic,semantic,procedural,emotional,reflective,working},
  scope{user_id, agent_id, run_id}, tags[], metadata{},
  salience, decay_lambda, created_at, last_seen_at,
  valid_at, invalid_at,                    # bitemporal (graphiti-style)
  vector_ref, simhash, minhash, source, links[],  # links = typed edges, not single waypoint
  trust, quarantined, instruction_flag     # E1 firewall columns (day-one DDL)
}
```

Taxonomy from OpenMemory + ReMe + BEAM + MemMachine; `working` from ReMe/MemMachine; bitemporal `valid_at/invalid_at` from graphiti. Types optional — `profile="simple"` collapses to `semantic`.

### 4.3 Write pipeline (`add`) — with code-level adoptions

`extract → chunk → dedup → classify → embed → link → (consolidate)`

- **extract/chunk** **[D-29, QW]** — **markitdown** (docx/pdf/pptx/xls/html → text) + **chonkie** (chunking) under `[ingest]`; only extracted text is indexed, PII tier propagated (M13.9).
- **dedup** **[D-27, QW]** — two-stage: **datasketch MinHash-LSH** candidate generation (O(1), used by MemOS pref-mem) → embedding-cosine confirm; near-dup → salience boost, not duplicate. LLM **ADD/UPDATE/DELETE/NONE** decision behind `infer` (mem0 v2; terminal state is `NONE`, not "NOOP").
- **classify** **[D-28, DF]** — memory type via embedding-nearest-prototype; entity extraction via **gliner2** CPU NER (graphiti ships it) behind a flag, LLM fallback default. Entity resolution runs *before* conflict detection (M13.3).
- **embed** — pluggable; **fastembed/ONNX** default (D-08), torch-based embedders in an optional `[st]` extra only.
- **link** — typed edges to related memories (k-nearest + entity co-occurrence), not a single waypoint; optional triplet extraction into `GraphStore` (fixes #3/#4).
- **structured output** **[D-31, QW]** — extraction/judge calls use **instructor** (typed, validated, retried) + **json-repair** as an always-on safety net.

### 4.4 Read pipeline (`search`) — hybrid + explainable (fixes limit #2)

`rewrite → recall → fuse → rerank → decay/salience → explain`

- **rewrite** (optional) — profile-aware query rewrite (powermem `QueryRewriter`).
- **recall** — **parallel** vector k-NN + lexical (BM25/FTS5) + graph traversal (temporal-aware).
- **fuse** — **RRF** (k=60, memspine constant) / weighted fusion; **cross-encoder or MMR rerank** optional — the exact recipe graphiti implements (`rrf`, `mmr`, `cross_encoder`, `node_distance`, `episode_mentions`).
- **score** — `sim + recency + salience + graph + tag`, weights per profile; **temporal graph consulted** on temporal queries (graphiti `valid_at/invalid_at/expired_at`; invalidate-on-contradiction, not delete).
- **explain** — return a trace (which signals fired). Only OpenMemory's `_debug` precedes this among the reviewed repos — a real differentiator.

### 4.5 Dynamics (background/maintenance)

- **decay** — tiered exponential salience decay (OpenMemory `decay.py`); Ebbinghaus + per-type multipliers + review schedule `[1,6,24,72,168]h` (powermem).
- **reinforce** — on recall: salience + edge weights up, propagate to neighbors.
- **consolidate** — episodic→semantic summarization; `working` compaction; profile refresh. Exemplars: ReMe **`auto_dream`**, EverOS **offline memory evolution** — a sleep-time job, not an inline step (memspine `schedule.py`).
- **forget** **[D-32, QW]** — prune weak edges; **zstandard**-compress + fingerprint cold low-salience memories, hard-delete only by explicit policy. MemoryBear **Dormancy→Decay→Clearance** is the reference lifecycle (<8% redundancy, >60% waste cut).
- **evolve** **[RG, hook only]** — SimpleMem EvolveMem-style loop that tunes *retrieval policy* (Evaluate→Diagnose→Propose→Guard); out of core, a `profile="evolving"` research hook.

### 4.6 Profiles

| Profile | Behavior |
|---------|----------|
| `simple` | v1 semantics: flat memories, vector or lexical recall, optional recency. Drop-in for current users. |
| `cognitive` | Full pipeline: types, hybrid recall, decay+reinforce, links. |
| `graph` | cognitive + entity/relation extraction (glin