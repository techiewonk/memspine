# Development verification, 2026-10-11

Scope: `origin/feat/local-qwen-stack` HEAD `bc5bf30` (2026-10-11 08:40), audited read-only in a detached worktree. Sources audited: `evals/analysis/GAP_REGISTER.md` (673 lines, all tables and the progress log) and `docs/GAP_TO_SOLUTION_PLAN.md`. CPU only; no GPU, Ollama or model call was made.

**E03 and E04 are in flux.** Another agent was adding an E03 Wikipedia provider and an E04 precompute script while this audit ran; neither is on origin at `bc5bf30`. The E03/E04 rows describe only what is on that commit. **Addendum:** while the audit ran, origin advanced to `6be76ee` (adds `src/memspine/services/external/wikipedia.py`, `evals/precompute_assets.py`, arms `scr-e03-wiki`/`scr-e04-assets`, `tests/unit/test_external_wikipedia.py`, `evals/tests/test_precompute_assets.py`). Those new files were not audited row by row; their 9 new tests plus the existing E03/E04 test files were run on `6be76ee` and all pass. The 4378-test full-suite numbers below are for `bc5bf30`.

Abbreviations in the evidence column: `S/` = `src/memspine/`, `E/` = `evals/`, `EM/` = `evals/memspine_evals/`. A test reference `name (n pass)` is the number of tests from that file in the full-suite junit run. Row ids: `K:` = register section K (Codex families), `PLAN:` = plan-only findings.

Result meanings: **VERIFIED** = the keys/flags/modules named exist, tests exist and pass, keys are in `docs/USAGE.md` (engine keys) or the CLI help (harness flags); open remainders the row discloses itself are noted but do not downgrade it. **PARTIAL** = the row claims done but something it relies on is missing or untested. **CLAIMED-BUT-MISSING** = a named artefact does not exist. **STATUS-STALE** = code and register/plan disagree (the row is outdated). **NOT BUILT** = open, on hold, blocked, deferred or needs a run. **NOT A BUILD CLAIM** = measurement, decision or information row.

## Summary counts

| Result | Rows |
|---|---|
| VERIFIED | 145 |
| PARTIAL | 8 |
| CLAIMED-BUT-MISSING | 0 |
| STATUS-STALE | 17 |
| NOT BUILT | 10 |
| NOT A BUILD CLAIM | 26 |
| Total rows audited | 206 |

Note: no row is CLAIMED-BUT-MISSING at the level of a claimed build. Missing named items found: `bench_http.py` and a localhost warning (E1, listed as solution options under a 'fixed' row, recorded as PARTIAL), the key `read.relevance_margin` (I29 text; the real key is `relevance_gate_margin_sd`), the key `read.relative_dates_durations` (I79; disclosed as not done), `[inferred]` render (I48; disclosed).

## Test suite (step 5)

Command: `pytest -n 2 tests/unit evals/tests --junitxml=...` with `PYTHONPATH=<worktree>/src`, Python 3.13 venv of the main checkout, `data/locomo10.json` copied into the worktree.

| Run | tests | failures | errors | skipped |
|---|---|---|---|---|
| Standard (only `data/locomo10.json` present) | 4378 | 0 | 0 | 47 |
| Same, plus gitignored `evals/data/*` copied from the main checkout (so the data-gated tests run) | 4378 | 1 | 0 | 38 |

The one failure only appears when LoCoMo data is on disk: `tests/unit/test_prompt_gold_lint.py::test_no_locomo_gold_answer_or_speaker_in_any_prompt`. See Finding F1.

Skips in the standard run: 15 kuzu/ladybug (one native graph library per process), 7 graspologic-native/[community] extra not installed, unreachable-branch guards (flashrank, taskiq, dbos, fastembed, markitdown, [static]), 1 SQLCipher, and data-gated tests (LoCoMo expected under `evals/data`, ConvoMem, BEAM, CPB, MemoryAgentBench, LaMP, LoCoMo-Plus, PrefEval, OP-Bench, PerLTQA, PersonaBench, TOFU/MUSE). With the datasets present 9 of those run and pass; `test_review_r3.py::test_r3_1_preset_loads_1540_questions...` still skips because it hard-codes `D:\mem\memory research\memspine\evals\data\locomo10.json` (Finding F2), and LaMP, LoCoMo-Plus, OP-Bench, PerLTQA, PersonaBench stayed skipped.

## Findings that need action

- **F1 (real defect, hidden by a skip).** With LoCoMo on disk, the prompt-leak lint fails: `EM/readers.py` contains LoCoMo gold answers `"the week before 9 June 2023"` (prompt text at lines 153 and 299, `_EXAMPLE_ANSWER` at line 163) and the speaker names Caroline and John (comments, lines 196-197). The default `grounded` and `grounded_detail` prompts therefore carry a gold string; only the `*_nodate` variants (I79) remove it, and they are opt-in. CI does not catch it because `data/` is absent there.
- **F2.** `evals/tests/test_review_r3.py:38` hard-codes a `D:\mem\...` path, so that test is dead on every other machine. `tests/unit/test_prompt_gold_lint.py` and `evals/tests/test_locomo_category_counts.py` look in `evals/data/`, not `data/`; the H6 note says to copy `data/locomo10.json`, which does not un-skip them.
- **F3.** The plan and register disagree in many places (see STATUS-STALE rows): plan 1.2 says I56/I57/I76 were never screened although the progress log screened them in r7-post; the plan lists M01, A03, E06, E05 and R02 as 'in progress' although all are built; register section K still shows R02, R03, A01, A05, P01, P02, E05, V01 as open although code exists.
- **F4.** `I28`/`I38` CPU knobs (`decider_threads`, `decider_backend`, `decider_dtype`, `decider_workers`) have no test beyond the defaults golden; the bf16/ONNX parity numbers in the register are not reproducible from the suite.
- **F5.** Built but not in the register: SpineTune (`EM/spinetune.py`, `E/tests/test_spinetune.py`), GLiNER2 planner eval (`E/gliner2_planner_eval.py`, prereg G24), wrapper-threshold sweep (`E/sweep_wrapper_threshold.py`, prereg G8a). The repo log mentions them (TODO_IMPROVE G-23), the register does not.

## Row-by-row table

