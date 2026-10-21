# Models, prompts, cost and speed

This area decides which models MemSpine v2 runs, for which roles and on which device; how prompts are written,
linted, versioned and kept cache-friendly; how structured output is obtained and repaired; and what the engine
promises about calls, tokens, latency and cost. It has 25 questions (P-01 to P-25). The owner took the
recommendation on all 25, with no combination notes. The work here is therefore mostly reconciliation with
decisions in other areas that did not take the recommendation or that set a standing rule:

- **Reranker (standing owner decision, 2026-10-11): jina-reranker-v3.5 is the reranker; no further reranker testing.**
  P-18 chose a *gated* reranker and R-14 chose "Jina v3.5 on by default". Reconciled reading: Jina is the model,
  the gate decides when it runs in the engine, and the eval harness keeps it always on.
- **G-19 chose A (library-first, inline runner)**, not the durable outbox. Background model work (windows,
  session close, sleep, re-derivation) runs in the engine process with process-local queues. This touches
  batching (P-20), time-to-searchable (P-21), deferral under budgets (P-22) and re-derivation (P-25).
- **M-02 chose "B + D"**: an explicit layer field on one record shape, with a two-layer core (raw + facts).
  Fewer derived layers by default means fewer write roles, a smaller ledger and a narrower re-derivation scope.
- **R-22 chose B (sufficiency check plus one rewrite)**: the deep read tier gains up to one model call and one
  extra search. That is a new read-side role for the tier map (P-01), the thinking policy (P-15), the
  cache layout (P-08), the call targets (P-14) and the latency budgets (P-21).
- **O-15 chose A (no observation layer)**: no consolidated-belief role, so one fewer background role.

Two facts from today change the evidence base of this area:

- **P0 prompt contamination found and fixed (commit 11853ed, 2026-10-11 21:30).** A single-word LoCoMo gold answer
  sat as a worked example in `window_extract`, `extract@session3/4`, `extract_edges` and `extract_edges@session`;
  further gold-answer words sat in `invalidate_edge`, `extract@assertions`, `plan@v2/v3`, `chat@dated3` and the
  harness reader (`evals/memspine_evals/readers.py`). The gold-lint only screened multi-word answers. The
  prompt-lint rules therefore belong in this area (P-07, P-13) and are spelt out below.
