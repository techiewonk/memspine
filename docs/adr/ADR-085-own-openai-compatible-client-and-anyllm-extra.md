# ADR-085: Own OpenAI-compatible client in core, any-llm as the optional hosted-provider extra

**Status:** Accepted (design) on 2026-10-23. Not implemented; the build is RM-124 and is handed
to Codex.
**Date:** 2026-10-23
**Deciders:** Owner (question rounds of 2026-10-23)
**Supersedes:** D-33 / ADR-024 (litellm as the unified LLM/embedding/rerank gateway in core).
D-39/D-46 (OpenAI-compatible local surface over `clients/http`) and D-07 (Bedrock) stay; this
ADR makes them the implementation.
**Relates to:** ADR-082 (model slots), ADR-068 (no aliases), RM-113 (connector standard),
ADR-084 (the other Hub 2.x blocker).

## Context

- **Hub 2.x is blocked.** litellm 1.106.2 (and 1.108.0 on `main`) requires
  `huggingface-hub<2.0`, which is one of two blockers for Hub 2.x.
- **litellm is very large** and reimplements every provider's API. A March 2026 supply-chain
  incident with compromised litellm PyPI releases is reported by several 2026 articles; it is
  not verified against a primary source.
- **memspine's use of litellm is contained:**
  - One connector, `clients/litellm.py` (`acompletion`, `aembedding`, `arerank`), with three
    adapters: `services/llm/litellm.py`, `services/embedding/litellm.py` and
    `services/rerank/litellm.py`.
  - Four LLM descriptors. `litellm` takes a free model id. `ollama`, `vllm` and `bedrock` fix
    the litellm route (`ollama_chat/`, `hosted_vllm/`, `bedrock/`).
  - Used features: messages, temperature, top_p and max_tokens; timeouts; `/no_think` for
    Qwen3 plus Ollama `think: false`; token usage totals (economy ledger); embeddings with
    `dimensions` and `input_type`; rerank scores; JSON-schema constrained decoding on the
    structured retry (I69).
  - No streaming or tool calls are used by the engine.
- **Tests and evals fake LLM calls by patching `litellm.acompletion`**
  (`evals/memspine_evals/stub_llm.install_stub_litellm` and 13 test/eval files).
- **2026 alternatives reviewed:**
  - any-llm (Mozilla.ai): Apache-2.0, Python ≥3.11, uses each provider's official SDK through
    extras; libraries should depend on the base package only.
  - The official `openai` SDK.
  - RouteHub: young, vendor-written evidence.
  - aisuite: not verified.
  - Gateways (Bifrost, Portkey and others): separate servers, out of scope for an embedded
    engine.

## Decision

1. **Core uses our own OpenAI-compatible client** on `clients/http` (httpx, already core). A
   new connector, `clients/openai_compat`, owns the base URL, the API key (env-var name and
   digest-only identity), timeouts and pooled HTTP. It covers:
   - `POST /v1/chat/completions`: messages, temperature, top_p, max_tokens, `response_format`
     with `json_schema` when the server supports it, and usage tokens;
   - `POST /v1/embeddings`: `dimensions` when supported;
   - rerank adapters per server family: vLLM `/v1/rerank`, Jina/Cohere-compatible `/rerank`,
     and an Ollama-native path where it differs.
2. **Providers:**
   - `openai_compatible`: any OpenAI-compatible server or cloud, with an explicit `base_url`.
   - `ollama`, `vllm`, `lmstudio`, `llamacpp_server`: presets of `openai_compatible` with
     fixed defaults (base URL, `think: false` for Ollama, `/no_think` for Qwen3 kept).
   - `bedrock`: boto3 `bedrock-runtime` Converse plus embeddings and rerank, in the existing
     `[aws]` extra.
   - `anyllm`: an optional `[anyllm]` extra (`any-llm-sdk` base package). The user installs
     the provider extras they need. It is the only route to the hosted long tail.
   - The in-process `llamacpp` provider (`clients/weights/llama_cpp`) is unchanged.
3. **`litellm` leaves the project:** the provider name, the connector and the dependency go.
   A config naming `litellm` is rejected with an error that names `openai_compatible` or
   `anyllm` and gives the model-id mapping (for example `ollama/llama3` →
   `{provider: ollama, settings: {model: llama3}}`). There is no alias (ADR-068), and a codemod
   rewrites configs.
4. **Behaviour parity.** The no-think rules, usage accounting, timeout semantics, retry policy
   (`services/_retry.py`), structured-output recovery (I69: one re-prompt and an optional
   constrained retry) and offline/locality rules must behave as today. Locality: a loopback
   base URL is `host`, otherwise `cluster`, as in `LiteLLMConnector.locality`. No health check
   ever makes a model call.
   - **Constrained decoding.** `services/llm/capabilities.py` and `services/llm/structured.py`
     choose `json_schema`, `grammar` or `none` today by litellm route prefix, and litellm
     translates `response_format` json_schema into Ollama's native `format`. The new providers
     declare `supports_json_schema` and `supports_grammar` explicitly. The `ollama` preset
     performs that translation itself, through the OpenAI-compatible endpoint's
     `response_format` where the server version supports it, else through the native
     `/api/chat` `format` field. Choose one per server version, documented and tested.
5. **The test transport replaces the litellm patch.** `install_stub_litellm` becomes a stub on
   httpx `MockTransport`, mounted on the `openai_compat` connector and answering the same
   canned replies (window-extraction stub and the others). Every test and eval that used the
   old stub moves to it. No test may reach the network.
6. **Dependencies:** `litellm` is removed from core. `any-llm-sdk` sits only in `[anyllm]`,
   pinned per D-92. The audit and `PYTHON313_DEPENDENCIES.md` are updated. Together with
   ADR-084, this removes the last Hub `<2` blockers among core dependencies.

## Consequences

- A slimmer core, with no reimplemented provider APIs in-process. Local servers (the
  local-first default) need no third-party client library.
- Hosted providers other than OpenAI-compatible clouds and Bedrock need the `[anyllm]` extra.
- The build covers: the connector; LLM, embedding and rerank adapters and their presets;
  Bedrock on boto3; the `anyllm` adapter; config rejection plus the codemod; the stub
  transport and test migration; docs (USAGE, FEATURES, PROVIDER_DEFAULTS, DEPENDENCY_ANALYSIS,
  PYTHON313_DEPENDENCIES); and the regression including evals with 0 new failures.
- After both ADR-084 and ADR-085 land, re-run the Hub 2.x attempt (RM-02 / T-07). The
  remaining Hub users (transformers, sentence-transformers, tokenizers, model2vec) already
  allow 2.x.
