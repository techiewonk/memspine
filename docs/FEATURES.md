# memspine — feature catalog

The reference for what memspine ships (P0–P7). Each memory type is opt-in; the
`simple` profile collapses to flat semantic memory. Every config key below is
real — grounded in `src/memspine/config/schema.py` and `config/constants.py`.

See [`USAGE.md`](./USAGE.md) for how to call each verb, and the
[structure plan](./memspine-structure-plan.md) for the authoritative design.

---

## The nine memory types

Enable a type with `memories.<type>.enabled: true` (YAML) or the equivalent
constructor override `memories={"<type>": {"enabled": True}}`. The dependency
closure (C1b) auto-enables prerequisites and logs each one.

### 1. Semantic — durable facts
- **Purpose:** long-lived facts about the world, with an M4 conflict ladder and
  bitemporal validity (`valid_from`/`valid_to`) — contradictions invalidate the
  prior fact instead of destroying it. Writes run the full M5 pipeline: dedup
  (MinHash-LSH → cosine confirm) → optional entity extraction → conflict resolve.
- **Enable:** `memories.semantic.enabled: true` (on in `base`).
- **Key verbs:** `write` (pass `entity`/`attribute` to key the ladder), `search`,
  `assemble`, `retrieve`.
- **Policies:** `memories.semantic.policies` — `conflict`, `dedup`,
  `entity_extraction` (`off` | `llm` | `gliner`), `pii_default_tier`, `trust`.
- **Traces to:** M4/M5, D-27 (dedup), D-28 (NER), ADR-013.

### 2. Working — the hot window
- **Purpose:** the bounded, current-session buffer plus a pinned persona.
  Overflow beyond `page_size` pages the oldest turns out to episodic memory via
  `DECAY_TRANSITION` events (M13.1). The persona keeps one stable record per
  namespace (updates supersede in place) so the E2 cache prefix stays stable.
- **Enable:** `memories.working.enabled: true` (on in `base`).
- **Key verbs:** `write(memory_type="working")`, `set_persona`, `assemble`,
  `retrieve(ns, "working")`.
- **Policies:** `memories.working.policies.page_size` (default 16).
- **Traces to:** M13.1, E2, ADR-013.

### 3. Episodic — the event stream
- **Purpose:** the raw chronological record of what happened; derives session
  boundaries from inter-event gaps. Receives working-memory overflow.
- **Enable:** `memories.episodic.enabled: true` (on in `base`).
- **Key verbs:** `timeline(ns, start, end)`, `sessions(ns, gap_minutes)`,
  `write(memory_type="episodic")`.
- **Constants:** `SESSION_GAP_MINUTES` (30).
- **Traces to:** M13.2.

### 4. Resource — ingested documents
- **Purpose:** turn a document into firewall-gated resource records
  (extract → chunk). Chunks pass the same anomaly context as `write`.
- **Enable:** `memories.resource.enabled: true`; the `ingest` verb needs
  `memspine[ingest]` (markitdown + chonkie) for rich extraction, with a
  built-in char-chunk fallback.
- **Key verbs:** `ingest(path, namespace, pii_tier)`.
- **Traces to:** D-29.

### 5. Procedural — skills & plans
- **Purpose:** reusable know-how on a promotion ladder — a skill enters at
  `draft` and must climb `staged → verified → active`, where `verified → active`
  requires a passed dry-run. Nothing pre-active is ever offered. E6 plan caching
  stores validated multi-step plans (enter at `staged`) and recalls them by
  task-embedding similarity. Prompt versions can be mirrored as procedural
  reference records.
- **Enable:** `memories.procedural.enabled: true` (on in `coding`).
- **Key verbs:** `add_skill`, `promote_skill(dry_run_passed=...)`,
  `deprecate_skill`, `skills(usable_only=True)`, `record_plan`,
  `recall_plan(min_similarity=...)`, `sync_prompt_versions`.
- **Constants:** `PLAN_RECALL_MIN_SIMILARITY` (0.6).
- **Traces to:** M13.4, E6, ADR-014.

### 6. Reflective — higher-order notes
- **Purpose:** memories derived from other memories. Depth is computed from the
  parents and hard-capped at 2; quarantined/deleted/cross-namespace parents are
  refused (no laundering, no tenant crossing) and the reflection's trust is
  capped at its least-trusted parent.
- **Enable:** `memories.reflective.enabled: true` (auto-enables episodic; on in
  `personal`).
- **Key verbs:** `reflect(content, source_record_ids, namespace)`.
- **Constants:** `REFLECTION_DEPTH_CAP` (2).
- **Traces to:** M13.7, ADR-014.

### 7. Associative — the link graph
- **Purpose:** an explicit link graph over records; recall by personalized
  PageRank. New semantic/episodic writes also auto-propose bounded links to their
  vector neighbourhood (A-MEM evolution). Links ride budget-checked `LINK` events
  through the same door (the graph is a projector, not a second source of truth).
