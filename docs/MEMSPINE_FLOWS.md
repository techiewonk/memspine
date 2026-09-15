# memspine — Flow Graph

**Code-traced** (Pass #6, SHA `c6a9fe3`). One canonical top-level graph + the four sub-flows.
Source of truth: append-only `memory_events` (`core/events.py`); vector/lexical/graph/relational/cache are **rebuildable projectors** (`core/replay.py`), never a second SoT.

Companion: [`ARCHITECTURE_FLOWS.md`](./ARCHITECTURE_FLOWS.md) · [`ECOSYSTEM_METHODOLOGY.md`](./ECOSYSTEM_METHODOLOGY.md) §2 (memspine chapter).

---

## 1. Top-level engine flow graph

```mermaid
flowchart TB
  Agent([Agent / caller]) --> Facade

  subgraph Facade["memspine.Engine — async verbs (engine.py)"]
    direction LR
    W["write · write_messages · write_episode"]
    S["search · assemble · shared_search"]
    F["forget · verify_forget · audit_taint"]
    SL["sleep · SleepScheduler"]
  end

  subgraph WritePipe["Write pipeline — per-namespace asyncio.Lock"]
    direction TB
    FW["Firewall.assess<br/>trust matrix + instruction-regex + anomaly (E1)"]
    Q{"quarantine?"}
    QI["inert QUARANTINED record<br/>(audit only, no dedup/retrieval)"]
    SEM["SemanticMemory.write<br/>M5 dedup → entity extract → M4 conflict ladder"]
    PLAIN["plain typed record"]
  end

  CORE[("memory_events<br/>append-only SoT")]

  subgraph Proj["Rebuildable projectors — inline high-water marks"]
    direction LR
    RP["Record / Relational<br/>SQLite"]
    VP["Vector<br/>LanceDB"]
    LP["Lexical<br/>Tantivy BM25"]
    GP["Graph<br/>sqlite_adjacency"]
    CP["Cache<br/>LMDB"]
  end

  subgraph ReadPipe["Retrieve + assemble"]
    direction TB
    HY["vector + lexical → RRF hybrid (D-25)"]
    GT["E1 gate + optional cross-encoder rerank"]
    M1["M1 composite score<br/>recency · relevance · importance · utility"]
    ASM["θ-abstain → MMR → E5 fit → E2 cache-aware placement"]
  end

  subgraph Sleep["Sleep cycle — runner: inline / DBOS / taskiq (D-16)"]
    PL["consolidate · decay_sweep · compress<br/>reorganize (Leiden) · extract_graph · check_watches · prune"]
  end

  W --> FW --> Q
  Q -- yes --> QI --> CORE
  Q -- no --> SEM --> CORE
  Q -- no --> PLAIN --> CORE
  CORE -->|"_append_and_project"| RP
  CORE --> VP
  CORE --> LP
  CORE --> GP
  CORE --> CP

  S --> HY
  VP --> HY
  LP --> HY
  RP --> GT
  GP -. "PPR associative recall" .-> GT
  HY --> GT --> M1 --> ASM --> Agent

  SL --> PL
  PL -->|"all mutations via append_event (the one write door)"| CORE
  F --> CORE
  CORE -. "core/replay.py — rebuild()" .-> Proj
```

**Reading it:** every verb funnels writes through one door (`_append_and_project`) into the event log; projectors are replayed from that log and are the only things reads touch; the sleep cycle mutates only by appending more events. Nothing writes a read model directly — that is the event-sourced invariant (golden rule / ADR-001).

---

## 2. Write path

`Engine.write` (`engine.py:522`) → per-namespace lock → `_write_locked` (`engine.py:636`).

```mermaid
flowchart TD
  A[Engine.write] --> B[validate_namespace + per-ns Lock]
  B --> C[_assess_write: Firewall.assess]
  C --> D{quarantine?}
  D -- yes --> E[WRITE event status=QUARANTINED inert]
  D -- no --> F{memory_type == semantic?}
  F -- yes --> G[SemanticMemory.write]
  G --> G1[annotate simhash+minhash]
  G1 --> G2[M5 dedup: LSH candidates -> cosine]
  G2 -->|dup| G3[union merge + MERGE event]
  G2 -->|new| G4[entity extract opt-in]
  G4 --> G5[M4 conflict ladder vs active fact]
  G5 --> G6[WRITE/CONFLICT events + close intervals]
  F -- no --> H[plain WRITE event]
  E --> I[_append_and_project: append + projectors]
  G3 --> I
  G6 --> I
  H --> I
  I --> J[_corroborate quarantine promotion]
  J --> K[_evolve_links A-MEM auto-link]
  K --> L[working overflow -> page out]
```

---

## 3. Update / conflict — M4 bi-temporal R-ladder

`ConflictPolicy.resolve` (`core/policies/conflict.py:44`), pure & deterministic, over records sharing an `(entity, attribute)` key. Single-active-fact invariant via `find_active_fact`.

```mermaid
flowchart TD
  A[incoming shares entity+attribute key] --> B[find_active_fact]
  B -->|none| Z[plain WRITE added]
  B -->|exists| C[ConflictPolicy.resolve]
  C -->|R0 same fingerprint| N[NOOP rejected]
  C -->|R1 trust below margin| N
  C -->|R3 newer| U["UPDATE: close incumbent valid_to,<br/>status=ARCHIVED, evolve_to; WRITE new"]
  C -->|R4 older| BF[ADD backfill: closed interval]
  C -->|invalidate| IV["archive incumbent no successor +<br/>store negation closed interval"]
  U --> E[CONFLICT audit event]
  BF --> E
  IV --> E
  N --> E
```

---

## 4. Retrieve / rank / assemble

`Engine.search` (`engine.py:722`) → `Engine.assemble` (`engine.py:877`).

```mermaid
flowchart TD
  A[Engine.search query] --> B[embed query]
  B --> C{rescore active E4?}
  C -->|yes| D[vector.search_rescore]
  C -->|no| E[vector.query]
  D --> F{read.hybrid?}
  E --> F
  F -->|yes| G[lexical BM25 leg -> rrf_fuse -> normalize]
  F -->|no| H[cosine order]
  G --> I[E1 gate: ACTIVATED, not quarantined, group/tags, inflate]
  H --> I
  I --> J[E8 static prefilter opt-in]
  J --> K[E4 model2vec prefilter opt-in]
  K --> L[cross-encoder rerank opt-in -> minmax]
  L --> M[M1 composite_score + sort]
  M --> N[RETRIEVE event reinforcement]
  N --> O[assemble: theta-abstain -> MMR -> E5 fit -> E2 placement]
```

---

## 5. Sleep / background dynamics

`Engine.sleep` (`engine.py:1963`) → `workers/schedule.py:run_sleep_cycle` over the runner. Pipelines are pure idempotent step functions (`workers/pipelines.py`); runners decorate them (D-16/D-17).

```mermaid
flowchart TD
  A[Engine.sleep / SleepScheduler tick] --> B[run_sleep_cycle over runner]
  B --> C[consolidate: sessions -> semantic summaries]
  C --> D[extract_graph: LLM edges -> facts + LINKs]
  D --> E[reorganize: Leiden communities -> parent summaries]
  E --> F[decay_sweep: tier transitions delta]
  F --> G[compress: dormant -> zstd]
  G --> H[sleep_compute E7 no-op hook]
  H --> I[check_watches: due + invalidation firing]
  I --> J[event_log_prune: rolling retention]
  C -.all mutations.-> K[ctx.append_event = write door]
```

---

## Legend

- **Solid arrow** = data/control flow. **Dashed arrow** = rebuild/recall path or "goes through the write door."
- **Cylinder** = the append-only event log (single source of truth).
- `Mn` = methodology algorithm ids; `En` = enhancement-program features (see [`ECOSYSTEM_METHODOLOGY.md`](./ECOSYSTEM_METHODOLOGY.md) §2 memspine, and `docs/exports/ECOSYSTEM_ALGORITHMS.csv`).
