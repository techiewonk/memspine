# Verified non-generative checkpoints below 250M

Metadata reviewed 2026-10-18 using hf models info. A subsequent subset was downloaded/cached and benchmarked on CPU; see [measured results](CPU_NON_LLM_BENCHMARK_RESULTS.md). This shortlist table remains a metadata inventory. Counts are Hub safetensors totals and may include small non-trainable buffers. Rounded model sizes are not RAM or performance measurements.

| Model | Total (M) | Task | License metadata | ONNX-named files |
|---|---:|---|---|---|
| [cross-encoder/ms-marco-TinyBERT-L2-v2](https://huggingface.co/cross-encoder/ms-marco-TinyBERT-L2-v2) | 4.4 | Tiny cross-encoder reranking | apache-2.0 | Present; runtime not tested |
| [minishlab/potion-base-8M](https://huggingface.co/minishlab/potion-base-8M) | 7.6 | Static embeddings / label matching | mit | Present; runtime not tested |
| [sentence-transformers/all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) | 22.7 | English sentence embeddings | apache-2.0 | Present; runtime not tested |
| [cross-encoder/ms-marco-MiniLM-L-6-v2](https://huggingface.co/cross-encoder/ms-marco-MiniLM-L-6-v2) | 22.7 | Cross-encoder reranking | apache-2.0 | Present; runtime not tested |
| [mixedbread-ai/mxbai-embed-xsmall-v1](https://huggingface.co/mixedbread-ai/mxbai-embed-xsmall-v1) | 24.1 | Small contextual embeddings | apache-2.0 | Present; runtime not tested |
| [knowledgator/gliclass-instruct-edge-v1.0](https://huggingface.co/knowledgator/gliclass-instruct-edge-v1.0) | 32.7 | Compact schema/label classification | apache-2.0 | Not observed in listing |
| [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) | 33.4 | English retrieval embeddings | mit | Present; runtime not tested |
| [intfloat/e5-small-v2](https://huggingface.co/intfloat/e5-small-v2) | 33.4 | English retrieval embeddings; preserve query/passage conventions | mit | Present; runtime not tested |
| [answerdotai/answerai-colbert-small-v1](https://huggingface.co/answerdotai/answerai-colbert-small-v1) | 33.4 | Token-level late-interaction retrieval; not single-vector drop-in | apache-2.0 | Present; runtime not tested |
| [mixedbread-ai/mxbai-rerank-xsmall-v1](https://huggingface.co/mixedbread-ai/mxbai-rerank-xsmall-v1) | 70.8 | Cross-encoder reranking | apache-2.0 | Present; runtime not tested |
| [fastino/gliner2.5-small-v1](https://huggingface.co/fastino/gliner2.5-small-v1) | 73.9 | Small schema-conditioned extraction | apache-2.0 | Not observed in listing |
| [deepset/tinyroberta-squad2](https://huggingface.co/deepset/tinyroberta-squad2) | 81.5 | Extractive QA spans, not generative answering | cc-by-4.0 | Not observed in listing |
| [sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2) | 117.7 | Multilingual sentence embeddings | apache-2.0 | Present; runtime not tested |
| [knowledgator/gliclass-small-v1.0](https://huggingface.co/knowledgator/gliclass-small-v1.0) | 143.6 | Label classification | apache-2.0 | Present; runtime not tested |
| [knowledgator/gliclass-base-v3.0](https://huggingface.co/knowledgator/gliclass-base-v3.0) | 186.5 | Label classification | apache-2.0 | Not observed in listing |
| [fastino/gliner2.5-base-v1](https://huggingface.co/fastino/gliner2.5-base-v1) | 193.6 | Larger schema-conditioned extraction | apache-2.0 | Not observed in listing |

## Recommendation

Keep Model2Vec and GLiNER2.5 as the main existing-capability candidates. Add GLiClass Instruct Edge only as a routing/classification experiment when rules/BM25/static label matching leave a measured gap. Compare MiniLM or BGE Small for contextual matching; select one, not every encoder. TinyBERT is the first bounded reranker candidate. All selections remain explicit named profiles, without automatic download or generative escalation.

GLiNER2.5 Small and Base are both below the ceiling; larger base is a quality/cost alternative, not a mandatory fallback. Candidate extraction always passes exact source/role/modality/time checks. The cap applies per checkpoint unless a deployment total-memory budget is separately specified; loading four eligible models does not produce a 250M total stack.

## Integration limits

- GLiClass is a classification encoder; it does not replace exact span extraction. It needs a new reviewed decision/routing adapter before ingestion use; current GLiNER decision adapter is a different implementation.
- Extractive QA returns spans from supplied context; it does not reliably perform arbitrary cross-session joins or resolve unsupported premises. Use fixed templates selectively and calibrate abstention. The tinyroberta card declares CC-BY-4.0; retain attribution and check deployment licensing.
- ColBERT returns multiple token vectors and late-interaction scoring. Current single-vector indexing/ranking contracts are not a drop-in fit; treat this as a separate later retrieval experiment.
- ONNX-named files only establish artifact presence. Quantisation type, operator support, task head, token limits and CPU/Python3.13 behaviour remain untested. No automatic export or model switch is selected.
- Existing model loaders may fetch weights; local-only artifact enforcement and named task binding remain required.
- Checkpoint metadata and main-library APIs can differ; pin both model revision and package before kernel checks.

## Not admitted to confirmed shortlist

- knowledgator/gliner-bi-small-v2.0: no verified total in retrieved metadata; do not infer full checkpoint size from its small name or encoder alone.
- urchade/gliner_small-v2.1: no verified total in retrieved metadata; do not infer full checkpoint size from its small name or encoder alone.

## Measurement gates

Compare identical task/input/labels on CPU: cold load, warm p50/p95, process RSS, thread count and exact retained evidence. Model bytes and quantised size are not runtime RSS. Classification candidate recall, span/role precision and reranker evidence coverage must be separate. Use independent examples rather than fitting inspected LoCoMo gold. No live acceptance or fastest-model claim is made.

[Pinned metadata snapshot](schema_review/models_under_250m_metadata.json) contains exact model revision hashes. [Earlier CPU comparison](CPU_NON_GENERATIVE_MODEL_OPTIONS.md) covers package-level Python 3.13 wheel checks; it does not certify these checkpoints.
