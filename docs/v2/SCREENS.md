# SCREENS: the v2 screen queue

Owner decision 2026-10-13: the GPU stays stopped, every behaviour change is built flag-off, and all screens are queued.
This file is the ordered queue; `evals/v2_queue.tsv` is the same list in machine form and `evals/run_v2_queue.sh` runs it.
Nothing here has been run. Every status is `queued`; the runner writes live status to `evals/runs/_logs/v2_queue.state`.

## Sample-first workflow (owner decision 2026-10-13)

"When we use GPU, don't run complete tests; test with samples and keep recording, so we can iterate, and in the final run enhance
and optimise based on the small results." The queue therefore has three modes (`MODE=sample` is the DEFAULT):

| Mode | What runs | Output | Never |
|---|---|---|---|
| `sample` | each arm and its reference on a small seeded stratified sample slice | descriptive row in the ledger | blind sets; a PROMOTE verdict; skip-if-finished (re-running is the point) |
| `focus` | today's focus-slice paired screens (LoCoMo 437 q, OP-Bench dev 331) | gate verdicts + ledger row | full and blind sets |
| `final` | LoCoMo full with the paired gate (+ OP-Bench dev where the row has it), then the sealed blind sets (one exposure per arm) | gate verdicts + ledger row | any screen not marked `promote_candidate` |

The loop:

1. **Sample batch.** `MODE=sample bash evals/run_v2_queue.sh --only B1` (or `B2`, `B3`, or a list of ids). Dry run first with
   `PINNED_DRY_RUN=1`. The reference run of each reference arm is built once per slice + commit and reused by every arm
   (`runs/smp-ref-<ref arm>-<commit>-<slice>`); run ids of arms are `smp-<id>-<loc|opb>-<MMDD-HHMM>`, so an iteration never
   overwrites the last one.
2. **Read.** `python evals/v2_ledger.py summary --mode sample` lists the latest result of every screen, best net first, ties broken
   by the lower end of the interval. Each row: wins / losses / ties, net, delta in points with a paired bootstrap interval
   (a spread over 60-80 questions, not a test), per-category wins/losses, tokens and latency p50/p95. `history <screen>` shows every
   iteration with the arm-file digest, so a change to the arm between iterations is visible next to its effect.
3. **Iterate.** Change the arm (flags, parameters in `evals/arms/<arm>.json`), commit, re-run only that screen
   (`--only S13`). The reference is rebuilt only if the commit or the slice changed (pass `--commit` to keep one code commit
   when only arm files moved). Record the decision with `python evals/v2_ledger.py mark S13 iterate|drop|promote_candidate "why"`.
4. **One final run.** `MODE=final bash evals/run_v2_queue.sh`: only `promote_candidate` screens run the full set (LoCoMo 1,540 vs
   `clean-jina-full`, paired gate) and, for rows with a blind set, the sealed MAB-CR / ConvoMem sets via `eval_blind.py`. The
   rest print "not a promote_candidate". Freeze the arms before this run: the blind sets are exposed once per arm.

Sample slices (ids only, no benchmark text; `evals/make_sample_slice.py`, seed 20261013; the `slice_id` and the 16-hex ids hash go
into the run manifest and so into the comparability key; a sample run is only comparable with a run on the same slice):

| Slice file | Questions | Strata | Used by |
|---|---|---|---|
| `analysis/sample_slice_loc_80_s20261013.json` | 80 of 1,540 LoCoMo (cat 1: 16, 2: 18, 3: 9, 4: 37; the source run has no cat 5) | category (floor 6), then conversation; all 10 conversations | all non-engine-LLM rows |
| `analysis/sample_slice_loc_60_s20261013_c3.json` | 60 LoCoMo from 3 seeded conversations (conv-42, conv-48, conv-50) | same | engine-LLM rows (kind GC: S01-S04, S28), which are ingest-bound |
| `analysis/sample_slice_opb_60_s20261013.json` | 60 of 331 OP-Bench probes (diversity 25, irrelevance_easy 12, irrelevance_hard 8, sycophancy 15) | probe family, then person | OP-Bench rows (S20, S23, S27) |

Blind sets (MAB-CR, ConvoMem, PrefEval) are never sampled: a sample is for iteration, a blind set is seen once. A sample of 60-80
questions cannot carry an adoption decision (band 0.328*sqrt(80) = 2.9 questions); it ranks arms and shows where to iterate.
The 2-question check run is dropped in sample mode (the sample itself is the check; a failed run is recorded as RUN_FAILED).

Ledger `evals/runs/_logs/v2_results.tsv` (append-only, header row, git-ignored): `ts, mode, screen, arm, arm_digest, reference,
commit, slice_id, slice_hash, n, wins, losses, ties, net, delta, ci_low, ci_high, per_category, cost, latency_p50_ms,
latency_p95_ms, comparability_key, verdict, notes`. Decisions: `v2_decisions.tsv` beside it (last line per screen wins).

GPU time per mode (estimates from measured runs: focus 437 q = 17 min, of which ingest about 3.5 min; full 1,540 q = 41 min; about
1.7 s per question after ingest; engine-LLM ingest from `GAP_REGISTER.md`):

| Unit | Sample | Focus | Final |
|---|---|---|---|
| Non-GC LoCoMo screen | about 7 min (3.5 ingest, 80 q about 2.3, start-up 1) | 20 min | 45 min (full) + 65 min blind (+ 65 min once for the blind reference) |
| OP-Bench screen | about 6 min | 20 min | included in the row's full set |
| Engine-LLM screen (GC, 3 conversations) | 11 (gc9) / 24 (gc2) / 39 (gc3) / 33 (window) / 40 (S28) min | 45-150 min | ingest + 45 min + 65 min blind |
| Reference runs | once per ref arm + commit + slice: 7 (LoCoMo 80), 5 (c3), 6 (OP-Bench) min | one run | `clean-jina-full` already exists |

Whole queue: **sample about 6.5 h** (B1 2.3 h, B2 2.9 h, B3 1.3 h, references included) against the 17 h floor / 25.3 h ceiling of
the focus-first plan; **final** about 110 min per promote_candidate with a blind set (+ 65 min once), so 5 candidates are about
10 h. Each further sample iteration of one non-GC screen costs about 7 min. These are planning figures derived from the measured
runs above; no sample run has been made yet.

## Targeted slices (FAIL-SLICE, 2026-10-13)

Why. The random LoCoMo samples hold only 13-15 wrong questions, almost none in the class a screen targets, so a sample cannot show
whether a screen fixes its class. Reference reruns are deterministic (0 of 80 flips, `LOCOMO_PROBLEM_MAP.md`), so every flip is caused
by the screen. A targeted slice is mostly the questions the screen is meant to fix, plus controls that catch regressions.

