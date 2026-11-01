# ADR-094: Per-family vocabulary domains and fixture-gated activation

**Status:** Accepted
**Date:** 2026-11-01
**Decider:** Repository owner (decision rounds 1-8, 2026-11-01)
**Register:** D-118

## Context

Built-in packs declare no top domain, so `graph_filter` by domain cannot match built-in data
(RM-107). Proposed families D, E, F, H, I, J, L, M and N are registered but inactive
(RM-62, RM-63).

## Decision

- Each built-in family declares one domain from a fixed generic set: `personal`, `work`,
  `health`, `finance`, `travel`, `learning`, `social`, `tools`, `other`. Packs carry none.
  No benchmark-specific names.
- A proposed family moves into the active pack only when its executable fixtures (positive,
  negative, known-time, replay, erasure) pass. Until then it stays opt-in via
  `memspine.proposed`.
- RM-66 closes as not applicable in the first cycle. The alias audit script stays as an
  optional diagnostic.
