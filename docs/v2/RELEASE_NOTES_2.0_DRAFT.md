# MemSpine 2.0 release notes (DRAFT)

> **DRAFT. NOT FOR RELEASE.** R6-01 (freeze the arms, run the blind sets once) has not happened. The GPU is paused, so
> no flag below has a measured adoption verdict. Every default is off; nothing here graduates a flag. This file was
> written at FU-REL and must be re-checked against the tree after R6-01. Counts, ids and candidate lists can change
> before R6-02.

Sources: `docs/v2/RETIRED.md`, `docs/adr/ADR-067-restructure-in-place.md`, `scripts/moves/*.toml`,
`docs/USAGE.md` (config-key reference), `docs/v2/SCREENS.md`, `evals/v2_queue.tsv`.

## 1. Removed or moved APIs

ADR-067: memspine is restructured in place. There are **no compatibility shims**: no `memspine.compat.v1`, no
deprecated aliases, no re-export modules at old import paths, no deprecation period. A removed or renamed config
key fails at load with `ConfigError` naming the replacement. Run the codemod (`scripts/codemod_move.py`) on your own
code to rewrite imports.

### Public API

| Before | After | Notes |
|---|---|---|
| `memspine.protocols.*` | `memspine.api.*` | `protocols/` is deleted. `protocols.rest` -> `api.rest`, `protocols.mcp` -> `api.mcp`, `protocols.tools` -> `api.tools` (F-13b). |
| `memspine.engine` | `memspine.api.engine` | The old `engine.py` facade is split (F-13a/b/c); the rest of it lives under `api/`. |
| `memspine.open()` (sync) | `await memspine.open(config)` | `open` is now `async` and returns a started engine. |
| old worker entry | `memspine.api.worker` | Start workers from here. |
| `memspine.workers.pipelines` | `memspine.derive.pipeline.stages.context` (+ sibling stage modules) | F-15; the stage code was then split by name. |

### Module moves (from `scripts/moves/*.toml`)

The full lists are in the TOML files (one `[[move]]` per module; importers are rewritten in the same commit).

| Move file | Scope |
|---|---|
| `F-05.toml` | `memspine.core.*` store and trust leaves -> `memspine.store.*` (`records`, `events`, `replay`, `projector` -> `store.projections.projector`, `namespace` -> `store.scope`) and `memspine.trust.*` (`visibility`, `firewall`, `audit`, `erasure`, `privacy`, `redaction`). |
| `F-06.toml` | `memspine.services.*` model clients -> `memspine.models.*` (`llm`, `embedding`, `query_encoder`, `rerank`, `decision`, `cache`); `core.query_shape`, `temporal_query`, `temporal_resolve`, `perspective` -> `models.encoders.query.*`; `core.query_contract`, `core.lead` -> `memspine.read.*`; `core.policies.assembly`, `core.policies.compression`, `core.answer`, `core.answer_check` -> `memspine.compose.*`. |
| `F-13b.toml` | `memspine.protocols.{rest,mcp,tools}` -> `memspine.api.{rest,mcp,tools}`. |
| `F-15.toml` | `memspine.workers.pipelines` -> `memspine.derive.pipeline.stages.context`. |

### Retired config and features (`docs/v2/RETIRED.md`, F-14a)

All were default-off, so simple-profile defaults and read goldens are unchanged. A retired key in YAML raises
`ConfigError` naming the replacement (`RETIRED`, `RETIRED_VALUES` in `src/memspine/config/flags.py`).

| Retired | Replacement |
|---|---|
| Slot loop (`read.agentic_mode: slot`) | `read.agentic`; the decided successor is the `read.deep` sufficiency round |
| Read-time fact chain (`read.fact_chain`, `read.fact_chain_trigger`) | none for the read arm (the write-time `fact_projection` arm stays) |
| Word-vector leg (`read.word_vector_*`) | `read.hybrid` (BM25 + vector legs) |
| Decider relevance gate (`read.relevance_gate: decider`) | `read.relevance_gate: store_calibrated` |
| Kuzu graph backend (`graph.provider: kuzu`, `[kuzu]`) | `graph.provider: ladybug` (or `auto`) |
| OpenSearch lexical stub (`read.lexical_provider: opensearch`) | `read.lexical_provider: tantivy` |