| gap id | claimed status | verification result | evidence | notes |
|---|---|---|---|---|
| A1 | deferred (user) | NOT BUILT | tools exist: EM/rejudge.py, EM/second_view.py | second judge deferred to wave 4 final-claims gate; no second-judge result exists |
| A2 | deterministic part implemented | VERIFIED | EM/cli.py:704 --judge-date-check; EM/date_check.py; E/rescore_dates.py; test_date_check (12 pass); test_i12_date_check (5 pass) | rescore_dates.py has no dedicated test (script); second judge part = A1 |
| A3 | measured and reported | VERIFIED | E/forensics_report.py:68 list_recall, :794 list_questions; test_measurement_gaps (22 pass) |  |
| A4 | implemented | VERIFIED | EM/cli.py:580,587 --judge-prompt mem0-official; EM/rejudge.py; test_rejudge (9 pass) | register itself says vendored prompt is not byte-verified upstream |
| A5 | errata 47 + reporting done | VERIFIED | E/analysis/locomo_errata.json (52 entries = 47 + 5 from I62); E/forensics_report.py:409; EM/errata.py; test_measurement_gaps (22 pass) |  |
| A6 | split saved | VERIFIED | E/analysis/locomo_split.json |  |
| A7 | plan written, not run | NOT BUILT | arms E/arms/a7-engine-old.json, a7-engine-new.json exist; cells r4-a7-A..D at E/run_screens_r4.sh:40-43 | 2x2 never run; needs GPU |
| A8 | implemented | VERIFIED | EM/cli.py:932 --presence-penalty, :945 --top-p, :951 --sampler-seed; test_infra_gaps (37 pass) | option 'derived Modelfile with correct defaults' is not in the repo (not claimed in status) |
| A9 | DONE, not adopted | NOT A BUILD CLAIM | E/run.sh:49 --think; result is a measurement (63.2 vs 88.8 conv-26) | no code claim |
| A10 | implemented | VERIFIED | EM/results.py:125 accuracy_ci; test_infra_gaps::test_summary_carries_a_cluster_bootstrap_ci |  |
| A11 | addressed | VERIFIED | E/prereg/2026-10-10_heldout_reader_fixes.md exists | process item |
| A12 | implemented | VERIFIED | E/forensics_report.py:738 sufficiency_on_qa_set, :382 recall_at_10_hits |  |
| A13 | implemented | VERIFIED | E/forensics_report.py:354 n_adversarial |  |
| A14 | done | VERIFIED | E/QWEN_STACK_RESULTS.md contains the 'Status of each claim' table | table content not re-audited against runs |
| A15 | decided: keep | NOT A BUILD CLAIM | decision |  |
| A16 | applied from now on | NOT A BUILD CLAIM | rule; E/noise_floor.py estimator | measured bands pending (see I16) |
| B1 | ADOPTED | VERIFIED | S/config/schema.py:871 list_mode, :880 list_trigger; E/arms/BEST_dev_2026-10-10.json; test_list_mode (21 pass); test_list_mode_round2 (38 pass) |  |
| B2 | audited (hygiene only) | VERIFIED | E/analysis/B2_EVIDENCE_AUDIT.md exists |  |
| B3 | prompt built, screen pending | VERIFIED | EM/cli.py:609 grounded_generic_infer; test_reader_open_gaps (31 pass) | built, never screened (r3c never ran) |
| B4 | tested: rejected / neutral | VERIFIED | S/config/schema.py:351 lexical_dates, :368 temporal_relative, :372 temporal_infer_year; test_temporal_relative_leg (2 pass); test_temporal_leg_v2 (17 pass) | screen verdict: lexical_dates rejected; temporal_relative neutral |
| B5 | tested: not adopted | VERIFIED | S/config/schema.py:857 lexical_strip_names; test_lexical_strip_and_session_leg (7 pass) | screened: rejected |
| B6 | tested: neutral | VERIFIED | S/config/schema.py:346 lexical_analyzer; test_tantivy_english (10 pass) | screened: neutral |
| B7 | fixed; not adopted in default | VERIFIED | rerank_balanced in engine; test_rerank_balanced (2 pass) | screened: not adopted (churn +30/-23) |
| B8 | tested: neutral | VERIFIED | S/config/schema.py:861 session_leg; test_lexical_strip_and_session_leg (7 pass) | screened: neutral |
| B9 | fixed in code; 2-conv screen | VERIFIED | S/config/schema.py:619 rerank_floor; test_retrieval_gap_fixes (14 pass) | in BEST config; full run of that single change pending per row |
| B10 | in use; rerank_context screen planned, not run | VERIFIED | S/config/schema.py:440 rerank_context, :412 rerank_keep, :566 candidate_pool; E/arms/scr-rctx1.json, scr-rctx2.json; test_rerank_blend_context (7 pass); test_rerank_keep (6 pass) | rerank_context arms never run (built, never screened) |
| B11 | implemented | VERIFIED | EM/runner.py:358 rerank_check; EM/cli.py:466-475 exit 3; test_infra_gaps::test_rerank_check (param) | 'rerank_required' engine flag (solution option) does not exist; not claimed in status |
| B12 | implemented, merged | VERIFIED | S/config/schema.py:574-575 replay_window_before/after; test_retrieval_gap_fixes (14 pass) |  |
| B13 | checked (code), no divergence | VERIFIED | test_batch_consistency (3 pass) | bt1 vs bt32 GPU measurement open |
| B14 | decided: do not run | NOT A BUILD CLAIM | decision |  |
| B15 | closed, measured negative | VERIFIED | S/config/schema.py:846 word_vector_leg; test_word_vector_leg (7 pass) | screened: negative (-12 net) |
| B16 | fixed | VERIFIED | test_word_vector_leg (7 pass) | unit test for widened fetch is in this file |
| C1 | implemented; dev neutral | VERIFIED | EM/cli.py:852 --memspine-mark-hits, :804 --verify-answer; test_mark_hits (8 pass); test_verify_answer (8 pass) | screened: neutral |
| C2 | implemented; dev neutral | VERIFIED | grounded_detail prompt; test_qa_prompts (85 pass) | screened: neutral |
| C3 | yes/no rule tested and REJECTED | NOT A BUILD CLAIM | grounded_v2 rejected (81.5 vs 86.7) | screen verdict |
| C4 | prompt built, screen pending | VERIFIED | EM/cli.py:609 grounded_generic_infer, routed_generic; test_reader_open_gaps (31 pass); test_routed_qa (72 pass) | built, never screened |
| C5 | duration annotator implemented; elapsed helper not done | STATUS-STALE | S/config/schema.py:552 resolve_durations; test_temporal_durations (24 pass) | 'elapsed helper not done' is stale: --duration-solve (I76) and --milestones (E05) now implement it |
| C6 | partly addressed; screen pending | VERIFIED | grounded_generic_list (EM/cli.py); test_reader_open_gaps (31 pass) | built, never screened |
| C7 | in test - caption rule kept in grounded_v3 (screen queued) | STATUS-STALE | EM/cli.py:607 grounded_v3 | C8 says grounded_v3 was screened (85.8 vs 86.7) and not adopted; C7 row not updated |
| C8 | tested, not adopted | VERIFIED | EM/cli.py:607 grounded_v3 | screen verdict: rejected |
| C9 | analysed, fix proposed; screen pending | VERIFIED | E/paired_regressions.py; EM/readers.py:208 DETAIL_CLAUSE | built, never screened |
| C10 | limits documented and capped | VERIFIED | EM/refusal.py:26 MAX_RETRIES; EM/cli.py:637 --retry-guard; test_refusal_retry_neutral (8 pass) |  |
| C11 | blocked by GPU memory | NOT BUILT | - | needs 14B reader + 9B judge > 16 GB; two-pass option not built |
| D1 | done | VERIFIED | EM/cli.py:970 --token-count; test_infra_gaps (37 pass) |  |
| D2 | implemented | VERIFIED | EM/cli.py:957 --server-ctx, :965 --strict-ctx; test_infra_gaps (37 pass) |  |
| D3 | implemented | VERIFIED | EM/refusal.py:381 retry_prompt_tokens; test_infra_gaps:249-250 |  |
| D4 | implemented | VERIFIED | EM/cli.py:381 capture_runtime; test_infra_gaps:256-284 |  |
| D5 | done | VERIFIED | EM/timing.py:36-45 p50/p95; EM/timing.py:32 MEMSPINE_CONCURRENT_ARMS; E/bench_llm.py |  |
| D6 | done and verified live | VERIFIED | E/run.sh (set -euo pipefail, --force :48, --topk :36) | live run not repeated here |
| D7 | implemented | VERIFIED | test_infra_gaps (37 pass) | run_id/query_id in logs (test_ingest_and_forensics_rows_carry_run_and_query_ids) |
| E1 | fixed | PARTIAL | 127.0.0.1 used in arms; E/bench_llm.py exists | solution options 'bench_http.py' and 'warn on localhost' do not exist (no bench_http.py anywhere; no localhost warning in the harness) |
| E2 | done, merged | VERIFIED | EM/readers.py:919 _shared_client (pooled per loop) |  |
| E3 | done (not run) | PARTIAL | E/ollama_env.ps1 sets FLASH_ATTENTION, KV_CACHE_TYPE, CONTEXT_LENGTH, NUM_PARALLEL, KEEP_ALIVE; S/services/_retry.py (5xx backoff) | option 'lower log level' not set in the script; script never executed (stated) |
| E4 | done | VERIFIED | EM/provenance.py:205 gpu_memory start/end; EM/runner.py:671; E/run.sh:145 expandable_segments |  |
| F1 | clean | NOT A BUILD CLAIM | audit finding |  |
| F2 | implemented | VERIFIED | EM/runner.py:211 n_quarantined; test_infra_gaps (37 pass) |  |
| F3 | implemented | VERIFIED | test_infra_gaps::test_unparseable_timestamp_is_an_error_not_now |  |
| F4 | done | VERIFIED | EM/runner.py:678 ingest_timing |  |
| F5 | implemented 2026-10-10 opt-in | VERIFIED | S/config/schema.py:481 naive_timezone, :486 session_sequence; EM/cli.py:652 --stamp-timezone; test_event_time_zones (7 pass); test_stamp_timezone (6 pass) |  |
| G1 | done | VERIFIED | uv.lock tracked (257 packages, huggingface-hub 1.16.1); justfile:40 evals-setup | no explicit huggingface-hub<2 pin in pyproject.toml (lock only) |
| G2 | implemented | VERIFIED | E/legacy_datasets; test_no_datasets_shadow (3 pass) |  |
| G3 | implemented | VERIFIED | E/run.sh:136 unset; remaining 'env -u' only in comments of superseded scripts |  |
| G4 | done | VERIFIED | .gitattributes |  |
| H1 | CI job present and checked | VERIFIED | .github/workflows/ci.yml:32-50 (evals job, uv run --with pyarrow --with jsonschema --with httpx) | stated 'not yet run on GitHub'; quoted '905 passed / 13 skipped' is stale (evals/tests alone are far larger now) |
| H2 | tests added | VERIFIED | test_forensic_schema (2 pass); test_infra_gaps (37 pass) |  |
| H3 | done (merged) | VERIFIED | feat/locomo-fixes + feat/locomo-infra merged into branch (git history) |  |
| H4 | resolved | VERIFIED | tests/unit/test_pre_wave1_read_golden.py:42 MEMSPINE_UPDATE_PRE_WAVE1_GOLDEN; test_pre_wave1_read_golden (2 pass) |  |
| H5 | FIXED | VERIFIED | EM/readers.py:916-919 WeakKeyDictionary; test_reader_raw (10 pass) |  |
| H6 | fixed | VERIFIED | pyproject.toml pythonpath=['evals']; combined run here: 4378 tests, 0 fail |  |
| H7 | fixed | VERIFIED | E/pinned_run.sh |  |
| H8 | done | VERIFIED | E/build_forensics_explorer.py:1250 reconcile | explorer output is local-only, not rebuilt here |
| I1 | fixed in harness, baseline pending | PARTIAL | EM/refusal.py:6 mode='assertive', :260 require_context_overlap; test_refusal_retry_neutral (8 pass) | (a) 'baseline pending' is stale (I2 measured 79.7%); (b) 'To do: CLI flag for assertive / valve' is still NOT done: no such flag in EM/cli.py; STATUS-STALE+PARTIAL |
| I2 | measured | NOT A BUILD CLAIM | run result |  |
| I3 | screened r3-generic: REJECT | VERIFIED | EM/cli.py:608 grounded_generic; test_generic_reader_prompts (6 pass) | screen verdict: reject (guard) |
| I4 | screened r3-intent: NEUTRAL, safe | VERIFIED | S/config/schema.py:880 list_trigger='intent'; S/engine.py:185 is_intent_list; test_intent_trigger_subject_vote (26 pass) |  |
| I5 | vote part implemented, not screened | VERIFIED | S/config/schema.py:893 speaker_vote_mode; test_intent_trigger_subject_vote (26 pass); test_perspective_metadata (3 pass) | open remainder: NER for unnamed third parties, lexical_strip_names over participants; not screened |
| I6 | implemented opt-in; screens pending | VERIFIED | S/config/schema.py:582-584 replay_window_unit/tokens_before/after; test_assembly_window_dedupe (19 pass) | never screened |
| I7 | done (opt-in) | VERIFIED | S/config/schema.py:476 skip_defaulted_dates; test_i7_i8_i23_dates_language (26 pass) | never screened on ConvoMem/PrefEval |
| I8 | done (checked, bug fixed) | VERIFIED | EM/cli.py:846 --memspine-as-of-question-date; EM/systems/memspine_system.py:687 parse_question_date; test_as_of_question_date (5 pass); test_i7_i8_i23_dates_language (26 pass) | screen plan only |
| I9 | partly done (engine) | VERIFIED | S/config/schema.py:469-470 rerank_chunk_chars/overlap; S/services/rerank/chunking.py; test_rerank_chunkmax (7 pass) | retrieval-only screens and pool-by-haystack-size open; rerank_chunk_overlap not named in any test |
| I10 | screened: neutral on OP-Bench | VERIFIED | arm best-generic-prompt; test_generic_reader_prompts (6 pass) | screen verdict: neutral, not adopted |
| I11 | R2-1b not adopted | VERIFIED | S/config/schema.py:1120 bridge_hop (+ bridge_hop_gate) | screen verdict: not adopted |
| I12 | done | VERIFIED | EM/date_check.py:19 DateParserUnavailable; test_i12_date_check (5 pass) | open: per-benchmark with/without reporting; 'date asked' rule for multi-date answers |
| I13 | calibration set built | VERIFIED | E/analysis/judge_calibration_dev.jsonl; E/judge_agreement.py; E/build_judge_calibration.py | open: 150-row human set, second judge, official prompts per benchmark |
| I14 | tooling done | VERIFIED | EM/errata.py (errata/v1) | open: per-benchmark errata files and gold_quality tags |
| I15 | tooling done | VERIFIED | E/make_split.py:134 --check; E/analysis/opbench_persona_split.json; E/analysis/blind_split_mab.json, blind_split_convomem.json | open: BEAM, PrefEval, PersonaBench, LaMP splits |
| I16 | estimator done | VERIFIED | E/noise_floor.py:165 --by-item | repeat runs not done; noise bands still estimates |
| I17 | screened r3-latest: NEUTRAL, not adopted | VERIFIED | S/config/schema.py:1080 latest_wins; test_latest_wins (14 pass) | screen verdict: neutral |
| I18 | clause + scaffold built, no run | VERIFIED | EM/cli.py:611 grounded_generic_prefs; E/forensics_report.py:735 preference_adherence | no preference dataset run; adherence judge not built |
| I19 | probe done, no leak | VERIFIED | EM/leakage.py; test_leakage_probe (17 pass) | open: per-tenant R@k, shared-grant cases |
| I20 | assembly side implemented opt-in | VERIFIED | S/config/schema.py:589-594 replay_budget_scaling/reference, replay_hits_first; test_assembly_window_dedupe (19 pass) | scale-curve screen open |
| I21 | measured, fix opt-in | VERIFIED | S/config/schema.py:1370-1375 minja_bridge_exempt_roles etc.; S/config/presets/chat_roles.yaml; test_firewall_chat_boilerplate (5 pass) | open: per-benchmark n_quarantined assertion |
| I22 | fixed (agent H) | VERIFIED | EM/cli.py:659 --refusal-match; test_refusal_whole_answer (46 pass) |  |
| I23 | done (guard, opt-in) | VERIFIED | S/config/schema.py:490 language_guard; S/core/language.py; test_i7_i8_i23_dates_language (26 pass) | multilingual triggers not built (stated) |
| I24 | partly done | VERIFIED | E/forensics_report.py (abstention P/R/F1, fired rates); test_forensic_schema (2 pass) | open: session-level R@k, stale-rank, adherence, per-benchmark latency/cost, judge kappa |
| I25 | implemented, opt-in | VERIFIED | S/config/schema.py:1750 data_profile, :1772 data_shape; 7 presets in S/config/presets; EM/shape.py; test_data_profiles (18 pass); test_data_shape (6 pass) | adoption per profile pending |
| I26 | open (in scope as engine work) | NOT BUILT | - | no cross-benchmark run with the current best config (BEAM/PrefEval/ConvoMem/LaMP); MAB-CR/ConvoMem blind slices built (V03) but not run |
| I27 | implemented; run pending | STATUS-STALE | EM/cli.py:504 --dataset op_bench, :180 opbench_assistant; EM/opbench.py; test_opbench_eval (30 pass); test_op_bench (5 pass) | 'run pending' is stale: OP-Bench dev baseline (21.2) and full-persp-opb (23.5) exist per the same register |
| I28 | implemented; unit + harness tests pass; screens pending | PARTIAL | S/services/decision/decider.py, opendecider_nano.py; S/config/schema.py:1188-1257; EM/cli.py:681-695; test_decider (17 pass); test_decision (26 pass); test_decider_refusal (5 pass) | decider_model/threads/backend/dtype/workers have NO test beyond the defaults golden (ONNX/bf16/bucketing parity claims are not in the suite); relevance_gate text omits store_calibrated; temporal_intent/abstention tasks defined but not wired (stated) |
| I29 | screened r3d: ADOPT-CANDIDATE | STATUS-STALE | S/config/schema.py:1203 relevance_gate off\|decider\|store_calibrated; S/engine.py:7720; S/core/relevance_probes.py; test_relevance_gate_calibrated (15 pass); test_relevance_bypass (7 pass) | Solution cell still says store_calibrated 'planned, not implemented' and a relevance_margin default 2.0: code has relevance_gate_margin_sd=1.0 (schema.py:1215), no key named relevance_margin; clean arm r3e never run |
| I30 | implemented (agent H) | VERIFIED | S/config/schema.py:1231 abstain_on_raw; test_relevance_gate_calibrated (15 pass) |  |
| I31 | screened r3-dedupe: INERT | VERIFIED | S/config/schema.py:601-603 dedupe/threshold/keep; test_assembly_window_dedupe (19 pass) | screen verdict: inert |
| I32 | screened r3-norecord: NEUTRAL, safe | VERIFIED | EM/cli.py:667 --no-record-hint; EM/no_record.py | screen verdict: neutral; decider swap (noul) not built |
| I33 | implemented opt-in | VERIFIED | S/config/schema.py:610 profile_relevance_gate; test_assembly_window_dedupe (19 pass) |  |
| I34 | done | VERIFIED | E/run_opb_base.sh | measurement (BASE 62.8) |
| I35 | tool done (no model call) | VERIFIED | EM/second_view.py:193,197,203 prepare/score/collect | second view itself not run |
| I36 | done | VERIFIED | E/run_baseline_x.sh | measurement |
| I37 | open as a rule; store_calibrated applies it | VERIFIED | S/engine.py:7720 store_calibrated; S/core/relevance_probes.py; margin S/config/schema.py:1215 | zero-tuning transfer check on later benchmarks not run |
| I38 | decider part done; rest open | PARTIAL | S/config/schema.py:1248-1257 decider_threads/backend/workers/dtype; EM/cli.py:692-695 | no unit test exercises threads/dtype/backend/workers; bge-small embedder and pytest -n cap still open (stated) |
| I39 | screened r3-persp: NEUTRAL by rule | VERIFIED | S/core/perspective.py; S/config/schema.py:895 subject_weight, :900 perspective_mode; test_perspective (21 pass) | screen + full run done |
| I40 | implemented opt-in | VERIFIED | S/config/schema.py:922 perspective_axes; test_perspective (21 pass); test_perspective_axes (6 pass) | never screened |
| I41 | implemented opt-in | VERIFIED | pol: axis in S/core/perspective.py; test_perspective_axes (6 pass) | never screened |
| I42 | exists + linked; per-subject as-of implemented | VERIFIED | read.perspective_as_of_subject (S/engine, S/config); test_perspective_open_items (20 pass) |  |
| I43 | implemented opt-in | VERIFIED | S/config/schema.py:1424 firewall.hearsay_trust_cap; test_perspective_open_items (20 pass) |  |
| I44 | implemented opt-in | VERIFIED | S/config/schema.py:929 perspective_marker; test_perspective_open_items (20 pass) |  |
| I45 | flat bar implemented opt-in; graded in I52 | VERIFIED | S/config/schema.py:1240 sensitivity_gate; test_governance_labels (31 pass) |  |
| I46 | implemented opt-in | VERIFIED | scope axis in perspective_axes; test_perspective_axes (6 pass) |  |
| I47 | implemented opt-in (incl. ack promotion) | VERIFIED | S/engine.py:124 acknowledges; test_perspective (21 pass); test_perspective_open_items (20 pass) | open: pinned read-only persona block |
| I48 | built, default off, screen pending | VERIFIED | S/core/inference.py; S/config/schema.py:1244-1245 inferred_gate/min_support, :1717 write.inferred; S/engine.py:4829 review_inferred; test_governance_labels (31 pass) | open: support growth after write, '[inferred]' render (no such render exists), decider resolver; inferred_trust_cap not named in any test |
| I49 | partly implemented opt-in | VERIFIED | S/core/perspective.py:60 due_window_for; test_perspective_open_items (20 pass) | open: ranking below standing facts |
| I50 | partly; card implemented opt-in | VERIFIED | S/config/schema.py:978 profile_subject_card, :808 profile_slots_header; test_perspective_open_items (20 pass) | open: screen |
| I51 | implemented opt-in | VERIFIED | memories.episodic.policies.perspective.context {owner, speakers} (S/engine.py perspective state); test_perspective (21 pass) | open: feed to extractor/reader prompts |
| I52 | built, default off, screen pending | VERIFIED | S/core/sensitivity.py; S/config/schema.py:1706 write.sensitivity, :1240 read.sensitivity_gate; test_governance_labels (31 pass) | never screened |
| I53 | built, default off, screen pending | VERIFIED | S/core/visibility.py; S/config/schema.py:1711 write.participants; EM/leakage.py:36 probe_viewer; test_governance_labels (31 pass); test_leakage_probe (17 pass) | never screened |
| I54 | implemented | VERIFIED | kin/possessive binding in S/core/perspective.py; test_perspective_axes (6 pass) | open: owner relation table for non-owner speaker |
| I55 | implemented opt-in | VERIFIED | S/memories/semantic/store.py:220 _incumbent_for_subject; S/engine.py:128 inherit_perspective_tags; test_governance_labels (31 pass); test_perspective_open_items (20 pass) | open: 'contradicts' decider task (not in services/decision) |
| I56 | built, opt-in, not screened | STATUS-STALE | EM/cli.py:719 --count-verify; EM/count_verify.py; test_i56_i57_i58_post_steps (55 pass) | progress log 2026-10-11 08:45: r7-post screened --count-verify single: +1/-16 REJECT; row and plan 1.2 still say 'not screened'/'never screened'. two_call and --evidence-table variants still unscreened |
| I57 | built, opt-in, not screened | STATUS-STALE | EM/cli.py:730 --date-repair; EM/date_repair.py; test_i56_i57_i58_post_steps (55 pass) | r7-post: 7 fired +1/0 (safe small candidate) per progress log; row says 'no screen' |
| I58 | built, reported as second column | VERIFIED | EM/cli.py:711 --judge-conventions; EM/judge_conventions.py; E/rescore_conventions.py; test_i56_i57_i58_post_steps (55 pass) | offline only; no dedicated test for rescore_conventions.py |
| I59 | built, opt-in, unit tested, no screen | VERIFIED | S/config/schema.py:938 owner_check; S/core/owner_check.py; test_owner_check (16 pass) | never screened |
| I60 | built, opt-in, unit tested, no screen | VERIFIED | S/config/schema.py:942 entity_check; test_owner_check (16 pass) | never screened |
| I61 | built, opt-in; not screened | VERIFIED | EM/cli.py:674 --premise-tolerant; EM/premise.py; test_e03_e04_i61 (7 pass) | needs cat-5 guard slice; never screened |
| I62 | done | VERIFIED | E/analysis/locomo_errata.json (cat5_answerable 3, premise_error 1, bad_evidence_id 1); E/forensics_report.py:398-400 |  |
| I63 | built, opt-in, unit tested, no screen | VERIFIED | S/config/schema.py:946 user_header, :909 perspective_asker; test_owner_check (16 pass) | never screened; perspective_asker not named in tests |
| I64 | built, opt-in, unit tested, no screen | VERIFIED | S/config/schema.py:967-968 reinjection_penalty/window; S/engine.py:109 InjectionLog; test_owner_check (16 pass) | never screened |
| I65 | done (opt-in) | VERIFIED | S/protocols/tools.py; docs/AGENT_TOOLS.md; test_agent_tools (15 pass) | test count 15 matches the claim |
| I66 | done | VERIFIED | S/protocols/mcp.py; S/cli.py:241 'memspine mcp' (+ --profile --principal --session-writes --turn-writes); test_mcp_server (3 pass) | official-SDK [mcp] extra not shipped (stated) |
| I67 | built, screen pending | VERIFIED | S/config/schema.py:1140-1157 agentic*; E/arms/scr-agentic.json; E/run_screens_r6.sh; test_agentic_read (30 pass) | never screened; agentic_first_share and agentic_max_new not named in tests |
| I68 | open (hypothesis, unmeasured) | NOT BUILT | no read.mode: agent_gated in schema | ablation kept in reserve; I65 (prerequisite) is done |
| I69 | steps 1-3 done | VERIFIED | S/config/schema.py:225-229 retry_on_error/constrained_retry; S/engine.py:13046 structured_stats; test_structured_retry (7 pass) | open: read structured_stats() on a Qwen run; uv.lock still lists the removed extra (stated) |
| I70 | on hold (user) | NOT BUILT | no adapters in src (grep BaseStore/langgraph/llamaindex: 0) | on hold by the user |
| I71 | on hold (user) | NOT BUILT | no write.mode in schema | on hold by the user; study docs/DEFERRED_INGEST_STUDY.md exists |
| I72 | done, reproduced then fixed | VERIFIED | S/engine.py:1941 _derived_parents_gone; test_forget_miner_race (3 pass) |  |
| I73 | done | VERIFIED | S/config/schema.py:1588 observability.write_timers; S/engine.py:1287; test_write_timers (5 pass); test_infra_gaps (37 pass) |  |
| I74 | screened r3d: ADOPT-CANDIDATE | VERIFIED | S/config/schema.py:1211 relevance_gate_bypass default 'named'; test_relevance_bypass (7 pass) |  |
| I75 | built; I75a measured +15; v2 screen pending | VERIFIED | S/config/schema.py:447 pool_protect_per_leg, :453 pool_protect_mode, :905 perspective_leg; arms scr-pool-protect/persp-mult/persp-mult-protect/pool-family exist; test_pool_protect (11 pass) | I75a/I75b screened; source_family v2 built, never screened; per-leg floors unlogged |
| I76 | built, screen pending | STATUS-STALE | EM/cli.py:737 --duration-solve, :748 --milestones, :762 --milestones-inclusive; EM/duration_solve.py; EM/milestones.py; test_i76_i79_duration_and_prompts (20 pass); test_e05_milestones (37 pass) | r7-post screened duration-solve (6 fired, +3/0, safe candidate) per progress log; row says screen pending. --milestones (E05 extension) still unscreened. test count 37 vs 22 claimed |
| I77 | open (not built) | NOT BUILT | no contains_single/date_range/relative_year in EM/judge_conventions.py |  |
| I78 | built, screen pending | VERIFIED | S/engine.py:5473 pool_cut logging; test_pool_protect (11 pass) | per-leg floor logging not built (stated) |
| I79 | built, screen pending | VERIFIED | S/config/schema.py:557 relative_dates_weekdays, :561 relative_dates_happened; EM/cli.py:603-604 grounded_nodate/grounded_detail_nodate; test_temporal_weekdays (12 pass); test_i76_i79_duration_and_prompts (20 pass) | named opt-in key read.relative_dates_durations does NOT exist (duration part is the older resolve_durations, disclosed); 'the weekend' not done (disclosed); the original grounded prompts still carry the LoCoMo gold example - see finding F1 (gold lint) |
| J1 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: I29 |
| J2 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: I59 |
| J3 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: - |
| J4 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: - |
| J5 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: I32 |
| J6 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: I63/I64 |
| J7 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: - |
| J8 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: - |
| J9 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: I56 |
| J10 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: I57 |
| J11 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: I3 |
| J12 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: I59/I60 |
| J13 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: - |
| J14 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: - |
| J15 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: - |
| J16 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: - |
| J17 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: - |
| J18 | per-subscore baseline fragment | NOT A BUILD CLAIM | measurement rows | status column 'open/guarded' = awaiting screens; features referenced: - |
| V03 | built (not yet run) | VERIFIED | E/analysis/blind_split_mab.json, blind_split_convomem.json; E/freeze_blind_slices.py; E/run_blind_validation.sh; E/eval_blind.py; test_blind_validation (14 pass) | blind validation itself has not been run (1 skip in test file: data-dependent) |
| Stable failures | target list | NOT A BUILD CLAIM | list of questions |  |
| R2-1 | screened - not adopted | VERIFIED | S/config/schema.py:1120 bridge_hop | screen verdict |
| R2-1b | not adopted (opt-in only) | VERIFIED | bridge_hop_gate in S/config/schema.py | screen verdict |
| R2-2 | implemented; inert | VERIFIED | speaker vote per person (S/engine); test_list_mode_round2 (38 pass) | screen verdict: inert |
| R2-3 | DONE - neutral | VERIFIED | set_question_wide (schema.py:880); test_list_mode_round2 (38 pass) |  |
| R2-4 | DONE - rejected | VERIFIED | hits_first / hit_blocks context order; test_context_order (8 pass) |  |
| R2-5 | done | VERIFIED | errata evidence_label_error entries |  |
| R2-6 | prompt built, screen pending | VERIFIED | EM/cli.py:609 grounded_generic_infer | built, never screened |
| K:M01 | done (work/m01) | VERIFIED | E/build_forensics_explorer.py:1250 reconcile; test_m01_m02_measurement (31 pass) | plan 2.1/wave-0 still list M01 'in progress' (STATUS-STALE in the plan) |
| K:M02 | done (work/m01) | VERIFIED | EM/evidence.py:20 recall_pair; EM/errata.py:23 check_adjudications; test_m01_m02_measurement (31 pass) | no adjudication entry added yet (stated) |
| K:R01 | refined by E02+E01 (built, unscreened) | VERIFIED | see E01/E02/I67 |  |
| K:R02 | open (in progress) | STATUS-STALE | S/config/schema.py:453 pool_protect_mode=source_family; test_pool_protect (11 pass) | I75 v2 is built (register I75 row); K row still says open/in progress |
| K:R03 | open | STATUS-STALE | components built: I6 replay_window_unit, I20 budget scaling, B10 rerank_context, I31 dedupe | K row says open; plan 2.4 says built-opt-in, unscreened. Evidence bundles (claim/antecedent/time) NOT built |
| K:A01 | open | STATUS-STALE | components built: I39-I55, I59, I63 | K row says open; plan 2.3 says built-opt-in; bounded reply edge not built |
| K:A02 | open | NOT BUILT | no write.event_ledger in schema | not built |
| K:A03 | built, unscreened (work/a03) | VERIFIED | S/config/schema.py:956 query_contract off\|heuristic\|llm, :960 query_contract_use; S/core/query_contract.py; E/arms/scr-contract.json; test_query_contract (38 pass) | never screened |
| K:A04 | built, unscreened (work/a04) | VERIFIED | EM/cli.py:780 --evidence-table; EM/evidence_table.py; test_a04_evidence_table (15 pass) | unsupported-item rate metric not built (stated) |
| K:A05 | open (in progress with E05) | STATUS-STALE | I57 --date-repair, I76 --duration-solve, I79 annotator, E05 --milestones all built | K row still says open |
| K:A06 | built, unscreened (work/a04) | VERIFIED | --count-verify --evidence-table (EM/cli.py:719,780); test_a04_evidence_table (15 pass) | write-time identity needs A02 (not built) |
| K:A07 | partly built | PARTIAL | E04 asset availability recorded (S/services/assets/*) | reply/reference edges not built (no read.reference_edges); plan 2.3 lists it not-built |
| K:A08 | partly built | VERIFIED | S/services/external/trigger.py (invited-inference trigger); routed_generic unchanged | r3c never ran |
| K:P01 | open | STATUS-STALE | I29/I74 built and screened (r3d ADOPT-CANDIDATE); S/config/schema.py:1203,1211 | K row says open; gate not default-on; r3e clean arm and store_calibrated never screened |
| K:P02 | open | STATUS-STALE | I64 reinjection_penalty built (S/config/schema.py:967); test_owner_check (16 pass) | K row says open; plan says built-opt-in; unscreened |
| K:P03 | built, unscreened (work/a04) | VERIFIED | EM/cli.py:791 --assertion-check; EM/assertion_check.py; test_p03_assertion_check (8 pass) | noul decider swap not built (stated) |
| K:G01 | open | NOT BUILT | no diagnosed-defect retry beyond E06 --verify-slots repair | not built; follows E06 |
| K:V01 | open (MAB/ConvoMem wiring in progress) | STATUS-STALE | V03 slices/scripts exist and are tested (blind_split_*.json, run_blind_validation.sh) | wiring is done (V03); frozen baseline manifest hash and behavioural control suite NOT built |
| K:V02 | open | PARTIAL | I78 pool_cut (S/engine.py:5473), write timers, --trace-full, H8 explorer | query-contract log fields exist via A03 forensics; per-leg floor logging, observed/reconstructed/absent marks only in explorer; K row says open |
| K:E01 | built, opt-in, unscreened (work/e02) | VERIFIED | S/config/schema.py:1177 fact_chain, :1182 fact_chain_trigger; S/engine.py:11018 project_facts; S/core/fact_chain.py; E/arms/scr-factchain.json; test_fact_chain (30 pass) | never screened; write-time fact_projection is a free-form policy key |
| K:E02 | built, opt-in, unscreened (work/e02) | VERIFIED | S/config/schema.py:1168 agentic_mode query\|slot; S/core/slot_loop.py; E/arms/scr-agentic-slot.json; test_slot_loop (28 pass) | asset/public-knowledge actions stay with E03/E04 (stated); never screened |
| K:E03 | built, opt-in (fakes-tested) | VERIFIED | S/config/schema.py:1272 external_evidence off\|cache\|web, :1275 external_provider none\|http, :1277 external_max_calls, :1280 external_cache_dir; S/services/external/{broker,privacy,provider,trigger}.py; test_external_privacy (28 pass); test_asset_external_engine (16 pass); test_e03_e04_i61 (7 pass) | IN FLUX: another agent is adding a Wikipedia provider; origin HEAD bc5bf30 has only the http provider. No provider/web arm run. external_cache_dir not named in tests |
| K:E04 | built, opt-in (fakes-tested) | VERIFIED | S/config/schema.py:1263 ingest.assets, :1266 read.asset_evidence off\|cached\|fetch; S/services/assets/{fetch,registry,vision,cue,adapter,models}.py; test_assets (28 pass); test_asset_external_engine (16 pass) | IN FLUX: another agent is adding an E04 precompute script (not in origin HEAD). Vision model not pulled; no multimodal arm run |
| K:E05 | open (in progress) | STATUS-STALE | EM/cli.py:748 --milestones; EM/milestones.py; test_e05_milestones (37 pass) | E05 extension is built and tested (register I76 row); K row says open; unscreened |
| K:E06 | built, unscreened (work/a03) | VERIFIED | EM/cli.py:767 --verify-slots; EM/slot_verify.py; S/core/answer_check.py; test_slot_verify (16 pass); test_answer_check (30 pass) | never screened |
| PLAN:1.2 'built, never screened' list | I56, I57, I76 listed as never screened | STATUS-STALE | progress log 2026-10-11 08:45 (r7-post) | I56 single screened (rejected), I57 and I76 duration-solve screened (safe small candidates) |
| PLAN:wave1 'in progress' | A03/E06/E05/R02/M01 in progress | STATUS-STALE | all five are built and tested (K rows, I75, I76) | plan wave headers and 2.1/2.2/2.4/2.6 status cells predate the builds |
| PLAN:2.7 I38 | partly done | PARTIAL | see I38 |  |
| PLAN:2.5 E03/E04 | built-opt-in | VERIFIED | see K:E03/K:E04 | in flux |

## PARTIAL items

- **E1** (fixed): solution options 'bench_http.py' and 'warn on localhost' do not exist (no bench_http.py anywhere; no localhost warning in the harness)
- **E3** (done (not run)): option 'lower log level' not set in the script; script never executed (stated)
- **I1** (fixed in harness, baseline pending): (a) 'baseline pending' is stale (I2 measured 79.7%); (b) 'To do: CLI flag for assertive / valve' is still NOT done: no such flag in EM/cli.py; STATUS-STALE+PARTIAL
- **I28** (implemented; unit + harness tests pass; screens pending): decider_model/threads/backend/dtype/workers have NO test beyond the defaults golden (ONNX/bf16/bucketing parity claims are not in the suite); relevance_gate text omits store_calibrated; temporal_intent/abstention tasks defined but not wired (stated)
- **I38** (decider part done; rest open): no unit test exercises threads/dtype/backend/workers; bge-small embedder and pytest -n cap still open (stated)
- **K:A07** (partly built): reply/reference edges not built (no read.reference_edges); plan 2.3 lists it not-built
- **K:V02** (open): query-contract log fields exist via A03 forensics; per-leg floor logging, observed/reconstructed/absent marks only in explorer; K row says open
- **PLAN:2.7 I38** (partly done): see I38

## STATUS-STALE items

- **C5** (duration annotator implemented; elapsed helper not done): 'elapsed helper not done' is stale: --duration-solve (I76) and --milestones (E05) now implement it
- **C7** (in test - caption rule kept in grounded_v3 (screen queued)): C8 says grounded_v3 was screened (85.8 vs 86.7) and not adopted; C7 row not updated
- **I27** (implemented; run pending): 'run pending' is stale: OP-Bench dev baseline (21.2) and full-persp-opb (23.5) exist per the same register
- **I29** (screened r3d: ADOPT-CANDIDATE): Solution cell still says store_calibrated 'planned, not implemented' and a relevance_margin default 2.0: code has relevance_gate_margin_sd=1.0 (schema.py:1215), no key named relevance_margin; clean arm r3e never run
- **I56** (built, opt-in, not screened): progress log 2026-10-11 08:45: r7-post screened --count-verify single: +1/-16 REJECT; row and plan 1.2 still say 'not screened'/'never screened'. two_call and --evidence-table variants still unscreened
- **I57** (built, opt-in, not screened): r7-post: 7 fired +1/0 (safe small candidate) per progress log; row says 'no screen'
- **I76** (built, screen pending): r7-post screened duration-solve (6 fired, +3/0, safe candidate) per progress log; row says screen pending. --milestones (E05 extension) still unscreened. test count 37 vs 22 claimed
- **K:R02** (open (in progress)): I75 v2 is built (register I75 row); K row still says open/in progress
- **K:R03** (open): K row says open; plan 2.4 says built-opt-in, unscreened. Evidence bundles (claim/antecedent/time) NOT built
- **K:A01** (open): K row says open; plan 2.3 says built-opt-in; bounded reply edge not built
- **K:A05** (open (in progress with E05)): K row still says open
- **K:P01** (open): K row says open; gate not default-on; r3e clean arm and store_calibrated never screened
- **K:P02** (open): K row says open; plan says built-opt-in; unscreened
- **K:V01** (open (MAB/ConvoMem wiring in progress)): wiring is done (V03); frozen baseline manifest hash and behavioural control suite NOT built
- **K:E05** (open (in progress)): E05 extension is built and tested (register I76 row); K row says open; unscreened
- **PLAN:1.2 'built, never screened' list** (I56, I57, I76 listed as never screened): I56 single screened (rejected), I57 and I76 duration-solve screened (safe small candidates)
- **PLAN:wave1 'in progress'** (A03/E06/E05/R02/M01 in progress): plan wave headers and 2.1/2.2/2.4/2.6 status cells predate the builds

## NOT BUILT items

- **A1** (deferred (user)): second judge deferred to wave 4 final-claims gate; no second-judge result exists
- **A7** (plan written, not run): 2x2 never run; needs GPU
- **C11** (blocked by GPU memory): needs 14B reader + 9B judge > 16 GB; two-pass option not built
- **I26** (open (in scope as engine work)): no cross-benchmark run with the current best config (BEAM/PrefEval/ConvoMem/LaMP); MAB-CR/ConvoMem blind slices built (V03) but not run
- **I68** (open (hypothesis, unmeasured)): ablation kept in reserve; I65 (prerequisite) is done
- **I70** (on hold (user)): on hold by the user
- **I71** (on hold (user)): on hold by the user; study docs/DEFERRED_INGEST_STUDY.md exists
- **I77** (open (not built)): no contains_single/date_range/relative_year in EM/judge_conventions.py
- **K:A02** (open): not built
- **K:G01** (open): not built; follows E06

## Open remainders inside VERIFIED rows (disclosed by the register itself)

- **I5**: open remainder: NER for unnamed third parties, lexical_strip_names over participants; not screened
- **I12**: open: per-benchmark with/without reporting; 'date asked' rule for multi-date answers
- **I13**: open: 150-row human set, second judge, official prompts per benchmark
- **I14**: open: per-benchmark errata files and gold_quality tags
- **I15**: open: BEAM, PrefEval, PersonaBench, LaMP splits
- **I19**: open: per-tenant R@k, shared-grant cases
- **I21**: open: per-benchmark n_quarantined assertion
- **I24**: open: session-level R@k, stale-rank, adherence, per-benchmark latency/cost, judge kappa
- **I47**: open: pinned read-only persona block
- **I48**: open: support growth after write, '[inferred]' render (no such render exists), decider resolver; inferred_trust_cap not named in any test
- **I49**: open: ranking below standing facts
- **I50**: open: screen
- **I51**: open: feed to extractor/reader prompts
- **I54**: open: owner relation table for non-owner speaker
- **I55**: open: 'contradicts' decider task (not in services/decision)
- **I69**: open: read structured_stats() on a Qwen run; uv.lock still lists the removed extra (stated)

## Built but never screened (no GPU result)

I6, I7, I8, I9 (chunked rerank), I18, I20, I23, I33, I40-I46, I47, I48, I49, I50, I51, I52, I53, I54, I55, I59, I60, I61, I63, I64, I67, I69, I72-I73 (infra), I75 `source_family` v2, I76 `--milestones` (E05), I78, I79, B3/C4/C6/C9/R2-6 prompts (`grounded_generic_infer`, `_list`, `_prefs`, `routed_generic`; r3c never ran), B10 `rerank_context` arms, I29 `store_calibrated` and clean arm r3e, I56 `two_call` and `--evidence-table`, A03 `query_contract`, A04/A06 `--evidence-table`, E06 `--verify-slots`, P03 `--assertion-check`, E01 `fact_chain`/`fact_projection`, E02 slot loop, E03 `external_evidence`, E04 `ingest.assets`/`asset_evidence`, V03 blind slices (built, not run), A7 2x2, I35 second view.

## Screened, with verdict (from register and progress log)

| Feature | Verdict |
|---|---|
| B1 list mode + fixed reranker (BEST) | ADOPTED (84.1 vs 82.9, +12/-5, 584 dev q) |
| B9 rerank_floor skip, pool 2 | in BEST (84.5 vs 84.1, coverage up) |
| B4 lexical_dates | REJECTED (-3.1 temporal) |
| B4 temporal_relative / infer_year | NEUTRAL |
| B5 lexical_strip_names | REJECTED (+3/-6) |
| B6 english analyzer | NEUTRAL |
| B7 wide pool + balanced rerank | NOT ADOPTED (churn +30/-23) |
| B8 session_leg | NEUTRAL |
| B15 word vectors in place of BM25 | REJECTED (-12 net) |
| C1/C2/C5 variants | NEUTRAL |
| C3 grounded_v2 yes/no rule | REJECTED (+5/-17) |
| C8 grounded_v3 | REJECTED (+3/-5) |
| A9 thinking mode | REJECTED (63.2 vs 88.8) |
| R2-1 bridge hop / R2-1b gated | NOT ADOPTED |
| R2-2 per-person vote | INERT |
| R2-3 wide trigger | NEUTRAL |
| R2-4 hits-first order | REJECTED |
| I1 neutral retry | ADOPTED default |
| I3 grounded_generic | REJECTED (guard) |
| I4 intent list trigger | NEUTRAL, safe (generality candidate) |
| I10 generic query instruction | NEUTRAL |
| I17 latest-wins annotate | NEUTRAL, not adopted |
| I29 decider relevance gate alone | REJECTED as built |
| I29+I74 gate + named bypass (r3d) | ADOPT-CANDIDATE (confounded; r3e not run) |
| I31 dedupe | INERT |
| I32 no-record hint | NEUTRAL, safe |
| I39 perspective subject_weight | NEUTRAL by rule (full run 80.4) |
| I75a leg-protected pool (r7-protect) | CANDIDATE: +15 net LoCoMo, OP-Bench +0.0 |
| I75b perspective multiplier + protect (r7-multprot) | NOT ADOPTED |
| I56 --count-verify single (r7-post) | REJECTED (+1/-16) |
| I57 --date-repair (r7-post) | safe, small (+1/0) |
| I76 --duration-solve rewrite (r7-post) | safe candidate (+3/0) |
| I58 judge conventions (offline) | +0.5 on dev baseline (second column) |
| I19 leakage probe (offline) | no leak in 12 configs |

## Commands run

Static checks: scripted grep of every backticked key/flag/path in the register and plan against `config/schema.py`, `cli.py`, `run.sh`, `docs/USAGE.md`, `tests/`, `evals/tests/`. Dynamic: the two pytest runs above.
