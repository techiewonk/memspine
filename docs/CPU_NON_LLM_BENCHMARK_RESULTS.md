# CPU non-generative kernel results â€” 2026-10-18

Measured on this machine. These results identify promising starting profiles; they do **not** establish global best-in-class or production/LoCoMo accuracy. All models are non-generative and below 250M verified model parameters; BM25 has no neural weights.

## Recommended starting profiles

| Purpose | Recommended candidate | Reason and boundary |
|---|---|---|
| Cheap semantic label/context candidates | Model2Vec potion-base-8M | 23/24 label matches at 0.040 ms median; does not establish factual entailment |
| Source-bound entity candidates | GLiNER2.5 Small | 95.8% entity F1 on eight simple texts; validate spans, roles, polarity and time before claim persistence |
| Faster passage reranking | TinyBERT-L2-v2 | 7/8 top choices, 1.064 ms per four-passage batch |
| Stronger measured passage ordering | MiniLM-L6 cross-encoder | 8/8 top choices, 8.605 ms per four-passage batch; broader quality remains unverified |
| More expensive entity option | GLiNER2.5 Base | 98.0% entity F1 here, approximately 3x Small latency; only use where measured extra coverage merits cost |

Do not run every model serially. Rules/BM25 and Model2Vec propose candidates; invoke the extractor/reranker only for the missing capability. Models remain task-specific named profiles. This review does not activate an ingestion adapter or change engine defaults.

## Protocol

- Windows 11, AMD Ryzen 9 9950X3D (16 physical cores / 32 logical CPUs), Python 3.13.5. Four configured CPU threads per sequential isolated model process; no GPU. This is a high-end desktop CPU, not a deployment-server latency guarantee.
- Routing: 24 synthetic texts across six fixed English labels. Two warm-up items, then three repeats (72 timing samples). Label vectors are precomputed and excluded from warm query time.
- NER: eight synthetic texts with 24 expected person/location/organization mentions. Exact source offsets are validated. F1 matches surface+type pairs; repeated identical mentions and relation extraction are not assessed.
- Passage selection: eight queries, four short passages each, including actor, modality and temporal distractors. Two warm-up cases, three repeats (24 timing samples). Cross-encoder time includes scoring all four passages.
- Secondary similarity probes precompute the document vectors; warm time includes query encoding and dot products only. This differs from cross-encoder pair inference. Vector construction is recorded separately in JSON.
- Warm p50/p95 includes tokenization, inference and scoring. Fresh-process setup includes imports, model loading and label setup against cached local weights; it is not a true cold-disk measurement.
- Memory is process RSS sampled every 10 ms, including Python/frameworks; it is not weight size, incremental model RAM or a guaranteed OS peak. GLiNER RSS covers both routing and NER in the same process.
- Torch inference uses float32, inference mode and eval mode. No ONNX/int8 optimisation was benchmarked. Package versions and exact checkpoint revisions are recorded per run.
- Selected TinyBERT and GLiClass weights were explicitly downloaded; the other tested weights were already cached. Inference uses local artifacts with Hub/Transformers offline flags. Benchmark-only dependencies were installed in an isolated target directory.
- No LoCoMo question/gold content, training, full engine execution or evaluation migration was used. Quality counts come from unique cases; timing repeats do not increase quality sample size.

## Label routing: same six descriptive labels

| Model | Top1 /24 | Warm p50 ms | Warm p95 ms | Sampled RSS MiB | Process setup ms |
|---|---:|---:|---:|---:|---:|
| BAAI/bge-small-en-v1.5 | 21 | 7.692 | 9.298 | 542.6 | 6704.4 |
| bm25-labels | 11 | 0.028 | 0.037 | 35.9 | 0.1 |
| fastino/gliner2.5-base-v1 | 21 | 141.793 | 154.499 | 1989.2 | 9318.6 |
| fastino/gliner2.5-small-v1 | 23 | 43.429 | 50.187 | 1146.9 | 8248.6 |
| knowledgator/gliclass-instruct-edge-v1.0 | 4 | 14.587 | 17.503 | 568.3 | 7112.5 |
| minishlab/potion-base-8M | 23 | 0.040 | 0.052 | 126.0 | 610.3 |
| minishlab/potion-retrieval-32M | 21 | 0.039 | 0.051 | 319.9 | 683.0 |
| sentence-transformers/all-MiniLM-L6-v2 | 20 | 2.790 | 4.267 | 506.5 | 18733.4 |

