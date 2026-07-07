# ADR-006 — Config layering + templates with `extends:`

- **Status:** accepted
- **Date:** 2026-07-07
- **Decision id:** D-11 / D-12 / D-14
- **Phase:** P0 · **Tier:** QW

## Context

One engine, many profiles (voice, coding, personal, regulated, multi-agent). Users must be able to see *why* a knob has its value, and profile authors must compose rather than copy.

## Decision

Layer order: schema defaults → template (`extends:` chains, cycles rejected) → user YAML → env (`MEMSPINE_A__B=x`) → runtime kwargs. `${secret:NAME}` resolves via the secrets port during two-phase bootstrap (D-22). Every dotted key records its source layer; `memspine config resolve` prints `key: value  # source: layer`. Per-instance type enablement + per-namespace *policy* overrides in v0.1; `namespaces.<ns>.memories` is reserved and rejected until v0.2 (D-14). Lands in `config/loader.py`, `config/schema.py`, `config/templates/`.

## Consequences

- Positive: one customization model reused by prompts (ADR-009); debuggable provenance per key; golden-testable.
- Negative / cost: custom loader instead of stock pydantic-settings sources (needed for source tracking + extends).
- Follow-up: v0.2 unlocks the reserved per-namespace key.

## Alternatives rejected

- **Plain pydantic-settings** — no `extends:`, no per-key source tracking.
- **Hydra/omegaconf** — heavy, different mental model, core-dep cost.
