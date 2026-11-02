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

## Amendment 2026-11-01 (direction review)

Further copy classes:
- the entity name index;
- the message `metadata` bag;
- open-window drafts;
- truncated packed excerpts.

Each wave that adds a copy class adds its erasure test in the same wave.

## Amendment 2026-11-02 (owner decision: hidden facts)

A derived fact whose every source has been erased is redacted by default: its text and quote
are removed and an id-only tombstone is kept for lineage. A fact that still has a live source
stays hidden with its text, pending re-derivation. `erasure_redact_hidden` remains the opt-in
that redacts every hidden fact.
