# Screening reranker: a lighter stand-in for Qwen3-Reranker-4B (2026-10-11)

Goal: faster fast-screens on the single 16 GB GPU while the ranking stays faithful to the 4B reranker. Final and blind
runs keep `Qwen3-Reranker-4B` 4-bit. Everything here was measured on CPU from saved traces; no GPU model was loaded
and no QA run was started (the eval queue was live).

## 1. Candidates already measured (before this test)

Retrieval-only grid, full LoCoMo 1,986 q, `evals/QWEN_STACK_RESULTS.md`. Sufficiency is retrieval sufficiency, not QA.

| Reranker | GPU pairs/s (pool 30, shared GPU) | Sufficiency (Qwen3-Emb) | Ctx tokens | Grid wall-clock | QA (1,540 q) |
|---|---|---|---|---|---|
| none | | 0.615 | 1567 | 22 min | 74.4 |
| bge-reranker-base (fastembed ONNX, CPU) | 91 (CPU) | 0.602 | 1001 | 29 min | not run |
| Qwen3-Reranker-0.6B (torch) | 139 | 0.593 | 871 | 28 min | not run |
| Qwen3-Reranker-4B 4-bit | 35 | 0.597 | 774 | 55 min | (BEST config uses it) |
| jina-reranker-v3.5 (listwise) | 196 | 0.597 | 673 | 13 min (batched writes) | 71.3 |
| bge-reranker-v2-m3, MiniLM-L6 | never benchmarked | | | | |

Jina v3.5 was the fastest, the 4B the slowest (5.6x fewer pairs/s). Those grids ran on a different engine (no protected
pool, no perspective leg), so they say how fast each model is, not how faithful it is to the 4B on today's pools. That is
what the test below measures.

## 2. Offline fidelity test

Data: run `r7-protect-full` (the current reference, protected pool I75a), `evals/runs/r7-protect-full--forensics/`
(`forensics.jsonl`: query, pool, 4B `rerank_scores`; `ingest.jsonl`: stored text per turn) and gold evidence from
`data/locomo10.json`. 150 questions of the 1,540, seeded (20261011), stratified by category (28 / 31 / 9 / 82 for
categories 1-4). Each candidate re-scored the same pools the 4B saw: mean pool 23.4 candidates (max 36), 3,510 pairs per
candidate. Document text is the engine's `concat_background` header plus the stored turn; query is the raw question.
Script: `evals/analysis/screening_reranker_fidelity.py` (CPU only, `CUDA_VISIBLE_DEVICES` hidden). Recall is gold
evidence turns found in the top 10 by raw reranker score, before the engine's windowing and floors. Pool ceiling (gold in
the pool at all): 0.831.

| Reranker | Spearman vs 4B | Kendall vs 4B | top-10 overlap | 4B-confident recall* | gold recall@10 | any-gold hit@10 | CPU pairs/s |
|---|---|---|---|---|---|---|---|
| pool order (no reranker) | 0.437 | 0.320 | 0.612 | 0.702 | 0.715 | 0.773 | |
| **Qwen3-Reranker-4B 4-bit (reference)** | 1 | 1 | 1 | 1 | **0.816** | **0.880** | |
| Qwen3-Reranker-0.6B | 0.691 | 0.534 | 0.734 | 0.860 | 0.804 | 0.853 | 3.1 |
| **jina-reranker-v3.5** | 0.668 | 0.512 | 0.716 | 0.855 | **0.822** | **0.880** | 16.2 |
| bge-reranker-v2-m3 | 0.694 | 0.538 | 0.731 | 0.868 | 0.815 | 0.873 | 13.5 |
| bge-reranker-base | 0.572 | 0.428 | 0.677 | 0.801 | 0.768 | 0.833 | 46.7 |
| ms-marco-MiniLM-L-6-v2 | 0.611 | 0.464 | 0.701 | 0.813 | 0.756 | 0.820 | 268 |

\* Share of the 4B's confident documents (P(yes) >= 0.5, at most its top 10) that the candidate also has in its top 10.
Needed because the 4B saturates: it marks a mean 7.7 documents per pool at >= 0.5 (more than 10 in 41 of 150 questions),
so which ten make its plain top-10 is partly arbitrary, and the plain overlap understates agreement for every candidate.

Paired bootstrap, gold recall@10 minus the 4B's (95% CI, 2,000 resamples over the 150 questions):

| Reranker | difference | CI |
|---|---|---|
| jina-reranker-v3.5 | +0.006 | [-0.008, +0.025] |
| bge-reranker-v2-m3 | -0.001 | [-0.016, +0.018] |
| Qwen3-Reranker-0.6B | -0.012 | [-0.027, +0.002] |
| bge-reranker-base | -0.049 | [-0.087, -0.013] |
| MiniLM-L6 | -0.060 | [-0.097, -0.028] |

