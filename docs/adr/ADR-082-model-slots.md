# ADR-082: One model block with a slot per model category

**Status:** Accepted (design) on 2026-10-23. Implementation is RM-117.
**Date:** 2026-10-23
**Deciders:** Owner (model-config naming review, 2026-10-23, two question rounds)
**Supersedes:** model selection in `embedding`, `llm.roles`, `llm.structured`, `models.*`
(profiles, bindings, escalation, thinking, topic, planning), `decision.provider`, the
`read.rerank*` / `read.decider*` keys and `ingest.asset_vision*`. It supersedes the D-97 /
ADR-075 named-profile + binding mechanism (no alias). D-96's single-field removal stands.
**Relates to:** ADR-068 (no aliases), ADR-079 (shared weights), ADR-081 (storage slots; this
ADR is its compute counterpart), RM-113 (model adapters under `services/<slot>/`).

## Context

Model selection is set in eight places. The same kind of model is configured differently in
each: an embedder is `embedding.{provider, settings}`, an LLM is `llm.roles.<role>` or a
`models.bindings` entry pointing at a `models.profiles` entry, a GLiNER model is only a profile
and binding, the decider is either `decision.provider` (H24) or `read.decider*` (I28), a
reranker is `read.rerank` + `read.rerank_settings` or `read.rerank_v2_chain`, and the vision
model is four `ingest.asset_vision*` keys. RM-113 already moved the code to
`services/{embedding,llm,rerank,extraction,decider}`; the configuration did not follow.

## Decision

1. **One `model` block, one slot per category:** `model.embedding`, `model.llm.<role>`,
   `model.rerank`, `model.extraction.<consumer>`, `model.decider.<consumer>`, `model.vision`,
   plus `model.placement` for device planning.
2. **Slot shape (owner principle).** Every slot is
   `{provider, <typed common keys>, settings}`:
   - `provider` is resolved by the registry; an unknown provider is a config error.
   - Keys every provider of the category shares (timeouts, retry, chain, confidence floor) are
     typed at the slot root and validated by memspine's schema.
   - Keys that vary by provider (model id, api_base, device, dtype, tasks, n_ctx, ...) go in
     `settings`, a free JSON object validated by that provider's descriptor. A new provider
     never changes the schema.
3. **No named profiles.** Every slot states its provider and settings inline.
   `models.profiles` and `models.bindings` are removed. Identical effective settings still
   share one set of loaded weights (ADR-079).
4. **Shared-weights key ignores consumer-only fields.** The weights fingerprint of an
   extraction or decider slot excludes `tasks` (and other per-consumer settings each
   descriptor names), so five consumers of one GLiNER checkpoint load it once.
5. **LLM roles are slots:** `model.llm.<role>` with typed `timeout_s`, `structured`
   (`retry_on_error`, `constrained_retry`), `thinking` (token budget; judge / reflect only, as
   P-15) and `escalation` (triggers, max_attempts, timeout_s, max_tokens_per_attempt, inline
   `targets` that are themselves LLM slots). A role that is not listed is off.
6. **Rerank is always a chain:** `model.rerank.chain` is a list of `{provider, settings}`; the
   first that builds and works wins, `[]` is off, one entry is today's single reranker. The
   behaviour keys lose their `rerank_` prefix and the `v2` flag: `pool`, `search_pool`,
   `max_top_k`, `keep`, `blend`, `gate`, `context`, `date_prefix`, `balanced`, `chunk_chars`,
   `chunk_overlap`, `floor`, `skip_for_ordering`.
7. **Extraction has one slot per consumer:** `semantic`, `prepass`, `topic` (with typed
   `labels`, `min_confidence`), `sensitivity`. A consumer that is not listed is off.
   `topic` and `sensitivity` are enabled by their slot (as their binding enabled them);
   `semantic` and `prepass` stay switched by `memories.semantic.policies.entity_extraction` /
   `write.prepass` and need their slot (missing slot = config error naming it). The RM-99 /
   RM-106 rule stays: topic and sensitivity are offline only.
8. **Decider has one slot per consumer:** `model.decider.route` (H24 mode routing,
   `read.planner: decision`; was `decision.provider`) and `model.decider.read` (I28 read-path
   decision points; was `read.decider*`). Typed keys: `tasks` (read only), `min_confidence`,
   `workers`. Providers `heuristic`, `opendecider`, `gliner2`; later JEPA-style and
   YOLO-style deciders register as providers. Not listed = off (`read` off = the heuristic
   rules, as today's default).
9. **`model.vision`** (was `ingest.asset_vision*`): `{provider: ollama, timeout_s, settings:
   {model, api_base}}`; null = no description.
10. **Scope: selection plus model behaviour move.** Keys that configure the model itself move
    (provider, settings, timeouts, rerank behaviour, embedding space options). Read-pipeline
    flags that only consume a model's output stay where they are (`read.planner`,
    `read.relevance_filter`, `read.cards`, `read.sensitivity_gate`, legs, gates). Resource
    sharing and scheduling stay in `runtime` (`shared_resources`, `model_concurrency`).
