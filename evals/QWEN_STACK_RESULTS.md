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

## Throughput per token (`evals/bench_models.py`, 2026-10-09, RTX 5080)
Measured while the grid shared the machine (GPU 8-99% busy), so treat as conservative. Embedders: 256
passages (~70 tokens) in batches of 32, plus batch 1. Rerankers: one query vs a pool of 30 passages.
LLM: Ollama, thinking off, Q4_K_M.

| Model | Backend | tokens/s | ms/token | Other |
|---|---|---|---|---|
| bge-small-en-v1.5, batch 32 | fastembed ONNX (CPU) | 1,246 | 0.80 | 36 texts/s |
| bge-small-en-v1.5, batch 1 | fastembed ONNX (CPU) | 361 | 2.77 | |
| bge-base-en-v1.5, batch 32 | fastembed ONNX (CPU) | 365 | 2.74 | 10 texts/s |
| bge-base-en-v1.5, batch 1 | fastembed ONNX (CPU) | 119 | 8.42 | |
| Qwen3-Embedding-0.6B, batch 32 | torch bf16 (GPU) | 7,360 | 0.136 | 196 texts/s |
| Qwen3-Embedding-0.6B, batch 1 | torch bf16 (GPU) | 91 | 10.99 | |
| jina-embeddings-v5-text-small, batch 32 | torch bf16 (GPU) | 6,756 | 0.148 | 185 texts/s |
| jina-embeddings-v5-text-small, batch 1 | torch bf16 (GPU) | 81 | 12.36 | |
| bge-reranker-base, pool 30 | fastembed ONNX (CPU) | 4,529 | 0.221 | 91 pairs/s |
| Qwen3-Reranker-0.6B, pool 30 | torch (GPU) | 6,325 | 0.158 | 139 pairs/s |
| Qwen3-Reranker-4B 4-bit, pool 30 | bitsandbytes (GPU) | 1,617 | 0.618 | 35 pairs/s |
| jina-reranker-v3.5, pool 30 | torch listwise (GPU) | 8,943 | 0.112 | 196 pairs/s |
| Qwen3.5-9B Q4_K_M prefill | Ollama | 4,869 | 0.205 | |
| Qwen3.5-9B Q4_K_M decode | Ollama | 115 | 8.72 | |

Reading: batching the GPU embedders is 80-90x cheaper per token than one text at a time, so runs should use
`--memspine-batch-turns 32`. Jina reranker v3.5 is the fastest reranker and Qwen3-Reranker-4B 4-bit the slowest.

## Qwen3.5-9B Q4_K_M: thinking off vs on (LoCoMo conversation 1, categories 1-4, 152 questions)
Reader and judge both Qwen3.5-9B via Ollama; only the reader thinks. Retrieval: Qwen3-Embedding-0.6B, no
reranker, batched writes. Thinking off sends `reasoning_effort: none` (the `/no_think` switch does not work on
Qwen3.5). Thinking on allows 4,096 completion tokens.

| Setting | Headline accuracy | Answered | Truncated | Tokens/answer | Answer latency | Run time |
|---|---|---|---|---|---|---|
| thinking off | 0.730 | 152 | 0 | 45 | 1.2 s | 320 s |
| thinking on | 0.592 | 103 | 49 | 1,017 | 8.9 s | 1,834 s |

On the 103 questions both settings answered, accuracy is identical: **0.806 vs 0.806** (think-on wins 5,
loses 5). The 49 truncated answers hit the token limit; think-off scores 0.571 on those same questions.
Thinking costs 5.7x the run time and 22x the tokens for no measured gain, so keep it off for this task.

**Correction (judge artifact).** Of the 49 truncated think-on answers (all empty strings), the Qwen3.5-9B judge
marked 7 as correct, so the harness headline 0.592 includes 7 credits for empty answers. `make_sota_table.py`
scores any unanswered or truncated question as a miss, giving think-on 54.6 vs think-off 73.0 on the same 152
questions. The matched-question comparison above (0.806 vs 0.806 on the 103 both answered) is unaffected.
A small judge can credit an empty answer; a stronger or rubric-checked judge is needed for publishable numbers.

## Consolidated retrieval grid (full LoCoMo, 1,986 questions) - updated 2026-10-09 10:40
Raw per-arm summaries are tracked in `evals/results_qwen_stack/<run>/` (COMPARISON.md, summary.json); the
large `runs/` folder is gitignored. "bt32" = batched writes (`--memspine-batch-turns 32`), which halves wall-clock.

| Embedder | Reranker | Sufficiency | Ctx tokens | Wall-clock |
|---|---|---|---|---|
| bge-small | none | 0.594 | 1508 | 1,334 s |
| bge-small | bge-reranker-base | 0.579 | 971 | 1,775 s |
| bge-small | Qwen3-Reranker-0.6B | 0.573 | 789 | 1,782 s |
| bge-small | Qwen3-Reranker-4B 4-bit | 0.575 | 695 | 3,318 s |
| Qwen3-Embedding-0.6B | none | **0.615** | 1567 | 1,321 s |
| Qwen3-Embedding-0.6B | bge-reranker-base | 0.602 | 1001 | 1,745 s |
| Qwen3-Embedding-0.6B | Qwen3-Reranker-0.6B | 0.593 | 871 | 1,685 s |
| Qwen3-Embedding-0.6B | Qwen3-Reranker-4B 4-bit | 0.597 | 774 | 3,294 s |
| Jina v5 text-small (bt32) | none | 0.610 | 1517 | 662 s |
| Jina v5 text-small (bt32) | Jina reranker v3.5 | running | | |
| Qwen3-Embedding (bt32) / bge-small (bt32) | Jina reranker v3.5 | running / queued | | |
| bge-base | various | queued last | | |