Built by `evals/build_targeted_slices.py` (seed 20261013) from the problem labels of `clean-jina-full`
(`analysis/locomo_problem_labels_clean_jina_full.txt`) and the classes of `analysis/locomo_solution_map.json`; files
`analysis/fail_slice_<name>_s20261013[_c3].json`, same format as the sample slices (`query_ids`, `category`, `slice_id`, `hash`) plus
`target_ids`, `control_ids`, `target_class`, `family`. Ids only, no text. Each slice = all wrong questions of the family (cap 60,
class-stratified when over the cap) + correct controls matched on conversation x category (about 40% of the slice). `gold_error`,
`judge_false_negative`, `no_gold_resolvable`, `write_path_loss` are never targets.

| Slice | Classes | Wrong | Controls | n | `_c3` variant (wrong / controls) |
|---|---|---|---|---|---|
| `fail_list` | list_aggregation_generic, list_incomplete, count_wrong | 60 (of 62) | 40 | 100 | 31 / 21 |
| `fail_retrieval` | open_domain_inference_retrieval, temporal_evidence_missing, multi_evidence_recall, vocabulary_mismatch_recall, implicit_or_paraphrased_gold | 55 | 37 | 92 | 27 / 18 |
| `fail_fusion` | fusion_loss_* (3), rerank_keep_cut, lexical_outranked_recall | 23 | 15 | 38 | 12 / 8 |
| `fail_temporal` | date_duration_arithmetic, date_offset_small, date_wrong_event, relative_date_gold_vs_judge | 20 | 13 | 33 | 11 / 7 |
| `fail_reader` | wrong_detail, refusal_premise_denial, inference_open_domain, distractor_line, image_caption_misuse, speaker_confusion | 60 (of 96) | 40 | 100 | 45 / 30 |
| `fail_mix_120` | 23 fixable classes, 60 wrong class-stratified (floor 2) | 60 | 60 | 120 | 60 / 60 |

`lexical_outranked_recall` sits in `fail_fusion` (a cut/outrank loss) and `speaker_confusion` in `fail_reader`; the brief did not place them.
The `_c3` variant keeps the 3 conversations with the most failures of the family (engine-LLM rows are ingest-bound, so they ingest 3
conversations, like `sample_slice_loc_60_s20261013_c3`).

Conversations. The owner allowed all 10 conversations for the logged full run (2026-10-10), so the slices use dev and held-out conversations
(held-out policy of `build_forensics_explorer.py`: conv-43/44/47/48/49/50 dropped by default). Every slice says which in its metadata:
`uses_heldout_conversations`, `dev_conversations_used`, `heldout_conversations_used`, `conv_policy`. They are screening slices, never blind sets.

**Reading rule.** A targeted slice is NOT an estimate of overall accuracy. Read two numbers, printed by `eval_sample.py` and shown as
`tgt_rec` / `ctl_lost` by `python evals/v2_ledger.py summary`:

* `tgt_rec a/b`: target-class questions recovered (wrong in the reference run, right in the arm) / wrong target questions in the slice
  (wrong in the reference run). Per-class split in the `eval_sample.py` output.
* `ctl_lost a/b`: controls lost (right in the reference, wrong in the arm) / controls the reference had right.

A good screen recovers a visible share of its target class with `ctl_lost` near 0. The ledger gained two columns at the end; a ledger file
that already exists keeps its header and the two values are written into `notes` (`tgt_rec=a/b ctl_lost=a/b`), which `summary` also reads.

**Screen to slice mapping** (`evals/v2_slice_map.tsv`, written by the builder; read by `run_v2_queue.sh` in MODE=sample). Columns: screen,
set, slice (reader-flag rows), slice for engine-LLM rows (the `_c3` file), note. A screen with several rows runs once per slice, run id
`smp-<id>-tgt-<family>-<stamp>`, each with its own cached reference (`smp-ref-<ref arm>-<commit>-<slice_id>`). A screen without a row keeps
the random sample slices (old behaviour); `V2_SLICE_MAP=` (empty) turns the map off, `V2_SLICE_MAP=file` uses another one. OP-Bench sets
are not remapped. The mapping comes from each screen's primary target classes in the solution map (secondary classes if it has no
primary, one slice), with the owner's examples as overrides; PARITY / REF / REPORT rows (S05, S25, S26, S29, S47, S65, S66) and screens
without target classes (S41, S42, S49, S50 ...) are not mapped.

| Slice | Screens |
|---|---|
| `fail_list` | S34, S35, S40, S06, S11, S56, S64 |
| `fail_retrieval` | S55, S56, S61, S64, S01-S04, S13, S24, S28, S30-S33, S39, S44, S45, S48 |
| `fail_fusion` + `fail_mix_120` | S60, S67, S14, S51 |
| `fail_fusion` | S10, S12, S15, S16, S36-S38 |
| `fail_temporal` | S62 (write-v2 and the W2 / temporal bundles) |
| `fail_reader` | S63 (plus its OP-Bench sample slice), S19, S53, S54 |
| random sample slices (not mapped) | S05, S25, S26, S29, S47, S65, S66 (parity / report), S41, S42, S49, S50 (no target class) |

S56 also runs `fail_list` (its primary classes are list_incomplete and count_wrong; it sits on the S55 deep reader). The exact table is the
TSV; `evals/tests/test_fail_slices.py` checks it against the builder.

GPU time (planning figures from the table above; 40 mapped screens: 15 reader-flag rows, 25 engine-LLM rows): focus runs of these rows
cost about 4,720 min (805 + 3,915); the targeted runs about 1,250 min (128 + 1,119), a saving of about 3,470 min (58 h, 74%), plus
one-off references per ref arm x slice (about 7 min reader-flag, about 39 min gc3 per `_c3` slice; at most about 280 min for all six
families). The random sample runs of the same rows would cost about 1,120 min, so targeted slices cost about 10% more than the random
samples but hold 20-60 wrong target questions instead of 13-15 mostly off-target ones.

## Category 5 (adversarial) slices (S20-CAT5, 2026-10-14)

The category 1-4 slices above never contain LoCoMo category 5 (`run.sh` defaults to `--categories 1,2,3,4`; the source run has none). Category 5
is where a relevance gate should help: the question is about something never said, and the only correct answer is a refusal.

* **Scoring.** The dataset gives a cat-5 question the gold `ABSTENTION_GOLD` ("Not mentioned in the conversation"), never the file's
  `adversarial_answer` (that is the distractor, kept for analysis only), and no gold turns. `ABSTENTION_BINARY_PROMPT` (the judge) marks the
  answer CORRECT if it says the information is not mentioned or points out the wrong person, WRONG if it answers concretely. `eval_sample.py`
  counts a win when the arm refuses and the reference did not, a loss the other way. A cat-5 sample is not comparable with the 1-4 slices,
  and has no `tgt_rec` / `ctl_lost`. Caveat: the `J` flags include `--retry-refusal`, which re-asks after a refusal; it applies to the reference
  and the arm alike, but it can turn a correct refusal into an answer, so a cat-5 number is a measure of "refuses and keeps refusing".
* **Slices** (`evals/make_cat5_slice.py`, seed 20261013, from `data/locomo10.json`, ids only): `analysis/sample_slice_loc_cat5_s20261013.json`
  (80 of the 446 cat-5 questions, all 10 conversations, largest remainder on the per-conversation population: 9/4/7/11/12/6/7/9/7/8) and
  `..._c3.json` (30 questions in conv-42/48/50, the conversations of the 60-question gc3 sample, for engine-LLM rows). Metadata `categories: [5]`.
