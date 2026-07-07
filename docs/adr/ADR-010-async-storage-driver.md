# ADR-010 — Async storage driver: aiosqlite + greenlet in core

- **Status:** accepted
- **Date:** 2026-07-07
- **Decision id:** D-44
- **Phase:** P0 · **Tier:** QW

## Context

The facade is async-first with thin sync wrappers (D-01), and storage is SQLAlchemy Core (D-36) — but the original core dependency list had no async SQLite driver, leaving "async-first" unimplementable without threads.

## Decision

Add `aiosqlite` and `greenlet` to core. The SQLite client builds one `sqlite+aiosqlite` async engine per database (WAL + pragmas on connect); the whole storage service is genuinely async. Sync verbs wrap the async path via `asyncio.run` — never a second engine.

## Consequences

- Positive: true async end to end; both deps are tiny; SQLAlchemy's async engine is the ecosystem-standard route.
- Negative / cost: greenlet is a compiled wheel (ubiquitous, prebuilt everywhere); sync wrappers cannot be called from inside a running loop (documented).
- Follow-up: Postgres swap-in reuses the same async engine pattern via asyncpg (`[postgres]`).

## Alternatives rejected

- **Sync engine + `asyncio.to_thread`** — thread-pool contention on the single write door; "async" in name only.
- **Raw aiosqlite without SQLAlchemy** — reopens the hand-rolled-SQL problem D-36 closed.
