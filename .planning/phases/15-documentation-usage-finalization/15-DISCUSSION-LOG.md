# Phase 15: Documentation & Usage Finalization — Discussion Log

**Date:** 2026-07-10 · **Mode:** discuss (default) · Human reference only (not consumed by downstream agents).

## Overarching steer (user)
- "My target is to develop the best memory system."
- "Added architecture / eco compare in doc — check all docs." → reconcile the full doc set, not just the 3 named docs.

## Areas selected to discuss
All four: Doc-set scope · Positioning · Example depth · Drift-prevention.

## Decisions

| Area | Options presented | Chosen |
|---|---|---|
| Doc-set scope | Full set / Named 3 + config catalog / Named 3 only | **Full set** — USAGE, FEATURES, README, ARCHITECTURE_FLOWS, ECOSYSTEM_COMPARISON + config-key reference |
| Positioning | Composable & swappable / Capability breadth / Engineering rigor | **Composable & swappable** — lead claim; eco-comparison table in README, deep-dive in ECOSYSTEM_COMPARISON |
| Example depth | Comprehensive runnable / Minimal + reference table | **Comprehensive runnable** — per-surface snippets + backend-swap config diffs + full config-key table |
| Drift-prevention | Automated doc-test / Manual audit once | **Automated doc-test** — extract documented keys/verbs/routes/examples, assert each resolves against code |

## Claude's discretion (not asked)
- Per-doc section ordering, prose, featured examples.
- Config-key reference hand-written vs schema-generated (prefer generated).
- Doc-test extraction strategy (regex vs small parser).

## Deferred
- Live-backend contract verification → Phase 16 (roadmapped).
- Alembic migration squash → Phase 17.
- mkdocs site publish → possible follow-up.

## Coordination hazard noted
- Concurrent session mid-edit on USAGE.md/FEATURES.md — execution must confirm it has settled first.