By category (gold recall@10, n = 28 / 31 / 9 / 82): 4B 0.599 / 0.769 / 0.426 / 0.951; Jina 0.590 / 0.785 / 0.500 / 0.951;
bge-v2-m3 0.589 / 0.758 / 0.537 / 0.945; Qwen 0.6B 0.576 / 0.774 / 0.389 / 0.939. Category 3 has nine questions, so
ignore its differences.

CPU speed is only a relative guide (3.1 pairs/s for the fp32 0.6B Qwen, 16 for Jina in bf16, 268 for MiniLM); the 4B
was not run on CPU because its scores were already saved.

## 3. Recommendation: jina-reranker-v3.5

Criterion as written: top-10 overlap with the 4B >= ~85% and gold recall@10 within 1-2 points.

- **Gold recall: met.** Jina is level with the 4B (0.822 vs 0.816, any-gold hit identical at 0.880). bge-v2-m3 is also
  level; Qwen 0.6B is 1.2 points behind (inside the margin, CI touches zero); bge-base and MiniLM clearly fail (-5 and -6).
- **Top-10 overlap: not met by any candidate.** The best plain overlap is 0.73 (Qwen 0.6B, bge-v2-m3), Jina 0.72. This
  figure is bounded by the 4B's score ties (see the footnote). On the tie-aware measure the three leaders sit at 0.855-0.868
  and are indistinguishable. I treat the overlap target as met only on the tie-aware reading, and say so; a literal reading
  of "85% plain overlap" selects nothing.
- Jina over bge-v2-m3 and Qwen 0.6B because it is (a) the fastest on GPU (196 pairs/s vs 139 for Qwen 0.6B; bge-v2-m3 was
  never GPU-benchmarked and has no adapter in `services/rerank` besides a generic cross-encoder path), (b) already wired
  (`rerank: jina`, arms `qs-*-rjina`) and (c) it has the best recall point estimate. Caveats: CC-BY-NC weights, so
  screens only (not a shipped default); `trust_remote_code`; scoring is listwise, so a pool is scored as one window (cap
  `max_documents` 64, pools here are at most 36, so no tail is dropped).

### Expected speed-up (estimate, not measured on an idle GPU)

From `QWEN_STACK_RESULTS.md` throughput (pool 30, GPU shared, so conservative): the 4B scores 35 pairs/s and Jina 196, so
a 34-candidate pool costs about 1.0 s with the 4B and 0.17 s with Jina (a 23-candidate pool: 0.7 s vs 0.12 s). The
retrieval-only grid agrees: the 4B added ~33 min over 1,986 q (~1.0 s per question at pool 20), Jina ~2 min.
That saves roughly 0.6-1.5 s of the ~20 s per screen question, **about 3-8%**. This is a modest gain: the reranker is a
small part of the per-question time, which the 9B reader/judge (Ollama) dominates. The 5.6x speed difference between
the rerankers matters for retrieval-only screens and for GPU memory (the 0.6B bf16 model needs well under half the VRAM of
the 4B 4-bit model plus its activation buffers, which may relieve the 15/16 GB pressure), not much for QA screens. Measure
the real saving on the first light screen (compare `rerank` wall-clock in `forensics`/`summary.json`) before relying on it.

## 4. Arm and how to use it

`evals/arms/scr-pool-protect-lightrr.json` = `scr-pool-protect` with `rerank: jina`, `rerank_model: jinaai/jina-reranker-v3.5`
and `rerank_quant` removed (`rerank_device: cuda`). Nothing launched. Everything else, including `pool_protect_per_leg: 3`,
`rerank_keep: 10`, `rerank_floor: skip`, is identical.

Risk: **a light-reranker screen is not comparable with the 4B reference** (`r7-protect-full`, and the focus slice
`focus_slice_r7protect.json` is built from the 4B run's failures). Jina's scores are not saturated like the 4B's and the
engine min-max normalises them before the relative floor (`assembly.relative_floor: 0.3`), so the floor and the final
cut will behave differently even where the ranking agrees. The offline test measured ranking only, not those downstream effects.

Protocol for screens:
1. Run the focus-slice reference once with the light reranker: `scr-pool-protect-lightrr` on `focus_slice_r7protect.json`
   (437 q), same flags as the other screens, run id `f-lightref-i2`. This is the new light reference.
2. Every light screen is then an arm of the form `scr-<change>` with the two rerank keys above, scored by
   `eval_focus.py ... --ref f-lightref-i2`, never against `r7-protect-full`.
3. Before the light reference, check that it keeps the 4B's focus-slice result within noise (fixed/broken counts vs the 4B
   reference; the band is +-0.328 sqrt(n)). If the light reference moves far from the 4B reference, drop the idea.
4. Promote only after a re-run with the 4B: full 1,540 and blind validation always use `Qwen3-Reranker-4B` 4-bit.
   A change that only wins with Jina (for example one that exploits its unsaturated scores) is not a win.

Not tested: OP-Bench pools (LoCoMo only here), longer-turn corpora, the engine's windowing/floor after reranking, and
the effect on QA accuracy. The sample is 150 questions, so the recall CIs are about +-2 points.
