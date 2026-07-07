# ADR-001 — Event-sourced core: append-only `memory_events` as the single source of truth

- **Status:** accepted
- **Date:** 2026-07-07
- **Decision id:** D0.1
- **Phase:** P0 · **Tier:** DF

## Context

A memory engine juggles many derived stores (vector, graph, FTS, cache). If each is written independently, they drift, and there is no way to audit how a memory came to exist — which the Memory Firewall (E1) and `audit taint` require. Every serious event-sourced system solves this with one append-only log and rebuildable read models.

## Decision

An append-only `memory_events` table is the *only* source of truth. All state mutations pass a single write door (`SQLiteStorage.append_event`). Vector/graph/FTS/cache/relational read models are `Projector`s: idempotent `apply(event)` with durable per-projector high-water marks, `catch_up()` at startup, `rebuild()` from seq 0. Lands in `core/events.py`, `core/projector.py`, `core/replay.py`, `services/storage/`.

## Consequences

- Positive: total auditability; every derived store rebuildable; crash-safe catch-up; the E1 taint audit becomes a log walk.
- Negative / cost: log growth (mitigated by ADR-011 retention modes); every new store must be written as a projector.
- Follow-up: later phases add projectors for vector/graph/FTS; the M7 hard-delete cascade must tombstone through the log.

## Alternatives rejected

- **Direct multi-store writes** — irreversible drift, no audit trail.
- **CDC/replication from the relational store** — makes SQLite the truth rather than the events; loses intent (kind/actor/provenance).
