# CPU-first non-generative model options

Reviewed 2026-10-18. Research and code inspection only. No model download, dependency installation, training, live inference or speed measurement. No universal fastest model is established; classification, embeddings, extraction and reranking are different tasks.

## Recommended shortlist

1. Rules + BM25 always establish the baseline.
2. Evaluate TF-IDF/HashingVectorizer + a linear classifier for predicate/rule routing once independent labelled examples exist.
3. Reuse optional configured Model2Vec for semantic candidate matching before adding a new static-vector stack.
4. Evaluate GLiNER2.5 Small only for source spans/structured candidates needing a learned extractor; retain grounding validation.
5. Evaluate a TinyBERT cross-encoder only for a bounded ambiguous candidate pool; do not run every model on every message.

This is an engineering recommendation, not an accepted model switch. Generative ingestion remains disabled, evidence budget remains 4K, model identity/settings stay explicit and source/copy-erasure policies remain unchanged.

## Task comparison

Resource descriptions below are qualitative architectural expectations, not measured rankings. A vector lookup can be cheaper per operation than a transformer while failing to perform the same extraction task.

| Option | Task | Expected deployment shape | Needs our labelled training data? | Limitation |
|---|---|---|---|---|
| TF-IDF or hashing + SGD/logistic/linear SVM | Predicate/rule routing, polarity candidates | Sparse CPU arithmetic; model size depends on feature count and labels | Yes, for our classifier | Not general semantic extraction or exact role grounding |
| Model2Vec/static embeddings + cosine or classifier | Semantic matching and label candidates | Token-vector lookup/pooling; no contextual transformer forward pass | No for similarity; yes for task classifier | Word order/negation/role understanding limited |
| Classic Word2Vec/GloVe with reviewed pooling | Related-word expansion | Vector tables; large vocabulary can dominate RAM | Pretrained for similarity; yes for classifier | Vocabulary/word-sense limitations; not a full extractor |
| Quantised fastText classifier | Predicate/topic routing | Compact supervised C++ classifier | Yes | Packaging and text classification semantics; no standalone span/role extraction |
| spaCy small pipeline + matchers | Tokenisation, syntax, standard NER, dependency patterns | CPU trained NLP pipeline; disable unused components only when dependencies permit | Not for standard pipeline; yes/rules for our relations | Ordinary NER categories do not cover every memory predicate |
| GLiNER2.5 Small | Schema-conditioned spans and structured candidates | Encoder inference; heavier than lookup/linear methods | Intended pretrained schema-conditioned use; domain validation still required | Supported heads/labels and local installed API must be verified |
| MiniLM sentence encoder, ONNX/quantised candidate | Contextual embeddings | Transformer inference; model/runtime specific | No for pretrained retrieval | Similarity is not fact extraction |
| TinyBERT cross-encoder through FlashRank | Candidate reranking | Joint query-passage inference per pair | No for pretrained relevance ranking | Ranking only; not grounding, and relevance can misrank binding evidence |
| Existing OpenDecider nano / larger decision encoders | Bounded decision classification | Larger encoder plus choice head | Pretrained task-dependent | Not a cheap universal first stage merely because called nano |

## Verified source details