- **Enable:** `memories.associative.enabled: true` (auto-enables semantic).
  Constructs a graph store — `sqlite_adjacency` by default, `kuzu` via
  `memspine[kuzu]`. An explicit `graph:` block without associative is a config
  error.
- **Key verbs:** `associate(src_id, dst_id, rel, weight)`,
  `related(record_id, k)`.
- **Constants:** `LINK_BUDGET` (12), `PPR_DAMPING`/`PPR_ITERATIONS`,
  `EVOLUTION_LINK_MIN_SIMILARITY`.
- **Traces to:** M13.6, D-40, ADR-015.

### 8. Prospective — watches
- **Purpose:** things to do or recheck later. A **due-time** watch fires when its
  time is reached (the due instant reuses `valid_from` — no scheduler, no new
  columns). An **invalidation** watch fires when the M4 conflict ladder
  invalidates a watched `entity`/`attribute` fact. Delivery is pull-based: a
  fired watch stays pending until acknowledged. Watches pass the firewall — an
  instruction-shaped watch is quarantined and can never fire.
- **Enable:** `memories.prospective.enabled: true` (auto-enables semantic; on in
  `personal`).
- **Key verbs:** `watch(content, due_at, entity, attribute)`,
  `due(namespace, now)`, `acknowledge_watch(record_id)`.
- **Traces to:** M13.8, ADR-016. (Invalidation watches need a persisted log —
  they never fire under `event_log.mode: ephemeral`.)

### 9. Shared — cross-namespace grants
- **Purpose:** let one namespace read another's records. Foreign hits from
  `shared_search` are **live views** (never copied) marked by their differing
  `record.namespace`, trust-capped at `TRUST_RETRIEVED_CAP`, and never mutate
  grantor state. Standing queries (`subscribe`) are pull-based in v0.1.
- **Enable:** `memories.shared.enabled: true` (needs ≥1 of semantic|episodic; on
  in `multi_agent`).
- **Key verbs:** `grant(to_namespace, memory_types)`, `revoke`,
  `shared_search`, `subscribe`, `subscriptions`, `grants_from`.
- **Traces to:** R2, ADR-016. Grant/subscription records are engine-internal —
  a public `write(memory_type="shared")` is refused.

---

## Memory Firewall (E1 / M17)

Every write of every type passes a **deterministic** gate before the append-only
door. Bound from `memories.semantic.policies.trust`; covers all types. OWASP
**ASI06** (memory & context poisoning) + LLM08.

| Mechanism | What it does | Constant |
|-----------|--------------|----------|
| **Trust scoring** | trust assigned per source class × channel; external channels (retrieved/web/tool/mcp/rest) capped low | `TRUST_DEFAULT` 0.5 · `TRUST_RETRIEVED_CAP` 0.3 |
| **Quarantine** | writes below the trust floor, or instruction-shaped, are stored inert — no dedup, no conflict ladder, never retrievable, but recorded for audit | `QUARANTINE_TRUST_THRESHOLD` 0.25 |
| **Corroboration** | independent trusted writes (operator/system/user, different source) promote an honest quarantined record out of hold | `QUARANTINE_PROMOTION_CORROBORATIONS` 2 |
| **Anomaly detection** | embedding-outlier (similarity to namespace centroid) + MINJA shared-prefix heuristic; quarantined rows excluded from the signal | `ANOMALY_CENTROID_MIN_SIMILARITY`, `ANOMALY_MIN_NEIGHBOURS`, `MINJA_BRIDGE_PREFIX_CHARS` |
| **Instruction-flag** | imperative-shaped content is stored inert, then **wrapped** ("treat as data") when it enters a context window via `assemble` | `INSTRUCTION_FLAG_WRAP` |
| **Audit taint** | `audit_taint(record_id)` walks the log for a record's origin + full derivation (blast radius), scoped to the caller's namespace | — |
| **Hard erasure** | `forget(hard=True)` removes the row from every projection and redacts every log payload carrying its content; `verify_forget` proves it (GDPR-erasure in an append-only design); legal holds block it | — |

Retrieval paths (`search`, `assemble`, `related`, `shared_search`) apply the E1
gate themselves: only `ACTIVATED`, never quarantined, never cross-namespace.
`retrieve()` is the operator/audit surface and intentionally keeps quarantined
rows inspectable — never feed its output straight to a context window.

---

## Optimization program (E2–E9)

