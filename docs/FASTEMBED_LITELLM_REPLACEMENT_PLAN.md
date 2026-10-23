# Replacement plan: own ONNX embedder (ADR-084) and own OpenAI-compatible client (ADR-085)

Status: planned 2026-10-23 and handed to Codex. Register rows: RM-123 (ADR-084) and RM-124
(ADR-085) in [PROVIDER_TODO.md](PROVIDER_TODO.md). Statuses live there only.

Goal: remove `fastembed` and `litellm` from core, which are the two packages that block
`huggingface-hub` 2.x, with no behaviour regression and no aliases (ADR-068).

## Facts checked on 2026-10-23 (re-verify before building)

| Fact | Source |
|---|---|
| fastembed 0.9.0 requires `huggingface-hub>=0.20,<2.0` (release and `main`) | RM-02 Hub attempt, `docs/PYTHON313_DEPENDENCIES.md` |
| litellm 1.106.2 requires `huggingface-hub>=0.34.0,<2.0`; `main` 1.108.0 too | same |
| fastembed is imported only in `src/memspine/clients/weights/fastembed.py` (`TextEmbedding`, `TextCrossEncoder`) | grep |
| litellm is imported only in `src/memspine/clients/litellm.py` (`acompletion`, `aembedding`, `arerank`) | grep |
| Adapters: `services/embedding/fastembed.py`, `services/rerank/fastembed.py`, `services/llm/litellm.py`, `services/embedding/litellm.py`, `services/rerank/litellm.py` | tree |
| fastembed models in use: `BAAI/bge-small-en-v1.5` (default), `BAAI/bge-base-en-v1.5`, `sentence-transformers/all-MiniLM-L6-v2` (`FASTEMBED_MODEL_DIMS`), `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (`cpu_multilingual` preset), `Xenova/ms-marco-MiniLM-L-6-v2` (`RERANK_FASTEMBED_MODEL`) | `config/schema/providers.py`, `models/embedding_profiles.py`, `config/constants.py` |
| Embedder id today is `fastembedder:<model>`; it is part of the vector table and projection identity | `services/embedding/fastembed.py`, `providers/projection_bindings.py` |
| litellm routes are fixed per descriptor: `ollama_chat/`, `hosted_vllm/`, `bedrock/` | `providers/builtin/models.py` |
| Features in use: messages, temperature, top_p, max_tokens, timeout, `/no_think` + Ollama `think: false`, usage totals, embeddings `dimensions`/`input_type`, rerank, `response_format` json_schema or grammar (`services/llm/capabilities.py`, `structured.py`); no streaming, no tool calls | grep |
| Tests and evals stub LLM calls by patching `litellm.acompletion` (`evals/memspine_evals/stub_llm.install_stub_litellm`; 13 files) | grep |
| Footprint: fastembed in src 25 / tests 29 / evals 85 / scripts 7 files; litellm in src 24 / tests 45 / evals 42 / scripts 5 files | `git grep -l` |
| `instructor` is not a dependency; structured output is memspine's own code + `json-repair` | `pyproject.toml` |

## Order

The two ADRs are independent, so build them in parallel worktrees. Then run the Hub 2.x
attempt (WP-H).

### RM-123: ONNX embedder and reranker (ADR-084)

| WP | Work | Done when |
|---|---|---|
| E1 | **Parity fixtures first, while fastembed is still installed.** A script (`scripts/make_onnx_parity_fixtures.py`) embeds a fixed multilingual text set with each catalogue model and scores fixed pairs with the cross-encoder. It commits small fixtures (vectors rounded to 1e-7, plus model revision hashes) under `tests/unit/services/embedding/data/`. | Fixtures committed; the script is documented |
| E2 | `clients/weights/onnx.py` (session plus tokenizer through `hf_hub_download`, `local_files_only`, execution providers, threads) and the catalogue (`clients/weights/onnx_catalogue.py`: repo, revision, files, pooling, normalisation, max length, prefixes, dim, certified flag) | Connector unit tests; offline load with a missing file raises naming the file |
| E3 | `services/embedding/onnx.py` (batching, query/passage prefix, `embedder_id = embedder:<model>`), `services/rerank/onnx.py`, descriptors, the warm-up category `embedding`/`rerank`, shared-weights key | Parity tests pass for all 5 models; the shared-weights test confirms one load per model spec |
| E4 | Config: provider `onnx`; `fastembed` is rejected naming `onnx` (embedding and rerank chain); `model.embedding` default provider becomes `onnx`; the `cpu_multilingual` preset moves to `onnx`; templates and presets. Codemod rule (extend `scripts/migrate_config_keys_adr082.py` or add `scripts/migrate_config_keys_adr084.py`) with tests. | Old configs are rejected, the codemod rewrites them, and a second run is a no-op |
| E5 | Vector-space adoption `fastembedder:<m>` → `embedder:<m>` for certified models: binding and space metadata only, one audited step, idempotent. First check how the LanceDB table name and the projection fingerprint are derived (RM-111 pinned them byte-identical). Adoption must keep the existing table and data. | Tests on SQLite + LanceDB (and pgvector on a disposable container): a store written with fastembed reads identically after the switch, with no re-embed. An uncertified model goes through the F-03 controlled re-embed. |
| E6 | Remove `fastembed` from `pyproject`. Make `onnxruntime`, `tokenizers` and `huggingface-hub` direct core dependencies. `uv lock` (winget uv 0.13.5), `uv lock --check`, the audit; docs: USAGE, FEATURES, PROVIDER_DEFAULTS, DEPENDENCY_ANALYSIS, PYTHON313_DEPENDENCIES, the CLAUDE.md "Embedder" default row, the status table in `CLIENT_ADAPTER_FACTORY_STANDARD.md` §2a. Migrate the tests, evals and scripts that name fastembed. | Full chunked regression green; evals 0 new failures against `d9a87db6`; import contracts 18/0 |

### RM-124: Own OpenAI-compatible client plus `[anyllm]` (ADR-085)

| WP | Work | Done when |
|---|---|---|
| L1 | `clients/openai_compat` connector on the shared httpx pool (base URL, key by env-var name, digest identity, locality loopback→host, timeouts; health never calls a model) | Connector tests incl. credential redaction |
| L2 | Adapters: LLM chat (sampling, no-think rules, usage totals, `supports_json_schema`/`supports_grammar`, Ollama `format` translation), embeddings (`dimensions`), rerank per server family (vLLM, Jina/Cohere-style, Ollama). Presets `openai_compatible`, `ollama`, `vllm`, `lmstudio`, `llamacpp_server`. | Behaviour-parity tests against recorded litellm request and response shapes for each feature in the facts table |
| L3 | `bedrock` on boto3 `bedrock-runtime` (Converse, embeddings, rerank) in `[aws]`, with the boto3 vocabulary kept in the client layer (follow `clients/s3`) | Unit tests with botocore Stubber; live test only if credentials are explicitly provided (no paid runs by default) |
| L4 | `anyllm` provider in an optional `[anyllm]` extra (`any-llm-sdk` base package, pinned per D-92) | Adapter tests with a fake provider; lazy import; missing extra gives a `MissingServiceError` naming `[anyllm]` |
| L5 | Test transport: replace `install_stub_litellm` with an httpx `MockTransport` stub mounted on the connector, giving the same canned replies; migrate the 13 users and every test that patches litellm | No test or eval imports litellm; nothing reaches the network |
| L6 | Config: `litellm` rejected with a model-id mapping hint (`ollama/llama3` → `{provider: ollama, settings: {model: llama3}}`, `bedrock/...` → `bedrock`, other → `openai_compatible` or `anyllm`); codemod rule with tests; templates and presets | Old configs rejected; codemod rewrites them |
| L7 | Remove `litellm` from `pyproject`; lock; audit; docs as in E6, plus ADR-024/D-33 marked superseded in the register | Full chunked regression green; evals 0 new failures; import contracts 18/0 |

### WP-H: Hub 2.x attempt (after both rows land)

Raise the `[decider]` cap to `huggingface-hub>=2.2,<3`, then `uv lock`. Re-run the offline and
cache checks of `clients/weights/*` (onnx, gliner2, opendecider, model2vec) and the
regression. Record the result under RM-02 and task-transfer T-07.

## Rules for the build

Use the session transfer note
([CODEX_SESSION_TRANSFER_2026-10-23.md](CODEX_SESSION_TRANSFER_2026-10-23.md)):

- no commit trailers;
- push only when the owner asks;
- one tracker;
- chunked tests;
- regenerate the generated files;
- worktrees for parallel work.