11. **`model.placement`** holds device planning: `tiers_v2`, `catalogue`,
    `catalogue_rerankers`, `gpus`, `detect_gpus`, `ram_mb`, `gpu_headroom_mb`, `role_tier`,
    `pins`, `allow_hosted`, `required_roles`, `on_missing_model`, `max_loaded_models`.
    `models.rerank: chain|off` is removed (`model.rerank.chain: []` is off).
12. **Embedding:** `model.embedding.{provider, batch_size, versioned_spaces, preset,
    pin_preset, migrate_preset, settings}`. `embedding.profile` becomes `preset` (a shipped
    preset name such as `gpu` or `cpu_multilingual`), so it is not confused with the removed
    profiles; `models.embedder_profile` (auto/cpu/gpu) folds into `preset`.
13. **Vocabulary follows the slots:** warm-up categories become `embedding`, `rerank`,
    `extraction`, `llm`, `decider`, `vision`; catalogue `kind` becomes `llm | rerank |
    embedding`.
14. **No aliases (ADR-068).** Every old key is rejected with an error naming its new key. A
    migration script (`scripts/migrate_config_keys_adr082.py`) rewrites configs, expanding
    each old binding into an inline slot and copying `llm.structured` into every role.

## Sample

```yaml
model:
  embedding:
    provider: fastembed
    batch_size: 32
    versioned_spaces: false
    settings: {model: BAAI/bge-small-en-v1.5}
  llm:
    extract:
      provider: litellm
      timeout_s: 60
      structured: {retry_on_error: true, constrained_retry: false}
      escalation:
        triggers: [error, invalid_json]
        max_attempts: 2
        timeout_s: 120
        max_tokens_per_attempt: 2048
        targets:
          - {provider: litellm, timeout_s: 120, settings: {model: ollama/qwen3.5:27b}}
      settings: {model: ollama/qwen3.5:9b, api_base: http://localhost:11434}
    judge:
      provider: llamacpp
      timeout_s: 60
      thinking: 512
      settings: {model_path: C:/models/qwen.gguf, n_ctx: 8192}
  rerank:
    chain:
      - {provider: jina, settings: {}}
      - {provider: fastembed, settings: {}}
    pool: 64
    floor: minmax
  extraction:
    semantic: {provider: gliner2, settings: {model: fastino/gliner2.5-base-v1, tasks: [entities]}}
    topic:
      provider: gliner2
      labels: {cardiology: health/condition}
      min_confidence: 0.6
      settings: {model: fastino/gliner2.5-base-v1, tasks: [classification]}
  decider:
    route: {provider: gliner2, settings: {model: fastino/gliner2.5-base-v1}}
    read:
      provider: opendecider
      tasks: [list_mode, bridge_hop]
      min_confidence: 0.5
      settings: {model: manjunathshiva/opendecider-nano, device: cpu, backend: torch}
  vision: null
  placement:
    tiers_v2: false
    max_loaded_models: 1
read:
  planner: decision      # pipeline flag stays
```

## Owner selections (2026-10-23)

- Block `model`; slot shape typed root + provider-owned `settings` JSON.
- Inline only (no profiles); per-role LLM options typed inside each role.
- Scope: selection plus model behaviour; planning in `model.placement`.
- Rerank: chain plus typed behaviour; extraction: per-consumer slots.
- Resolutions accepted with "proceed": the `tasks`-free weights key (item 4).
- Found while writing the ADR and resolved the same way as extraction (per-consumer slots):
  the two deciders (item 8) and the vision model (item 9). Open to owner revision before
  WP1 lands.

## Consequences

- One place shows every model the deployment uses; the configuration mirrors
  `services/<slot>/` and the `storage` block.
- Breaking change across about 160 source files, 380 test files, the eval harness, templates
  and docs. Templates, USAGE, the config baseline, stage params, e2e and examples change in
  the same change; the eval harness configs move with PA-107 where they are already flat.
- Repetition: a model used by several consumers is written once per slot. The weights are
  still shared; config size is the cost of having no profiles.