Follow-up recorded in RETIRED.md: the `kuzu` and `opensearch` extras are still declared in `pyproject.toml`.
Not retired (reported only): the full / compose read modes and the v1 read headers.

## 2. New config flags, grouped by area

Everything below is **off by default**. Non-boolean keys carry an inert default (`null`, `[]`, `0`, `inline`, `embedded`,
`auto`) or are tuning knobs that apply only once their parent flag is on. With defaults, the simple-profile read goldens
are byte-identical (ADR-067 item 6). "Package" is the first work-package id cited in the key's `docs/USAGE.md` row.
Defaults are quoted from USAGE.md.

### Storage

| Key | Default | Package |
|---|---|---|
| `storage.profile` | `embedded` | F-16d |

### Embedding

| Key | Default | Package |
|---|---|---|
| `embedding.versioned_spaces` | `False` | F-03 |
| `embedding.profile` | `null` | MD-02 |
| `embedding.pin_profile` | `False` | MD-02 |
| `embedding.migrate_profile` | `False` | MD-02 |

### Graph

| Key | Default | Package |
|---|---|---|
| `graph.key_v2` | `false` | F-01 |
| `graph.edge_compiler_v2` | `false` | F-02c |
| `graph.backend` | `auto` | F-16e |
| `graph.neo4j_outbox` | `false` | F-16e |
| `graph.neo4j_url_env` | `MEMSPINE_NEO4J_URL` | F-16e |
| `graph.neo4j_user_env` | `MEMSPINE_NEO4J_USER` | F-16e |
| `graph.neo4j_password_env` | `MEMSPINE_NEO4J_PASSWORD` | F-16e |
| `graph.neo4j_database` | `neo4j` | F-16e |

### Isolation

| Key | Default | Package |
|---|---|---|
| `isolation.scope_keys_v2` | `false` | F-16c |
| `isolation.viewer_required` | `false` | F-16c |
| `isolation.multi_party_namespaces` | `[]` | F-16c |

### Models

| Key | Default | Package |
|---|---|---|
| `models.tiers_v2` | `false` | MD-01 |
| `models.catalogue` | `[]` | MD-01 |
| `models.gpus` | `[]` | MD-01 |
| `models.detect_gpus` | `false` | MD-01 |
| `models.ram_mb` | `0` | MD-01 |
| `models.gpu_headroom_mb` | `512` | MD-01 |
| `models.default_rerankers` | `shipped` | MD-01 |
| `models.rerank` | `chain` | MD-01 |
| `models.embedder_profile` | `auto` | MD-01 |
| `models.role_tier` | `{}` | MD-01 |
| `models.pins` | `{}` | MD-01 |
| `models.allow_hosted` | `false` | MD-01 |
| `models.required_roles` | `[]` | MD-01 |
| `models.on_missing_model` | `error` | MD-01 |
| `models.thinking` | `{}` | MD-01 |

### Read path