* **Map flag.** A sixth column `categories=5` in `v2_slice_map.tsv` makes the queue pass `--categories 5` to `run.sh` (the dataset filters by
  category before `--query-ids`, so the flag is required) for the arm AND its reference. The reference gets its own cached name
  (`smp-ref-<ref arm>-<commit>-sample-loc-cat5-80-s20261013-cat5`); a row without the column behaves exactly as before.
* **Mapped screens.** S20 (`read.gate_v2`): cat5 plus `fail_mix_120` (it must not break correct 1-4 answers), and its OP-Bench sample slice as
  before. S55 (`deep_v2`) and S63 (read-v2-safe) also get a cat5 row because their arms contain `gate_v2`; S55's reference is the S20 arm, so
  its cat5 row is relative to the gate. S56 (`scr-b7-obligations`) contains the gate too but targets lists and is not mapped.

## How to run the whole batch

1. Merge the wiring branches first. The queue is pinned to ONE commit (`--commit`, default HEAD) and `pinned_run.sh` builds that
   commit's venv, so the commit must contain the flags, `evals/run_v2_queue.sh`, `v2_queue.tsv`, `v2_py.sh`, `v2_behavioural.py`
   and `evals/arms/v2-*.json`. Do not change the commit mid-queue.
2. `python -m pytest evals/tests/test_v2_arms.py -q -rs`: the skip list names every flag the commit still lacks. A screen whose flag
   is missing is not run (its behavioural step reports ERROR and the row becomes BLOCKED).
3. `PINNED_DRY_RUN=1 bash evals/run_v2_queue.sh` prints every command and runs nothing.
4. Start Ollama (qwen3.5:9b, reader/judge and engine roles) and run `MODE=sample bash evals/run_v2_queue.sh` (sample is the default; see
   the sample-first workflow above; the focus-first description below is `MODE=focus`, the full + blind run is `MODE=final`). Run it again after a crash: finished
   rows are skipped (`--rerun` forces). Limit with `--only S01,B2`.
5. Read `evals/runs/_logs/v2_queue.log`. Adoption is a separate step: `suite/gate.py` composed with the behavioural report, and the owner.

Fixed for every row: reader and judge qwen3.5:9b with thinking off, `--qa-prompt grounded --retry-refusal --judge-guards
--judge-date-check`, embedder Qwen3-Embedding-0.6B, reranker jina-reranker-v3.5 (decided; no further reranker testing). Eval scope:
LoCoMo cat 1-5, OP-Bench, MAB-CR, ConvoMem, PrefEval; LongMemEval excluded. Engine-LLM rows (kind GC) add
`--memspine-build-sleep --max-model-calls 4000`.

## Gates and rules

- Sets, in the order of P0-01: focus slice (LoCoMo 437 q, `focus_slice_r7protect.json`; OP-Bench dev 331 probes,
  `focus_slice_opb.json`), then full (LoCoMo 1,540 q vs `clean-jina-full`, 81.4%), then blind (MAB-CR 400 + ConvoMem 200, sealed
  one-shot, reference `bv-scr-pool-protect-jina-*`, built once on first need, about 65 min).
- Paired gate verdicts: PROMOTE / HOLD / NO CHANGE / REJECT / INCOMPLETE / INCOMPARABLE (manifest keys differ). Band 0.328*sqrt(n).
- Adoption needs ALL of: paired gate PROMOTE on the rule's sets, behavioural suite with no regression against
  `suite/behavioural_baseline.json`, no safety failure. A safety failure (`SAF-TENANT-*`, `SAF-VIEWER*`) is REJECT whatever else holds.
  NONINF rows only need "not REJECT"; the queue marks them CANDIDATE and the owner decides whether non-inferior is enough.
- Runner stops: INCOMPARABLE or a safety REJECT stops the rest of that batch; INCOMPLETE or REJECT stops that screen only.
- Blind sets are scored once per arm (commit + flags hash). The runner passes `--reexpose` only if you give it a reason.
- The behavioural step (CPU, model-free, `evals/v2_behavioural.py`) runs first on each row: it builds the golden-harness engine plus
  the arm's flag overlay, so a safety failure costs no GPU time.

## Batches

| Batch | What | Shares | Rows | Est. GPU time (upper bound) |
|---|---|---|---|---|
| B1 | P0 re-measure: P0-06 engine-LLM re-screens, P0-02 venv reproduction, P0-07 A03 | Ollama qwen3.5:9b + jina + embedder warm; ingest-heavy | 6 | 9.3 h |
| B2 | Read-path flags (R3-*, T5-02) vs the Jina reference | same warm-up; about 20 min per focus screen | 19 | 13.5 h |
| B3 | Graph key identity on the gc3 arm (F-01) | same; needs S02 | 1 | 2.5 h |

Total: 26 rows, 3 batches, 1,515 min = 25.3 h if every conditional set runs (full and blind sets run only after the earlier set
passes). Floor if nothing promotes: about 17 h (first set of each row), plus 65 min if any row reaches blind. The 20 min per focus
screen is an estimate: the 41 min clean-jina-full run on 1,540 q scaled to 437 q, plus the 2-q check and ingest. Engine-LLM rows use
the ingest estimates in `evals/analysis/GAP_REGISTER.md` (gc2 70 min, gc3 2 h, gc9 25 min) plus a focus run. S03 (window) is the
least certain: its only measured run was a failing extractor.

## Queue


