# C0-1 — verbatim baseline gate

- dataset: `locomo` revision `sha256:79fa87e90f040813` (10 items, 1986 queries)
- mode: **retrieval** | budget: 4096 tokens | top_k: 10 | seed: 11 | retriever: bm25 lexical
- reader: `none (retrieval only)`
- screening: retrieval-only (no reader, no judge)

| system | retrieval sufficiency | R@1 | R@5 | R@10 | ctx tokens (mean) | p50 latency ms | CPC $ | D16 |
|---|---|---|---|---|---|---|---|---|
| memspine | 0.573 | n/a | n/a | n/a | 789 | 0.0 | 0.000000 | no |

> Retrieval sufficiency is **not** answer accuracy. These rows are inadmissible as QA numbers by construction (no backbone), which is exactly the distinction MemPalace's 96.6% needs and the field's comparison tables lose.
