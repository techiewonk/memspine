# Licence audit: benchmark dataset content in committed files (2026-10-14)

Package LIC-AUDIT, branch `wp/LIC-AUDIT` (off `restructure`, HEAD `45580381` before this package's commit). Rule: LoCoMo, OP-Bench, LongMemEval, ConvoMem, MAB and PrefEval TEXT must not be in committed files or published artifacts; only ids, counts and labels. Examples use invented names (Bram, Ilse, Noor, Tomasz). This report cites `<file>:<line>` only and never quotes dataset text.

## 1. Method

- Scope: all 2,632 files from `git ls-files` on this branch, plus the members of the tracked zip `docs/combined-memory-locomo-eval-v4.zip`.
- Names: whole-word match for the 18 LoCoMo speaker first names, then context check.
- Text: every string of 40 or more characters was extracted from the local dataset files and indexed as 30-character windows (stride 10) over lower-cased alphanumerics (whitespace, punctuation and JSON escaping removed). Every tracked file was slid against the index at every offset. A hit cluster of 2 or more windows means a shared run of about 40 or more characters ("strong"); a single window means 30 to 39 characters ("weak"). Counts below are clusters, not characters.
- Datasets checked locally: LoCoMo (`memspine/data/locomo10.json`, all fields: turns, QA, observations, event and session summaries), LongMemEval oracle (`memspine/data/longmemeval_oracle.json`), and under `memspine/evals/data/`: ConvoMem, OP-Bench (`opbench_src`, 17 data files), MAB, PrefEval, ASB, CPB, HaluMem, MuSE-news, PerLTQA, PersonaBench. Note: the OP-Bench source tree embeds LoCoMo-derived content, so a hit labelled "LoCoMo or OP-Bench" cannot be attributed to one of the two; both are covered by the rule.
- Not checked (reader failed): BEAM (parquet read error) and TOFU (non-standard `.json` line format). Neither is on the rule's list; re-run if they become relevant.
- Limits: a paraphrase that changes wording is not detected by a substring test, so "paraphrase" below is a judgement from reading the file structure, not a measurement. Single-window clusters in `src/` are common-word-list false positives (see 2.5).

## 2. Findings

### 2.1 Verbatim dataset text (needs the owner's decision; not touched in this package)

| File | Kind | Strong / weak clusters | Example (reference only) | Recommended action |
|---|---|---|---|---|
| `docs/combined-memory-locomo-eval-v4.zip` (member `module/locomo_eval/data/locomo10.json`) | verbatim text: the complete LoCoMo file (2.8 MB, byte size equal to the local dataset) inside a tracked zip; bypasses the `data/` gitignore | 4879 / 477 | `docs/combined-memory-locomo-eval-v4.zip!module/locomo_eval/data/locomo10.json:21` | delete from the tree (and the unpacked `docs/combined-memory-locomo-eval-v4/` bundle if it is a download artifact); highest priority |
| `evals/results_qwen_stack/qa-full-qs-eq06-roff/results.jsonl` | verbatim text (questions, gold, retrieved turns) | 1478 / 557 | `evals/results_qwen_stack/qa-full-qs-eq06-roff/results.jsonl:4` | move to gitignored `evals/runs/_analysis`, keep summary files |
| `evals/results_qwen_stack/qa-full-qs-ebge-roff/results.jsonl` | verbatim text | 1476 / 526 | `.../results.jsonl:4` | same |
| `evals/results_qwen_stack/qa-full-qs-ejina-rjina/results.jsonl` | verbatim text | 1398 / 506 | `.../results.jsonl:4` | same |
| `evals/results_qwen_stack/qa-full-qs-eq06-rjina/results.jsonl` | verbatim text | 1390 / 500 | `.../results.jsonl:4` | same |
| `evals/results_qwen_stack/qa-q35-think-off/results.jsonl` | verbatim text | 115 / 80 | `.../results.jsonl:4` | same |
| `evals/results_qwen_stack/qa-q35-think-on/results.jsonl` | verbatim text | 98 / 49 | `.../results.jsonl:4` | same |
| `evals/analysis/RECALL_GAPS_FORENSIC.md` | verbatim text (turn and question quotes) | 264 / 7 | `evals/analysis/RECALL_GAPS_FORENSIC.md:129` | strip text to ids, or move the full version to `evals/runs/_analysis` |
| `evals/analysis/READER_GAPS.md` | verbatim text | 116 / 38 | `evals/analysis/READER_GAPS.md:65` | strip text |
| `evals/analysis/RETRIEVAL_GAPS.md` | verbatim text | 100 / 36 | `evals/analysis/RETRIEVAL_GAPS.md:103` | strip text |
| `evals/analysis/READER_GAPS_FORENSIC.md` | verbatim text | 83 / 21 | `evals/analysis/READER_GAPS_FORENSIC.md:73` | strip text |
| `evals/prereg/G24_gliner2_planner_set.json` | verbatim text: 98 items with the question string, plus ids and split | 68 / 15 | `evals/prereg/G24_gliner2_planner_set.json:14` | replace `question` by id; the set is a pre-registered artifact, so keep the `source_sha256` and ids and regenerate text locally |
| `evals/analysis/judge_calibration_dev.jsonl` | verbatim text (`question`, `gold`, `answer` fields, 43 rows) | 28 / 15 | `evals/analysis/judge_calibration_dev.jsonl:1` | move to gitignored `evals/runs/_analysis`; `evals/build_judge_calibration.py` rebuilds it |
| `evals/analysis/contract_offline_full_persp_loc.json` | verbatim text (question strings) | 6 / 1 | `evals/analysis/contract_offline_full_persp_loc.json:140` | strip to ids |
| `evals/analysis/locomo_errata.json` | verbatim text excerpts (errata evidence) | 2 / 2 | `evals/analysis/locomo_errata.json:405` | keep ids and error class only |
| `evals/analysis/GAP_REGISTER.md`, `B1_LIST_QUESTIONS_DESIGN.md`, `FAILURE_FORENSICS_BASELINE_2026-10-10.md`, `READER_OPEN_GAPS_2026-10-10.md`, `GENERALISATION_AUDIT.md`, `B2_EVIDENCE_AUDIT.md`, `BENCHMARK_READINESS.md` | verbatim question snippets in prose (1 to 4 strong clusters each) | 4 / 4, 4 / 2, 3 / 2, 4 / 0, 1 / 2, 0 / 2, 1 / 0 | `evals/analysis/GAP_REGISTER.md:146`, `evals/analysis/B1_LIST_QUESTIONS_DESIGN.md:193`, `evals/analysis/FAILURE_FORENSICS_BASELINE_2026-10-10.md:86`, `evals/analysis/READER_OPEN_GAPS_2026-10-10.md:54` | replace quotes by qids and invented-name paraphrases |
| `tests/unit/golden/routed_read_off.json` | paraphrase or near-verbatim (40 weak clusters of 30 to 39 characters, no strong one) | 0 / 40 | `tests/unit/golden/routed_read_off.json:1301` | owner to review; the golden is a regression fixture, so regenerate it from invented-name inputs rather than edit by hand |

### 2.2 Short verbatim questions embedded in test code (lesser; owner decision)

One-sentence LoCoMo or LongMemEval questions used as inputs. Each should be re-written with an invented name and a changed predicate.

| File | Kind | Strong / weak | Example | Action |
|---|---|---|---|---|
| `evals/tests/test_i76_i79_duration_and_prompts.py` | verbatim question text | 9 / 2 | `evals/tests/test_i76_i79_duration_and_prompts.py:49` | rewrite inputs |
| `tests/unit/core/test_query_shape.py` | verbatim question text | 5 / 2 | `tests/unit/core/test_query_shape.py:13` | rewrite inputs |
| `tests/unit/test_intent_trigger_subject_vote.py` | verbatim LongMemEval question | 4 / 1 | `tests/unit/test_intent_trigger_subject_vote.py:23` | rewrite inputs |
| `tests/unit/test_list_mode_round2.py` | verbatim question text | 3 / 1 | `tests/unit/test_list_mode_round2.py:85` | rewrite inputs |
| `tests/unit/test_list_mode.py` | verbatim question text | 2 / 0 | `tests/unit/test_list_mode.py:40` | rewrite inputs |
| `evals/tests/test_reader_open_gaps.py` | verbatim question text | 2 / 1 | `evals/tests/test_reader_open_gaps.py:86` | rewrite inputs |
| `evals/tests/test_routed_qa.py` | verbatim question text | 2 / 0 | `evals/tests/test_routed_qa.py:38` | rewrite inputs |
| `tests/unit/prompts/test_chat_by_shape.py` | verbatim question text | 1 / 0 | `tests/unit/prompts/test_chat_by_shape.py:32` | rewrite inputs |
| `tests/unit/test_assembly_window_dedupe.py` | verbatim sentence | 1 / 0 | `tests/unit/test_assembly_window_dedupe.py:57` | rewrite inputs |
| `evals/tests/test_i56_i57_i58_post_steps.py` | verbatim answer sentence | 1 / 0 | `evals/tests/test_i56_i57_i58_post_steps.py:287` | rewrite inputs |
| 14 further test or script files with a single weak cluster (for example `tests/unit/test_read_levers.py:254`, `tests/unit/test_current_state.py:272`, `tests/unit/test_agentic_read.py:481`, `evals/feature_audit.py:1003`, `evals/smoke_gc.py:40`) | probable verbatim or near-verbatim fragments, 30 to 39 characters | 0 / 1 each | as listed | review with the rewrite pass |
| `evals/tests/test_mab_gold.py:41`, `evals/memspine_evals/datasets/mab_gold.py:60` | MAB-style fact templates (sentence frames, possibly from MAB); lesser | 3 / 1, 2 / 1 | as listed | owner to confirm whether frames are MAB text |

### 2.3 Third-party text (not benchmark data, separate licence question)

| File | Kind | Clusters | Example | Action |
|---|---|---|---|---|
| `docs/ECOSYSTEM_PROMPTS.md` | verbatim catalogue of other engines' prompts; coincident matches with LongMemEval, PerLTQA and LoCoMo or OP-Bench few-shot material inside those prompts (and 184 speaker-name hits, mostly John, Caroline, Sam, Maria, Tim) | 39 / 29 | `docs/ECOSYSTEM_PROMPTS.md:1844` | owner decision: keep as a vendor-prompt quote under their licences, or reduce to hashes and ids; on main since 2026-07-11 |
| `evals/memspine_evals/vendor_judges.py` | vendor judge prompt (Mem0 unified judge) | 1 / 0 | `evals/memspine_evals/vendor_judges.py:39` | prompt licence, not a dataset licence; matches OP-Bench's copy of it |
| `tests/unit/core/test_firewall_signals.py` | ASB attack-template phrases | 2 / 0 | `tests/unit/core/test_firewall_signals.py:20` | likely a public template phrase; confirm |

### 2.4 Checked and clean of dataset text

- `evals/analysis/catalogue/locomo_dev_failures.jsonl` and `.md`: ids and labels only (fields: query id, category, failure code and stage, dia-id evidence, authored `generic_fix`, gap ids). Only 3 speaker-name hits, all in authored prose.
- `evals/analysis/catalogue/opbench_dev_failures.md`: aggregate counts only, no names.
- `evals/prereg/`: `G8a_*` and `2026-10-10_heldout_reader_fixes.md` have no text hits; `G24_gliner2_planner.md` and `_results.md` have no text hits and one name each. Only `G24_gliner2_planner_set.json` carries text (2.1).
- `evals/analysis/*.json` slices (`fail_slice_*`, `sample_slice_*`, `focus_slice_opb.json`, `opbench_persona_split.json`, `blind_split_*.json`, `locomo_split.json`): ids and counts, no text hits. `focus_slice_opb.json` has about 990 speaker-name hits because OP-Bench probe ids embed the persona name (for example a `conv-NN:<Name>/...` key). These are dataset ids; renaming would break joins to the dataset. Recommendation: leave.
- `evals/analysis/judge_calibration*.jsonl`: only `judge_calibration_dev.jsonl` exists, and it contains text (2.1).
- `evals/tests/fixtures/adapter_parity_snapshot.json` (12 weak) and `evals/tests/fixtures/cpb/scenarios/stage2/syn-00.json` (1 weak): synthetic fixtures, false positives.
- `src/`: no dataset text. The flagged lines were read; all are weekday lists, month lists, stop-word lists, import lines or regexes (for example `src/memspine/read/contract.py:233`, `src/memspine/derive/ground.py:298`).

### 2.5 Speaker names

Whole-word hits on the 18 names, excluding result dumps, by area (counts are hits, not files): `evals/analysis` 5,505, `tests/unit` 1,554, `tests/integration` 484, `evals/tests` 214, `docs/ECOSYSTEM_PROMPTS.md` 184, `evals/prereg` 109, `src/memspine` 52 (Melanie and Caroline: 40), other docs about 50, `evals/*.sh` run scripts 4 each. The common names (John, Tim, Sam, James, Jon, Dave, Evan, Andrew) are ambiguous: in `evals/analysis`, `evals/prereg` and the OP-Bench ids they are LoCoMo speakers; in `evals/memspine_evals/leakage.py` "Sam" is a synthetic persona of the repo's own leakage test (not dataset text, no action); in `src/memspine/core/policies/conflict.py`, `vetting.py` and `sensitivity.py` the few hits are generic examples to review by hand. Only Caroline and Melanie are used as invented-example stand-ins throughout the docs and tests.

## 3. Git history

- `main` (568 commits, local): no LoCoMo dataset file, zip, results dump, forensics markdown or judge-calibration file has ever been committed to `main`. Files on `main` with strong hits: `evals/prereg/G24_gliner2_planner_set.json` (added 2026-10-06, question strings), `tests/unit/golden/routed_read_off.json` (2026-10-06, weak), `tests/unit/core/test_query_shape.py` (8 commits since 2026-10-02), `evals/tests/test_routed_qa.py`, `tests/unit/prompts/test_chat_by_shape.py`, `tests/unit/core/test_firewall_signals.py`, `evals/memspine_evals/datasets/mab_gold.py` and its test, `docs/ECOSYSTEM_PROMPTS.md` (2026-07-11). No deleted data file with a dataset-like path was found in `git log main --diff-filter=D`; only the files above were content-scanned, not every historical blob, so deleted-and-rewritten content in older commits is not excluded.
- Not on `main`, but on `restructure`, `feat/local-qwen-stack` and several `work/*` branches, AND pushed to `origin` (`origin/restructure`, `origin/feat/local-qwen-stack`, remote `github.com/techiewonk/memspine`): the zip with the full LoCoMo file (added 2026-10-11, commit `b652379e`), the six `results_qwen_stack` result files (2026-10-11 and earlier), the forensics markdowns (2026-10-09 and 2026-10-10), `judge_calibration_dev.jsonl` (commit `f6c9ba23`) and `contract_offline_full_persp_loc.json`. If these branches are merged to `main`, the text enters `main` history; if `restructure` is the publication branch it is already published.
- Rewriting history (filter-repo on the zip, results files, forensics markdown, calibration file and G24 set, then force-pushing `origin/restructure` and `origin/feat/local-qwen-stack`, plus removing the same blobs from any forks or clones) is the owner's decision. Nothing was rewritten here.

## 4. Fixes applied in this package (names only)

LoCoMo speaker names were replaced by invented names in config field descriptions and doc prose where they are examples: Caroline to Ilse, Melanie to Bram, and the alias example "Mel" to "Tom" for Tomasz.

- `src/memspine/config/schema/read.py` (2 field descriptions), `src/memspine/config/constants.py` (1 comment).
- `docs/USAGE.md` (22 line edits across the tables and the perspective example), `docs/adr/ADR-044`, `ADR-046`, `ADR-049`, `ADR-061`, `docs/PERSPECTIVE_LAYER.md`, `docs/PIPELINE_TREE.md`, `CHANGELOG.md`.
- Regenerated by `scripts/build_stage_params.py`: `docs/v2/data/stage_params.json`, `docs/v2/STAGE_PARAMS.md` (`--check` passes). `docs/v2/data/screen_atlas_internals.json` and `docs/v2/data/screen_internals_write.json` were edited directly (names only; both parse).
- No config baseline or golden file needed regeneration (descriptions are not part of them).
- Tests run (147 passed): `tests/unit/test_stage_params.py`, `test_docs_config_surface.py`, `test_flag_evidence.py`, `tests/unit/config`, the four read goldens, `evals/tests/test_inspect_step.py`.

## 5. Left for the owner

1. Delete or relocate: the tracked zip (and its unpacked bundle if present), the six `evals/results_qwen_stack/qa-*` result files, `judge_calibration_dev.jsonl`.
2. Strip text to ids: the four forensics markdowns, the smaller `evals/analysis/*.md` quoting questions, `contract_offline_full_persp_loc.json`, `locomo_errata.json`, `G24_gliner2_planner_set.json` (a pre-registered artifact; changing it changes its hash, so the prereg record needs a note).
3. Re-write test inputs that are verbatim questions (2.2), and decide on names still present in tests, `evals/analysis`, `docs/ECOSYSTEM_PROMPTS.md`, `docs/*_ENGINE*.html`, `docs/combined-memory-locomo-eval-v4/` and `src/` comments (the remaining 40-odd hits in `src/memspine` outside config are code comments and regex tests; they were out of this package's step 2 scope).
4. History decision (section 3).
5. Re-run the scan for BEAM and TOFU if they enter scope.
