# ADR-086: One current base implementation in the first development cycle

**Status:** Accepted
**Date:** 2026-10-24
**Decider:** Repository owner

## Context

The restructure is the first supported development cycle. Earlier development
generations are not released compatibility contracts. Generation flags and old
store adoption paths obscure the actual supported behavior and duplicate tests,
configuration and runtime branches.

## Decision

Use the latest implementation as the single base implementation. Internal code,
configuration, preset and prompt names are descriptive and unversioned. For example,
`segmentation_v2` becomes the base segmentation implementation, not a second mode.
The latest read planner becomes `plan`, without a planner-generation selector.

Remove switches that only select a generation. Keep meaningful optional features
and policy controls under descriptive names; adopting the latest implementation
does not enable every optional model call, retention policy or external lookup.
Merge shared helpers into cohesive modules rather than deleting functionality
merely because its original module had an older name.

Remove historical store import/adoption, event transformation, configuration
codemods, rename hints and old record decoding/emission. Initialize a fresh SQL
store from a complete current baseline. Require one current record/event format.
There are no old-key aliases, old-store relabeling or migration bridges.

Preserve current replay, idempotency, namespace/owner isolation, locks, leases,
projection target validation, provider/model changes, vector-space lifecycle,
erasure verification and rejection of unsupported schema versions. Current entity
reconciliation aliases are not historical format compatibility.

External API protocol paths, dependency/model identifiers and actual lifecycle
generation counters retain their required meanings. Historical ADRs, benchmark
provenance and audit snapshots remain historical evidence.

## Options considered

| Option | Maintenance | Consequence |
|---|---|---|
| Keep generations and rename selectors | High | Continues multiple unsupported development contracts |
| Rename newer generation to v1 | Medium | Retains generation naming without a supported predecessor |
| One descriptive base implementation | Lowest | Breaking first-cycle cleanup; selected by owner |

The current entity-community implementation is owned deterministic label propagation.
The older record-graph Leiden implementation and its unused `community` extra are
removed; this supersedes the implementation portions of D-40 / ADR-043.

## Consequences

Fresh/current stores and current configurations are the supported contract.
Existing development stores and historical configuration files need recreation,
not compatibility handling. Golden fixtures, presets, prompt identity and
configuration snapshots change with the coordinated current contract.

Implementation status and validation evidence belong only to RM-126 in
`docs/PROVIDER_TODO.md`.
