# Model slots migration plan (ADR-082, RM-117)

Status: planned 2026-10-23. Design: [ADR-082](adr/ADR-082-model-slots.md). Tracker row: RM-117
in [PROVIDER_TODO.md](PROVIDER_TODO.md). Pattern: the ADR-081 storage-slot migration
([STORAGE_SLOTS_MIGRATION_PLAN.md](STORAGE_SLOTS_MIGRATION_PLAN.md)).

## 1. Old key → new key

Every old key is rejected with an error naming its new key (no aliases, ADR-068).

| Old | New | Note |
|---|---|---|
| `embedding.provider` / `settings` | `model.embedding.provider` / `settings` | |
| `embedding.batch_size`, `versioned_spaces` | `model.embedding.*` | |
| `embedding.profile`, `pin_profile`, `migrate_profile` | `model.embedding.preset`, `pin_preset`, `migrate_preset` | avoids "profile" |
| `models.embedder_profile` (auto/cpu/gpu) | `model.embedding.preset` | `auto` = unset |
| `llm.roles.<role>` | `model.llm.<role>` | |
| `models.bindings.<llm role>` + `models.profiles.<name>` (kind llm) | `model.llm.<role>` inline | script expands |
| `llm.structured` | `model.llm.<role>.structured` | script copies into every role |
| `models.thinking.<role>` | `model.llm.<role>.thinking` | judge / reflect only |
| `models.escalation.<role>` | `model.llm.<role>.escalation` | targets become inline LLM slots |
| `ModelDecl.timeout_seconds` (catalogue llm) | unchanged inside `model.placement.catalogue` | catalogue entries are declarations, not slots |
| `models.bindings.gliner_semantic` | `model.extraction.semantic` | |
| `models.bindings.gliner_prepass` | `model.extraction.prepass` | |
| `models.bindings.gliner_topic` + `models.topic` | `model.extraction.topic` (+ `labels`, `min_confidence`) | slot enables it |
| `models.bindings.gliner_sensitivity` | `model.extraction.sensitivity` | slot enables it |
| `models.gliner_max_models` | `model.placement.max_loaded_models` | |
| `decision.provider` + `models.bindings.gliner_decision` | `model.decider.route` | `off` = slot absent |
| `read.decider`, `decider_model`, `decider_device`, `decider_threads`, `decider_backend`, `decider_dtype` | `model.decider.read.provider` / `settings.{model, device, threads, backend, dtype}` | `heuristic` = slot absent |
| `read.decider_tasks`, `decider_min_confidence`, `decider_workers` | `model.decider.read.tasks`, `min_confidence`, `workers` | |
| `read.rerank` + `read.rerank_settings` | `model.rerank.chain: [{provider, settings}]` | `off` = `[]` |
| `read.rerank_v2` + `rerank_v2_chain` | `model.rerank.chain` | mode names become provider entries |
| `read.rerank_v2_pool`, `rerank_v2_search_pool` | `model.rerank.pool`, `search_pool` | |
| `read.rerank_{max_top_k, keep, blend, gate, context, date_prefix, balanced, chunk_chars, chunk_overlap, floor}` | `model.rerank.*` without the prefix | |
| `read.skip_rerank_for_ordering` | `model.rerank.skip_for_ordering` | |
| `ingest.asset_vision`, `asset_vision_model`, `asset_vision_url`, `asset_vision_timeout_s` | `model.vision.provider`, `settings.model`, `settings.api_base`, `timeout_s` | `none` = null |
| `models.tiers_v2`, `catalogue`, `gpus`, `detect_gpus`, `ram_mb`, `gpu_headroom_mb`, `role_tier`, `pins`, `allow_hosted`, `required_roles`, `on_missing_model` | `model.placement.*` | |
| `models.default_rerankers` | `model.placement.catalogue_rerankers` | |
| `models.rerank` (chain/off) | removed | `model.rerank.chain: []` |
| `models.*`, `llm.*`, `decision.*` roots | removed | rejected naming `model` |
| warm-up categories `embedder`, `reranker`, `gliner` | `embedding`, `rerank`, `extraction` (+ `vision`) | `runtime.shared_resources.warmup_*` |
| catalogue `kind: reranker`, `embedder` | `rerank`, `embedding` | |