| Key | Default | Package |
|---|---|---|
| `read.exact_budget` | `false` | R3-09a |
| `read.reader_tokenizer` | `null` | R3-09a |
| `read.exact_budget_max_skips` | `8` | R3-09a |
| `read.evidence_bundles` | `false` | R3-09b |
| `read.bundle_pool_factor` | `2.0` | R3-09b |
| `read.bundle_fact_share` | `0.6` | R3-09b |
| `read.trust_rendering` | `false` | T5-02 |
| `read.family_fusion` | `false` | R3-06 |
| `read.lift_before_rerank` | `false` | R3-08 |
| `read.lift_max_turns` | `2` | R3-08 |
| `read.rerank_v2` | `false` | R3-07 |
| `read.rerank_v2_chain` | `[jina, qwen3, fastembed]` | R3-07 |
| `read.rerank_v2_pool` | `64` | R3-07 |
| `read.tiers_v2` | `false` | R3-01 |
| `read.tiers_v2_depth` | `fast` | R3-01 |
| `read.contract_rules` | `false` | R3-02 |
| `read.router_rules` | `false` | R3-03 |
| `read.freshness_v2` | `false` | O4-01b |
| `read.freshness_stale_gap` | `20` | O4-01b |
| `read.freshness_fallback` | `false` | O4-01b |
| `read.freshness_fallback_max` | `20` | O4-01b |
| `read.standing_v2` | `false` | O4-08 |
| `read.standing_answers_budget` | `120` | O4-08 |
| `read.external_v2` | `false` | R3-13 |
| `read.external_v2_budget_tokens` | `300` | R3-13 |
| `read.cache.enabled` | `false` | R3-15 |
| `read.cache.max_entries` | `256` | R3-15 |
| `read.cache.ttl_seconds` | `null` | R3-15 |
| `read.parallel_legs` | `false` | R3-15 |
| `read.gate_v2` | `false` | R3-11 |
| `read.gate_v2_weak` | `"keep"` | R3-11 |
| `read.gate_v2_named` | `"hit"` | R3-11 |
| `read.deep_v2` | `false` | R3-12 |
| `read.obligations` | `false` | R3-16 |
| `read.entity_leg_v2` | `false` | R3-05a |
| `read.legs_v2` | `false` | R3-04 |
| `read.temporal_precision_aware` | `false` | W2-14 |
| `read.lexical_scorer` | `bm25` | R3-04 |
| `read.lexical_language` | `en` | R3-04 |
| `read.episode_leg_v2` | `false` | R3-05c |
| `read.graph_leg_v2` | `false` | R3-05b |
| `read.graph_leg_min_seeds` | `2` | R3-05b |
| `read.graph_leg_seed_hits` | `3` | R3-05b |
| `read.graph_leg_lift_max_turns` | `8` | R3-05b |
| `read.graph_leg_carry` | `0.9` | R3-05b |
| `read.episode_lift_max_turns` | `8` | R3-05c |
| `read.episode_lift_carry` | `0.9` | R3-05c |
| `read.lexical_namespace_languages` | `{}` | R3-04 |
| `read.timeview_v2` | `false` | R3-14 |
| `read.neighbours_v2` | `false` | R3-10 |

### Write path

| Key | Default | Package |
|---|---|---|
| `write.intake_v2` | `false` | W2-01 |
| `write.asset_describer` | `off` | W2-11 |
| `write.asset_describe_timeout_s` | `3.0` | W2-11 |
| `write.stamping_v2` | `false` | W2-02 |
| `write.stamp_participant_tags` | `v1` | W2-02 |
| `write.stamp_default_tz` | `null` | W2-02 |
| `write.role_policy_v2` | `false` | W2-03 |
| `write.echo_threshold` | `0.6` | W2-03 |
| `write.segmentation_v2` | `false` | W2-05 |
| `write.segment_gap_s` | `1800.0` | W2-05 |
| `write.segment_calendar_day_cut` | `false` | W2-05 |
| `write.segment_grow_tokens` | `400` | W2-05 |
| `write.segment_max_turns` | `24` | W2-05 |
| `write.batch_max_tokens` | `3000` | W2-05 |
| `write.batch_max_windows` | `8` | W2-05 |
| `write.batch_max_age_s` | `30.0` | W2-05 |
| `write.envelope_v2` | `false` | W2-06a |
| `write.temporal_v2` | `false` | W2-14 |
| `write.integration_v2` | `false` | W2-10 |
| `write.integration_cardinality` | `{}` | W2-10 |
| `write.integration_decide_unhinted` | `false` | W2-10 |
| `write.lineage_table` | `false` | F-16a |
| `write.feedback_v2` | `false` | O4-11 |
| `write.feedback_operators` | `[]` | O4-11 |

### Offline / sleep

