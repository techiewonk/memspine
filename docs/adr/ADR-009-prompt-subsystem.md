# ADR-009 — Customizable prompt subsystem: YAML pack + registry + config layering

- **Status:** accepted
- **Date:** 2026-07-07
- **Decision id:** D-43
- **Phase:** P2 · **Tier:** DF

## Context

Every internal LLM call (extract, judge, consolidate, summarize, subcluster, query-rewrite, reflect, dedupe, firewall-flag) needs a named, versioned, user-overridable prompt. Survey (plan Part E): graphiti pairs prompts with pydantic output models; cognee/powermem ship prompts as external, documented-overridable files; langmem self-optimizes.

## Decision

First-class `src/memspine/prompts/` package: YAML default pack (frontmatter `id/version/role/output_model/format` + Jinja2 body), `PromptRegistry` keyed `(role, name, version)` resolving the active prompt through the existing D-11 config layering, per-role binding, pydantic output-model pairing straight into instructor (D-31), versioned lifecycle riding the procedural draft→active ladder, prompt-version in E3 cache keys and provenance. `memspine prompts list|show|diff|resolve|set|rollback` CLI. Self-optimization is an RG hook only (`[promptopt]`).

## Consequences

- Positive: one customization model (config layering) for both knobs and prompts; auditable + rollbackable prompt changes; typed, token-lean internal calls (E9 CoD/YAML formats).
- Negative / cost: registry/lifecycle machinery before first use; YAML pack must ship in the wheel.
- Follow-up: lands in Phase 2 with the extract/judge/dedupe prompts.

## Alternatives rejected

- **Inline prompt strings** — unversionable, untraceable (forbidden by CLAUDE.md convention).
- **baml** — rejected for instructor (D-31 anti-decision).
- **Prompt marketplace/hub** — out of scope v0.1.