Stays where it is: `read.planner`, `planner_min_confidence`, `relevance_filter`,
`sensitivity_gate`, `static_embedding_prefilter`, all legs, gates, cards and render flags;
`write.prepass`, `memories.semantic.policies.entity_extraction`; `prompts.*`;
`runtime.shared_resources.*`, `runtime.model_concurrency`, `runtime.service_switches.*`.

## 2. Validation rules (WP1)

- Slot root keys are typed per category; `settings` is validated by the selected descriptor
  (as today for `llm.roles` and `read.rerank_settings`).
- `model.llm.<role>`: role must be a known prompt or catalogue role; `thinking` only for
  judge / reflect; escalation targets must differ from the primary slot.
- `model.extraction.*` providers must expose the task the consumer needs (`semantic`,
  `prepass`, `sensitivity`: entities; `topic`: classification); `topic` and `sensitivity`
  need `settings.local_files_only: true`.
- Active consumer without its slot (for example `write.prepass: gliner` with no
  `model.extraction.prepass`) raises naming the slot.
- `read.planner: decision` without `model.decider.route` raises naming it.
- `read.sensitivity_gate: decider` without `model.decider.read` (with task
  `sensitivity_scope` if that task is listed) raises.

## 3. Shared weights (ADR-082 item 4)

Each weights descriptor declares `consumer_fields` (GLiNER: `tasks`, `inference_concurrency`;
OpenDecider: `workers`). The ADR-079 weights key is the effective settings minus those
fields, so five GLiNER slots naming one checkpoint, revision and device load it once. Test:
five consumers + route decider on one checkpoint = one load.

## 4. Work packages

| WP | Scope | Proof |
|---|---|---|
| WP1 | Schema: `config/schema/model.py` (`ModelConfig` and slot classes), `MOVED_KEYS` rejections, removal of `EmbeddingConfig`/`LLMConfig`/`ModelsConfig`/`DecisionConfig` and the read/ingest keys, validators of section 2 | schema unit tests; every old key rejected naming its new one |
| WP2 | Wiring: container, engine, `models/resolve`, rerank selection, extraction pool, decider factories, vision port, warm-up categories read the new slots; weights key of section 3 | wiring + providers + services chunks; one-load test |
| WP3 | Templates (`base`, `assistant`, `simple`, `benchmark`, `embedded`, `server`, presets), config baseline, stage params, PROVIDER_DEFAULTS, USAGE / FEATURES config sections | docs-config surface test; `refresh` scripts `--check` |
| WP4 | `scripts/migrate_config_keys_adr082.py` (dict/kwargs/YAML/JSON, idempotent, skip marker, binding expansion, structured copy) and its run over tests, examples, evals, docs, scripts | script unit tests; grep for old keys returns only rejection tests and history docs |
| WP5 | Full regression in chunks, e2e, combinations, eval harness tests (no new failures over the PA-107 baseline), import contracts 18 kept / 0 broken | tracker evidence |

Order: WP1 → WP4 script (so the codemod is ready) → WP2 and WP3 in parallel agents → WP4 run
→ WP5. Agents work in worktrees; merges are cherry-picks with the tracker and the status table
resolved row by row.

## 5. Risks

- `llm.roles` appears in about 425 files (evals 98); the codemod must handle Python dicts,
  kwargs and YAML inside strings, as the ADR-081 script did.
- Profile expansion can multiply a GLiNER block into five slots in test fixtures; fixtures may
  use a helper that builds the slot once.
- Rerank v2 chain mode names (`jina`, `qwen3`, `fastembed`) must map to registered rerank
  descriptors with the same defaults; pinned by a test comparing the built chain.
