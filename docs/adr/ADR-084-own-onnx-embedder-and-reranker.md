# ADR-084: Our own ONNX embedder and cross-encoder reranker replace fastembed

**Status:** Accepted and implemented on 2026-10-23. Verification and current follow-ups: RM-123 in [PROVIDER_TODO.md](../PROVIDER_TODO.md).
**Date:** 2026-10-23
**Deciders:** Owner (question rounds of 2026-10-23)
**Supersedes:** D-08 "fastembed (ONNX, CPU)" as the embedder implementation (the ONNX/CPU
default itself stays), and the `fastembed` entry of `model.rerank.chain`.
**Relates to:** ADR-082 (model slots), ADR-079 (shared weights), ADR-068 (no aliases), F-03
(versioned vector spaces), RM-02 (dependency baseline).

## Context

- **Hub 2.x is blocked.** `huggingface-hub` 2.x cannot be adopted because fastembed 0.9.0, a
  core dependency, requires `huggingface-hub<2.0` in its latest release and on `main`
  (checked 2026-10-23, `docs/PYTHON313_DEPENDENCIES.md`).
- **What fastembed does for us** is narrow: download an ONNX export, tokenize, run
  `onnxruntime`, pool and normalise. Its two users are `clients/weights/fastembed.py`
  (`TextEmbedding`, `TextCrossEncoder`) and the adapters `services/embedding/fastembed.py` and
  `services/rerank/fastembed.py`.
- **Models memspine uses through fastembed today:**
  - `BAAI/bge-small-en-v1.5`: the default, 384-d.
  - `BAAI/bge-base-en-v1.5`: 768-d.
  - `sentence-transformers/all-MiniLM-L6-v2`: 384-d. These first three are the dimension table
    `FASTEMBED_MODEL_DIMS` in `config/schema/providers.py`.
  - `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`: the `cpu_multilingual`
    preset (`models/embedding_profiles.py`).
  - `Xenova/ms-marco-MiniLM-L-6-v2`: the cross-encoder, `RERANK_FASTEMBED_MODEL`.
- **Vector identity is tied to the library.** The embedder id is `fastembed:<model>`
  (`services/embedding/fastembed.py`). It is part of every vector table's projection identity
  (`providers/projection_bindings.py`) and of the result-cache key.

## Decision

1. **New provider `onnx`** for `model.embedding` and for `model.rerank.chain` entries. The
   provider `fastembed` is removed with no alias (ADR-068): a config naming it is rejected,
   naming `onnx`. `scripts/migrate_config_keys_adr082.py` (or a successor codemod) rewrites
   configs.
2. **Layering follows the RM-113 standard:**
   - The weights connector in `clients/weights/onnx.py` owns
     `onnxruntime.InferenceSession` plus `tokenizers.Tokenizer`. Files come through
     `hf_hub_download` / `snapshot_download` into the standard Hugging Face cache, and
     `local_files_only` is honoured for offline runs.
   - The adapters are `services/embedding/onnx.py` and `services/rerank/onnx.py`.
   - The descriptors live in `providers/builtin/models.py` and `providers/builtin/rerank.py`.
   - Weights are shared through the ADR-079 registry, keyed by the model spec (repo, revision,
     file, execution provider, threads), never by per-holder fields such as the query
     instruction.
3. **Explicit model catalogue.** A supported-model table (`clients/weights/onnx_catalogue.py`
   or data) pins, for each model:
   - the source repo and revision, the ONNX file (fp32, plus an optional int8 variant) and the
     tokenizer file;
   - pooling (`cls` or `mean`), normalisation, max length, the default query/passage prefix,
     and the dimension (cross-encoders have a score head instead).

   The catalogue starts with the five models above. The source ONNX exports are the same
   files fastembed downloads today, so the arithmetic matches. A model outside the catalogue
   needs an explicit `settings.model_spec` with all of those fields; nothing is guessed.
4. **Execution:** the CPU execution provider is the default.
   - `settings.threads` (0 = runtime default) and `settings.batch_size` control throughput.
   - `settings.providers` may list `CUDAExecutionProvider` and others when installed. No
     download happens at import time.
5. **Parity is proven before a model enters the catalogue.** Reference vectors and scores are
   generated with fastembed for a fixed text set and committed as test fixtures. This happens
   once, before fastembed is removed.
   - Embeddings must reach cosine ≥ 0.99999 and a maximum absolute difference ≤ 1e-5 per
     dimension.
   - Cross-encoder scores must keep the same order with an absolute difference ≤ 1e-4.
6. **Library-independent embedder id: `embedder:<model>`** (owner, 2026-10-23). It replaces the
   provider-named `fastembed:<model>`.
   - The `onnx` embedder reports `embedder:<model>`. The vector-space identity then names the
     model, not the library that computed it.
   - Another embedder may report the same id only when it is parity-certified against the
     catalogue reference vectors for that model (item 5). Uncertified embedders keep a
     provider-qualified id such as `st:<model>`, because different libraries can produce
     different vectors for the same model name.
   - Hosted embedders (OpenAI-compatible, ADR-085) keep provider-qualified ids: their vectors
     are defined by the service, not by a local file.
7. **Vector-space adoption, not re-embedding, for parity-certified models.**
   - A store whose vector table or binding records `fastembed:<m>` is adopted on first start as
     `embedder:<m>` when the configured embedder is `onnx` with catalogue model `m` and that model
     is parity-certified.
   - The adoption rewrites the identity metadata only (binding and space rows), never vectors,
     in one audited step with a tap event and a log line. It is idempotent.
   - A model without certification, or a changed model, pooling or prefix, is a new space and
     goes through the F-03 controlled re-embed.
   - The projection fingerprint and table-name rules must be checked during the build. If the
     table name embeds the embedder id, adoption must keep the existing table (rename the
     binding, not the data).
8. **Dependencies:**
   - `onnxruntime`, `tokenizers` and `huggingface-hub` become direct core dependencies. All
     three are already installed as fastembed's dependencies, so the core does not grow.
     `fastembed` leaves `pyproject.toml`.
   - `flashrank` (`[rerank]`) keeps its own dependencies.
   - The Hub `<2` cap then remains only where another package still needs it. litellm is the
     other blocker (ADR-085).

## Consequences

- Removes one of the two Hub 2.x blockers. memspine owns the pooling and normalisation code
  for a small, tested set of models.
- Weights move from fastembed's own cache to the standard Hugging Face cache. The first
  start after the switch downloads the ONNX files once, or finds them already there when the
  same repo files are cached. Document this in USAGE and the upgrade notes.
- The build must cover: the catalogue; the connector and adapters; config rejection plus the
  codemod; the parity fixtures and tests; the space-adoption step with tests on SQLite and
  LanceDB, and pgvector if available; the warm-up category; and docs (USAGE, FEATURES,
  PROVIDER_DEFAULTS, DEPENDENCY_ANALYSIS, PYTHON313_DEPENDENCIES).
