# memspine v0.3 — Memory Types: detailed ecosystem comparison + finalized per-type plan

**Companion to** `PLAN_v0.3.md` (§6b/§6c). **Status:** planning. **Sources:**
`ECOSYSTEM_MEMORY_TAXONOMY.md`, `_METHODOLOGY.md`, `_PROMPTS.md`, `_COMPARISON.md`
(14 peers, code-traced pass #6). Legend ✅ first-class · 🔶 partial · ❌ absent.

---

## 1. Full type inventory — who has which type

| Type | memspine | mem0 | graphiti | cognee | MemOS | honcho | MemMachine | MemoryBear | hindsight | OpenMemory | powermem | langmem | A-mem | EverMemOS |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| semantic | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 🔶 | ✅ |
| working | ✅ | 🔶 | ❌ | 🔶 | ✅ | ✅ | ✅ | ✅ | 🔶 | ❌ | ✅ | ✅ | ❌ | ✅ |
| episodic | ✅ | ❌ | ✅ | 🔶 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 🔶 | ✅ | 🔶 | ✅ |
| resource | ✅ | ❌ | ❌ | ✅ | ✅ | ❌ | ❌ | 🔶 | ✅ | ✅ | ❌ | ❌ | ❌ | ✅ |
| procedural | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | 🔶 | ❌ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ |
| reflective | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | 🔶 | ✅ | ❌ | ✅ |
| associative | ✅ | 🔶 | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | 🔶 |
| prospective | ✅ | ❌ | ❌ | ❌ | 🔶 | ❌ | ❌ | ❌ | 🔶 | ❌ | ❌ | ❌ | ❌ | 🔶 |
| shared | ✅ | 🔶 | 🔶 | 🔶 | 🔶 | ✅ | 🔶 | ❌ | ✅ | 🔶 | ✅ | ❌ | ❌ | 🔶 |

**Coverage:** semantic (14) + associative (12) near-universal · episodic/procedural/working/reflective common (10–11) · resource ~7 · **prospective + shared = memspine-rare/unique.**

---

## 2–3. Per-type detailed comparison + memspine FINAL plan

Each section: how the field **extracts** it, how it **stores** it, the **best practice**, then **memspine's finalized v0.3 plan** (extraction · storage/schema · routing · build).

### semantic — the `add()` core
- **Extraction (field):** LLM. mem0 `ADDITIVE_EXTRACTION` (**ADD-only**, `attributed_to`); graphiti `extract_nodes`+`extract_edges` (**inline bitemporal invalidate**); cognee `extract_content_graph` (instructor, uuid5 merge); MemOS reader (**background** resolve); honcho deriver (**background**, cosine supersede); powermem `FACT_RETRIEVAL` (in-place UPDATE); MemMachine/MemoryBear/hindsight (background/on-write).
- **Storage (field):** vector (mem0), **bitemporal graph edge** (graphiti valid/invalid/expired), graph+LanceDB (cognee), Neo4j tree (MemOS), Postgres+pgvector (honcho).
- **Best practice:** LLM-extract *self-contained keyed facts* + role attribution → dedup → **reconcile bitemporally** (graphiti/memspine), NOT ADD-only; store retrievable vector + optional graph.
- **memspine FINAL:**
  - *Extraction:* LLM `extract` (+gliner2 opt) → self-contained facts keyed `entity`/`attribute` + `attributed_to=role`; grounding = only-stated + no-implicit-inference + **DEC-5 corroboration clause**. **Deferred to background** (turns anchor first).
  - *Storage:* event-log `MemoryRecord` (entity, attribute, value, valid_from/to, confidence, trust) → vector + FTS projectors.
  - *Reconcile:* **deterministic M4 bitemporal ladder** — NOT an LLM update prompt (differentiator).
  - *Routing:* HEAVY → background.
  - *Build:* reuse `extract`/`extract_edges`/`resolve_entity`/`invalidate_edge` prompts + M4; add role-attribution + corroboration clause.

### episodic — turn storage
- **Extraction (field):** **deterministic raw store** (honcho Message, graphiti EpisodicNode, memspine event, MemMachine SQL episode-log anchor); LLM STM-summarize on cap; EverMemOS boundary-cut segmentation.
- **Storage (field):** raw row/record with session_id + ts; markdown daily (EverMemOS).
- **Best practice:** store turns **verbatim** (no LLM), session_id + ts + role; derive semantic later; detect session boundaries.
- **memspine FINAL:**
  - *Extraction:* deterministic — one episodic record per turn (role, session_id, thread_id, message_id, ts). **Sync.**
  - *Storage:* event-log `MemoryRecord` (episodic); consolidate→semantic in sleep; decay tiers.
  - *Routing:* deterministic (sync).
  - *Build:* this is `write_messages` today → folded into `add()`; add typed role + thread_id/session_id.

### working — hot window
- **Extraction (field):** deterministic bounded window (memspine page-out); **rolling LLM summary on eviction** (honcho, langmem, MemMachine STM); **KV-activation cache** (MemOS `act_mem`).
- **Storage (field):** hot-window records, `RunningSummary` (langmem), `KVCacheMemory` (MemOS), pinned persona.
- **Best practice:** bounded hot window + persona; rolling summary on overflow; page out to episodic.
- **memspine FINAL:**
  - *Extraction:* deterministic; hot window (`WORKING_PAGE_SIZE`), overflow→episodic; persona slot. **Sync.**
  - *Storage:* working records + persona.
  - *Routing:* deterministic (sync).
  - *Build:* exists. **Gap:** no KV-activation tier (MemOS) — defer to v0.3+.

### resource — document ingest
- **Extraction (field):** deterministic chunk — memspine markitdown+chonkie; **cognee TextChunker (multi-format, typed Document/Chunk — richest)**; MemOS doc-mode; OpenMemory root/child; hindsight docs.
- **Storage (field):** Document node + chunk vectors (cognee), flat chunk WRITEs (memspine), SQLite root/child (OpenMemory).
- **Best practice:** multi-format parse → chunk → embed → firewall-gate; **typed Document/Chunk hierarchy** (cognee).
- **memspine FINAL:**
  - *Extraction:* deterministic — chunk → per-chunk record (firewall-gated). **Folds into `add()` via content-type detection** (path/doc → resource). Sync.
  - *Storage:* chunk records (resource, doc_id/chunk_id).
  - *Routing:* folds into `add()`; deterministic.
  - *Build:* remove `ingest()` verb (DEC-8) → `add()` detects documents. **Gap:** richer typed Document/Chunk schema (vs cognee) — optional.

### procedural — skills/plans
- **Extraction (field):** **LLM-derived** — EverMemOS cluster cases→AgentSkill, powermem distill+similarity-merge, MemOS authoring/tool-gen (Skill/ToolSchema/LoRA), langmem trustcall; memspine governance ladder.
- **Storage (field):** skill record w/ stage (memspine), graph skill node (MemOS Neo4j), skill_store (powermem), LanceDB clustered md (EverMemOS).
- **Best practice:** derive skills from repeated cases (cluster/distill) **and govern promotion** — memspine ladder + dry-run is the *unique* governance lead.
- **memspine FINAL:**
  - *Extraction:* explicit `add_skill`/`record_plan` API (kept, FORK-SURFACE) + optional **background skill-mining** from repeated procedures.
  - *Storage:* skill record w/ `skill_stage` (draft→staged→verified→active); plan cache by task-embedding.
  - *Routing:* background-derived + API.
  - *Build:* **v0.3 = keep current** (`MemoryRecord` + `skill_stage` ladder + dry-run) — **no change** (DEC-20). **Future:** reify → `Procedure`/`Statement` node + background cluster-synth skill mining (EverMemOS-style).

### reflective — higher-order notes
- **Extraction (field):** **LLM background** — honcho dreamer (deductive/inductive, **tracked premises**, surprisal), MemOS dream (CREATE/UPDATE/MERGE/ARCHIVE), hindsight consolidation→observation+**mental_model**, cognee session-distillation (write-gated, **GROUNDED**), OpenMemory cluster+summarize.
- **Storage (field):** observation node w/ level+premises (honcho), insight node (MemOS), observation+mental_model (hindsight).
- **Best practice:** background reflection over episodic/semantic; **typed levels** (deductive/inductive); **tracked premises + parent links**; grounded (no assistant-only claims); **depth cap**.
- **memspine FINAL:**
  - *Extraction:* LLM `reflect` (**background**, sleep); depth≤2 parent-guarded; `source_record_ids` provenance.
  - *Storage:* reflection record (`reflection_depth`, parent ids).
  - *Routing:* background-derived.
  - *Build:* `reflect()` **removed as a public verb (DEC-8)** → becomes a **background sleep stage**; **wire the `reflect` prompt** (currently unwired — trace gap). Optional: honcho-style premise tracking.

### associative — link graph
- **Extraction (field):** **LLM edge-extract, graph-native** — cognee triplet (kuzu), graphiti `extract_edges`+communities (Neo4j), MemoryBear incremental LPA+clustering, OpenMemory waypoint co-activation, powermem `UPDATE_GRAPH_PROMPT`; memspine LINK + PPR + Leiden.
- **Storage (field):** **embedded graph DB — kuzu (cognee default, graphiti option) is best-in-class**; Neo4j (graphiti/MemOS/MemoryBear); memspine `sqlite_adjacency` (thin) or kuzu/ladybug extra.
- **Best practice:** LLM entity+edge extraction into an **embedded graph DB**; Leiden communities; **bitemporal edge invalidation** (graphiti).
- **memspine FINAL:**
  - *Extraction:* LLM `extract_edges`/`resolve_entity` (v0.2 C1) + A-MEM auto-link → LINK events. **Background** (sleep `extract_graph` + `reorganize`).
  - *Storage:* LINK events → graph projector + Leiden communities (leidenalg, ADR-028). **DECISION: graph-store default** — `sqlite_adjacency` (slim) vs promote **kuzu** (field best). See FORK-GRAPH.
  - *Reconcile:* **wire `invalidate_edge`** (bitemporal LINKs — reserved, graphiti-parity gap).
  - *Routing:* background-derived.
  - *Build:* wire `invalidate_edge`; resolve graph-store default.

### prospective — watches
- **Extraction (field):** **memspine-unique real API** (`watch`/`due`/`acknowledge` + fact-invalidation); EverMemOS foresight (write-only, no recaller); hindsight async_operation task queue.
- **Storage (field):** prospective record (due_at OR watched entity, ack state) — memspine.
- **Best practice:** memspine leads — due()/acknowledge + fact-invalidation watches.
- **memspine FINAL:**
  - *Extraction:* none — explicit `watch()` API (kept, FORK-SURFACE); fires in sleep `check_watches`.
  - *Storage:* prospective record (due_at, watched entity/attribute, ack state).
  - *Routing:* config/API write, **no extraction**.
  - *Build:* exists; unchanged.

### shared — cross-namespace
- **Extraction (field):** memspine + hindsight first-class; honcho workspace scope; powermem agent modes; cognee dataset ACL. Most peers = a `user_id`/`group_id` filter, not real sharing.
- **Storage (field):** grant record (namespace, scope, trust cap) + subscription (memspine); bank/mission cascade (hindsight — relational-richest).
- **Best practice:** memspine **trust-capped live views** (foreign records never copied) = security-best; hindsight bank cascade = relational-richest.
- **memspine FINAL:**
  - *Extraction:* none — `grant()`/`subscribe()` API (engine bookkeeping, firewall-exempt).
  - *Storage:* grant/subscription records; `shared_search` reads foreign live (trust-capped).
  - *Routing:* config/API write, **no extraction**.
  - *Build:* exists; unchanged.

---

## 4. Finalized picture + decisions

**The `add()` internal dispatch (by extraction type):**

| Timing | Types | What `add()` does |
|---|---|---|
| **Sync, deterministic** | episodic · working · resource | anchor the record immediately (firewall parallel) |
| **Background, LLM** | semantic · reflective · associative | mine in the sleep cycle after the anchor |
| **Background-derived + API** | procedural | explicit `add_skill` + optional background mining |
| **Config/API, no extraction** | prospective · shared | `watch`/`grant` verbs, unchanged |

**memspine LEADS:** 9-type registry over one event-sourced `MemoryRecord`; semantic pipeline (extract→firewall→2-stage dedup→**deterministic M4 bitemporal** — only graphiti matches conflict, none match firewall); prospective/shared/reflective(depth-capped)/procedural(ladder) as first-class governed types; full `rebuild()`.

**New decisions this surfaces (to add to PLAN_v0.3):**
- **FORK-GRAPH** 🟡 — associative store default: keep slim `sqlite_adjacency` or promote **kuzu** (field best-practice, embedded)? Lean: keep `sqlite_adjacency` default (slim-core D-03), document kuzu as the recommended production graph.
- **DEC (assoc):** wire **`invalidate_edge`** — bitemporal LINK invalidation (graphiti parity; reserved today).
- **DEC (reflect):** wire the **`reflect` prompt** and move reflection to a **background sleep stage** (DEC-8 removed the public verb).
- **Optional/deferred:** background skill-mining (procedural), KV-activation working tier (MemOS), richer typed Document/Chunk resource schema (cognee).

---

## 5. Master per-type processing spec (DEC-21) — code-grounded

**Model (locked, DEC-21):**
- **Substrate:** ONE event-sourced `MemoryRecord` + `memory_type` discriminator. Separation is by **policy** (`decay`/`compression`/`consolidation` bind by `memory_type` — verified in `workers/pipelines.py`) + **node projection** (DEC-20). Not separate stores.
- **Responder:** sleep **pre-computes the units**; `read()` **assembles them live** (MMR λ=0.7 + `boundary_index` E2 + θ-abstain). No pre-rendered response cache — units stay rebuildable projections.
- **Read surface:** ONE fused `read()` (4-leg RRF: semantic kNN · lexical BM25 · graph edge-type PPR · temporal) + per-type verbs kept for single-type reads. Flat `AssembledContext` now; typed envelopes fast-follow (DEC-17).
- **Decay:** M3 tier ladder (`DECAY_HOT_TO_WARM=7d` · `WARM_TO_COLD=30d` · `COLD_TO_DORMANT=90d`) for lifecycle/compression **+** category multiplier (DEC-18) for PPR weight. Reinforce-on-read = `RETRIEVE_UTILITY_STEP=0.1` (max 1.0), rides the RETRIEVE event (rebuild-deterministic).

**Per-type table** — all parameters:

| Type | Input | Timing | Store (node / record) | Depends | Decay | Reinforce | Responder unit (pre-computed in sleep) | Read leg / shape |
|---|---|---|---|---|---|---|---|---|
| **episodic** | turn | **sync** verbatim | `Episode` record + vector | — | tier ladder; `consolidate`→semantic (heat≥50, 30-min session gap) | +0.1/read | raw turns *(session summary pre-computed, ≤600c)* | timeline, session-grouped |
| **working** | recent turns | **sync** | hot window + persona slot | — | page-out→episodic (`WORKING_PAGE_SIZE`) | — | hot window *(persona pinned, materialized)* | STM prefix (before `boundary_index`) |
| **semantic** | user turn | **bg** LLM extract | `Statement` + `Entity` + vec/FTS | Entity | tier, **slow**; Chronicle bitemporal supersede | +0.1/read | **facts** (keyed `entity`/`attribute`, role-attributed) | semantic kNN + graph PPR |
| **preference** *(overlay)* | user turn | **bg** LLM | `Statement`(+`reinforced`) + `Profile` cache | semantic | reinforce-gated (S-class expiry ~14d) | **on-use** | **Profile** (distilled cache, projection) | personal, proportional |
| **associative** | Statements/entities | **bg** derived | LINK edges + `Community` | Entity | **fastest** (category multiplier) | edge weight | **communities** (Leiden) *(live PPR subgraph)* | graph PPR subgraph |
| **reflective** | episodic/semantic | **bg** sleep | `Reflection` (depth≤2) + vec | Statement/Episode | tier, slow | +0.1/read | **syntheses** *(⚠ `reflect` prompt currently UNWIRED — B7)* | semantic (high-level) |
| **procedural** | `add_skill` API + bg mine | API + bg | **record + `skill_stage`** (not reified — DEC-20) | — | stage-gated (draft→…→deprecated) | on success | **skill cards** (intent/approach/steps) | skills / recall_plan |
| **prospective** | `watch()` API | **sync** | record `valid_at`>now (Foresight) | Entity | none (time-fired via `check_watches`) | ack | — *(already-existing; live scan)* | `due()` |
| **shared** | `grant()` API | **sync** | grant / subscription record | — | none (bookkeeping, firewall-exempt) | — | — *(live foreign read, trust-capped)* | `shared_search` |

*(resource deferred — DEC-8; would be sync-chunk → `Source`/chunk records.)*

**Read-combine (one `read()`):** 4 legs → RRF → cross-encoder rerank → live assembly under one token budget, order **persona → semantic facts(+validity) → episodic timeline → associative neighbors**, + citations + trust (DEC-17). **STM legs (working/episodic tail) fresh at t+0; LTM legs (semantic/associative/reflective) may lag** but converge — `recall@k` t+0 MUST ≈ t+∞ for verbatim-Episode facts (eval E5).

**Read-surface peer evidence (why fused-default + per-type verbs):**

| Pattern | Peers | Takeaway |
|---|---|---|
| Per-type recallers / SearchTypes | EverMemOS (Episode/AgentSkill/Profile recallers), cognee (15 SearchTypes) | worth keeping per-type verbs |
| One fused object w/ per-type fields | MemMachine, OpenMemory, MemoryBear | default should be one response |
| Flat fused list | mem0, graphiti, langmem, **memspine today** | simplest fused baseline |
| **None** ship per-type-only | — | fused `read()` is the correct default |
