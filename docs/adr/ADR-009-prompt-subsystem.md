# ADR-009 — Customizable prompt subsystem: YAML pack + registry + config layering

- **Status:** accepted
- **Date:** 2026-07-07
- **Decision id:** D-43
- **Phase:** P2 · **Tier:** DF
- **Amended:** 2026-07-10 (v0.2 B1) — Jinja partials + fingerprinted version; see *B1* below.
- **Amended:** 2026-07-10 (v0.2 B2) — `(role, selector)` keying + scenario variants; see *B2* below.
- **Amended:** 2026-07-10 (v0.2 B4) — the B1/B2/B3 contract (partials fingerprint, selector coverage, structured-model+budget pairing) is now locked by a golden/round-trip/lint harness (`tests/unit/prompts/test_harness.py`).

## Context

Every internal LLM call (extract, judge, consolidate, summarize, subcluster, query-rewrite, reflect, dedupe, firewall-flag) needs a named, versioned, user-overridable prompt. Survey (plan Part E): graphiti pairs prompts with pydantic output models; cognee/powermem ship prompts as external, documented-overridable files; langmem self-optimizes.

## Decision

First-class `src/memspine/prompts/` package: YAML default pack (frontmatter `id/version/role/output_model/format` + Jinja2 body), `PromptRegistry` keyed `(role, name, version)` resolving the active prompt through the existing D-11 config layering, per-role binding, pydantic output-model pairing straight into instructor (D-31), versioned lifecycle riding the procedural draft→active ladder, prompt-version in E3 cache keys and provenance. `memspine prompts list|show|diff|resolve|set|rollback` CLI. Self-optimization is an RG hook only (`[promptopt]`).

## Consequences

- Positive: one customization model (config layering) for both knobs and prompts; auditable + rollbackable prompt changes; typed, token-lean internal calls (E9 CoD/YAML formats).
- Negative / cost: registry/lifecycle machinery before first use; YAML pack must ship in the wheel.
- Follow-up: lands in Phase 2 with the extract/judge/dedupe prompts.

## Amendment — B1: partials loader + fingerprinted version (v0.2)

Prompt bodies (and `system`) render through a Jinja `Environment` whose loader
resolves `{% include %}` names against a shipped `prompts/defaults/_partials/`
directory, so repeated boilerplate — the anti-injection safety block
(`no_injection.j2`), the YAML-only output footer (`yaml_only.j2`) — lives once
instead of drifting across ten prompts. A `prompts.partials.<name>` config layer
(a `DictLoader` consulted before the filesystem loader) lets a deployment retune
the shared language without forking every prompt.

Because an included partial's text is part of the effective prompt, the registry
folds a digest of the partials each prompt transitively includes into
`prompt_version` (`<id>@<version>+<digest>`), so an edit to a fragment — or a
`prompts.partials` override — invalidates E3 caches and shifts E1 provenance
exactly as a body edit does. Prompts that include no partials keep the bare
`<id>@<version>` identity. This extends, and does not break, the D-43 contract:
the version-in-cache-key and version-in-provenance invariants still hold.

## Amendment — B2: scenario keying + conditional selection (v0.2)

The registry key generalizes from a bare `id` to `(role, selector)`. A role may
now have scenario *variants* shipped as `<role>@<scenario>.yaml` files carrying a
`when:` block (`memory_type` and/or `condition`); the base prompt (`id == role`)
has no `when` and matches everything at specificity 0. `registry.select(role,
memory_type=…, condition=…)` returns the most-specific eligible prompt — a
variant is eligible only if every constraint its `when` sets equals the query —
falling back to the base; two equally-specific matches are a config error.
`for_role(role)` now delegates to `select(role)`, so existing call sites keep
working and automatically honor a new `prompts.selection.<role>` config layer
that pins per-role default selectors. With no variants in the shipped pack the
base always wins, so `profile="simple"` is byte-identical. B3 populates the
actual variants (judge cheap/arbiter, extract short-text/document, summarize
length tiers); C's scenario-conditioned write prompts depend on this keying.

## Alternatives rejected

- **Inline prompt strings** — unversionable, untraceable (forbidden by CLAUDE.md convention).
- **baml** — rejected for instructor (D-31 anti-decision).
- **Prompt marketplace/hub** — out of scope v0.1.
