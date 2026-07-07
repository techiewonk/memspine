# ADR-004 — Two-layer ports & adapters: services/ + clients/

- **Status:** accepted
- **Date:** 2026-07-07
- **Decision id:** D-22 / D-24 (M14)
- **Phase:** P0 · **Tier:** DF

## Context

Swappable backends (the whole point of an engine) die when capability logic and connection management tangle. MemOS's backend sprawl is the cautionary tale (plan Part D).

## Decision

`services/` are capability **ports** (storage, vector, graph, llm, embedding, lexical, cache, secrets) — interfaces plus `CapabilityManifest`. `clients/` own **every** connection to an external system (engine/WAL pragmas for SQLite, boto3 session, httpx pool); the lifecycle manager injects connected clients into services and closes them centrally. Services never open a connection. Engine/memories/policies talk only to ports.

## Consequences

- Positive: swap a backend by writing one adapter + one client; centralized retry/timeout; testable ports.
- Negative / cost: one extra layer of indirection for trivial backends.
- Follow-up: lifecycle manager grows dependency-ordered start/stop as services multiply (P1+).

## Alternatives rejected

- **Services own their connections** — duplicated retry/pool logic, untestable, leaks at shutdown.
- **litellm as the LLM layer** — adapter only, never core (D-33).