| id | batch | package | flag | arm file | reference | sets | rule | after | est min | status |
|---|---|---|---|---|---|---|---|---|---|---|
| S01 | B1 | P0-06 | `(thinking fix; existing arm)` | `scr-gc9-summary-jina` | `f-jref2-i2` | focus | PROMOTE on first set and full; blind CONFIRMS |  | 45 | queued |
| S02 | B1 | P0-06 | `(thinking fix; existing arm)` | `scr-gc3-graph-jina` | `f-jref2-i2` | focus | PROMOTE on first set and full; blind CONFIRMS |  | 150 | queued |
| S03 | B1 | P0-06 | `(window_extract; existing arm)` | `scr-window-facts-jina` | `f-jref2-i2` | focus | PROMOTE on first set and full; blind CONFIRMS |  | 120 | queued |
| S04 | B1 | P0-06 | `(thinking fix; existing arm)` | `scr-gc2-facts-jina` | `f-jref2-i2` | focus | PROMOTE on first set and full; blind CONFIRMS |  | 90 | queued |
| S05 | B1 | P0-02 | `(none: reproduction run)` | `scr-pool-protect-jina` | `clean-jina-full` | full | NO CHANGE (answers identical) |  | 43 | queued |
| S06 | B1 | P0-07 | `query_contract=heuristic (existing arm)` | `scr-contract-jina` | `clean-jina-full` | full > blind | PROMOTE on first set and full; blind CONFIRMS | S05 | 108 | queued |
| S09 | B2 | P0-01 | `(none: same-commit focus reference)` | `scr-pool-protect-jina` | `-` | focus | reference run |  | 20 | queued |
| S10 | B2 | R3-01 | `read.tiers_v2` | `v2-R3-01-tiers_v2` | `f-jref2-i2` | focus | not REJECT (non-inferior) |  | 20 | queued |
| S11 | B2 | R3-02 | `read.contract_rules` | `v2-R3-02-contract_rules` | `f-jref2-i2` | focus > full > blind | PROMOTE on first set and full; blind CONFIRMS | S06 | 128 | queued |
| S12 | B2 | R3-03 | `read.router_rules` | `v2-R3-03-router_rules` | `f-jref2-i2` | focus | not REJECT (non-inferior) |  | 20 | queued |
| S13 | B2 | R3-05a | `read.entity_leg_v2` | `v2-R3-05a-entity_leg_v2` | `f-jref2-i2` | focus > full > blind | PROMOTE on first set and full; blind CONFIRMS |  | 128 | queued |
| S14 | B2 | R3-06 | `read.family_fusion` | `v2-R3-06-family_fusion` | `f-jref2-i2` | focus | not REJECT (non-inferior) |  | 20 | queued |
| S15 | B2 | R3-08 | `read.lift_before_rerank` | `v2-R3-08-lift_before_rerank` | `f-jref2-i2` | focus | not REJECT (non-inferior) |  | 20 | queued |
| S16 | B2 | R3-07 | `read.rerank_v2` | `v2-R3-07-rerank_v2` | `v2-S15-i2` | focus > full | not REJECT (non-inferior) | S15 | 63 | queued |
| S17 | B2 | R3-09a | `read.exact_budget` | `v2-R3-09a-exact_budget` | `f-jref2-i2` | focus | not REJECT (non-inferior) |  | 20 | queued |
| S18 | B2 | R3-09b | `read.evidence_bundles` | `v2-R3-09b-evidence_bundles` | `v2-S17-i2` | focus | not REJECT (non-inferior) | S17 | 20 | queued |
| S19 | B2 | R3-10 | `read.neighbours_v2` | `v2-R3-10-neighbours_v2` | `f-jref2-i2` | focus | not REJECT (non-inferior) |  | 20 | queued |
| S20 | B2 | R3-11 | `read.gate_v2` | `v2-R3-11-gate_v2` | `f-jref2-i2` | OP-Bench dev > focus > blind | PROMOTE on first set and full; blind CONFIRMS |  | 105 | queued |
| S21 | B2 | R3-12 | `read.deep_v2` | `v2-R3-12-deep_v2` | `v2-S20-i2` | focus | not REJECT (non-inferior) | S20 | 20 | queued |
| S22 | B2 | R3-16 | `read.obligations` | `v2-R3-16-obligations` | `v2-S21-i2` | focus | not REJECT (non-inferior) | S21 | 20 | queued |
| S23 | B2 | R3-13 | `read.external_v2` | `v2-R3-13-external_v2` | `f-jref2-i2` | OP-Bench dev | report only |  | 20 | queued |
| S24 | B2 | R3-14 | `read.timeview_v2` | `v2-R3-14-timeview_v2` | `f-jref2-i2` | focus > blind | not REJECT (non-inferior) |  | 85 | queued |
| S25 | B2 | R3-15 | `read.cache.enabled` | `v2-R3-15-cache-enabled` | `f-jref2-i2` | focus | NO CHANGE (answers identical) |  | 20 | queued |
| S26 | B2 | R3-15 | `read.parallel_legs` | `v2-R3-15-parallel_legs` | `f-jref2-i2` | focus | NO CHANGE (answers identical) |  | 20 | queued |
| S27 | B2 | T5-02 | `read.trust_rendering` | `v2-T5-02-trust_rendering` | `f-jref2-i2` | focus > OP-Bench dev | not REJECT (non-inferior) |  | 40 | queued |
| S28 | B3 | F-01 | `graph.key_v2` | `v2-F-01-key_v2` | `v2-S02-i2` | focus | not REJECT (non-inferior) | S02 | 150 | queued |
| S29 | B4 | F-13c | `(none: eval adapter on the public API)` | `scr-pool-protect-jina` | pre-port sample reference | sample (focus) | PARITY: NO CHANGE (answers identical) |  | 7 | queued |

Notes per row:

- S01-S04 (P0-06): the four engine-LLM arms on the think=False fix (P0-05, done). Reference `f-jref2-i2`. Order follows the register
  (gc9, gc3, window, gc2). The earlier f-gc9 net +15 was measured with thinking on; these are the first valid numbers. Arm files are
  the existing `scr-*` arms, so they have no `v2-` copy.
- S05 (P0-02): `scr-pool-protect-jina` on all 1,540 in a fresh per-run venv. It must reproduce 81.4% (1,253): the paired verdict vs
  `clean-jina-full` is expected NO CHANGE. Anything else is flagged NOT_ADOPTED and means the new venv changed the result.
- S06 (P0-07): A03 (`scr-contract-jina`, focus +15/-7, net +8 HOLD) on the full set vs `clean-jina-full`, then blind. Gate: PROMOTE on
  1,540 then CONFIRMS on MAB-CR and ConvoMem. S11 (the rules version of the contract) is compared with S06 by hand.
- S09: a same-commit focus reference (`f-jref3-i2`). Unused unless `REF_OVERRIDE=f-jref3-i2` is set after an INCOMPARABLE.
- Stacked rows (S16 on S15, S18 on S17, S21 on S20, S22 on S21, S28 on S02): the arm file carries the prerequisite flags too and the
  reference is the prerequisite's own focus run (`v2-<id>-i2`), so the paired delta isolates one flag. They are BLOCKED if the
  prerequisite did not finish. S16 also needs R3-07's gate "matches 81.4%": its full run is compared with `clean-jina-full`.
- S23 (R3-13 external knowledge) is report only and never enters a headline number.
- S25 and S26 (read.cache.enabled, read.parallel_legs) must give NO CHANGE on answers; the latency gain is read from the run timing,
  not the gate. They are mapped to R3-15 by name only; confirm the mapping.
- S28 (F-01): `graph.key_v2` on the gc3 graph arm, reference S02. It rebuilds the graph, so about 2.5 h.

## Placeholders: packages with no flag name yet

These have a screen in the plan but no flag name was given, so they have no arm file and no queue row. To add one: arm =
`scr-pool-protect-jina` + the flag (file `v2-<pkg>-<flag>.json`), add a row to `evals/v2_queue.tsv`, add the flag to `PENDING_FLAGS`
in `evals/tests/test_v2_arms.py`.