| Key | Default | Package |
|---|---|---|
| `offline.patterns_v2` | `false` | O4-05 |
| `offline.patterns_kinds` | `[]` | O4-05 |
| `offline.patterns_max_new` | `50` | O4-05 |
| `offline.standing_v2` | `false` | O4-08 |
| `offline.standing_max_answers` | `50` | O4-08 |
| `offline.standing_idle_after` | `5` | O4-08 |
| `offline.communities_v2` | `false` | O4-06 |
| `offline.community_summary_budget_tokens` | `20000` | O4-06 |
| `offline.community_min_size` | `3` | O4-06 |
| `offline.community_max_size` | `200` | O4-06 |
| `offline.community_hubs` | `[]` | O4-06 |
| `offline.community_hub_fraction` | `0.5` | O4-06 |
| `offline.community_max_entities_per_record` | `8` | O4-06 |
| `offline.decay_v2` | `false` | O4-09 |
| `offline.decay_boundaries` | `[0.5, 0.3, 0.15]` | O4-09 |
| `offline.decay_margin` | `0.05` | O4-09 |
| `offline.decay_protected_kinds` | `[]` | O4-09 |
| `offline.decay_multires_jobs` | `0` | O4-09 |
| `offline.decay_group_min_size` | `2` | O4-09 |
| `offline.reconcile_v2` | `false` | O4-03 |
| `offline.histories_v2` | `false` | O4-04 |
| `offline.near_duplicate_text` | `0.80` | O4-04 |
| `offline.session_close_v2` | `false` | O4-02 |
| `offline.session_close_budget` | `6000` | O4-02 |
| `offline.profile_card_v2` | `false` | O4-07 |
| `offline.profile_kappa_p` | `24` | O4-07 |
| `offline.profile_max_chars` | `2400` | O4-07 |
| `offline.profile_allow_inferred` | `false` | O4-07 |
| `offline.profile_alpha` | `0.3` | O4-07 |
| `offline.sleep_v2` | `false` | O4-01a |
| `offline.sleep_min_turns` | `20` | O4-01a |
| `offline.sleep_idle_minutes` | `10.0` | O4-01a |
| `offline.sleep_context_pressure` | `0.7` | O4-01a |
| `offline.sleep_min_gap_minutes` | `60.0` | O4-01a |
| `offline.sleep_maintenance_hours` | `24.0` | O4-01a |
| `offline.sleep_lease_ttl_seconds` | `300.0` | O4-01a |
| `offline.sleep_manual_only` | `false` | O4-01a |
| `offline.budget_v2` | `false` | O4-01b |
| `offline.budget_calls_per_cycle` | `null` | O4-01b |
| `offline.budget_tokens_per_cycle` | `null` | O4-01b |
| `offline.sleep_scoped` | `false` | O4-01a |

### Firewall / trust

| Key | Default | Package |
|---|---|---|
| `firewall.gates_v2` | `false` | W2-04 |
| `integrity.horizon_v2` | `false` | T5-01 |

### Retention, audit, privacy

| Key | Default | Package |
|---|---|---|
| `retention.v2` | `false` | T5-05 |
| `retention.profile` | `null` | T5-05 |
| `audit.chain_v2` | `false` | T5-05 |
| `privacy.erasure_v2` | `false` | T5-04 |
| `consent.v2` | `false` | T5-03 |

### Observability

| Key | Default | Package |
|---|---|---|
| `observability.ops_ledger` | `false` | T5-06 |
| `observability.otel.enabled` | `false` | T5-06 |

### Runtime and workers

| Key | Default | Package |
|---|---|---|
| `workers.outbox` | `false` | F-04 |
| `workers.transport` | `"inline"` | RT-01 |
| `workers.transport_broker` | `"memory"` | RT-01 |
| `runtime.scheduler_v2` | `false` | RT-02 |
| `runtime.model_concurrency` | `0` | RT-02 |
| `runtime.background_reserve` | `1` | RT-02 |
| `runtime.encoder_batch_max` | `32` | RT-02 |
| `runtime.encoder_batch_wait_ms` | `5.0` | RT-02 |
| `runtime.backlog_coalesce_items` | `200` | RT-02 |
| `runtime.backlog_coalesce_age_s` | `60.0` | RT-02 |
| `runtime.backlog_max_items` | `2000` | RT-02 |
| `runtime.backlog_max_age_s` | `900.0` | RT-02 |
| `runtime.backlog_max_total_items` | `0` | RT-02 |
| `runtime.backlog_retry_after_s` | `1.0` | RT-02 |
| `runtime.read_p95_budget_s` | `null` | RT-02 |
| `runtime.lag_p95_budget_s` | `null` | RT-02 |

### Economy

| Key | Default | Package |
|---|---|---|
| `economy.ledger_v2` | `false` | W2-12 |
| `economy.budget` | `null` | W2-12 |

### Prompts

| Key | Default | Package |
|---|---|---|
| `prompts.layout_v2` | `false` | W2-07 |
| `prompts.layout_partials` | `[same_language, coarse_date]` | W2-07 |

### Memory-type policies

| Key | Default | Package |
|---|---|---|
| `memories.prospective.policies.v2` | `false` | O4-10 |
| `memories.procedural.policies.v2` | `false` | O4-10 |
| `memories.shared.policies.observer_views_v2` | `false` | O4-11 |


## 3. Migration notes

