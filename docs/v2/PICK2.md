# PICK2: the combined "winners" arm for the final full and blind runs

Status 2026-10-14: built, validated offline, **not run**. Counts only; no benchmark text. Evidence numbers are from the screen
ledger (`evals/runs/_logs/v2_results.tsv`, sample mode, targeted slices; wins/losses are paired question counts, n in brackets).
They are descriptive screens, not verdicts.

Arms (overlays on `scr-gc3-graph-jina`, the S02 sleep-graph GC arm):

* `evals/arms/v2-PICK2-winners.json` (queue row P2)
* `evals/arms/v2-PICK2-winners-iv.json` (queue row P2IV): P2 plus `write.index_view` + `write.index_view_carry=anaphoric` (S70)

## Selection

| Screen | Flag(s) added | Evidence (ledger) | Decision | Reason |
|---|---|---|---|---|
| S02 | base: `read.graph_leg`, entity graph, sleep `extract_graph` | LoCoMo 60: +5/-1 | base | GC reference; every B5 screen is relative to it |
| S20 | `read.gate_v2` | OP-Bench 60: +4/0 (+4.45, CI +0.14..+9.78); LoCoMo 80: 0/0; cat5 80: 0/0; fail_mix_120: 0/0 | PROMOTE | only screen with an interval above zero; cat5 and correct 1-4 answers untouched |
| S82 | `read.pool_protect_per_leg=8` (was 3) | fail_fusion 38: +3/0; fail_mix_120: +4/0 (+3.33, CI +0.83..+6.67) | PROMOTE | no losses on either slice; one integer, no new code path |
| S10 | `read.tiers_v2` | fail_fusion 38: +7/-1 (fixed 7/23, broke 1/15); random LoCoMo 80: 0/0 (inert on J) | PROMOTE | widens the rerank search pool when a tier asks for depth |
| S38 | `read.legs_v2` | fail_fusion 20 (GC): +4/0 | PROMOTE | leg registry plus BM25 lexical leg; prerequisite of the episode leg |
| S31 | `read.episode_leg_v2` + `offline.session_close_v2` + `llm.roles.session_close` (arm `scr-gc3-episodeleg`; GC, includes `legs_v2`) | fail_retrieval 45: +7/-2 (CI 0..+24); random LoCoMo 60: +5/-6 | PROMOTE | targeted win; the random-slice net is -1 (see risks) |
| S34 | `offline.standing_v2` + `read.standing_v2` (arm `scr-gc3-standing`; GC) | OP-Bench 60: +17/-4 (+4.56, CI -0.75..+10.0); fail_list 52: +6/-7 | PROMOTE | OP-Bench gain; LoCoMo list slice net -1 (see risks) |
| S57 | `read.trust_rendering` (arm is a J overlay) | OP-Bench 60: +4/-1 (+2.04, CI -0.45..+4.88) | PROMOTE | trust-banded rendering; LoCoMo records are uniform trust, so no effect expected there. The N1 poisoning set is its real test |
| S77 | `read.decompose_v2` | fail_list 100: +2/0; fail_mix_120: +4/-1 | PROBABLE, included | per-entity quotas for multi-entity questions; rules only, no model call |
| S42 | `offline.histories_v2` (arm `scr-gc3-histories`; GC) | random LoCoMo 60: +6/-2 (+6.67, CI -1.67..+16.7) | PROBABLE, included | near-duplicate merge and histories; small |
| S70 | `write.index_view` + `write.index_view_carry=anaphoric` | pending. Parent S69: fail_retrieval 92: +11/-4; fail_mix_120: +15/-7; failed behavioural INV-FILLER, which the anaphoric carry fixes (now PASS) | **P2IV only** | build both so the final run shows what the index view is worth |

All keys exist in the config schema; with them on, `check_requires(strict=True)` holds (`episode_leg_v2` needs `legs_v2`,
`read.standing_v2` needs `offline.standing_v2`, `session_close` role bound).

## Exclusions