| Package | Gate in the plan | Likely sets |
|---|---|---|
| F-02c, F-03 | GC arm screen before the graph default counts; shadow index switch | focus on the gc3 arm |
| W2-02, W2-03 | Focus slice non-inferior | focus |
| W2-04 | False-positive screen on the behavioural suite | behavioural only |
| W2-05, W2-06b, W2-06c, W2-11 | Window arm screen; calls per turn down | focus on the window arm (S03 as reference) |
| W2-10 | MAB-CR non-inferior; A-B-A tests | focus + blind |
| W2-13, W2-14, W2-15 | Calls down; temporal up; omitted-fact recall | focus on the window arm |
| MD-01, MD-02 | Worker-tier screen per role; English R@k within band | focus |
| O4-02 | G-C9-style screen (S01 is its precursor) | focus on the gc9 arm |
| O4-03, O4-04 | MAB-CR non-inferior; zero wrong numeric merges | focus + blind |
| O4-05, O4-07 | OP-Bench, PrefEval, MAB-CR non-inferior; OP-Bench and PrefEval up | OP-Bench dev + blind |
| O4-06, O4-08, O4-09 | Global-question workload; refresh on watermark; context tokens down at no QA loss | focus |
| T5-01, T5-07 | Read cost of the bound; N1 attack/utility frontier | behavioural + N1 run |
| R3-05b, R3-05c | Graph one-hop leg; episode leg | after F-02c / O4-02 |

Not screens (no command): F-14b (owner decision per item); EX-1a, EX-1, EX-2, EX-3, EX-4, EX-5 (exit gates composed of the rows above);
R6-01 (freeze the configuration, one sealed blind run per set; do it after every row above is decided).

## Open questions

1. Reference environment: `f-jref2-i2` was run on an older commit and environment. If the manifest comparability keys differ, the
   first paired score gives INCOMPARABLE and stops B1. `REF_OVERRIDE=f-jref3-i2` (row S09) is the fallback, so S09 is best run first
   or on demand.
2. PrefEval has no frozen blind split yet (`analysis/blind_split_prefeval.json` is "to be created") and `eval_blind.py` only scores
   MAB-CR and ConvoMem, so PrefEval is in no row. The P0-08 suite lists it as a blind set.
3. OP-Bench focus scoring compares with the stored `r7-protect-opb` baseline (a different engine arm), not with a Jina OP-Bench
   reference run; fixing that needs `eval_focus.py --opb` to accept `--ref`.
4. `run_blind_validation.sh` does not pass the `--qa-prompt grounded ...` reader flags. The queue follows it as is, for the arm and its
   reference alike. Decide whether blind runs should use the same reader flags as the LoCoMo screens.
5. Flag value shapes: the arm files set each named flag to `true`. If a wiring agent made one an enum or a section, edit that arm
   file (the test will say so, because validation then fails).
6. The behavioural overlay covers `read`, `write` and `graph` keys only. Flags that need a model cannot show there and come out as
   NOT_SUPPORTED, which never blocks.

## Coordinator answers (2026-10-13)
1. Reference comparability: keep S09 (fallback reference on the queue commit) first in B2; use `REF_OVERRIDE=f-jref3-i2` if S01 comes back INCOMPARABLE.
2. PrefEval: follow-up task: freeze a PrefEval blind split (`evals/freeze_blind_slices.py`) and add it to `eval_blind.py`, before any blind exposure of read-path promotions.
3. OP-Bench reference: follow-up task: `eval_focus.py --opb` accepts `--ref` so OP-Bench screens compare paired runs.
4. Blind reader flags: yes. `run_blind_validation.sh` must pass the same reader flags (`--qa-prompt grounded`) as focus/full runs; otherwise the comparability key differs and the gate returns INCOMPARABLE.
5. Flag shapes: arm files are regenerated from the merged config schema after the wiring branches merge.
6. Unnamed flags: rows are added as W2/O4/T5 wiring lands.
7. Queue commit: the queue is re-pinned after all wiring branches merge.

## F-13c parity check (S29)

F-13c moved `memspine_system.py` onto the public API (`memspine.open`, a scoped `Memory` handle for read/compose/sleep). The
claim is "no behaviour change", so S29 runs the reference arm `scr-pool-protect-jina` on the new commit and compares it with a
sample reference built on the commit before the port (`7ddd111`). The expected paired-gate verdict is NO CHANGE (identical
answers); anything else is flagged. CPU parity is already covered by `evals/tests/test_adapter_parity.py` (hash embedder,
synthetic dataset, snapshot from the base commit). Run order, GPU:

1. Build the reference on the pre-port commit: `MODE=sample bash evals/run_v2_queue.sh --only S25 --commit 7ddd111`
   (any row whose reference arm is `scr-pool-protect-jina` builds `smp-ref-scr-pool-protect-jina-7ddd111<...>-<slice>`; the
   run id uses the first 8 characters of the commit).
2. Run the parity row on the F-13c commit with the reference pinned to the old one:
   `SMP_REF_COMMIT=<8-char pre-port commit> MODE=sample bash evals/run_v2_queue.sh --only S29 --commit <F-13c commit>`.
   `SMP_REF_COMMIT` makes the runner reuse the pre-built reference and fail loudly if it is missing, instead of rebuilding it
   on the new commit (which would compare the new code with itself).
Dry run first: `PINNED_DRY_RUN=1 SMP_REF_COMMIT=7ddd1119 MODE=sample bash evals/run_v2_queue.sh --only S29`.

## Sample findings 2026-10-13 (B2, reader-only J arms)

Forensics check (`evals/runs/smp-S*-loc-1013-1228--forensics/forensics.jsonl` vs the per-slice reference):

| Screen | Flag | fused / final / context differ (of 80) | Reading |
|---|---|---|---|
| S10 | `read.tiers_v2` (R3-01) | 0 / 0 / 0 | Inert: the J arm builds no derived layers, so there is nothing to tier. Re-screen on a GC arm (sleep on, L2/L3 populated). |
| S11 | `read.contract_rules` (R3-02) | 0 / 0 / 80 | Changes composition only (context differs on every item); +1/-1, net 0. Keep iterating. |
| S12 | `read.router_rules` (R3-03) | 0 / 0 / 0 | Inert on LoCoMo J arm: routes resolve to the same legs. Needs a layered (GC) arm or a router-sensitive set. |
| S13 | `read.entity_leg_v2` (R3-05a) | 0 / 0 / 0 | Inert: no entity graph without the GC sleep. Re-screen on the gc3-graph arm (S02 was the best B1 screen). |
| S14 | `read.family_fusion` (R3-06) | 80 / 80 / 74 | Live: +4/-1, net +3. Candidate for a focus run. |

Rule taken: a 0/0/0 forensic diff means the screen did not exercise the flag; it is marked `iterate`, never `drop`, and re-queued on an arm that builds the layer it needs.

## GPU paused 2026-10-13 15:20 (owner request)

The owner took the GPU for another task. B2 stopped: S16 (`read.rerank_v2`) crashed at 13:52 with a host `MemoryError` while several CPU test suites ran in parallel, and hung until killed; S17 was killed at start. Unrun and to resume when the GPU is released: S16 (rerun), S17–S27, B3 (S28), B4 (S29), and the B5/B6 GC-arm plan. Lesson: do not run a GPU screen while more than one full unit suite is running on the same host.

## Resume list (GPU taken by the owner, 2026-10-13)