- **Old stores need an explicit migration (ADR-067, G-23).** `memspine migrate SOURCE TARGET` replays the retained event
  log through versioned upcasters into a fresh SQLite database. The source is never written. A limited rebuild is
  advertised: the report lists the derived layers replay does not rebuild. A store with a rolling log fails with
  `RebuildUnavailableError` rather than rebuild silently (ER-27). There is no runtime compatibility layer.
- **SQLite Alembic 0006 to 0010**, applied after 0001-0005: `0006_lineage_table`, `0007_record_envelope`, `0008_outbox`,
  `0009_tri_temporal`, `0010_w202_stamping` (`src/memspine/services/storage/sqlite/alembic/versions/`).
- **Postgres tree pg0001 to pg0010** is a separate Alembic tree (`storage.profile: server` needs `storage.url`; pgvector
  when installed). It mirrors the SQLite chain, `pg0001_baseline` through `pg0010_w202_stamping`
  (`src/memspine/services/storage/postgres/alembic/versions/`). Both backends pass the same store contract tests.
- **Embedding profile pin (MD-02).** `embedding.profile` (`gpu` or `cpu_multilingual`) fills provider, model, dim and
  query instruction. With `embedding.pin_profile: true` the resolved profile (model, dim, dtype, instruction,
  normalisation, version hash) is written beside the store (`<db>.embedding-profile.json`, and into the Lance table
  metadata), and opening the store with a different profile is refused. To accept a change set
  `embedding.migrate_profile: true` (needs `embedding.versioned_spaces`) and run `VersionedSpaceStore.reembed` (F-03) to
  move the old records. Off by default: nothing is written or checked.
- **uv.lock.** Refreshed at FU-REL for the new `otel` extra (T5-06). The diff is the extra's metadata only; no package
  versions moved. `uv sync --frozen` per-run venvs (`evals/pinned_run.sh`) use it.

## 4. Measured / pending measurement

The GPU is paused (owner decision 2026-10-13). `docs/v2/SCREENS.md` states that nothing has been run and every status is
`queued`. The sample-first workflow applies: `MODE=sample`, then `focus`, then one `final` run of `promote_candidate`
screens only. **No flag is measured.** A 60-80 question sample ranks arms but cannot carry an adoption decision.
Flags with no row below (storage, trust, retention, runtime, prompts, observability keys) are covered by
byte-identical-off unit tests only and have no quality screen queued. Rule: PROMOTE = candidate for default, NONINF =
non-inferiority, PARITY = must match the reference, REPORT = descriptive. Sets: f = LoCoMo focus 437, o = OP-Bench dev 331,
F = LoCoMo full 1540, b = blind MAB-CR + ConvoMem.