- scikit-learn documents text vectorisation and sparse/online SGD classification. Hashing avoids storing a learned vocabulary but has collisions; word/character n-grams and negation-aware training must be validated. These models need independent labels for our routing problem. [Feature extraction](https://scikit-learn.org/stable/modules/feature_extraction.html), [SGDClassifier](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.SGDClassifier.html), [HashingVectorizer](https://scikit-learn.org/stable/modules/generated/sklearn.feature_extraction.text.HashingVectorizer.html)
- Model2Vec provides static sentence/token embeddings and classification support. Its inference documentation supports loading from a local path. Reported speedups are authors' comparisons, not measurements against our word vectors or ingestion. Its default maximum encoding length can truncate input, so explicit chunk/source accounting is required. [Project](https://github.com/minishlab/model2vec), [Inference API](https://minish.ai/packages/model2vec/inference/)
- fastText provides supervised classification and quantisation. Published compressed examples demonstrate small task models, not the size/quality of our untrained predicate classifier. [Classification tutorial](https://fasttext.cc/docs/en/supervised-tutorial.html), [Quantised models](https://fasttext.cc/docs/en/supervised-models.html)
- spaCy lists English small/medium/large/transformer pipelines; the small pipeline is not a replacement semantic-vector model. Syntax/entity output could support slot extraction through explicit patterns. [Official model documentation](https://spacy.io/models/en)
- Upstream GLiNER2 lists `fastino/gliner2.5-small-v1` as an English 74M-parameter boundary model for CPU/edge. Use architecture-aware loading; checkpoint heads determine extraction capabilities. README tests list Python 3.10–3.12, so Python 3.13 acceptance must be tested explicitly. The published package and main-branch feature set must not be assumed identical. This does not imply a measured speedup or quality parity with the existing larger checkpoint. [Official project](https://github.com/fastino-ai/GLiNER2)
- FlashRank lists `ms-marco-TinyBERT-L-2-v2` as approximately 4 MB. That is advertised model artifact size, not process RSS. Use the discriminative cross-encoder path only; the library also supports LLM-based listwise models, which violate the no-generative-ingestion requirement if selected. Passage length/pair count affect cost. [Official project](https://github.com/PrithivirajDamodaran/FlashRank)
- Sentence Transformers documents ONNX/OpenVINO and quantisation, but improvements depend on model and hardware; CPU INT8 must be separately exported/tested. CUDA fp16 examples are not evidence for CPU speedups. [Efficiency documentation](https://www.sbert.net/docs/sentence_transformer/usage/efficiency.html)

## Existing code fit

| Component | Current source finding | Integration assessment |
|---|---|---|
| Static embeddings | `models/encoders/embedding/static_local.py`; `StaticEmbeddingSettings` | Existing adapter; current loader can fetch weights, so offline/local-artifact enforcement needed |
| Static read prefilter | `read/pipeline/rerank.py::_static_embedding_prefilter` | Existing read-side code, not automatically ingestion routing |
| Fastembed CPU encoder/reranker | Existing ONNX embedding and `FastembedReranker` adapters | Candidate infrastructure already exists; exact model/task profiles and quality checks needed |
| FlashRank | `models/providers/rerank/flashrank_rerank.py` | Existing optional adapter; explicit model/cache/length/offline policy must be verified |
| GLiNER | Shared runtime in `models/runtime/gliner_runtime.py`; architecture-aware AutoExtractor and named profiles | Reuse shared ownership and grounding. Small checkpoint is a separate explicit profile, not silent replacement |
| OpenDecider | `models/providers/decision/opendecider_nano.py` | Local code describes approximately 395M parameters plus head and ONNX option; not default cheapest router |
| Sparse linear classifier | No dedicated ingestion predicate-classifier adapter established in this review | Proposed optional component, requires labels/abstention calibration |
| fastText/spaCy | No canonical ingestion adapters established in reviewed paths | Additional dependency/adapter engineering; compare only for a demonstrated gap |

## Python 3.13 distribution check

[Metadata snapshot](schema_review/cpu_model_package_metadata.json) was fetched directly from current PyPI JSON endpoints. This establishes distribution metadata only, not import/model/runtime/dependency compatibility. Pure Python packages still depend on native runtimes.

| Package | Observed release | Relevant distribution finding |
|---|---|---|
| model2vec | 0.10.0 | Universal Python wheel; requires Python >=3.10 |
| scikit-learn | 1.9.1 | CPython 3.13 Windows x64 wheel present |
| gensim | 4.4.0 | CPython 3.13 Windows x64 wheel present |
| spaCy | 3.8.16 | CPython 3.13 Windows x64 wheel present |
| fasttext | 0.9.3 | No CPython 3.13 Windows x64 wheel observed; source archive exists, custom build unverified |
| gliner2 | 2.1.0 | Universal wheel; local torch/transformer/checkpoint API acceptance is a separate gate |
| flashrank | 0.2.10 | Universal wheel; ONNX/tokenizer/model dependencies separate |
| onnxruntime | 1.31.0 | CPython 3.13 Windows x64 wheel present |

Primary metadata: `https://pypi.org/pypi/<package>/json`; filenames and Python requirements retained in the snapshot. No substitute fastText wheel/fork is selected here. Package wheels are not model weights and do not prove availability of a particular quantised checkpoint.

## System-1 cascade for MemSpine

System-1 here means fast bounded local processing, not a certification or a particular model brand.

```text
Raw durable source
  -> exact rules + BM25 label/context candidates
  -> optional sparse classifier/static embeddings when candidates need help
  -> optional small extractor for unsupported spans/roles
  -> optional reranker for ambiguous candidate/evidence ordering
  -> same typed grounding, temporal and authority checks
  -> canonical commit or unresolved annotation
```

Do not make a serial mandatory chain of every model. Route by missing capability and account for cumulative work. Reuse one shared model for compatible tasks/settings where safe; keep model/task identities and per-user result caches distinct. Decision/routing, extraction, embedding and reranking are separate named task bindings using the existing model-profile architecture; these descriptions are not new config field names.

Example: a sparse classifier/static embedding may propose move-related labels for “relocated”; GLiNER Small may supply source spans; explicit polarity/role/time validation still rejects a completed move from “might relocate”. Classifier scores and NER confidence remain hints, not source authority. Unknown words/ambiguous labels preserve raw text and unresolved reason; never hidden generative escalation.

## Kernel-only evaluation proposal

The initial review below proposed the evaluation. Subsequent [CPU kernel measurements](CPU_NON_LLM_BENCHMARK_RESULTS.md) cover a bounded subset; the broader size/batch/grounding matrix remains pending. Compare only equivalent tasks:

1. Label routing: BM25 definitions versus sparse linear classifier versus static embeddings; measure candidate recall@k, accepted label precision and unresolved/calibration behaviour.
2. Span extraction: rules/NER alternatives on identical annotated source spans; exact span and role accuracy plus latency/RSS.
3. Reranking: fixed candidate pool and dependency bundles; all-evidence retention, including negation and multi-hop branches.
4. Input sizes: 64/128/256-token segments; label sets 4/12/43; batch 1/8; CPU threads 1/4 as explicit experiments, not accepted defaults.
5. Separate cold load, warm p50/p95, peak RSS, model bytes and actual total inference calls. Prevent thread oversubscription between ONNX/torch/NumPy and background workers.
6. Explicit local immutable artifacts, disabled networking and license/model revision checks. No automatic benchmark download/training on LoCoMo gold; independent labelled examples and hard negatives required.

Keep any model only if it closes a measured coverage gap within our CPU/memory budget. “Smaller” does not establish “faster”, nor does faster routing establish better grounded extraction.

## Verified under-250M checkpoint shortlist — 2026-10-18

[MODELS_UNDER_250M.md](MODELS_UNDER_250M.md) records 16 checkpoints with metadata-verified totals below 250M and pinned revisions. Model2Vec/GLiNER2.5 remain preferred families; exact profiles/model switches require kernel and grounding checks. GLiClass Edge, small contextual encoders, tiny rerankers and extractive QA are role-specific alternatives, not mandatory serial stages. Two models with unknown totals were excluded. That initial checkpoint inventory used metadata only; subsequent downloads and inference are recorded in [the kernel report](CPU_NON_LLM_BENCHMARK_RESULTS.md).

## Measured starting shortlist - 2026-10-18

See [CPU_NON_LLM_BENCHMARK_RESULTS.md](CPU_NON_LLM_BENCHMARK_RESULTS.md) for same-machine p50/p95, RSS, small synthetic quality counts and pinned artifacts. Prefer Model2Vec8M for cheap semantic candidates; GLiNER2.5 Small for source entity proposals; TinyBERT for reranking speed or MiniLM for stronger measured ordering. These recommendations concern the tested roles, not a global leaderboard. GLiClass needs more compatibility/label work; ONNX/int8 and other untested checkpoints remain proposals.
