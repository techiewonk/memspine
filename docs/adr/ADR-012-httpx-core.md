# ADR-012 — HTTP client in core: httpx

- **Status:** accepted
- **Date:** 2026-07-07
- **Decision id:** D-46
- **Phase:** P1 · **Tier:** QW

## Context

D-39 makes local/open-weight model hosting first-class, and every such host (Ollama, vLLM, LM Studio, llama.cpp server) exposes the OpenAI-compatible HTTP surface. `services/llm/local.py` therefore needs an async HTTP client, and D-24 requires the connection to live in `clients/`. The core dependency list had none.

## Decision

Add `httpx` to core. `clients/http.py` owns one shared `httpx.AsyncClient` (pool, timeouts, retries); services receive it injected and never construct clients. Tests use `httpx.MockTransport` — no network.

## Consequences

- Positive: genuinely async LLM calls; one pool for all HTTP services (REST protocol client reuse later); first-class test story via MockTransport.
- Negative / cost: +httpx/httpcore/h11 in core (~small pure wheels).
- Follow-up: retry/backoff policy centralizes here as providers multiply.

## Alternatives rejected

- **aiohttp** — heavier, C extensions, no MockTransport-style seam.
- **stdlib urllib in threads** — sync-in-async, no pooling, painful streaming later.
- **Defer to an extra** — would make the *default* local-LLM path (D-39) uninstallable in core, contradicting D-07.
