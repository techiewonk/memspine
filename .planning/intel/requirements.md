# Requirements (intel)

> **Provenance note:** The ingest set contains **no PRD** documents. The
> requirements below are **DOC-derived** — extracted from the authoritative
> planning blueprint (`docs/memspine-structure-plan.md`, the phase plan and
> enhancement program) and the feature catalog (`docs/FEATURES.md`). They carry
> DOC precedence (below ADR/SPEC/PRD). Acceptance criteria are the plan's own
> phase deliverables + the C6 combination-matrix boot assertions. The downstream
> roadmapper should treat these as capability requirements, not formal
> PRD-authoritative requirements.

Acceptance-baseline for every phase (from C6 test matrix,
`structure-plan §6`): clean startup, one write→read round-trip per enabled
memory type, clean shutdown, `describe()` schema, `config validate` golden
tests, runner tests (dbos crash-resume, inline flush-on-exit, dead-letter).

---

## Phase requirements (P0–P7)

### REQ-p0-substrate
- source: docs/memspine-structure-plan.md §5 (Phase 0)
- description: Substrate — core/* (all), config/*, services base, SQLite storage
  (SQLAlchemy Core + Alembic, async via aiosqlite), secrets, observability/logging,
  CLI config commands. Includes E1 firewall columns, minhash/simhash fields,
  xxhash/fastuuid/orjson, MemOS provenance/lifecycle fields, json-repair in core,
  D-45 configurable event-log retention (full/rolling/ephemeral + zstd).
- acceptance: event log append + rebuild from log; retention modes honored; DDL
  carries firewall + provenance columns; ADR-001..011 invariants hold.

### REQ-p1-working-retrieval
- source: docs/memspine-structure-plan.md §5 (Phase 1)
- description: Working memory + retrieval — memories/working/*, services/llm
  (local: Ollama/vLLM/LM Studio/OpenAI-compat + llama_cpp), embedding + vector +
  cache defaults, policies scoring + assembly (E2), inline worker, E3 embedding cache.
- acceptance: working-memory write→read round-trip; cache-friendly assembly order
  (E2 stability-sorted placement); embedding cache hit path.

### REQ-p2-semantic
- source: docs/memspine-structure-plan.md §5 (Phase 2)
- description: Semantic memory — memories/semantic/*, conflict + dedup policies
  (datasketch two-stage), entity resolution (gliner2 `[ner]`), prompts subsystem
  (registry + loader + defaults pack, extract/judge/dedupe YAML, config-layered
  overrides), E9 YAML/CoD format, instructor `[structured]`.
- acceptance: semantic write→read; two-stage dedup (LSH → cosine confirm);
  prompt resolution via registry; structured extraction.

### REQ-p3-episodic-lifecycle
- source: docs/memspine-structure-plan.md §5 (Phase 3)
- description: Episodic memory + lifecycle — memories/episodic/*, consolidation +
  decay + compression policies (zstandard cold-tier), workers/pipelines +
  dbos_runner + schedule, resource ingest (markitdown + chonkie `[ingest]`),
  E3 extraction cache, E7 sleep-time hook (no-op default).
- acceptance: episodic round-trip; sleep-cycle stages (consolidate→decay→compress);
  dbos crash-resume; docx/pdf/pptx ingest smoke.

### REQ-p4-governance-firewall
- source: docs/memspine-structure-plan.md §5 (Phase 4); E1
- description: Governance + Memory Firewall — M7 delete hooks, `cli forget --verify`,
  memories/resource/*, E1 full Memory Firewall (trust scoring, quarantine tier,
  write-path anomaly detection, instruction-shaped-content flag, blast-radius audit taint).
- acceptance: quarantined content excluded from consolidation/promotion until
  corroborated; forget + verify_forget + audit taint; firewall columns enforced.

### REQ-p5-procedural-reflective
- source: docs/memspine-structure-plan.md §5 (Phase 5)
- description: Procedural + reflective memory — memories/procedural/*,
  memories/reflective/*, E6 plan-skill subtype (validated multi-step plans entering
  at `staged`, held out via RESOLVING).
- acceptance: skill lifecycle ladder (staged→active via RESOLVING); reflections
  carry caller trust (ADR-014); plan retrieval by task-embedding similarity.

### REQ-p6-associative
- source: docs/memspine-structure-plan.md §5 (Phase 6)
- description: Associative memory — memories/associative/*,
  services/graph/sqlite_adjacency (v0.1 default) + kuzu `[kuzu]` alt + ladybug
  `[graph]` real adapter, communities (graspologic `[community]`) + background
  reorganizer (D-42), E4 two-stage retrieval.
- acceptance: LINK events project to graph edges; link budget enforced
  (ConflictError); prune = weight-0 tombstone; PPR recall; graph-adapter parity
  (ladybug vs kuzu vs sqlite_adjacency same round-trip).

### REQ-p7-prospective-shared-rest
- source: docs/memspine-structure-plan.md §5 (Phase 7)
- description: Prospective + shared + REST — memories/prospective + shared,
  protocols/rest, workers/taskiq_runner (per-scope Redis-Streams queues + priority
  labels), E5 compression + E8 rerank/strategy-rerank opt-in.
- acceptance: prospective watches + shared-memory grants over existing DDL (no new
  EventKind); no-authn REST /write trust boundary; taskiq brokered runner; opt-in
  rerank + assembly compression.

---

## Enhancement program requirements (E1–E9)

### REQ-e1-memory-firewall  [DF — headline]
- source: docs/memspine-structure-plan.md Part B (E1); ADR-013
- description: Memory-poisoning defense (OWASP ASI06 + LLM08) as M17 in
  core/policies/trust.py — trust scoring at write, quarantine tier, write-path
  anomaly detection (embedding-outlier + MINJA heuristics), instruction-shaped
  flag, blast-radius audit taint. Columns trust/quarantined/instruction_flag in P0 DDL.
- acceptance: retrieved content capped low-trust; quarantine excludes from
  promotion; anomaly + instruction-flag detection active.

### REQ-e2-cache-assembly  [QW — highest ROI]
- source: Part B (E2)
- description: policies/assembly.py stability-sorted placement (persona →
  skills/rules → semantic facts → [cache boundary] → episodic + working + query);
  LLM manifest carries prompt_cache_ttl_tiers; metrics track cached_tokens.

### REQ-e3-caching-layer  [QW]
- source: Part B (E3)
- description: services/cache/semantic.py — embedding cache (content-hash),
  extraction/judge cache (semantic-keyed), retrieval micro-cache; keys include
  embedder-id + prompt-version.

### REQ-e4-embedding-quantization  [QW→DF]
- source: Part B (E4); ADR-020
- description: int8/binary quantization + float rescore, Matryoshka truncation,
  model2vec `[static]` prefilter; search_rescore(); default OFF (byte-identical
  simple profile); migration 0009 nullable columns.

### REQ-e5-assembly-compression  [DF]
- source: Part B (E5); ADR-017
- description: CompressionPolicy.assembly_stage ordered fallbacks — drop-lowest-score
  → LLMLingua block-compress `[compress]` → provider compaction. Never compress
  persona/disputed facts.

### REQ-e6-plan-caching  [DF]
- source: Part B (E6); ADR-014
- description: `plan` skill subtype (M13.4) — validated multi-step plans stored on
  task success, retrieved by task-embedding similarity, enter at staged/RESOLVING.

### REQ-e7-sleep-compute  [RG — hook only]
- source: Part B (E7)
- description: Fourth sleep-cycle stage slot (after consolidate→decay→compress) —
  pre-compute reflections, pre-warm caches, pre-assemble bundles. No-op default.

### REQ-e8-retrieval-quality  [DF, opt-in]
- source: Part B (E8); ADR-017, ADR-019
- description: ReadPolicy ordered optional stages — [static_prefilter?] → hybrid RRF
  → [rerank?] → score → MMR → assemble. Off by default; hybrid RRF BUILT
  (read.hybrid, default off, v0.2 flip intended).

### REQ-e9-token-microopt  [QW]
- source: Part B (E9)
- description: YAML/TSV output + Chain-of-Draft template changes in config/templates
  + prompt_registry; pairs with instructor (D-31) for cheap reliable extraction.