| Screen | Flag | Reason |
|---|---|---|
| S60, S61, S62, S63, S64 | bundles (b8 profiles) | mixed results; S60 (legs, tiers, family_fusion, rerank_v2, contract/router, exact_budget) was +4/0 on fail_fusion but +13/-11 on fail_mix_120, so the stack's gains were already absorbed by S38/S10; the bundle also carries unscreened members. The single winners are taken instead |
| S67 | `read.rerank_v2_search_pool` | OP-Bench 60: -4 (7 wins, 11 losses) |
| S79 | `read.list_mode_trigger_v2` + `read.coverage_select` | fail_list +6/-5, fail_mix_120 +5/-4: no net signal |
| S87 | `read.date_prior` | 0/0 on fail_temporal, +2/0 on fail_mix_120 (CI includes 0); see S14 note |
| S73, S74 | `read.inference_mode` (v1, v2) | 0/0 on cat5; +1/0 on fail_reader: below detectable |
| S14 | `read.family_fusion` | **excluded, justified below** |

S14 `family_fusion` (sample: LoCoMo 80 +4/-1, fail_fusion +6/-2, fail_mix_120 +10/-6). It is not in the coordinator's PROMOTE list and
it conflicts with the winners in three ways. (1) `router.py` takes the family-fusion branch *instead of* the per-leg RRF branch
(`if (use_hybrid or extra_legs) and read_now.family_fusion`). The `read.date_prior` step and the `pool_protect_mode` source-family
handling live only in the RRF branch, so S82 and any date prior would not behave as screened under it (date_prior would be
silently inert, with no `requires.py` check to say so). (2) Its fail_fusion gain is the same six category-4 questions that S10, S38
and S82 fix; the gains overlap and do not add, so it buys no extra recoverable questions. (3) It has never been run on a GC arm.
S82 and `legs_v2` run on the RRF path they were screened on.

## Expected effect (hypothesis, to be confirmed by the runs)

* LoCoMo 1540: +1.5 to +3 points over the J reference `clean-jina-full`, mostly category 4 and 1 (fusion and list questions: S10, S38, S82, S31),
  of which about 1 point is the S02 graph base. Interactions among eight flags are unmeasured; an overlap-adjusted net of about +2 is more
  plausible than the sum of the screens.
* OP-Bench dev 331: +4 to +8 points (S20 irrelevance, S34, S57); the largest and best-supported effect.
* MAB-CR and ConvoMem (blind): flat (the write-path bundle that targets MAB-CR is not included). Risk is only latency and cost.
* PrefEval: flat; no PrefEval set exists in `run_v2_queue.sh`, so it is not part of this run (see Gaps).
* P2IV minus P2: small positive on LoCoMo retrieval-bound questions (date words), possibly a small loss on the ones a previous-turn
  carry helped; the S70 screen decides whether P2IV is run at all.

## Offline validation (no GPU)

* `validate_engine_config` (schema, strict prerequisites, policy blocks): both arms valid. Removing `read.legs_v2` raises the
  `episode_leg_v2` prerequisite error as intended.
* `evals/v2_behavioural.py` (hash embedder, no reranker, overlay vs `scr-pool-protect-jina`), both arms identical:
  `behavioural: OK`. 10 cases: 7 PASS, 3 FAIL (INV-RENAME-SEARCH, INV-ORDER-SWAP, SAF-UNANSWERABLE). The frozen baseline fails 5
  (the same three plus INV-PARAPHRASE and SAF-GENERIC), so there is no regression and two cases improve. INV-FILLER passes in both.
* `evals/tests/test_v2_arms.py` gained a PICK2 branch (exact key set vs the S02 base, strict validation); queue, blind, cat5, ledger
  and v2 sample tests: 182 passed, 6 skipped.

## Queue rows (batch B11) and run plan

| Row | Arm | Kind | Sets (final mode) | est_min |
|---|---|---|---|---|
| P2REF | `scr-gc3-graph-jina` | GC | F (REPORT) | 190 |
| P2 | `v2-PICK2-winners` | GC | F, o, b (PROMOTE) | 300 |
| P2IV | `v2-PICK2-winners-iv` | GC | F, o, b (PROMOTE) | 300 |