Nothing below runs until the GPU is free again. In this order: S16 (rerun), S17-S27 (rest of B2), B3 (S28), B4 (S29), then B5 and B6 below.
Dry runs that load models are also held; the B5/B6 rows were checked statically only (see "Static validation").

## B5 / B6 plan (Q-B5)

B5 turns the flags that need the sleep cycle (kind GC) into screens on the best B1 GC arm, `scr-gc3-graph-jina` (S02, +4 net). B6
is the one J flag worth a focus run. No GPU was used; arms and rows are static.

Arms are generated: `python evals/gen_b5_arms.py` writes `evals/arms/scr-gc3-<name>.json` = the gc3 arm plus the listed flags and nothing
else (plus an LLM role `session_close` / `window_extract`, bound like the existing gc3 roles, where the flag needs one). `--check` validates every
arm against the config schema. Sample set for GC rows: `analysis/sample_slice_loc_60_s20261013_c3.json`. Reference: a row's `ref_run`
`v2-S02-i2` resolves to the S02 arm (one cached gc3 sample run per commit and slice) so each row isolates its flag; S44 and S45 use
S43 (gc3 + `write.window_extract=facts`) because their flags only exist on a window write. Run: `MODE=sample bash evals/run_v2_queue.sh --only B5`
(or ids), later `MODE=focus ... --only B6`.

| Row | Flag(s) in the arm | Base | Reference | Kind | Set | Expected effect | Sample min | Focus min |
|---|---|---|---|---|---|---|---|---|
| S30 | `read.graph_leg_v2` + `read.legs_v2` + `graph.edge_compiler_v2` | gc3 | S02 | GC | f | New graph one-hop leg over compiled edges; multi-hop and list questions. Read with S38 + S39 for the split | 42 | 150 |
| S31 | `read.episode_leg_v2` + `read.legs_v2` (L2 from `offline.session_close_v2`) | gc3 | S02 | GC | f | Episode hits lifted to member turns; temporal and summary questions | 45 | 160 |
| S32 | `read.entity_leg_v2` | gc3 | S02 | GC | f | Entity-id boost on a built entity index (S13 was inert without it) | 40 | 150 |
| S33 | `offline.session_close_v2` | gc3 | S02 | GC | f | Session summaries and episodes added as records; modest gain or noise | 45 | 160 |
| S34 | `offline.standing_v2` + `read.standing_v2` | gc3 | S02 | GC | f, o | Standing-answer lead block; only if gc3 yields keyed state facts, else inert | 46 | 150 |
| S35 | `offline.patterns_v2` | gc3 | S02 | GC | f | Observation records (recurrence, frequency); small, may be inert on LoCoMo | 40 | 150 |
| S36 | `read.tiers_v2` | gc3 | S02 | GC | f | Re-screen of S10: depth tier chosen with derived layers present | 40 | 150 |
| S37 | `read.router_rules` | gc3 | S02 | GC | f | Re-screen of S12: leg routing over populated layers | 40 | 150 |
| S38 | `read.legs_v2` | gc3 | S02 | GC | f | Leg registry and BM25 lexical leg replace the old legs; decomposes S30/S31 | 40 | 150 |
| S39 | `graph.edge_compiler_v2` | gc3 | S02 | GC | f | Compiled edges only; expected near-identical reads, tests the stage | 42 | 150 |
| S40 | `offline.communities_v2` | gc3 | S02 | GC | f | Community summaries; adds model calls | 50 | 170 |
| S41 | `offline.reconcile_v2` | gc3 | S02 | GC | f | Event-time re-fold of facts; helps knowledge-update and temporal | 40 | 150 |
| S42 | `offline.histories_v2` | gc3 | S02 | GC | f | Near-duplicate merge and histories; small | 40 | 150 |
| S43 | `write.window_extract=facts` | gc3 | (reference) | GC | f | Reference for S44 and S45 (REF, built automatically in sample mode) | 55 | 150 |
| S44 | `write.envelope_v2` | gc3 + window facts | S43 | GC | f | Validated assertion envelopes; fewer ungrounded facts | 55 | 170 |
| S45 | `write.segmentation_v2` | gc3 + window facts | S43 | GC | f | Gap/day cuts and batched window calls; mostly cost | 55 | 170 |
| S46 | `offline.profile_card_v2` (+ `session_close_v2`) | gc3 | S02 | GC | f | No read consumer of the card exists yet: expected inert | 45 | 160 |
| S47 | `offline.decay_v2` | gc3 | S02 | GC | f | Tiers do not change ranking (O-22): expected PARITY | 40 | 150 |
| S48 | `write.stamping_v2` | gc3 | S02 | GC | f | Three clocks, typed speakers; mostly integrity, small | 40 | 150 |
| S49 | `write.role_policy_v2` | gc3 | S02 | GC | f | Echo guard and role routing; LoCoMo has few echoes | 40 | 150 |
| S50 | `write.intake_v2` | gc3 | S02 | GC | f | Idempotent intake gateway; expected parity | 40 | 150 |
| S51 (B6) | `read.family_fusion` | Jina J reference | `f-jref2-i2` | J | f | S14 sample was live, +4/-1 net +3; confirm on 437 | 7 | 20 |

Order is by expected value: graph leg, episode leg, entity leg, session close, standing, patterns, the S10/S12 re-screens, then the rest.
S38 and S39 are decomposition rows so S30 and S31 can be read as (flag + its prerequisites) against (prerequisites alone). Dependency
notes: `graph_leg_v2` needs `legs_v2` and the compiled edges (which come from the gc3 `extract_graph` facts); `episode_leg_v2` needs
`legs_v2` and L2 episodes (taken from session close, which costs one call per session, instead of the heavier `window_extract=all`);
`profile_card_v2` needs `session_close_v2` to receive ops; `envelope_v2` and `segmentation_v2` need `window_extract`.

Sample GPU time for B5: about 16 h (rows 865 min, plus the gc3 reference 39 min if not cached from B1 and the S43 reference 55 min
= about 960 min). The first six rows alone are about 5 h with the reference. B6 sample adds 7 min, focus 20 min. These follow the
existing per-unit estimates (gc3 39 min, window 33 min, small extras for the added stage); no B5 run has been made.

Not expressed or weak: `offline.profile_card_v2` has no read-side consumer in this commit, so QA cannot see it (the row only proves the
stage runs); `offline.decay_v2` is ranking-neutral by design; `offline.standing_v2` needs keyed state facts, which the gc3 graph facts may
not supply (check the `job.standing` tap: built count 0 means inert, re-arm on a window-facts base). `write.window_extract=all` was not
used for the episode leg (session close gives L2 more cheaply); add it as a variant if S31 is inert.

### Static validation

`python evals/gen_b5_arms.py --check` and `pytest evals/tests/test_b5_arms.py evals/tests/test_v2_arms.py` (CPU only, no models): all 21 arms equal
the generator output and pass `validate_engine_config` (so no unknown key); every row's arm file exists and its reference resolves to a
queue row of the same kind. The queue runner itself was not run, not even with `--dry-run`.

## Resume 2026-10-13 19:29 (GPU released)

