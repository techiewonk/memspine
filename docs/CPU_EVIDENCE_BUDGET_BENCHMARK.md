# CPU ranking and 2K/3K evidence-budget experiment

Measured 2026-10-18. **Synthetic, length-controlled kernel probes; not production, LoCoMo accuracy or a best-in-class leaderboard.** No generative calls, service startup or engine/default changes.

## Protocol and scope

- Ryzen 9 9950X3D, Windows, Python 3.13.5, four threads per sequential isolated process. Cached pinned weights only; CPU float32 for contextual/cross encoders. No downloads, training or ONNX/int8 execution in this follow-up.
- Six methods, 16 queries at each of 64/128/256 target BERT-token message lengths: 48 conditions per method. Eight earlier single-support cases plus eight multi-support cases (identity, relative time, negation, correction, duration, intersection, event count and gift ownership).
- Each pool has 48 candidates and includes all annotated required sources. Distractors include near-topic actor/modality mismatches. Neutral conversational padding alternates before/after the important sentence. Actual token min/max is recorded. These are approximations of realistic lengths, not sampled real conversations.
- One complete warmup per condition, three timed ranking repeats; quantiles pool the 48 samples at each length. Quality is over 16 unique conditions, not 48 independent samples. Cross-encoders score 48 query/message pairs in three batches of 16. Cached similarity times query encoding plus dot products; document embedding setup is recorded separately.
- All cross-encoder inputs fit the 512-token cap; zero truncated pairs is validated. Ranking p50 excludes indexing, packing and model loading. Packing includes exact counting and is separately replayed 30 times per condition.
- 2K means 2,048 and 3K means 3,072 **evidence-only** tokens, counted with the same pinned TinyBERT/BERT tokenizer, including rendered source/session/time headers. This is not a universal or generative-reader token count and does not include answer/system-prompt reserves.
- Packing uses the actual pure fit_units loop from src/memspine/read/tiers.py, loaded by AST to avoid service imports. Its unit_tokens dependency is supplied fixture-computed exact rendered costs. The full schema, assembly routing and access-control path are not exercised. Top12 ranked roots enter the comparison.
- Flat/full treats messages independently. Linked/full closes pre-supplied dependency links and keeps each bundle whole. Linked/annotated-spans additionally uses manually annotated exact source sentences: an **optimistic trimming bound**, not an implemented automatic selector. Links are supplied fixture metadata, not inferred by the benchmark.
- Each bundle pays for all copied support excerpts; no free cross-bundle dedup. The fitter skips oversize units and can accept later smaller units. Missing evidence is recorded as incomplete by the harness; runtime readiness/abstention is not tested.
- RSS includes Python, Torch and tokenizer imports even for BM25/static workers in this common harness. It is sampled at 10ms, not guaranteed OS peak or model-only RAM. Do not compare it directly to the earlier lighter Model2Vec process.

## Speed and ranking quality

Top1 means the first item is one of the required sources; it is not answer correctness. All@12 requires every annotated source in the first twelve.