The BM25 row is a small handwritten label-definition baseline, **not** Tantivy/OpenSearch/BM25S backend throughput. Model2Vec is cosine-to-label retrieval, not a trained classifier; GLiNER/GLiClass use their classification heads. Their differing designs are part of this diagnostic. Larger encoders are not necessarily better on this tiny task.

## Entity extraction

| Model | TP / FP / FN | F1 | Warm p50 ms | Warm p95 ms |
|---|---|---:|---:|---:|
| fastino/gliner2.5-base-v1 | 24 / 1 / 0 | 0.9796 | 94.424 | 104.897 |
| fastino/gliner2.5-small-v1 | 23 / 1 / 1 | 0.9583 | 31.841 | 34.852 |

GLiNER confidence is not identity/claim authority. This does not measure residence transitions, relation roles, temporal anchoring, ambiguous aliases or cross-session joins. Small and Base each produced one false positive on this fixture.

## Same passage cases: similarity versus cross-encoders

| Method | Top1 /8 | Warm p50 ms | Warm p95 ms | Warm work |
|---|---:|---:|---:|---|
| BAAI/bge-small-en-v1.5 | 7 | 7.348 | 8.299 | Query vector + four cached dot products |
| minishlab/potion-base-8M | 4 | 0.040 | 0.095 | Query vector + four cached dot products |
| minishlab/potion-retrieval-32M | 6 | 0.040 | 0.044 | Query vector + four cached dot products |
| sentence-transformers/all-MiniLM-L6-v2 | 8 | 2.591 | 3.173 | Query vector + four cached dot products |
| cross-encoder/ms-marco-MiniLM-L-6-v2 | 8 | 8.605 | 11.480 | Four query/passage pairs |
| cross-encoder/ms-marco-TinyBERT-L2-v2 | 7 | 1.064 | 1.717 | Four query/passage pairs |

Model2Vec8M selected only 4/8 correct passages despite its 23/24 routing score. Examples include preferring an invitation over attendance, an unrelated negative statement over the intended plan, and another studioâ€™s opening date. Static topic similarity must not decide factual validity or trim away supporting dependencies. TinyBERT preferred a planned ten-kilometre walk over the completed three-kilometre walk; MiniLM cross-encoder passed these eight cases. No test here proves safe trimming at 4K or multi-hop evidence retention.

## Classifier sensitivity and failed setup

GLiClass Edge produced 4/24 matches with descriptive labels, always choosing the first label. A focused rerun with short labels gave 9/24 (p50 6.935 ms, p95 9.111 ms). The inputs therefore differ and the rerun is a sensitivity diagnostic, not a replacement score in the main table. A one-text single-/multi-label inspection produced finite scores in both modes; the evidence does not establish an SDK bug. A model-type compatibility warning was logged. Keep this checkpoint out of the starting recommendation pending package/model compatibility and schema-label validation.

Initial isolated runs failed because the temporary sklearn dependency set lacked cloudpickle/narwhals. Dependencies were completed, and every primary process was rerun successfully. Initial failed artifacts are retained in cpu_non_llm_review_2026-10-18; only the final run is used above. No engine dependency pins were changed.

## Published benchmarks: separate evidence