| Screen | Package | Flag or arm | Rule | Sets | Status |
|---|---|---|---|---|---|
| S01 | P0-06 | (thinking fix; existing arm) | PROMOTE | f | pending |
| S02 | P0-06 | (thinking fix; existing arm) | PROMOTE | f | pending |
| S03 | P0-06 | (window_extract; existing arm) | PROMOTE | f | pending |
| S04 | P0-06 | (thinking fix; existing arm) | PROMOTE | f | pending |
| S05 | P0-02 | (none: reproduction run) | PARITY | F | pending |
| S06 | P0-07 | query_contract=heuristic (existing arm) | PROMOTE | Fb | pending |
| S09 | P0-01 | (none: same-commit focus reference) | REF | f | pending |
| S10 | R3-01 | read.tiers_v2 | NONINF | f | pending |
| S11 | R3-02 | read.contract_rules | PROMOTE | fFb | pending |
| S12 | R3-03 | read.router_rules | NONINF | f | pending |
| S13 | R3-05a | read.entity_leg_v2 | PROMOTE | fFb | pending |
| S14 | R3-06 | read.family_fusion | NONINF | f | sample only (S14: +4/-1, net +3 on the live sample, descriptive) |
| S15 | R3-08 | read.lift_before_rerank | NONINF | f | pending |
| S16 | R3-07 | read.rerank_v2 | NONINF | fF | pending |
| S17 | R3-09a | read.exact_budget | NONINF | f | pending |
| S18 | R3-09b | read.evidence_bundles | NONINF | f | pending |
| S19 | R3-10 | read.neighbours_v2 | NONINF | f | pending |
| S20 | R3-11 | read.gate_v2 | PROMOTE | ofb | pending |
| S21 | R3-12 | read.deep_v2 | NONINF | f | pending |
| S22 | R3-16 | read.obligations | NONINF | f | pending |
| S23 | R3-13 | read.external_v2 | REPORT | o | pending |
| S24 | R3-14 | read.timeview_v2 | NONINF | fb | pending |
| S25 | R3-15 | read.cache.enabled | PARITY | f | pending |
| S26 | R3-15 | read.parallel_legs | PARITY | f | pending |
| S27 | T5-02 | read.trust_rendering | NONINF | fo | pending |
| S28 | F-01 | graph.key_v2 | NONINF | f | pending |
| S29 | F-13c | (none: eval adapter on the public API; reference built on the pre-port commit) | PARITY | f | pending |
| S30 | F-02c/R3-05b | read.legs_v2 + read.graph_leg_v2 + graph.edge_compiler_v2 | PROMOTE | f | pending |
| S31 | R3-05c | read.legs_v2 + read.episode_leg_v2 (L2 via offline.session_close_v2) | PROMOTE | f | pending |
| S32 | R3-05a | read.entity_leg_v2 (re-screen of S13 on GC) | PROMOTE | f | pending |
| S33 | O4-02 | offline.session_close_v2 | NONINF | f | pending |
| S34 | O4-08 | offline.standing_v2 + read.standing_v2 | NONINF | fo | pending |
| S35 | O4-05 | offline.patterns_v2 | NONINF | f | pending |
| S36 | R3-01 | read.tiers_v2 (re-screen of S10 on GC) | NONINF | f | pending |
| S37 | R3-03 | read.router_rules (re-screen of S12 on GC) | NONINF | f | pending |
| S38 | R3-04 | read.legs_v2 (alone; decomposes S30/S31) | NONINF | f | pending |
| S39 | F-02c | graph.edge_compiler_v2 (alone; decomposes S30) | NONINF | f | pending |
| S40 | O4-06 | offline.communities_v2 | NONINF | f | pending |
| S41 | O4-03 | offline.reconcile_v2 | NONINF | f | pending |
| S42 | O4-04 | offline.histories_v2 | NONINF | f | pending |
| S43 | W2-ref | (reference for S44/S45: gc3 + write.window_extract=facts) | REF | f | pending |
| S44 | W2-06a | write.envelope_v2 (on window_extract=facts) | NONINF | f | pending |
| S45 | W2-05 | write.segmentation_v2 (on window_extract=facts) | NONINF | f | pending |
| S46 | O4-07 | offline.profile_card_v2 (+ session_close_v2; no read consumer yet) | NONINF | f | pending |
| S47 | O4-09 | offline.decay_v2 (tiers do not change ranking, O-22) | PARITY | f | pending |
| S48 | W2-02 | write.stamping_v2 | NONINF | f | pending |
| S49 | W2-03 | write.role_policy_v2 | NONINF | f | pending |
| S50 | W2-01 | write.intake_v2 | NONINF | f | pending |
| S51 | R3-06 | read.family_fusion (focus candidate after S14 sample +4/-1; run MODE=focus after B2) | NONINF | f | pending |

The measured negatives behind the retired features in section 1 are in `evals/analysis/GAP_REGISTER.md`.

## 5. Candidates to retire at R6-02 (eval arms)

Do not delete before R6-01. Method: every `evals/arms/*.json` whose stem is not named in `evals/v2_queue.tsv`, in a
generator (`evals/gen_*.py`; `gen_qwen_stack_arms.py` emits `qs-<embedder>-<reranker>` for 4 embedders x 5 rerankers, and
`gen_b5_arms.py` emits `scr-gc3-*`), in any test, or in any Python file under `evals/` or `src/`. Not counted:
`spinetune_space.json` (a search space, not an arm) and `lme_sample72.txt` (a data list). The generated `qs-*` grid is kept
as regenerable. Docs are not counted as references.

Total: 79 candidates.

### 5a. Not referenced anywhere in code or scripts (16)

