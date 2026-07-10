# ADR-024 — LiteLLM as the unified LLM / embedding / rerank gateway

**Status:** accepted · **Date:** 2026-07-10 · **Register:** amends D-03 (slim core), D-33 (litellm adapter-only), D-39 (openai_compat) · **Phase:** services/* completion — Phase 4

## Context

LLM access was a hand-rolled `OpenAICompatLLM` over an injected `HTTPClient`
(D-39): one adapter for Ollama / vLLM / LM Studio / llama.cpp-server, selected by
`llm.roles.<role>.provider = openai_compat`. It could not reach **cloud**
providers (Bedrock, Vertex, Anthropic, Azure, OpenAI's native API) — `bedrock`
was reserved in the schema but rejected in code — and embeddings had no cloud
path at all. Every cloud provider would have meant another bespoke adapter and
its auth/retry quirks. D-33 had reserved litellm as an *optional* adapter.

## Decision

1. **litellm is a core dependency** (amends D-03) and the **single** adapter for
   cloud + OpenAI-compatible-local LLM, embedding, and cloud rerank. The
   `[litellm]` extra is removed. "Slim core" is re-scoped to "no
   torch/transformers", not "no gateway" — litellm is pure-Python and pulls no
   heavy ML runtime. It is imported **lazily** (first `chat`/`embed`/`rerank`)
   so a default engine with no LLM role never pays its multi-second import.
2. **`OpenAICompatLLM` and `HTTPClient` are removed** (amends D-39). The only
   users were each other; nothing else imported `clients/http.py`.
3. **The role's `model` prefix selects the provider** (D-33) — `provider` and
   `base_url` are gone from `LLMRoleConfig`:
   - `openai/…`, `ollama/…` (local — set `api_base`), `bedrock/…` (set
     `aws_region` or use the boto3 chain), `vertex_ai/…`, `azure/…`, … → LiteLLM.
   - `llamacpp/<path-to.gguf>` → the kept in-process `LlamaCppLLM` (`[llmlocal]`).
   - empty `model` → `ConfigError`.
4. **Cloud embeddings** via `LiteLLMEmbedding` (`embedding.provider = litellm`).
   `embedding.dim` is **required** — a remote embedder's dimension is not
   locally discoverable and the vector store needs it up front. E4 quantization
   is off (`manifest.quantization = None`; a remote model's tolerance is unknown).
5. **Cloud rerank** via `LiteLLMReranker` (`read.rerank = litellm`,
   `read.rerank_model = cohere/rerank-english-v3.0` | `bedrock/amazon.rerank-v1:0`).
   A missing `rerank_model` degrades like any unavailable rerank adapter
   (skip-logged, sticky) — rerank never fails retrieval.

## Consequences

- Positive: 100+ providers (cloud + local) with no per-provider adapter; AWS auth
  via the standard boto3 credential chain; one place for LLM/embedding/rerank.
- **Breaking config** (pre-alpha, acceptable): `llm.roles.<role>.provider` /
  `base_url` are removed. Migration: `provider: openai_compat` + a local
  `base_url` → `model: "ollama/<name>"` (or `openai/<name>`) + `api_base:
  "<url>"`. `provider: llama_cpp` + `model: <path>` → `model: "llamacpp/<path>"`.
- Cost: core wheel pulls litellm (pure-Python). No torch/transformers enter core.
- `profile="simple"` unchanged: it binds no LLM role, so litellm is never
  imported and behavior is byte-identical.

## Alternatives rejected

- **Keep `openai_compat` and add litellm alongside** — two overlapping LLM code
  paths and config shapes to maintain; the hand-rolled adapter covered strictly
  less than litellm.
- **litellm as an optional extra** — cloud LLM/embedding is a first-class
  deployment target, not an add-on; gating it behind an extra reintroduces the
  "which extra?" friction D-10 tries to remove for a now-core capability.
- **AWS Secrets Manager via litellm** — that is litellm's Enterprise/proxy
  feature and resolves *litellm's* keys, not memspine's config secrets; handled
  by ADR-023's own secrets tier instead.