- S16 `read.rerank_v2`: 0/0 on the 80-item sample (no answer changed) and the behavioural gate flagged **INV-FILLER** — marked `iterate`; investigate the filler invariant before any focus run.
- S17 `read.exact_budget`: 0/0 — inert on the J arm as the audit predicted (needs mined facts / a tight budget); marked `iterate`, re-screened in B7 by FU-REQS.
- 20:06: queue restarted at commit 410a4707 (includes FU-TEMPORAL) without the screens the audit found inert as configured (S18, S21–S24, S27, S46). Order: S19, S20, S25, S26, B3 (S28), B4 (S29), B5 (S30–S45, S47–S50). The inert screens return in B7 once their arms are fixed.

## B7 / B8 (FU-REQS, from `IMPROVEMENT_AUDIT_2026-10-13.md` findings 1-8 and section 3)

Static work only: no GPU, Ollama or `run_v2_*.sh` was touched. The B2/B5 rows above are unchanged and the running batch is untouched; B7 and B8 are
new rows at the end of `evals/v2_queue.tsv`, with arms generated by `python evals/gen_b7_arms.py` (`--check` validates them). Every arm passes the
strict arm validator (`memspine_evals.rehearsal.validate_engine_config`), which now also enforces the flag prerequisites in
`src/memspine/config/requires.py`, soft ones included. Tests: `evals/tests/test_b7_b8_arms.py`, `tests/unit/config/test_requires.py`,
`tests/combinations/test_profiles_v2.py`.

### Why the old rows could not show an effect, and what B7 does

| Old row | Why inert | B7 row | Change |
|---|---|---|---|
| S17 `exact_budget` | the budget never binds on the J arm | S52 (REF) + S53 | both arms carry `read.reply_reserve_tokens: 3072`, so a quarter of the 4096 cap is left for evidence; only `exact_budget` differs |
| S18 `evidence_bundles` | the J arm has no mined facts | S54 | on the window-facts gc3 arm (reference S43): its facts cite their source turns, so a bundle is more than the fact |
| S21 `deep_v2` | no bound `sufficiency` role: rules-only, zero model calls | S55 | `llm.roles.sufficiency` bound, sets `fo`; report deep calls per read (target at most 1) |
| S22 `obligations` | alone it is forensics-only | S56 | on top of the S55 arm (reference S55) |
| S27 `trust_rendering` | LoCoMo records are uniform trust | S57 | OP-Bench only, REPORT; the LoCoMo non-inferiority check is dropped. The poisoning (N1) set remains the real test |
| S23 `external_v2` | no provider; the arm now fails config validation | skip | no cache or provider can answer unseen questions, and `wikipedia`/`http` means network egress (owner decision) |
| S24 `timeview_v2` | LoCoMo QA passes no as-of bound | skip | needs a question set with a date (LongMemEval or locomo_plus with `--memspine-as-of-question-date`); the queue script has no such set and cannot be edited while it runs. Arm `scr-b7-timeview` (with `contract_rules`) is ready |
| S46 profile card | no read consumer | skip | `offline.profile_card_v2` is not linked to `read.profile_header`; the stage is covered by `test_ow_a_close_call2_profile.py` |

Skipped rows stay in the file as `#SKIP` comment lines (the queue ignores them) with the reason in the last column.

### B8: one gc3 arm per flag profile

Profiles are config presets in `src/memspine/config/presets/<name>.yaml` (`data_profile: "write-v2"`, comma lists layer). Each B8 arm is
`scr-gc3-graph-jina` + the preset + the model roles the preset cannot carry; reference `v2-S02-i2` for all.

| Row | Profile | Rule | Sets | Notes |
|---|---|---|---|---|
| S60 | `read-v2-core` | NONINF | f | legs, family fusion, lift, rerank v2, exact budget, contract and router rules, tiers |
| S61 | `read-v2-graph` | NONINF | f | + compiled edges, graph / entity / episode legs, session close; session_close role bound |
| S62 | `write-v2` | NONINF | f | whole W2 stack on gc3, not on window/gc2 (S03/S04 lose); window_extract role bound; grounding adjudicator stays `lexical` (the LLM tier is not built) |
| S63 | `read-v2-safe` | NONINF | fo | gate, deep (sufficiency bound), obligations, trust rendering, time view, neighbours |
| S64 | `offline-v2` | NONINF | f | sleep, budget, reconcile, histories, patterns, standing (both sides), communities, decay, profile card |
| S65 | `trust-v2` | REPORT | f | isolation, erasure (redact hidden on), retention, audit chain, consent, firewall gates, horizon; `viewer_required` may refuse eval reads (then RUN_FAILED is the finding and the profile belongs to the behavioural suite) |
| S66 | `run-v2` | PARITY | f | outbox, scheduler, ledger (+persist), read cache, parallel legs; `workers.transport: taskiq` is left out (needs the `[taskiq]` extra) |

Order of value: S60, S61, S62, S63, S64, S65, S66. If a profile passes, drop one flag at a time to find the carrier.

### Flag prerequisites are checks now

`MemspineConfig` raises `ConfigError` naming the missing key when a flag in `REQUIRES` is on without its prerequisite (the table is also
`FlagSpec.requires` in `memspine.config.flags.registry()`). Soft needs (a bound role) raise only in the arm validator. The cost target
`coverage_reextract_calls_per_window` (at most 1) now sees the coverage re-extraction calls, which the call-1 target could not.

**S23 decision (coordinator, 2026-10-13):** `read.external_v2` is not screened with live network egress (Wikipedia/HTTP). It stays skipped; screen it later only with an offline `cache` provider seeded from a licensed snapshot, or when the owner explicitly approves egress.

## Time-saving reorder, 2026-10-13 22:45

Bundles first, single flags only to explain a bundle result. Queue restarted (S33 lost ~10 min):
1. B5 read/offline singles still worth a look: S33, S34, S35, S38, S40, S41, S42.
2. B8 profile bundles: S60 read-v2-core, S61 read-v2-graph, S62 write-v2, S63 read-v2-safe, S64 offline-v2, S65 trust-v2, S66 run-v2.
3. B7 re-armed screens: S52, S53, S55, S56, S57, S54.
4. B9: S67 (rerank_v2 search pool).
Then B6 focus (S51 family_fusion, 437 q), and a reference-vs-reference noise run.

**Deferred (run only to bisect a bundle that regresses):** S36 tiers, S37 router, S39 edge compiler alone (covered by S30), S47 decay (ranking-neutral by design), and the write singles S43–S45, S48–S50 (covered by the write-v2 bundle S62). S46 stays skipped. Saves about 4–5 GPU hours.

## FX-EVAL (2026-10-13): eval and engine fixes behind inert screens

Findings from `docs/v2/data/screen_internals_sleep.json`. Fixes are in the adapter, the sleep order and the arm generators; every engine
change is flag-gated (flags off, the order and behaviour are unchanged).