* Sets: `F` LoCoMo full 1540 (cat 1-4), `o` OP-Bench dev 331, `b` blind MAB-CR 400 + ConvoMem 200. In `MODE=final` the queue derives
  F, then o if the row has it, then b for PROMOTE/NONINF rows, and the row's sets column ("Fob") is read the same way in sample and
  focus modes. The final mode runs a row only after `python evals/v2_ledger.py mark <id> promote_candidate` (P2REF, P2, P2IV);
  nothing is launched by this package. The behavioural gate runs first on CPU.
* **Reference.** `eval_paired.py` always compares F against `clean-jina-full` (J, no sleep). That is the right adoption comparison (the
  shipped reference). No GC full run exists (S02 ran only on the 437 focus slice), so **P2REF** builds one (`v2-P2REF-full`, S02 on 1540,
  no PICK2 flags) for attribution: `python evals/eval_paired.py v2-P2-full --ref v2-P2REF-full`. The blind verdict is against the J blind
  reference `scr-pool-protect-jina`, built lazily once (about 65 min) if `runs/bv-*` is missing. OP-Bench (`o`) is scored absolutely
  by `eval_focus.py --opb`; compare it to the J and GC OP-Bench runs by hand.
* Order: P2REF, P2, P2IV. Run P2IV only if the S70 screen (fail_retrieval, fail_mix_120) is not a loss.
* **Time.** Sum of `est_min`: 190 + 300 + 300 = 790 min (about 13 h); about 490 min for P2REF + P2 alone; add about 65 min if the J
  blind reference is missing. The blind part of a GC row (about 65 min for J) has no GC measurement; budget up to twice that, because the
  sleep build now runs on the MAB-CR and ConvoMem contexts.
* Two queue changes were needed, both inert for existing rows: (1) full-LoCoMo runs of a GC arm get `--max-model-calls 12000`
  (`BF` in `run_v2_queue.sh`); the 4000 cap of the focus runs would stop a 1540-question run (reader + judge + retry is about 3 calls per
  question, plus the build). (2) `run_blind_validation.sh` takes `BLIND_FLAGS` (default empty); the queue sets it to `BF` for a GC arm
  only. Before this, a GC arm's blind runs would have skipped `--memspine-build-sleep`, so the graph, session close, standing answers and
  histories would not have been built.

## Adoption rule

Average-led across the suite: LoCoMo (cat 1-4; cat5 as a guard for `gate_v2`), OP-Bench, MAB-CR (blind), ConvoMem (blind) and PrefEval.
Adopt PICK2 (or PICK2-IV) if the macro mean across benchmarks improves beyond the noise band, with a per-benchmark non-inferiority
guard at each benchmark's own noise band (`evals/analysis/GENERALISATION_AUDIT.md` 4.3). LongMemEval is excluded and never run. The blind
sets are validation only: one exposure per arm, aggregates only, no per-question reading. A pass is a CANDIDATE; adoption itself is
`suite/gate.py` plus the owner.

## Risks and gaps

* **Interactions are untested.** Every winner was screened alone (or in a small stack: S31 = legs + episode + session close). `gate_v2`,
  `tiers_v2`, `decompose_v2`, `trust_rendering` and `pool_protect=8` have only J-arm numbers, and S10 on a GC arm (S36) was skipped.
  `pool_protect=8` with `legs_v2` plus the graph, entity and episode legs protects up to 8 per leg over more legs than screened.
* S31 random-slice net -1 and S33 (session close alone) fail_retrieval -2 (cat3 -3): the session-close summaries may cost cat3 answers.
  S34 fail_list -1. Read the F category split for cat3 and list questions first.
* `gate_v2` on the GC arm uses the jina rerank score as in the screen; calibration probes run once per namespace at the first read.
* PrefEval has no run set in the queue; it needs one added before the adoption rule can use it as written.