| E# | What | Status | Enable key |
|----|------|--------|------------|
| **E1** | Memory Firewall | on by default | `memories.semantic.policies.trust` (tuning) |
| **E2** | cache-friendly context assembly — stability-ordered placement with a cache `boundary_index` | on by default | — (shape via `read.assembly`) |
| **E3** | semantic + operation caching — content-hash embedding cache; prompt-versioned extraction cache | on by default | — |
| **E4** | embedding quantization + static-embedding prefilter | opt-in | `vector.quantization` (`auto`\|`none`\|`int8`\|`binary` — LanceDB-native rescore, ADR-020); `read.static_embedding_prefilter: true` + `memspine[static]` (model2vec pre-rerank gate) |
| **E5** | assembly-time context compression | opt-in | `read.compression: {assembly: true}` + `memspine[compress]` |
| **E6** | plan & tool-value caching → procedural plans | on by default (with procedural) | `record_plan` / `recall_plan` |
| **E7** | sleep-time compute | reserved hook | 4th `sleep()` cycle slot (no-op default) |
| **E8** | retrieval-quality stages — cross-encoder rerank + static prefilter | opt-in | `read.rerank: fastembed`\|`flashrank`\|`litellm` (`flashrank` needs `memspine[rerank]`; `litellm` needs `read.rerank_model`); `read.static_prefilter: true` |
| **E9** | token micro-optimizations — YAML/CoD prompt formats + always-on `json-repair` | on by default | prompt `format` per prompt |

**Defaults are safe:** with the shipped config, E5 and E8 are off and results are
bit-identical to the plain pipeline — `profile="simple"` behavior never changes.

### Hybrid retrieval (D-25)

`search` is **vector-only by default**. Set `read.hybrid: true` to fuse a lexical
BM25 leg into the candidate ranking via reciprocal-rank fusion (RRF), so a record
only lexical search would surface can still enter results. The lexical store is
`read.lexical_provider`: `sqlite_fts5` (default, zero-dep, rides the storage SQLite
client) or `tantivy` (`memspine[tantivy]`, a standalone index — required with a
`postgres` backend). Off means bit-identical to the vector-only pipeline and no
lexical index is built; `profile="simple"` never builds one.

Example — turn on hybrid, E8 rerank, and E5 compression:
```yaml
read:
  hybrid: true               # fuse vector + lexical BM25 via RRF (D-25)
  lexical_provider: sqlite_fts5   # sqlite_fts5 | tantivy [tantivy]
  rerank: fastembed          # off | fastembed | flashrank [rerank] | litellm
  static_prefilter: true     # cheap lexical-overlap gate (post-vector)
  compression:
    assembly: true           # E5 fit stage (needs memspine[compress])
```

---

## Event-sourced substrate & lifecycle

- **One append-only `memory_events` log** is the source of truth; vector, graph,
  FTS, and cache are rebuildable projectors. `rebuild()` replays from seq 0.
- **Event-log modes** (`event_log.mode`): `full` (default), `rolling`
  (bounded window + `retention_days`), `ephemeral` (nothing persisted — rebuild
  and audit taint unavailable). Optional zstd payload compression
  (`event_log.compress`).
- **Sleep cycle** (`sleep()`): consolidate → decay → compress → prune, all
  idempotent. Decay is Ebbinghaus-informed (hot → warm → cold → dormant tiers).
- **Cold tier:** dormant record content is zstd-compressed and transparently
  inflated on read.
- **Prompts as data:** every internal LLM call resolves a named, versioned,
  override-able prompt (`prompts/defaults/*.yaml` + `prompts.overrides`), D-43.

---

## Pluggable backends (composable by config)

Every store is a capability port; the event-sourced core is the single source of
truth, so backends are swappable by config alone. Selecting an unbuilt/reserved
backend raises `ConfigError`.

| Capability | Config key | Options |
|------------|-----------|---------|
| SQL storage / event log | `storage.backend` | `sqlite` (default) · `postgres` `[postgres]` (ADR-025) |
| Vector | `vector.backend` | `lance` (sole store, ADR-021) · `weaviate` reserved |
| Graph | `graph.provider` | `sqlite_adjacency` (default) · `kuzu` `[kuzu]` · `ladybug` `[graph]` · `neo4j` reserved |
| Cache / KV | `cache.backend` | `memory` (default) · `lmdb` `[lmdb]` · `redis` `[redis]` · `valkey` `[valkey]` |
| Lexical (hybrid leg) | `read.lexical_provider` | `sqlite_fts5` (default) · `tantivy` `[tantivy]` |
| Embedding | `embedding.provider` | `fastembed` (default) · `hash` · `static` `[static]` · `litellm` (cloud; `dim` required) |
| LLM (per role) | `llm.roles.<role>.model` | LiteLLM prefix routing — `openai/` · `ollama/` · `bedrock/` · `vertex_ai/` · `llamacpp/<path>` (ADR-024) |
| Secrets | `MEMSPINE_SECRETS_BACKEND` *(env var)* | `env` (default) · `aws` `[aws]` (ADR-023) |
| Workers | `workers.runner` | `inline` (default) · `dbos` `[dbos]` · `taskiq` `[taskiq]` |

The full key-by-key reference (defaults + notes) is in
[`USAGE.md` § Config-key reference](./USAGE.md#config-key-reference); swap recipes
(config diffs) are in [`USAGE.md` § Swap a backend](./USAGE.md#swap-a-backend-config-alone).