- **Session close in evals (S33, S46, S61 episode leg, S31 L2 source).** `MemspineSystem.build()` now calls `Engine.close_session(ns, sid)`
  for every ingested session when `offline.session_close_v2` is on, before the build sleep (skipped when `write.window_extract` is on: the
  final window flush already fires the hook; the stage is idempotent). The calls are billed to the write cost; the per-session result is in
  `meta["session_close"]`. The arms already bind `llm.roles.session_close` (checked: sessionclose, episodeleg, profilecard, b8 read-v2-graph, b8 offline-v2).
- **`sleep_v2` handle (S64).** The adapter awaits `SleepHandle.wait()` and uses the returned stage stats.
- **Patterns and standing answers run after the graph (S35, S34).** With `offline.patterns_v2` / `offline.standing_v2` on, the stages now
  sit after `extract_graph`, `rule_edges`, `reconcile` and `histories` (before `compile_edges` / `summarize_entities`); taskiq priorities renumbered
  and still distinct. With both off the order is `SLEEP_CYCLE_ORDER`.
- **Community summaries (S40).** `communities_v2` calls the `summarize` role for communities of 3+ entities; the S40 arm and the B8
  `offline-v2` / `read-v2-graph` arms now bind it (qwen3.5:9b, no_think). Caveat: binding `summarize` also makes the consolidation summarizer
  use the model, so S40 against the gc3 reference differs in more than communities; read S40 with that in mind.
- **Standing answers need `kind:state` facts (S34).** gc3's `extract_graph` does write `kind:state` facts (a state edge keeps `attribute=rel`
  and carries the tag `kind:state`; `edge_fact_key`), and they pass `is_input_fact`. The stage only ran before them. The reorder fixes it, so S34
  stays on the gc3 base (no window-facts base needed). Its groups are (source entity, relation), valued by the edge sentence.

**Now meaningful (re-queue):** S33, S34, S35, S40, S46 (was skipped; its session-close leg now runs, and the card ops need the model to return ops), S61, S64, and S31 for the L2 source. Run the new tests with `evals/tests/test_fx_eval_adapter.py` and `tests/unit/wiring/test_fx_eval_order.py`.

## Targeted relaunch, 2026-10-13 23:25

Queue restarted at commit 87c2bd1c (FX-EVAL + FX-READPATH + FAIL-SLICE) with every row on its targeted slice from `evals/v2_slice_map.tsv`, ordered by LoCoMo recoverable upper bound (LOCOMO_PROBLEM_MAP): S60, S63, S64, S34, S35, S40, S61, S67, S14, S31, S33, S55, S56, S62, S38, S19, S10, S53, S54, S57, S65, S66, S41, S42. Read each targeted row by `tgt_rec` (target questions recovered) and `ctl_lost` (controls lost), not overall accuracy. Left out: S46 (no read consumer), S36/S37 (S10 and the bundles cover them), S43-S45/S48-S50 (covered by S62), S47 (parity by design), S23/S24 (skipped), S51 (focus run after S14's targeted result).

## Proposed R14 screens (not queued)

From `docs/v2/research/R14_MASTER.md` (2026-10-14). Proposals only: none is in `evals/v2_queue.tsv`, `evals/v2_slice_map.tsv` or `packages.py`. S68-S78 stay with `OBSERVATIONS_GAP_ANALYSIS.md`; S79-S100 are new and provisional for the coordinator to renumber. Flags marked (new) do not exist yet.

| Screen | Master item | Arm / change | Slice | First step without a model |
|---|---|---|---|---|
| S79 | R14-M-1 | trigger + coverage | fail_list (+ 60 matched n_gold>=3 correct as guard) | yes |
| S80 | R14-M-1 | second-retrieval bounded | fail_list (+ 60 matched n_gold>=3 correct as guard) | yes |
| S81 | R14-M-2 | session_cover | fail_retrieval (b+cf) and fail_mix_120; metric distinct gold sessions represented | no |
| S82 | R14-M-3 | protect8 + convex fusion | fail_fusion (+95 lucky/barely cases as guard) | yes |
| S83 | R14-M-3 | budget keep | fail_fusion (+95 lucky/barely cases as guard) | yes |
| S84 | R14-M-4 | doc expansion | fail_retrieval (81 ids, 21 paraphrase) | no |
| S85 | R14-M-5 | exchange_records | fail_retrieval + fail_context (OBS-GAPS) | yes |
| S86 | R14-M-6 | evidence_kind_probes | fail_retrieval (28) + all 96 cat3; oracle-gold reader-only run first to size the ceiling | no |
| S87 | R14-M-7 | date_prior | fail_temporal (13 fail + matched controls from the 180) | yes |
| S88 | R14-M-8 | duration_solve + milestones (built-off, needs a screen only) | fail_temporal date_* classes (19) + 38 unit questions | yes |
| S89 | R14-M-9 | vote | fail_reader (40 + 25 refusal rows); guards cat5 and OP-Bench irrelevance | no |
| S90 | R14-M-10 | quote-then-answer | fail_reader (46 + 59 ids); 2x2 narrow/wide window by shape on the dev slice | no |
| S91 | R14-M-10 | window_by_shape | fail_reader (46 + 59 ids); 2x2 narrow/wide window by shape on the dev slice | no |
| S92 | R14-M-10 | verify_binding | fail_reader (46 + 59 ids); 2x2 narrow/wide window by shape on the dev slice | no |
| S93 | R14-M-11 | evidence_table / two_call | fail_list (25 + list/count-shaped controls) | yes |
| S94 | R14-M-12 | reader-side join (after S77) | fail_list + new fail_multi (43, OBS-GAPS) | no |
| S95 | R14-M-13 | slot_verify diagnosed | fail_reader (37 ids); false-defect rate on the 188 correct | no |
| S96 | R14-M-17 | judge conventions v3 (re-score stored answers, no retrieval) | 23 FN ids + 34 FP ids frozen as a 57-row calibration extension; judge_calibration_dev.jsonl | yes |
| S97 | R14-M-18 | triple-column re-score of stored answers (reference run plus last two full runs) | 34 FP + 23 FN ids as a frozen calibration extension; whole-run re-score | yes |
| S98 | R14-M-19 | support-recall report | the 10 + 8 + 3 + 59 ids; guard: correct questions with context-dependent gold | yes |
| S99 | R14-M-20 | known_keys (after S34/S42) | fail_mix_120 attribute subset (33) + MAB-CR dev for the history half | no |
| S100 | R14-M-21 | link_table | fail_retrieval + fail_mix_120; CPU prototype over stored embeddings of one conversation first | no |

**S79 build (R14-LISTCOV)**: arm `evals/arms/v2-R14-M-1-list_cov.json` = J reference + `read.list_mode_trigger_v2` + `read.coverage_select`, slice fail_list (+ 60 matched n_gold>=3 correct as guard). Offline first step done by `evals/analysis/r14_listcov_trigger_check.py`: on the reference full run the trigger fires on 132/184 (71.7%) of n_gold>=3 questions vs 72/184 (39.1%) for the regex, and on 147/1123 (13.1%) of n_gold==1 vs 124/1123 (11.0%); 5-fold by-conversation refit gives 70.1% recall at the same false fire. Cost: one extra query embedding and 30-deep vector search per read, no model call.
