# C0-1 — verbatim baseline gate

- dataset: `locomo` revision `sha256:79fa87e90f040813` (10 items, 1540 queries)
- mode: **qa** | budget: 4096 tokens | top_k: 10 | seed: 11 | retriever: bm25 lexical
- reader: `qwen3.5:9b`

| system | answer accuracy | R@1 | R@5 | R@10 | ctx tokens (mean) | p50 latency ms | CPC $ | D16 |
|---|---|---|---|---|---|---|---|---|
| memspine | 0.730 | n/a | n/a | n/a | 1685 | 1020.6 | 0.000000 | yes |
