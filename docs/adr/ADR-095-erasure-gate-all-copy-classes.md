# ADR-095: Erasure gate across every copy class

**Status:** Accepted
**Date:** 2026-11-01
**Decider:** Repository owner (decision rounds 1-8, 2026-11-01)
**Register:** D-119

## Context

Erasure is checked at the fenced effect commit, but nothing proves forgotten content
disappears from every copy (DS-16).

## Decision

The phase C gate includes one test per copy class:
- records;
- event payload redaction;
- vector, lexical, graph and mention projections;
- copied excerpts;
- unresolved annotations;
- caches;
- logs and traces;
- restored backups, which re-hide erased ids on restore.

A forget during in-flight background work must leave no copy after the effect commit.
Unsupported copy classes fail the gate rather than being skipped silently.