- **18:17 `uv sync` incident.** A sync in the shared venv reinstalled the stale lock, moving `huggingface_hub` to
  1.16.1 (below `datasets` 5.1.0's `>=1.31`); sentence-transformers failed to import, so every Jina and
  embedder arm failed its 2-question check (GAP_REGISTER.md:751-753). This belongs to **environment pinning
  (G-24, G-25)**, not to this area; the only consequence here is that the model fingerprint (P-12) must carry
  the model-runtime library versions so a run that loads a different `huggingface_hub`/`transformers` is not
  silently comparable.

Measured anchors used below (GAP_REGISTER.md, QWEN_STACK_RESULTS.md): clean Jina re-baseline 81.4% on 1,540
LoCoMo cat 1-4, context 2,097 tokens mean, p50 latency 764 ms (GAP_REGISTER.md:762); Qwen3.5-9B decode 115 tok/s
vs prefill 4,869 tok/s; thinking on matched off (0.806 vs 0.806) at 5.7x run time and 22x tokens; Qwen3-Embedding-0.6B
+2.1 points retrieval sufficiency over bge-small at equal wall-clock; jina-reranker-v3.5 196 pairs/s vs Qwen3-Reranker-4B
4-bit 35 pairs/s, and Jina matches the 4B on gold recall@10 (0.822 vs 0.816, GAP_REGISTER.md:716); slot loop net -3 at
about 8x wall time (GAP_REGISTER.md:726).

## Design in one page

| # | Component (v2) | Decided design | Questions |
|---|---|---|---|
| 1 | `models.tiers` | Two named tiers, **worker** (window extract, summarise, resolve, dedupe, edges, sufficiency/rewrite) and **reader** (answer, conflict judge, reflect, verify); roles map to a tier by default and can be pinned. Both tiers may bind the same model (today's single 9B). | P-01 |
| 2 | `models.placement` (providers) | Write-side roles that read raw turns stay local; the reader (and the read-side sufficiency check) may be hosted, fed only tier-gated context. Local server is the zero-config profile. | P-02 |
| 3 | `models.fallback` | Fail fast at start when an enabled feature lacks a role; per-feature `on_missing_model: error|rules|skip` (default `error`); `rules` = GLiNER2/rules path. | P-03, P-23 |
| 4 | `prompts.structured` | Schema-constrained decoding from the pydantic model on every structured call, compact-key JSON, per-item validation, one diagnostic re-ask for invalid items only, truncation detected and rescheduled. | P-04, P-05, P-06, P-11 |
| 5 | `prompts.layout` | No worked examples (shape from schema; gold-linted "X, not Y" contrasts allowed); byte-stable static prefix enforced by test; provider cache hints when a hosted reader is bound; per-call integer aliases; same-language partial; soft per-item length guidance. | P-07 to P-11 |
| 6 | `prompts.lint` | Blocking lint over every prompt file, partial and harness reader prompt (capitalised tokens, quoted words, single-word gold tokens); warning lint over rendered eval prompts and config overrides at start. | P-13 |
| 7 | `provenance.fingerprint` + run manifest | Derivation fingerprint on every derived record and cache key (rendered static prompt hash, schema version, model id, quantisation, decoding settings, input block set, model-runtime library versions); per-run manifest shared with G-25. | P-12 |
| 8 | `economy.targets` | Write targets as regression gates (≤1 call per window, static prompt ≤500 tokens, output ≤2x input, sleep ≤0.2 calls per turn); plus a read target for the deep tier (≤1 extra call, ≤1 extra search). Hard budget optional, off by default. | P-14 |
| 9 | `models.reasoning` | Thinking off for write, answer and every hot-path call (including sufficiency); allowed with a token cap for background conflict judge, consolidation merges and reflect, only after a screen. | P-15 |
| 10 | `prompts.compress` | Optional write compressor port (LLMLingua-2), off by default; read-side assembly compression unchanged (opt-in). | P-16 |
| 11 | `retrieval.encoders` | Embedder: CPU multilingual ONNX default; GPU + `[st]` profile uses a 0.6B instruction embedder with Matryoshka truncation; the choice is pinned per store. Reranker: **jina-reranker-v3.5**, run behind a pre-run gate (flat score spread or pool over budget); harness forces always-on. | P-17, P-18 |
| 12 | `runtime.planner` | One device plan at start (detect devices and VRAM, place LLM > reranker > embedder by priority, reserve LLM context memory, report in `describe()`). | P-19 |
| 13 | `runtime.scheduler` | Parallel read legs; namespaces concurrent, ordered within a namespace; one global model semaphore matched to server slots; all in-process (G-19 A). | P-20 |
| 14 | `economy.slo` | Hot-path budgets (write p95 ≤250 ms, fast-tier read p95 300 ms without rerank (CPU), 350 ms with Jina (GPU), on stated hardware and concurrency, R-Q3, ER-39) plus a time-to-searchable target for derived items measured in-process; backlog age recomputed from persisted "underived" markers after a restart. | P-21 |
| 15 | `economy.ledger` | Uniform per-layer, per-tier, per-namespace ledger (calls, tokens, retries, failures, grounded/ungrounded, queue delay), persisted as events; optional budgets that defer (mark underived, never drop); money estimate from a price table. | P-22 |
| 16 | `write.prepass` | GLiNER2 pre-pass per window (entities, dates, sensitivity, importance label) as hints; skips windows with nothing extractable; off when `[ner]` is absent. | P-23 |
| 17 | `compose.prefix` | No KV-cache memory store; stable blocks first so server prefix caches hit. | P-24 |
| 18 | `models.upgrade` | Versioned vector spaces (shadow index, atomic switch) plus selective re-derivation of records whose fingerprint is stale, run as a resumable in-process scan. | P-25 |

## Decisions

### P-01 One model or a model per role: C Two tiers, small worker, large reader

- **Decision**: two named tiers. Worker: `window_extract`, `summarize`, `resolve_entity`, `dedupe`,
  `extract_edges`, `invalidate_edge`, `summarize_entity`, `query_rewrite`, and the new deep-read `sufficiency`
  check (R-22 B). Reader: `chat` (answer), `judge` (conflict adjudication), `reflect`, `verify_answer`. No
  observation role (O-15 A). Any role can be pinned to a named model; both tiers may point at one model.
- **Verdict**: needs-measurement. Recommendation holds with every other answer; RV15 requires a per-role
  extraction screen before a model smaller than the 9B becomes the worker default.
- **What will happen**: no change while both tiers bind Qwen3.5-9B. Moving the worker to a 1-4B model cuts write
  cost and decode time (estimate 2-4x faster per window) with an unknown grounding cost; reader quality unchanged.
  Touches G (all types) and D, K_C through the write roles.
- **Where**: `config/schema.py:193` `LLMRoleConfig`, `config/schema.py:232` `LLMConfig`, `prompts/roles.py:11`
  `PROMPT_ROLES` -> `models.tiers` (a `TierMap {worker, reader}` resolved before role binding).
- **Depends on / affects**: R-22 (adds `sufficiency` to the worker tier), O-15 (no observation role), M-02 (two-layer
  core needs only window extract plus resolve on the worker), P-02, P-15, P-22 (ledger per tier).
- **Migration**: `llm.roles` stays valid; a role with no binding inherits its tier, a tier with no binding inherits
  `llm.default`.
- **Test or gate**: worker-tier screen per role (grounding rate, items per window, sections dropped) on the
  window arm; no role moves to a smaller model without it.

### P-02 Local-first or hosted model default: D Split by sensitivity

- **Decision**: write-side roles that read raw turns stay local (BD-5); the reader may be hosted and sees only
  tier-gated context. The R-22 sufficiency check sees the same evidence as the reader, so it falls under the
  reader's rule (may be hosted, tier-gated). Local server (A) is the zero-config profile.
- **Verdict**: ok.
- **What will happen**: raw transcripts never leave the host; a deployment can buy reader quality (estimate
  +2-6 points on reader-bound categories such as open-domain, where C11 puts the 9B ceiling at about 88% with all
  gold present). Cost rises only with a hosted reader. Touches G, R.
- **Where**: `services/llm/tier_gate.py:110` `TierGatedLLM`, `services/llm/base.py` routing -> `models.placement`
  (a per-tier `locality: local|remote` with the tier gate enforced for every `remote` tier).
- **Depends on / affects**: P-01, P-08 (cache hints only matter when remote), P-19, G-06 (firewall), R-22.
- **Migration**: today's configs map to "both tiers local"; `consent.remote_llm_max_tier` keeps its meaning.
- **Test or gate**: unit test that a worker-tier role bound to a remote provider fails start unless explicitly
  allowed; tier-gate test on the sufficiency prompt.

### P-03 Behaviour when no model is configured: D Fail fast, with explicit fallback key

- **Decision**: missing role for an enabled feature raises `ConfigError` at start; per feature
  `on_missing_model: error|rules|skip`, default `error`. `rules` runs the GLiNER2/rules path (P-23 option C as the
  degraded mode); `skip` disables the feature and reports it in `describe()`.
- **Verdict**: ok.
- **What will happen**: no silent empty derived layers; edge devices and CI choose degradation on purpose. No
  quality, latency or cost change on the default path. Touches D (semantic, episodic).
- **Where**: `engine.py:13705-13713` (window_extract role check) -> `models.fallback` (one start-time check over
  every enabled feature).
- **Depends on / affects**: D-03 slim core, G-21 (no-model base profile), P-23.
- **Migration**: today's behaviour equals `error`; no config change.
- **Test or gate**: start-up matrix test: each feature x {bound, unbound} x {error, rules, skip}.

### P-04 Structured output method: C Schema-constrained decoding by default

- **Decision**: json_schema / GBNF grammar built from the pydantic model (with P-06 compact aliases) on every
  structured call; prompt-only fallback when a backend refuses; per-item validation still runs (P-05) and
  truncation is detected (P-11).
- **Verdict**: needs-measurement. Ollama, vLLM and llama.cpp all accept a schema, but strict grammars can push small
  models into empty or truncated lists (RV11).
- **What will happen**: removes the parse-failure class (`sections_dropped`); estimate +0-2% decode time for grammar
  sampling; risk of fewer items per window if the grammar is too strict. Touches D (semantic, episodic,
  associative).
- **Where**: `services/llm/base.py:43` (json-repair safety net, kept as last resort),
  `config/schema.py:218-229` (`constrained_retry`, default off) -> `prompts.structured` (`StructuredCall(schema,
  aliases, grammar=True)`).
- **Depends on / affects**: P-05, P-06, P-11, P-01 (worker model must handle grammars).
- **Migration**: `constrained_retry` becomes the default first attempt; the flag is retired.
- **Test or gate**: window-arm screen, constrained vs prompted: sections dropped down, items per window and
  grounding rate not worse beyond the band.

### P-05 Repair after an invalid reply: D Per-item validation, then targeted re-ask

- **Decision**: validate each item independently, keep valid items at once, re-ask once for invalid items with the
  validator errors, forbid new items in the re-ask and diff against the failed output; persist failure reason and
  retry eligibility on the source.
- **Verdict**: ok.
- **What will happen**: fewer lost items (estimate: most of today's dropped sections recovered); a second call on a
  minority of windows (estimate under 5% with constrained decoding). Touches D.
- **Where**: `workers/windows.py:618-639` (fail-closed per section, auto split),
  `config/schema.py:225` `retry_on_error` -> `prompts.structured.repair`.
- **Depends on / affects**: P-04, P-11, P-22 (retries counted), P-14 (retries count against the call target),
  W-15 (item envelope with ids).
- **Migration**: window_stats counters keep their names; `sections_dropped` becomes `items_dropped` plus
  `items_repaired`.
- **Test or gate**: unit tests with a fake LLM returning one bad item in five; gate: items dropped down, no new
  ungrounded items.

### P-06 Output serialisation format: C Compact keys JSON

- **Decision**: JSON everywhere with one- or two-letter keys mapped to full names in code; YAML output retired.
- **Verdict**: ok.
- **What will happen**: decode dominates local call time (115 tok/s decode vs 4,869 tok/s prefill), so output
  tokens drop (estimate 15-30% vs full-key JSON); one parser and one repair path. Small models see less
  self-describing keys, offset by the schema. Touches D.
- **Where**: `prompts/base.py:8` (`format: yaml` per prompt), `window_extract.yaml:4` -> `prompts.structured`
  (alias table per schema).
- **Depends on / affects**: P-04, G-W49 prompt v2 (already compact).
- **Migration**: `extract@session*` YAML prompts rewritten; old prompts kept under legacy names for replay.
- **Test or gate**: golden render tests; output-token count on the window arm recorded in the manifest.

### P-07 Examples in prompts: B No examples, shape only

- **Decision**: no worked input/output pairs anywhere; shape comes from the schema and constrained decoding;
  short "X, not Y" contrasts inside rules are allowed only with invented content and only if they pass P-13.
- **Verdict**: ok. Today's P0 contamination (11853ed) is direct evidence: every leaked word sat in an example.
- **What will happen**: removes the main contamination path and the copy risk for small models; shorter static
  prefix (estimate -50 to -150 tokens per prompt). Quality neutral if P-04 holds. Touches D (all types).
- **Where**: `prompts/defaults/*.yaml` (window_extract, extract@session3/4, extract_edges, invalidate_edge,
  extract@assertions, plan@v2/v3, chat@dated3) and `evals/memspine_evals/readers.py` -> `prompts.layout`.
- **Depends on / affects**: P-04, P-13.
- **Migration**: strip examples from every default prompt; prompt versions bump; contaminated runs (GC screens
  88294e0) stay relative-only.
- **Test or gate**: lint rule "no example block" in shipped prompts; window-arm screen shows no drop.

### P-08 Cache-friendly prompt layout: C Byte-stable prefix plus provider cache hints

- **Decision**: the system part may not reference a variable; a unit test renders every prompt under two contexts
  and asserts identical prefixes; mode switches (window sections) move to the user part; cache hints sent when a
  hosted provider is bound; llama.cpp/vLLM prefix caching kept on. Applies to the R-22 sufficiency prompt and the
  reader as well (same rule as R-26 D).
- **Verdict**: ok.
- **What will happen**: small local gain (prefill about 10% of call time, G-W45); large saving on hosted calls
  (estimate 50-90% input-token discount on the cached prefix). Touches C (working).
- **Where**: `prompts/defaults/window_extract.yaml` (`{{ sections | join }}` in the system text),
  `services/llm/litellm_llm.py` -> `prompts.layout` + `models.placement` (hint injection per provider).
- **Depends on / affects**: P-02, P-24, R-26.
- **Migration**: move section lists into the user turn; prompt versions bump.
- **Test or gate**: prefix-stability unit test over all prompts; manifest logs prefix-cache state with every
  latency claim (RV10).

### P-09 Identifiers shown to the model: B Per-call integer aliases, strict

- **Decision**: every record or line shown gets a per-call alias (`[3]`, `[k3]`); a reply citing an unshown alias
  is rejected and counted separately from uncited facts; entity names may still appear and are resolved in code.
- **Verdict**: ok. Matches R-20 D on the read side.
- **What will happen**: zero invented ids; invented aliases become visible instead of looking like missing
  citations. Touches D (semantic, associative).
- **Where**: `workers/windows.py` (`[n]` line refs, `facts_uncited` counter) -> `prompts.layout.aliases`.
- **Depends on / affects**: P-08, R-20, G-W4/G-W49 known-item block.
- **Migration**: `[n]` stays as the line alias; new `[kN]` prefix for known records.
- **Test or gate**: G-W36 gate: zero invented ids on the window arm; unit test for alias rejection.

### P-10 Language of stored items: B Same language, shared partial

- **Decision**: one partial in every write prompt: write each item in the language of its lines; never translate
  names, titles or quoted words; keys and enums stay English.
- **Verdict**: needs-measurement. Depends on a multilingual embedder (P-17), and RV15 requires English output to stay
  unchanged on the focus slice.
- **What will happen**: non-English stores keep names and quoted words; English unchanged if the screen passes.
  Touches D (semantic, episodic).
- **Where**: `prompts/_partials/*.j2` (no language rule today), `config/presets/non_english.yaml` ->
  `prompts.layout.language` partial.
- **Depends on / affects**: P-17, P-13 (partials must be linted).
- **Migration**: partial added; prompt versions bump.
- **Test or gate**: focus-slice screen with the partial on, paired against the reference: no net loss beyond band.

### P-11 Length rules for items and outputs: D Soft guidance plus truncation detection

- **Decision**: "one sentence, about 40 words; split longer statements" as guidance, validator warns and counts but
  does not drop; list members kept; `finish_reason=length` detected and the remaining lines rescheduled as a new
  window.
- **Verdict**: ok.
- **What will happen**: shorter items, no silent coverage loss (RV11). Touches D.
- **Where**: `workers/windows.py:116-118` (temperature 0, max_tokens), prompt `token_budget` -> `prompts.structured`
  (truncation detector) and `write.windows` (reschedule).
- **Depends on / affects**: P-05, P-04, G-19 A (rescheduled windows live in the process-local queue).
- **Migration**: counters `items_over_length`, `truncated_rescheduled` added.
- **Test or gate**: G-P3 gate: grounding unchanged, mean fact length down.

### P-12 Prompt and model identity in provenance: C Derivation fingerprint plus run manifest

- **Decision**: hash of rendered static prompt + schema version + model id + quantisation + decoding settings
  (temperature, thinking flag, grammar on/off) + input block set + model-runtime library versions
  (`transformers`, `huggingface_hub`, `sentence-transformers`, `onnxruntime`, `llama-cpp`), stored on each
  derived record and in every cache key; the per-run manifest is the same object as G-25 D.
- **Verdict**: ok. The library versions are added because of the 18:17 incident (the environment fix itself is
  G-24's).
- **What will happen**: exact cache invalidation and re-derivation targeting; slightly larger records (about
  32 bytes). Touches K_C (all derived types).
- **Where**: `prompts/registry.py:100-126` (version + partials fingerprint), `core/records.py:111-122`
  `SourceInfo.prompt_version`, `workers/windows.py:648-667` (window cache key) -> `provenance.fingerprint`.
- **Depends on / affects**: P-25, P-22, G-24, G-25.
- **Migration**: records without a fingerprint get `legacy:<prompt_version>` and count as stale for P-25.
- **Test or gate**: unit test: changing model id, temperature or grammar flag changes the key; manifest check in
  `evals/run.sh`.

### P-13 Gold-lint scope and strictness: C Every token, plus rendered prompts and overrides

- **Decision (prompt-lint rules)**:
  1. Blocking, in CI: every file under `prompts/defaults/`, `prompts/_partials/` and every harness reader prompt
     (`evals/memspine_evals/readers.py`) is screened.
  2. Every capitalised token, every quoted word and every single word (not only multi-word answers) is checked
     against the benchmark answer and speaker vocabularies of every in-scope set (LoCoMo, OP-Bench, MAB-CR,
     ConvoMem, PrefEval).
  3. Allow-list of invented names (Ilse, Bram) and of instruction words; additions need a reason in the file.
  4. The standing deny-list carries the single words found on 2026-10-11 (11853ed) so CI catches them with no
     dataset on disk.
  5. No example blocks in shipped prompts (P-07); contrasts are linted like any other text.
  6. Warning, at run time: rendered prompts in eval runs and config overrides at `start()` are linted; an eval run
     with a hit is marked contaminated in its manifest.
  7. Any lint change re-screens the arms that used the affected prompts; contaminated results stay relative-only.
- **Verdict**: ok.
- **What will happen**: closes the gap that let 11853ed's words through; some false positives to allow-list. No
  runtime cost. Touches D (all types).
- **Where**: `tests/unit/test_prompt_gold_lint.py:47` (deny-list), `:103` `find_leaks`, `:190` -> `prompts.lint`
  (CI + start-time hook).
- **Depends on / affects**: P-07, G-01, G-25 (contamination flag), RV12.
- **Migration**: extend the existing test; add the harness reader path and the partials directory.
- **Test or gate**: lint green on all shipped prompts; a planted single-word gold token in a partial must fail CI.

### P-14 Write-side call and token targets: D Targets as gates, optional hard budget

- **Decision**: write targets (≤1 call per window for all online layers, static prompt ≤500 tokens, output ≤2x
  input, sleep ≤0.2 calls per turn amortised), counted with retries and split calls; plus one read-side target from
  R-22: deep tier ≤1 extra model call and ≤1 extra search per read. Hard budget optional, off by default (same
  mechanism as W-26 D and P-22 C).
- **Verdict**: ok.
- **What will happen**: the economy stays visible; with M-02's two-layer core the online target is easy to meet
  (offline estimate 0.192 calls per turn on LoCoMo, GAP_REGISTER.md:724). Touches D.
- **Where**: `engine.py:13788` `window_stats`, `engine.py:13257` `model_usage` -> `economy.targets` (checked in the
  run manifest).
- **Depends on / affects**: P-12, P-22, W-26, R-22, M-02.
- **Migration**: no behaviour change; targets enter the manifest check.
- **Test or gate**: manifest check fails a run that exceeds a target without a recorded waiver.

### P-15 Thinking mode per role: B Per role default

- **Decision**: thinking off for every write role, the answer and every hot-path read call, including the R-22
  sufficiency check; allowed with a token cap for background roles where a wrong decision persists: conflict
  `judge` (O-12), entity resolution confirmation (O-11) and `reflect`. There is no observation role (O-15 A).
- **Verdict**: ok. "judge" here is v1's conflict-adjudication role, not the read-side sufficiency check.
- **What will happen**: no change on answers (measured 0.806 vs 0.806, at 5.7x time and 22x tokens with thinking);
  background merges may improve (unmeasured). Touches G (semantic, reflective).
- **Where**: `services/llm/litellm_llm.py:34` `default_no_think`, `config/schema.py:211-215` -> `models.reasoning`
  (per-role `thinking: off|capped(N)`).
- **Depends on / affects**: P-01, R-22, O-11, O-12.
- **Migration**: default `no_think` behaviour unchanged; capped thinking is opt-in per role.
- **Test or gate**: before enabling on any background role, a paired screen on conflict cases (truncations counted).

### P-16 Compression before model calls: B Opt-in write compressor port

- **Decision**: a pluggable compressor before call 1 (LLMLingua-2 on CPU or GPU), off by default, for long-turn
  workloads only (documents, tool output); read-side assembly compression stays opt-in as today.
- **Verdict**: ok.
- **What will happen**: no default change; on long inputs, fewer tokens (estimate 20-40%) at some risk to verbatim
  span grounding. Touches C (working, episodic).
- **Where**: `core/policies/compression.py:56` `ASSEMBLY_STAGES` -> `prompts.compress` (`Compressor` port).
- **Depends on / affects**: P-14, P-19 (device), G-19 A (in-process).
- **Migration**: none.
- **Test or gate**: G-W31 gate per workload: grounding rate unchanged, tokens down.

### P-17 Default embedding model and host: C Hardware-detected profile

- **Decision**: CPU default is a multilingual small ONNX embedder; with a GPU and `[st]`, a 0.6B instruction
  embedder (Qwen3-Embedding class) with Matryoshka truncation. The choice is pinned per store at creation and only
  changes through P-25.
- **Verdict**: needs-measurement. The multilingual CPU model has no screen against bge-small yet.
- **What will happen**: GPU stores gain (+2.1 points retrieval sufficiency measured at equal wall-clock);
  non-English stores become usable; risk of two vector spaces across deployments. Touches R (episodic, semantic).
- **Where**: `config/schema.py:94-105` `EmbeddingConfig` (fastembed bge-small), `engine.py:1051` (dimension mismatch)
  -> `retrieval.encoders.embedder` (profile picked by `runtime.planner`).
- **Depends on / affects**: P-10, P-19, P-25, D-03, G-21.
- **Migration**: existing stores keep bge-small (pinned); new stores take the profile.
- **Test or gate**: retrieval-only screen (free) of the multilingual candidate vs bge-small on LoCoMo and ConvoMem;
  adopt only if not worse beyond the band.

### P-18 Reranker model and when to run it: C Gated reranker (model fixed: jina-reranker-v3.5)

- **Decision (combined with the standing owner decision and R-14 B)**: jina-reranker-v3.5 is the one reranker
  model; no further model testing. In the engine it is configured by default whenever `[st]` is installed, and a
  **pre-run gate** decides per read whether it runs: run when the fused-score spread of the top candidates is flat
  or the candidate pool exceeds the context budget; otherwise keep the fused order. The **eval harness sets the gate
  to `always`**, so every benchmark run reranks, as the clean 81.4% reference did. Without `[st]` the reranker is
  off and `describe()` says so. It scores hits only, windows expanded afterwards (R-15 A).
- **Verdict**: ok-combined. Two open points: the gate thresholds need calibrating, and the weights' licence
  (CC-BY-NC-4.0, `trust_remote_code`) sits awkwardly with an Apache-2.0 engine default (see the conflicts table).
- **What will happen**: in the harness, no change from the reference (always on). In deployments, rerank time
  (+30-35% wall-clock when it runs) is paid only on ambiguous reads; reader tokens fall 35-50% when it runs.
  Touches R (working).
- **Where**: `config/schema.py:333` (`rerank: off`), `:436` `rerank_gate`, `:433` `rerank_blend`;
  `engine.py:5763` (today's gate is post-run: the reranker runs and its result is discarded when unconfident, so
  it saves no compute); `engine.py:11676` `_rerank_provider`; `services/rerank/jina_rerank.py` ->
  `retrieval.encoders.reranker` with `RerankGate(mode: always|gated|off)`.
- **Depends on / affects**: R-14 (model and default), R-15, R-16 and R-17 (they act on "the reranked score"; when the
  gate skips, the fused score stands in), R-21, P-19 (GPU placement), G-25 (manifest records the gate mode), B11
  `rerank_check` (must count gated skips, not treat them as failures).
- **Migration**: `rerank: jina` with `rerank_gate_mode: gated` becomes the `[st]` default; the post-run
  `rerank_gate` float is kept as a second, confidence-based skip.
- **Test or gate**: harness asserts `gate=always` and rerank calls > 0; a gated-vs-always paired run on the focus
  slice must be within the band before the gated default ships.

### P-19 GPU and CPU placement of models: C Device planner

- **Decision**: one placement step at start: detect devices and VRAM, place models by a priority list (generative
  model with its context memory first, then jina-reranker-v3.5, then the embedder), fall back to CPU for whatever
  does not fit, report the plan in `describe()` and the manifest.
- **Verdict**: ok. With G-19 A every model shares the engine process (or the local LLM server), which makes a
  single plan more necessary, not less.
- **What will happen**: no out-of-memory surprises (C11: 14B reader + 9B judge + embedder/reranker about 4 GB exceed
  16 GB); reproducible speed claims. Touches R (all types).
- **Where**: `config/schema.py:462` `rerank_device`, embedding `device`, Ollama outside the process ->
  `runtime.planner` (`DevicePlan {component: device, vram_mb}`).
- **Depends on / affects**: P-02, P-17, P-18, P-16, P-23, G-19.
- **Migration**: explicit device keys still win over the planner.
- **Test or gate**: planner unit tests with faked device inventories; plan recorded in every run manifest.

### P-20 Batching and concurrency of model work: C Parallel read legs plus global model semaphore

- **Decision**: read legs run concurrently (gather); namespaces processed concurrently, ordered within a
  namespace, under one model concurrency limit matched to the server's slots; the per-namespace lock is shared
  with `sleep()` (G-W38). All of this is in-process asyncio under G-19 A.
- **Verdict**: ok. G-19 A rules out multi-instance coordination; one process per store is the supported shape.
- **What will happen**: lower read latency (estimate 20-40% on multi-leg reads); multi-user write throughput up;
  more DB connection pressure. Touches D, R (episodic, semantic).
- **Where**: `workers/windows.py:474` `_drain` (ordered queue per namespace), `engine.py:2339-2780` (legs run one
  after another), `config/schema.py:130` `batch_size` -> `runtime.scheduler`.
- **Depends on / affects**: P-19, P-21, G-19, O-02.
- **Migration**: default semaphore = 1 reproduces today's order.
- **Test or gate**: unit test that results are identical with gather vs sequential; per-namespace order test under
  concurrency.

### P-21 Latency budgets and time-to-searchable: C Hot-path budgets plus time-to-searchable target

- **Decision**: write p95 ≤250 ms and fast-tier read p95 300 ms without rerank (CPU), 350 ms with Jina (GPU), on stated hardware and concurrency (R-Q3, ER-39); raw turn durable
  at commit, with time-to-searchable per index measured as an SLO (ER-06); derived items p95 ≤N s at stated load with queue delay and backlog age exported. The deep tier (R-22
  B) gets its own budget: reader time + at most one worker call + one search. Under G-19 A the queue is in memory,
  so backlog age is recomputed after a restart from persisted "underived" markers (W-26, O-10) rather than read
  from a durable queue.
- **Verdict**: conflict (G-19). The recommendation assumed durable queue telemetry (G-W27); with the inline runner
  a crash loses queued work and its timestamps.
- **What will happen**: freshness becomes visible; after a crash, derived items for in-flight windows arrive only
  when the start-time scan re-queues them, so p95 time-to-searchable has a restart tail. Touches D (working,
  episodic).
- **Where**: `workers/windows.py:492` `flush`, `:515` `close_session` -> `economy.slo` + `write.windows` start-time
  reconcile.
- **Depends on / affects**: G-19, P-20, W-26, O-10, R-22, G-25.
- **Migration**: no default change; budgets enter the manifest.
- **Test or gate**: kill-and-restart test: every underived source is re-queued and derived; p95 reported with the
  restart tail separated.

### P-22 Cost ledger scope and spend limits: C Persisted ledger plus optional budgets

- **Decision**: the same counters (calls, input/output tokens, retries, failures, grounded/ungrounded, queue
  delay) for every derived layer (keyed by M-02's explicit layer field), every tier (worker/reader, including the
  deep-read sufficiency call) and every namespace; persisted as events; OpenTelemetry export optional (G-20);
  budgets per namespace and layer that defer by marking sources underived, never drop; money from a price table.
- **Verdict**: ok. Deferral stays durable under G-19 A because the "underived" mark is a persisted record state,
  not a queue entry.
- **What will happen**: comparable per-layer costs; bounded spend for paid providers. Tokens no longer estimated
  as chars/4 where the provider reports usage. Touches K_C (all types).
- **Where**: `engine.py:13257` `model_usage`, `engine.py:13788` `window_stats` -> `economy.ledger`.
- **Depends on / affects**: P-12, P-14, W-26, G-20, G-25, M-02.
- **Migration**: in-process counters become views over the persisted ledger.
- **Test or gate**: ledger totals equal the manifest totals on a harness run; budget test shows sources marked
  underived and later derived.

### P-23 Role of small local extractors: B GLiNER2 pre-pass for every window

- **Decision**: GLiNER2 extracts entities, dates, sensitivity and an importance label before call 1; the LLM gets
  them as hints and windows with nothing extractable are skipped. Off when `[ner]` is absent; GLiNER2-only output
  (option C) is the `rules` degraded mode of P-03.
- **Verdict**: needs-measurement. Hint errors can bias extraction and skipped windows can lose facts.
- **What will happen**: fewer and shorter LLM calls (estimate 10-30% of windows skipped on chat data); risk of
  missed facts in skipped windows. Touches D (semantic, associative).
- **Where**: `services/decision/gliner2_decision.py`, `core/window.py:108-173` (segmenter) -> `write.prepass`.
- **Depends on / affects**: P-03, P-14, P-19, W-11, ADR-052.
- **Migration**: none (new opt-in stage).
- **Test or gate**: window-arm screen with and without the pre-pass: grounded facts not down beyond band, calls
  down.

### P-24 Activation (KV-cache) memory: B Stable-block prefix reuse

- **Decision**: no persisted KV store; order stable context (profile card, standing answers) first and keep it
  byte-identical across turns so server prefix caches hit.
- **Verdict**: ok. Scope "later".
- **What will happen**: small latency gain on repeated reads; no model-locked store for `forget` to reach (RV21).
  Touches C (working).
- **Where**: `services/llm/llama_cpp.py:27` (n_ctx 8192, no prefix persistence), composition order -> `compose.prefix`.
- **Depends on / affects**: P-08, P-02, R-18, R-20.
- **Migration**: none.
- **Test or gate**: prefix-stability test extended to composed reader context.

### P-25 Model upgrades and re-derivation: C Versioned vector spaces plus selective re-derivation

- **Decision**: each vector carries its embedder id; a new embedder builds a shadow index in the background and
  switches atomically; derived records with a stale fingerprint (P-12) are re-derived from raw turns, lazily or on a
  schedule, oldest-used last, old versions kept until replaced. Under G-19 A this is a resumable in-process scan
  (stale = fingerprint differs), so a restart simply resumes; with M-02's two-layer core and no observation layer
  (O-15 A) the scope is mainly facts and edges.
- **Verdict**: ok.
- **What will happen**: memory improves with models; temporary double storage; re-derivation cost charged to the
  P-22 ledger and budget. Touches K_C (all derived types).
- **Where**: `engine.py:1051` (dimension mismatch only), `engine.py:13357` `rebuild` -> `models.upgrade`
  (`ShadowIndex`, `RederiveScan`).
- **Depends on / affects**: P-12, P-17, P-22, G-19, RV07 (generation checks after forget).
- **Migration**: legacy records (no fingerprint) are stale by definition; re-derive on schedule, not at upgrade.
- **Test or gate**: same-dimension embedder swap test (must not mix spaces); forget during re-derivation must not
  publish stale results.

## Interface sketch (one per major component)

```python
class TierMap(Protocol):            # models.tiers
    def model_for(self, role: str) -> ModelBinding: ...   # pinned role > tier > llm.default

class RerankGate(Protocol):         # retrieval.encoders.reranker
    mode: Literal["always", "gated", "off"]               # harness: always
    def should_run(self, fused: Sequence[float], pool_tokens: int, budget: int) -> bool: ...

class Fingerprint(NamedTuple):      # provenance.fingerprint
    prompt_hash: str; schema_v: str; model_id: str; quant: str
    decoding: str; input_blocks: str; runtime_libs: str
```

## Conflicts and open points

| id | issue | proposal | needs |
|---|---|---|---|
| P-18 / R-14 | P-18's recommendation kept the reranker off by default; R-14 B and the standing decision put Jina on by default. | Jina configured by default when `[st]` is present, run behind a pre-run gate; harness forces `always`. | Owner confirmation that "gated by default in the engine, always in the harness" is the intended reading. |
| P-18 | jina-reranker-v3.5 weights are CC-BY-NC-4.0 and need `trust_remote_code`; the engine is Apache-2.0. | Keep Jina as default only when explicitly installed via `[st]`, state the licence in `describe()` and the docs, and name a permissive fallback for commercial deployers. | needs-owner: is a non-commercial default reranker acceptable for the shipped profile? |
| P-18 | v1's `rerank_gate` is post-run and saves no compute. | Add the pre-run gate; keep the post-run threshold as a second skip. | Gate calibration screen (below). |
| P-21 / G-19 | Time-to-searchable SLO assumed a durable queue; G-19 A keeps queues in memory. | Recompute backlog from persisted underived markers at start; report the restart tail separately. | Owner awareness; revisit if G-19 moves to B. |
| P-15 / R-22 | "judge may think" could be read as covering the new sufficiency check. | Sufficiency is a hot-path worker-tier call, thinking off. | none |
| P-01 / R-22 | The tier map had no read-side worker role. | Put `sufficiency` and `query_rewrite` on the worker tier, pinnable to the reader. | Covered by the R-22 screen. |
| P-12 / G-24 | The 18:17 incident changed model-runtime libraries under a running queue. | Fingerprint and manifest carry model-runtime library versions; the environment fix itself stays with G-24. | none |

## Measurements to run before building

| what | which screen or dataset | gate |
|---|---|---|
| Worker-tier model per role (P-01) | Window arm (`scr-window-facts-jina`) on LoCoMo dev conversations | Grounding rate and items per window not worse beyond the band vs the 9B |
| Constrained decoding (P-04, with P-05/P-06) | Window arm, constrained vs prompted | Items dropped down; items per window and grounding not worse beyond band |
| Same-language partial (P-10) | 437-q focus slice paired vs f-jref2-i2 | No net loss beyond the slice band (about +-7) |
| Multilingual CPU embedder (P-17) | Free retrieval-only screen, LoCoMo + ConvoMem | Retrieval sufficiency not worse than bge-small beyond band |
| Jina gate calibration (P-18) | Focus slice, `gated` vs `always`, then full 1,540 vs clean-jina-full 81.4% | Within band; report fired rate and token saving |
| GLiNER2 pre-pass (P-23) | Window arm with and without the pre-pass | Grounded facts not down beyond band; calls down |
| Capped thinking on background roles (P-15) | Conflict and resolution cases (MAB-CR) | Gain beyond band at a stated cost before enabling |
| Deep-tier sufficiency + rewrite cost (P-14/P-21, owned by R-22) | Focus slice, deep tier vs fast tier | Within the deep-tier budget; adoption per G-01 |


## Owner follow-up (2026-10-12)

- **Q2 Durable outbox** (as proposed): Library-first inline runner draining a durable outbox in the same store; leftover work resumes on the next open; dbos and taskiq are optional drainers. *(DBOS later replaced by Temporal + embedded Resonate, ADR-071.)*
- **Q5 Jina on every read if installed** (as proposed): Jina v3.5 reranks every read wherever the model is installed; the pre-run gate is a CPU-only mode; the eval harness forces it on.
- **Q6 Jina if installed, permissive fallback** (as proposed): Jina stays for research and evaluation; the library default is 'Jina if installed', with a permissively licensed reranker named as the commercial fallback (licence check is a research task).

## Change log

- 2026-10-11: first version. Verified P-01 to P-25 (all took the recommendation) against G-19 A, M-02 B + D, R-22 B,
  O-15 A, R-14 B, R-15 A and the standing Jina decision; recorded the 11853ed contamination fix as prompt-lint rules
  (P-07, P-13) and the 18:17 `uv sync` incident as a G-24 item with a fingerprint consequence (P-12).
- 2026-10-12: owner follow-up answers recorded (Q2, Q5, Q6).
- 2026-10-13: reconciled with ER amendments and owner decisions (consistency audit)
