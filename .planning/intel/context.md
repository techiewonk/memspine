# Context (DOC intel)

Running notes from DOC-classified sources, keyed by topic, with source
attribution. These are supporting rationale/reference — not binding decisions.
11 docs classified DOC (incl. ADR-000, which is an empty template, not a decision).

---

## Topic: Project identity
- source: docs/ECOSYSTEM_COMPARISON.md; docs/README.md; CLAUDE.md
- memspine is an open-source **event-sourced cognitive-memory engine** for AI
  agents — one clean `Engine` API over a real write pipeline, hybrid + graph
  retrieval, and background learning dynamics, with pluggable/composable stores.
  It is the *engine*, not a product. Status: pre-alpha; P0–P7 all implemented and
  review-passed; ~705 tests; ruff + mypy --strict clean; 25 ADRs; decision register
  through D-54. All 9 memory types and E1–E9 landed.

## Topic: Authoritative blueprint
- source: docs/memspine-structure-plan.md (v1.5)
- The buildable blueprint: repo tree, extras matrix, locked decision register
  (D-01…D-54), phase plan (P0…P7), enhancement program (E1–E9), C6 combination
  matrix, code-level adoption decisions (Part C D-26…D-35, Part D D-36…D-42),
  prompt subsystem (Part E, D-43). If a doc and this plan disagree, the plan wins —
  but the individual **ADRs** are the locked decision sources it indexes.

## Topic: Architecture rationale (why)
- source: docs/UNIMEM_V2_REWORK_PROPOSAL.md (Draft v3)
- Argues evolving unimem v1's storage facade into a layered cognitive memory engine
  feeding memspine. Contains an embedded proposed decision register (D-26…D-35) —
  these are *proposed* items meant to slot into the structure-plan register, not
  standalone locked decisions. Treated as rationale/context.

## Topic: Memory taxonomy & features
- source: docs/FEATURES.md; docs/USAGE.md
- Reference catalog of shipped features P0–P7: nine memory types (working, semantic,
  episodic, resource, procedural, reflective, associative, prospective, shared) with
  config keys, verbs, policies, profiles. USAGE.md is the install/Engine/CLI/REST
  tutorial. Config keys are the acceptance surface for phase requirements.

## Topic: Dependency selection evidence
- source: docs/DEPENDENCY_ANALYSIS.md; docs/PACKAGE_CATALOG.md
- 2026-07-07 cross-ecosystem scan inventorying deps across 27 memory-engine repos.
  DEPENDENCY_ANALYSIS maps candidate packages to memspine blueprint slots (vector,
  graph, embedder, LLM gateway, ingest, structured output). PACKAGE_CATALOG lists
  all 564 packages with a one-line role + memspine relevance markers. These are the
  evidence base behind ADR-008 (D-26…D-42) and the store defaults.

## Topic: Ecosystem positioning & flows
- source: docs/ECOSYSTEM_COMPARISON.md; docs/ARCHITECTURE_FLOWS.md
- Positions memspine against 16 peer memory engines; supplies ADR evidence for
  differentiators (event-sourced core, Memory Firewall) and convergent choices.
  ARCHITECTURE_FLOWS is a code-traced write/read pipeline reference with diagrams
  and cited entry points, plus CSV exports (ECOSYSTEM_PEER_TRACE, WRITE_STAGES).

## Topic: Research novelty backbone
- source: docs/RESEARCH_NOVELTY.md
- Inventory of memspine's research contributions (claim + evidence + code location)
  against a 15+ framework survey corpus. Headline novelties: the Memory Firewall
  (no surveyed framework ships poisoning defense), event-sourced retention modes,
  typed-memory combination calculus. Prose rationale — records no decision of its own.

## Topic: ADR template
- source: docs/adr/ADR-000-template.md
- Empty boilerplate ADR (Context/Decision/Consequences/Alternatives) with
  placeholder title + status. Classified DOC (not a decision) — no content. Not
  locked. Downstream should ignore for decision extraction.
