# Local Qwen stack: LoCoMo retrieval-only grid

Dataset: LoCoMo (10 conversations, 1,986 questions, all categories). Mode: `--retrieval-only`,
zero model-API calls, $0. Engine base: `local-combo-A`, budget 4096, top-k 10, replay read mode.
Hardware: one RTX 5080 (16 GB), two arms in parallel. "Sufficiency" is retrieval sufficiency, **not
answer accuracy**. Run ids: `qs-<embedder>-<reranker>`; arms in `evals/arms/`.

Embedders: `ebge` = fastembed BAAI/bge-small-en-v1.5 (384-d, CPU); `ebgeb` = bge-base-en-v1.5;
`eq06` = Qwen3-Embedding-0.6B (BF16, GPU, 1024-d, query instruction).
Rerankers: `roff` none; `rbge` bge-reranker-base (fastembed); `rq06` Qwen3-Reranker-0.6B;
`rq4b4` Qwen3-Reranker-4B, 4-bit (bitsandbytes).

| Embedder | Reranker | Sufficiency | Ctx tokens (mean) | Wall-clock |
|---|---|---|---|---|
| bge-small | none | 0.594 | 1508 | 1334 s |
| bge-small | bge-reranker-base | 0.579 | 971 | 1775 s |
| bge-small | Qwen3-Reranker-0.6B | 0.573 | 789 | 1782 s |
| Qwen3-Emb-0.6B | none | **0.615** | 1567 | 1321 s |
| Qwen3-Emb-0.6B | bge-reranker-base | 0.602 | 1001 | 1745 s |
| Qwen3-Emb-0.6B | Qwen3-Reranker-0.6B | 0.593 | 871 | 1685 s |
| bge-small | Qwen3-Reranker-4B 4-bit | pending | pending | pending |
| Qwen3-Emb-0.6B | Qwen3-Reranker-4B 4-bit | pending | pending | pending |
| bge-base | none / bge-reranker-base / Qwen3-Reranker-0.6B / 4B 4-bit | pending | pending | pending |

Pilot (2 items) timings, for estimating: 167 s (bge-small), 184 s (Qwen3-Emb), 197-242 s with a reranker.

## Readings so far
- Qwen3-Embedding-0.6B beats bge-small by 2.1 points of sufficiency at the same time cost.
- Every reranker so far lowers sufficiency by 1-2 points and cuts context by 35-50%.
- Rerankers add about 30-35% wall-clock. The 2-item pilot did not predict the sufficiency ordering.

## Not yet valid
- Qwen3.5-9B QA smoke (`qa-smoke-q35`): all scores 0.000 because the run hit `--max-model-calls 150`
  with 603 queries in scope (`--items 1` did not limit the query set), so answers were unattempted.
  Not a result. Open: `/no_think` behaviour for Qwen3.5, and a 9B judge versus earlier judges.