| Method | Target tokens | Rank48 p50 ms | p95 ms | Top1 /16 | All@12 /16 | MRR first source | Index setup mean ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| bm25 | 64 | 0.043 | 0.053 | 8 | 16 | 0.638 | 0.53 |
| bm25 | 128 | 0.043 | 0.053 | 10 | 16 | 0.700 | 0.90 |
| bm25 | 256 | 0.043 | 0.053 | 8 | 16 | 0.628 | 1.52 |
| cross-encoder/ms-marco-MiniLM-L-6-v2 | 64 | 250.354 | 355.226 | 12 | 16 | 0.854 | 0.00 |
| cross-encoder/ms-marco-MiniLM-L-6-v2 | 128 | 568.614 | 733.982 | 10 | 16 | 0.771 | 0.00 |
| cross-encoder/ms-marco-MiniLM-L-6-v2 | 256 | 1149.564 | 1260.400 | 8 | 16 | 0.674 | 0.00 |
| cross-encoder/ms-marco-TinyBERT-L2-v2 | 64 | 18.684 | 22.119 | 12 | 16 | 0.831 | 0.00 |
| cross-encoder/ms-marco-TinyBERT-L2-v2 | 128 | 33.950 | 36.221 | 7 | 15 | 0.654 | 0.00 |
| cross-encoder/ms-marco-TinyBERT-L2-v2 | 256 | 67.032 | 69.780 | 7 | 14 | 0.651 | 0.00 |
| minishlab/potion-base-8M | 64 | 0.056 | 0.078 | 8 | 16 | 0.597 | 3.86 |
| minishlab/potion-base-8M | 128 | 0.056 | 0.075 | 8 | 16 | 0.599 | 6.79 |
| minishlab/potion-base-8M | 256 | 0.056 | 0.079 | 6 | 16 | 0.537 | 12.78 |
| minishlab/potion-retrieval-32M | 64 | 0.060 | 0.083 | 7 | 16 | 0.546 | 4.25 |
| minishlab/potion-retrieval-32M | 128 | 0.065 | 0.266 | 10 | 16 | 0.680 | 8.39 |
| minishlab/potion-retrieval-32M | 256 | 0.062 | 0.088 | 11 | 16 | 0.753 | 16.01 |
| sentence-transformers/all-MiniLM-L6-v2 | 64 | 3.140 | 3.922 | 8 | 16 | 0.612 | 216.61 |
| sentence-transformers/all-MiniLM-L6-v2 | 128 | 3.292 | 4.287 | 7 | 16 | 0.631 | 454.55 |
| sentence-transformers/all-MiniLM-L6-v2 | 256 | 3.299 | 4.173 | 6 | 14 | 0.504 | 1002.95 |

## Complete evidence at the longest messages

Each result is complete cases out of 16 (eight require multiple sources). Partial means retained records whose declared dependencies are absent; zero partial does not imply all requested evidence was retrieved.

| Method | Budget | Flat/full complete | Linked/full complete | Annotated-span bound | Flat partial records | Linked/full packing p50 ms* |
|---|---:|---:|---:|---:|---:|---:|
| bm25 | 2048 | 13 | 14 | 16 | 2 | 3.403 |
| bm25 | 3072 | 16 | 16 | 16 | 0 | 3.366 |
| cross-encoder/ms-marco-MiniLM-L-6-v2 | 2048 | 9 | 14 | 16 | 6 | 5.404 |
| cross-encoder/ms-marco-MiniLM-L-6-v2 | 3072 | 15 | 16 | 16 | 2 | 5.290 |
| cross-encoder/ms-marco-TinyBERT-L2-v2 | 2048 | 11 | 15 | 16 | 5 | 3.345 |
| cross-encoder/ms-marco-TinyBERT-L2-v2 | 3072 | 14 | 16 | 16 | 2 | 3.305 |
| minishlab/potion-base-8M | 2048 | 12 | 14 | 16 | 3 | 3.356 |
| minishlab/potion-base-8M | 3072 | 16 | 16 | 16 | 0 | 3.346 |
| minishlab/potion-retrieval-32M | 2048 | 12 | 14 | 16 | 3 | 3.686 |
| minishlab/potion-retrieval-32M | 3072 | 16 | 16 | 16 | 0 | 3.715 |
| sentence-transformers/all-MiniLM-L6-v2 | 2048 | 6 | 8 | 14 | 4 | 3.514 |
| sentence-transformers/all-MiniLM-L6-v2 | 3072 | 13 | 13 | 14 | 0 | 3.545 |

*Median across per-condition packing medians, including source rendering/token counting; ranking is excluded. All lengths, span-level retention, multi-support counts, tokens used and memory appear in the portable JSON.

## Interpretation and next steps

