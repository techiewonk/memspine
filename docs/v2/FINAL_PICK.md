# Final pick: v2-PICK3-j (adopted 2026-10-15)

**Arm:** `evals/arms/v2-PICK3-j.json`. It is the J reference (`scr-pool-protect-jina`: jina-reranker-v3.5 and Qwen3-Embedding-0.6B, no sleep build) plus seven settings:

| Setting | Screen | Sample evidence (fixed / broken) |
|---|---|---|
| `read.gate_v2` | S20 | OP-Bench +4/−0. LoCoMo cat1–4 and cat5 unchanged |
| `read.pool_protect_per_leg = 8` | S82 | fail_fusion +3/−0, mix +4/−0 |
| `read.tiers_v2` | S10 | fail_fusion: 7 of 23 targets fixed, 1 of 15 controls lost |
| `read.trust_rendering` | S57 | OP-Bench +4/−1 |
| `read.decompose_v2` | S77 | fail_list +2/−0, mix +4/−1 |
| `write.index_view = true`, `write.index_view_carry = anaphoric` | S70 (S69 + IVFIX) | fail_retrieval +11/−4, mix +15/−7. Behavioural gate OK |

No extra model calls on the read path except the reader and judge. The index view is rule-built at ingest.

## Final measurement (same commit family, pinned runs)

| Benchmark | Reference | v2-PICK3-j | Net | Verdict |
|---|---|---|---|---|
| LoCoMo full, cat1–4 (1,540) | 81.36% (`clean-jina-full`) | **82.14%** | +58/−46 = **+12** (band ±12.9, sign p 0.28, cluster 95% [−4, +29]) | HOLD. No category below its band: cat1 +5, cat2 −3, cat3 0, cat4 +10 |
| OP-Bench dev (331) | `r7-protect-opb` | est. **+3.6 pts** (band ±3.0) | failures +0.048 mean, controls −0.058 | HOLD |
| MAB-CR blind (400) | 30.8% | **31.8%** | +22/−18 = +4 (band ±6.6) | guard ok |
| ConvoMem blind (200) | 74.0% | 74.0% | +4/−4 = 0 (band ±4.6) | guard ok |
| **Blind verdict** | | | | **CONFIRMS** |

**Adoption rule** (average-led, non-inferior on every benchmark, LongMemEval excluded): positive on LoCoMo and OP-Bench, non-inferior on MAB-CR and ConvoMem, so the arm is **adopted**. Neither LoCoMo nor OP-Bench alone is significant, so the claim is "better on average across benchmarks, worse on none". It is not a significant LoCoMo gain.

Judge-error band (R14): the raw LoCoMo numbers carry about ±1 point of judge error, in both directions. Report the strict-list column (`evals/r14_rescore.py`) next to the raw score whenever multi-hop deltas are discussed.

## Rejected or parked finals

| Run | Result | Why |
|---|---|---|
| P2REF: GC graph base (`scr-gc3-graph-jina`) | 79.35%, net −32 | REJECT. **Run with the LadybugDB FTS extension missing**: every entity-embedding write failed (1,119 errors). Fixed in `services/graph/ladybug.py` (install on first use). |
| P2: winners on the GC base (`v2-PICK2-winners`) | 82.08%, net +11 | REJECT: cat2 −10 below its band. Same FTS bug (3,369 errors). |
| G1: GTEMP temporal fix | On a working graph: mix 64 vs 66, temporal 8 vs 10 | Parked. It does not help once FTS works. |

With FTS working, the graph base beats the J reference on the GC slices: mix 66 vs 60, temporal 10 vs 7. P2REF2 (working graph base, full) and P2F (winners on the working graph, full + OP-Bench + blind) are queued to find out whether the graph returns as the default.

## What changes for users

- The recommended configuration is `v2-PICK3-j`. Every setting is a flag in the config schema; see `docs/USAGE.md`.
- To reproduce, use the queue rows `P3J` (LoCoMo) and `P3JOB` (OP-Bench and blind), or `evals/run_v2_queue.sh --only P3J --mode final`.
