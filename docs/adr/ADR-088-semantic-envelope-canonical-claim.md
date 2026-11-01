# ADR-088: SemanticEnvelope is the one canonical claim schema

**Status:** Accepted
**Date:** 2026-11-01
**Decider:** Repository owner (decision rounds 1-8, 2026-11-01)
**Register:** D-112

## Context

The rules-only producer validates claims against `SemanticEnvelope`. Stores, the read path
and the model-backed path use `AssertionEnvelope`, with an optional `.semantic` field read
only by the window worker (RM-24, DS-19). Two claim shapes mean two code paths everywhere.

## Decision

In the first cycle (ADR-086, fresh schema), every claim, rules-only or model-produced, is a
`SemanticEnvelope`: registry-typed predicate, role bindings with evidence spans, typed
`status` (asserted, planned, negated, contested) and `outcome` (none, fulfilled, failed),
valid time with precision, `known_at`, source binding (record, version, fingerprint,
binding hash) and producer (kind, revision). `AssertionEnvelope` is removed with no alias.
The projector, graph compiler, read side, inspect/REST/MCP and the model path consume the
single shape. Status and outcome move from edge-only qualifiers onto the claim (RM-57);
edges are compiled from the claim.

## Consequences

One validation and projection path. The model path must produce role-bound claims or
unresolved annotations (ADR-090). The F1..F5/F1..F12 fixtures (RM-22, RM-54) are written
against this schema.
