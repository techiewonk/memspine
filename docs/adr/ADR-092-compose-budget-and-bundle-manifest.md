# ADR-092: Hard token budget, configurable bundles and packer parity

**Status:** Accepted
**Date:** 2026-11-01
**Decider:** Repository owner (decision rounds 1-8, 2026-11-01)
**Register:** D-116

## Context

With compression off, the packer admits a first record larger than the budget (DS-15).
Bundling uses a fixed `_BUNDLE_TURNS = 2` with no record of dropped dependencies (DS-13).
The live packer is not checked against the benchmarked `read/tiers.fit_units` (DS-14).

## Decision

- The token budget is never exceeded. An oversize first record is truncated at a sentence
  boundary and marked `truncated=true` with its original token count. If even the truncated
  record cannot fit, the result has an explicit `oversize` status.
- Bundle turns becomes a read config key (default 2). Each packed hit carries a dependency
  manifest (needed turns, included turns, dropped turns), and a hit whose needed turn was
  cut is marked `incomplete`.
- A parity test asserts the live packer selects the same units as `fit_units` on fixtures.
- RS-07: only the read-time span-dedup experiment is selected. It is adopted only if answer
  support on the RS-05 set does not drop.

## Amendment 2026-11-01 (direction review)

- The hard cap uses the same token counter the packer already uses for budgeting
  (compose/pack.py), so the cap and the accounting cannot disagree. The optional render
  tokenizer does not set the cap.
- Truncation falls back to a whitespace boundary when no sentence boundary fits, which
  works for any script.
- The `incomplete` hit status is the contract's `partial` state; one name is used, as the
  contract spells it.