The [Sentence Transformers official table](https://sbert.net/docs/cross_encoder/pretrained_models.html) lists TinyBERT-L2 at MS MARCO MRR@10 32.56 / TREC DL19 NDCG@10 69.84, and MiniLM-L6 at 39.01 / 74.30. That supports the general speed/quality tradeoff, but these are different tasks, datasets and hardware from our local probes. Its docs/sec figures are not this CPUâ€™s throughput. The [Model2Vec card](https://huggingface.co/minishlab/potion-base-8M) and [GLiNER Small card](https://huggingface.co/fastino/gliner2.5-small-v1) document their intended roles; their published claims are not substituted for local grounding tests.

## Next focused measurements before adoption

| Priority | Remaining experiment | Promotion requirement |
|---|---|---|
| P0 | Independent labelled relation/role/time/negation cases, plus unknown/ambiguous cases | No unsupported state transitions; calibrated unresolved behaviour |
| P0 | Evidence bundles at the selected 4K limit, with multi-hop and negation dependencies | All required evidence retained or explicitly incomplete; never silently trim dependencies |
| P1 | Rerank48 at batch16 with representative 64/128/256-token lengths | Measured p50/p95 and memory under the actual candidate profile |
| P1 | One versus four threads, batch1/8, repeated fresh process measurements | Stable latency/memory envelope without worker oversubscription |
| P1 | ONNX/int8 TinyBERT/MiniLM, then GLiNER if supported | Actual Python3.13 CPU execution and quality parity, not artifact-name assumptions |
| P2 | Additional under-250M contenders (e.g. mxbai-xsmall) on identical cases | Pinned artifacts/licences and an actual improvement over the selected role baseline |

A new trained sparse classifier, fastText/word2vec, GLiClass alternatives, extractive QA and ColBERT were not benchmarked here. There is no global model winner; the tested shortlist is deliberately bounded. Lexical provider benchmarking remains a separate task.

## Reproduction and artifacts

Run from the repository root with the isolated Python/runtime and dependency directory created for this review:

```powershell
$py = 'C:/Users/hembad/AppData/Local/Temp/memspine-ner-runtime-313/Scripts/python.exe'
& $py evals/bench/run_cpu_non_llm_review.py `
    --python $py `
    --deps 'C:/Users/hembad/AppData/Local/Temp/memspine-cpu-kernel-review-deps' `
    --out 'evals/runs/_analysis/cpu_non_llm_review_recheck'
```

The driver uses cached local snapshots and never downloads missing weights. It chooses a cached complete layout and records the selected revision; supply explicit --path to the kernel worker to replay an exact revision when several exist. Current scripts include the later cached-similarity extension; primary timing tables above retain the earlier stable-run outputs. Script hashes in the portable package are labelled at packaging time, not falsely asserted as original-run hashes.

- [Portable results, fixtures, revisions and package versions](schema_review/cpu_non_llm_kernel_results.json)
- Primary raw run: evals/runs/_analysis/cpu_non_llm_review_2026-10-18_final/manifest.json
- Later cached-similarity run: evals/runs/_analysis/cpu_non_llm_review_2026-10-18_similarity/
- Label sensitivity: evals/runs/_analysis/cpu_non_llm_review_2026-10-18_sensitivity/gliclass-short-labels.json
- [Kernel source](../evals/bench/cpu_non_llm_kernels.py) and [driver](../evals/bench/run_cpu_non_llm_review.py)

Focused validation passed: 10 primary rows, four later similarity runs, one sensitivity run, output counts, recomputed top1 counts, exact entity offsets, finite ordered timing statistics, loaded neural parameter caps and AST parsing. No full suite run or runtime implementation is claimed.

## Follow-up at realistic lengths and smaller evidence budgets

See [48-candidate / 2K-3K kernel results](CPU_EVIDENCE_BUDGET_BENCHMARK.md). The earlier eight short passage cases do not generalise unchanged: at 256-token messages MiniLM cross-encoder costs about 1.15 seconds for 48 candidates versus TinyBERT 67ms, without better linked/full completeness at 3K on the follow-up fixture. Keep the reranker recommendation conditional on measured evidence retention, input size and task. No production default is selected.
