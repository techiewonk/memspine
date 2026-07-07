# ADR-007 — Public OSS from day one: semver, conservative schema, docs+ADRs

- **Status:** accepted
- **Date:** 2026-07-07
- **Decision id:** D-19 / D-20 / D-21
- **Phase:** P0 · **Tier:** QW

## Context

memspine is the open-source engine (Graphiti-style positioning); trust comes from discipline visible in the repo itself.

## Decision

Apache-2.0, keep-a-changelog + semver from 0.0.1. Public API = `memspine.Engine` + `__version__` only. Phase-0 DDL carries every column later phases need, so schema changes stay additive. mkdocs-material user docs plus `docs/adr/` — one decision, one ADR, one register row. Evals live at repo root, never in the wheel (D-19/D-35).

## Consequences

- Positive: reviewable decision history (this file included); no breaking-schema churn for early adopters.
- Negative / cost: wide day-one DDL must be designed carefully; ADR discipline is ongoing work.
- Follow-up: mkdocs site lands with P1 examples; release.yml publishes on tags.

## Alternatives rejected

- **Private incubation** — loses the ecosystem validation the dependency scan is built on.
