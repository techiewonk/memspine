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

1. **Current provider `onnx`** for `model.embedding` and `model.rerank.chain`.
   This is the first development cycle: there is no previous provider contract to
   support, no alias and no configuration migration tool. Unknown providers fail
   normal registry validation.
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
7. **First-cycle storage identities (owner amendment, 2026-10-24).**
   - Fresh tables, projection bindings and versioned spaces use the current
     `embedder:<model>` identity directly. There is no old-provider table naming,
     metadata adoption, profile-pin relabel, alternate read identity or migration.
   - The earlier adoption design is withdrawn: there are no deployed previous
     versions to preserve. Remove that implementation and its migration tests.
   - Normal versioned-space routing and explicit re-embedding remain real model
     lifecycle features; they are not compatibility with retired code.
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
- Model files use the standard Hugging Face cache and pinned revisions. No retired
  provider cache is searched. Offline loading stays explicit.
- The build covers the catalogue, connector, current adapters/settings, committed
  numerical reference fixtures, current table identities, warm-up and documentation.
- The 2026-10-24 owner amendment supersedes the old-store adoption and configuration
  migration requirements in the original plan.
