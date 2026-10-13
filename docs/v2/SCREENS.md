# SCREENS: the v2 screen queue

Owner decision 2026-10-13: the GPU stays stopped, every behaviour change is built flag-off, and all screens are queued.
This file is the ordered queue; `evals/v2_queue.tsv` is the same list in machine form and `evals/run_v2_queue.sh` runs it.
Nothing here has been run. Every status is `queued`; the runner writes live status to `evals/runs/_logs/v2_queue.state`.

## How to run the whole batch

1. Merge the wiring branches first. The queue is pinned to ONE commit (`--commit`, default HEAD) and `pinned_run.sh` builds that
   commit's venv, so the commit must contain the flags, `evals/run_v2_queue.sh`, `v2_queue.tsv`, `v2_py.sh`, `v2_behavioural.py`
   and `evals/arms/v2-*.json`. Do not change the commit mid-queue.
2. `python -m pytest evals/tests/test_v2_arms.py -q -rs`: the skip list names every flag the commit still lacks. A screen whose flag
   is missing is not run (its behavioural step reports ERROR and the row becomes BLOCKED).
3. `PINNED_DRY_RUN=1 bash evals/run_v2_queue.sh` prints every command and runs nothing.
4. Start Ollama (qwen3.5:9b, reader/judge and engine roles) and run `bash evals/run_v2_queue.sh`. Run it again after a crash: finished
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