At 256-token messages, TinyBERT ranks 48 candidates in 67.0 ms median (69.8 ms p95); MiniLM cross-encoder takes 1,149.6 ms (1,260.4 ms p95). Their linked/full completeness is 15/16 versus 14/16 at 2K, and both 16/16 at 3K. These are input-specific observations, not stable general quality estimates.

**Recommended comparison profile: 3K for full-message bundles; 2K only with explicit incomplete handling or a separately validated compact-evidence selector.** The earlier accepted 4K design remains unchanged. BM25 and both static models also reach 16/16 linked/full completeness at 3K on these lexically simple supplied candidate pools, so this fixture does not justify mandatory neural reranking.

The earlier short-text suggestion that MiniLM is the stronger reranker is now qualified: its extra latency does not buy extra 3K completeness here. Model2Vec32M gives better first-source ordering than 8M at the longest length (11/16 versus 6/16), but the same 3K completeness. Retain 8M as the lower-memory candidate profile and consider 32M only after incremental hybrid benefit is measured.

In the remaining TinyBERT 2K linked/full failure, the four-source recorded-hike count cannot fit after earlier ranked units consume budget. The harness marks it incomplete; this does not mean no supporting sources exist. An obligation-aware selector or read-time deduplication of repeated source spans may help, but neither optimisation was measured or implemented here.

Recommendations must account for both ranking and completeness; neither a larger budget nor a cross-encoder repairs missing identity/time links. The annotated-span result is a ceiling conditional on known correct links and spans. It cannot justify promoting an untested trimmer.

- Keep lexical/BM25 as an inexpensive candidate lane; this small fixed-pool reference implementation is not a lexical-provider throughput benchmark.
- Preserve source-bound dependency bundles and explicit incomplete status at either budget. Do not persist or answer from isolated high-scoring fragments as if their support were present.
- Treat 2K/3K as configurable comparison profiles, not a replacement of the previously accepted 4K target. Choose the smallest budget meeting independently measured coverage; do not silently drop dependencies to meet a cap.
- Cache immutable excerpt token costs by tokenizer revision and text/content hash to reduce repeated counting; scope access checks and invalidation independently. This optimisation was not implemented here.
- Next: independent naturally written longer messages, automatic dependency/span construction with measured precision, a reader-specific tokenizer/reserve, inaccessible/deleted-source handling, and ONNX/int8 quality parity. No full suite is needed for those kernel follow-ups.

Fixture limitations include only 16 query templates, repeated neutral padding/distractors, fixed source-ID tie breaking and guaranteed inclusion of all required evidence in each pool. Scores were not calibrated for abstention, and no first-stage retrieval recall, model disagreement policy, relation extraction or final answer quality is measured. Generalisation needs independently authored cases.

## Artifacts and reproduction

- [Portable results and per-condition outputs](schema_review/cpu_evidence_budget_results.json)
- [Focused replay validation](schema_review/cpu_evidence_budget_validation.json): every packing outcome reproduced, exact source spans and message lengths checked, fitter boundary/duplicate cases checked.
- [Kernel script](../evals/bench/cpu_evidence_budget_kernels.py) and [validation/report builder](../evals/bench/summarize_cpu_evidence_budgets.py).
- Raw model logs and incremental JSON: evals/runs/_analysis/cpu_evidence_budgets_2026-10-18/.

```powershell
$py = 'C:/Users/hembad/AppData/Local/Temp/memspine-ner-runtime-313/Scripts/python.exe'
& $py evals/bench/cpu_evidence_budget_kernels.py --python $py `
  --deps 'C:/Users/hembad/AppData/Local/Temp/memspine-cpu-kernel-review-deps' `
  --out 'evals/runs/_analysis/cpu_evidence_budgets_recheck'
& $py evals/bench/summarize_cpu_evidence_budgets.py evals/runs/_analysis/cpu_evidence_budgets_recheck
```

Six models, 48 cases/model, identical fixtures, no truncated pairs, exact 48-ID pools, budget ceilings, top12/retention recomputation, linked dependency completeness passed.