| Arm file | Still named by |
|---|---|
| `best-bridge-cue.json` | none |
| `local-n30-lexical-dates.json` | none |
| `local-n31-session-digest.json` | none |
| `local-n32-entity-expand.json` | none |
| `local-n43-cohesion.json` | none |
| `local-n52-minmax.json` | none |
| `local-n60-maxsim.json` | none |
| `qs-eq06-fix-cpu.json` | none |
| `qs-eq06-rq4b4-fix-english.json` | none |
| `qs-eq06-rq4b4-fix-ldates.json` | none |
| `qs-eq06-rq4b4-fix-session.json` | none |
| `qs-eq06-rq4b4-fix-strip.json` | none |
| `qs-eq06-rq4b4-fix-trel.json` | none |
| `scr-e03-wiki.json` | none |
| `scr-e04-assets.json` | none |
| `scr-pool-protect-lightrr.json` | none |

### 5b. Referenced only by historical `evals/run_*.sh` round drivers (63)

Retire together with the drivers that name them. The drivers are historical (RETIRED.md: rerun from the pinned commit).

| Arm file | Still named by |
|---|---|
| `_test-dates-cpu.json` | run_*.sh |
| `_test-fix-cpu.json` | run_*.sh |
| `_test-roff-cpu.json` | run_*.sh |
| `a7-engine-new.json` | run_*.sh |
| `a7-engine-old.json` | run_*.sh |
| `best-bridge-gated.json` | run_*.sh |
| `best-bridge.json` | run_*.sh |
| `best-wide-bridge.json` | run_*.sh |
| `best-wide.json` | run_*.sh |
| `local-cluster-expand.json` | run_*.sh |
| `local-depth-n02.json` | run_*.sh |
| `local-multi-intent.json` | run_*.sh |
| `local-n30-mentions.json` | run_*.sh |
| `local-n33-short-bm25.json` | run_*.sh |
| `local-n40-speaker-probe.json` | run_*.sh |
| `local-n41-blend05.json` | run_*.sh |
| `local-n41n42-blend05-ctx1.json` | run_*.sh |
| `local-n44-soft.json` | run_*.sh |
| `local-n45-infer-year.json` | run_*.sh |
| `local-n58-english-bm25.json` | run_*.sh |
| `local-n59-entity-leg.json` | run_*.sh |
| `local-n61-overlap-rank.json` | run_*.sh |
| `local-n62-temporal-weight.json` | run_*.sh |
| `local-n63-statement-probe.json` | run_*.sh |
| `local-n64-query-instruction.json` | run_*.sh |
| `local-prf.json` | run_*.sh |
| `local-rerank-base.json` | run_*.sh |
| `local-rulecards-floor-verbatim.json` | run_*.sh |
| `local-rulecards-floor.json` | run_*.sh |
| `local-rulecards-weak.json` | run_*.sh |
| `local-rulecards.json` | run_*.sh |
| `local-second-round.json` | run_*.sh |
| `local-sentence-leg.json` | run_*.sh |
| `local-session-cap2.json` | run_*.sh |
| `local-subject-leg.json` | run_*.sh |
| `local-sw-auto-mode.json` | run_*.sh |
| `local-sw-current-state.json` | run_*.sh |
| `local-sw-metadata-leg.json` | run_*.sh |
| `local-sw-standing.json` | run_*.sh |
| `local-temporal-n02.json` | run_*.sh |
| `local-w1-protected.json` | run_*.sh |
| `qs-eq06-rq4b4-fix-bal-list.json` | run_*.sh |
| `qs-eq06-rq4b4-fix-bal.json` | run_*.sh |
| `qs-eq06-rq4b4-fix-dur.json` | run_*.sh |
| `qs-eq06-rq4b4-fix-list.json` | run_*.sh |
| `qs-eq06-rq4b4-fix.json` | run_*.sh |
| `scr-agentic.json` | run_*.sh |
| `scr-dedupe.json` | run_*.sh |
| `scr-intent.json` | run_*.sh |
| `scr-latest.json` | run_*.sh |
| `scr-owner.json` | run_*.sh |
| `scr-persp-axes.json` | run_*.sh |
| `scr-persp-mult-protect.json` | run_*.sh |
| `scr-persp-mult.json` | run_*.sh |
| `scr-persp-owner.json` | run_*.sh |
| `scr-persp-w.json` | run_*.sh |
| `scr-pool-family-jina.json` | run_*.sh |
| `scr-pool-family.json` | run_*.sh |
| `scr-rctx1.json` | run_*.sh |
| `scr-rctx2.json` | run_*.sh |
| `scr-sens.json` | run_*.sh |
| `scr-storecal.json` | run_*.sh |
| `scr-userhdr.json` | run_*.sh |
