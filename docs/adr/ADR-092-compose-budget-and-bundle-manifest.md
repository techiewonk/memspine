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

## Amendment 2026-11-01 (final budget guard)

- The cap applies to the context a read or assemble returns, after every overlay, not only
  to the packer. One deterministic guard (`compose.pack.enforce_budget`, same counter)
  trims, until the context fits: retrieved records after the stable prefix from the end
  (with blocks whose every parent it removed), then header blocks in
  `LEAD_BLOCK_DROP_ORDER`, then other engine blocks from the end. E5 protected records and
  the retrieved stable prefix are never removed; when they alone are over, `oversize`.
- `budget_status` gains `trimmed` (fits because the guard removed something). Manifests stay
  consistent: a removed hit leaves `hits`; a removed included turn becomes a dropped turn.
- The routed budget is clamped at zero, so headers larger than the budget never hand the
  packer a negative budget.

## Amendment 2026-11-01 (review fixes)

- An opening record that cannot fit even truncated is skipped (traced `unfit`), and the next
  record is tried the same way. `oversize` means no record fitted at all; it no longer
  empties a context that a smaller record would fill.
- Truncation finds every engine wrapper wherever the render placed it (after an H5 date, an
  H22 gap marker or an I39 `[about: ...]` prefix): the B6 untrusted note (closed by its own
  nonce), the E1 instruction flag and the T5-02 neutralised quote with its directive
  brackets. The cut lands only inside the wrapped body, after the leading render markers, and
  every closing marker is kept or put back.
- Every record-dropping step on the read path (render fit, relevance gate, consent filter,
  final guard) calls one helper, `compose.pack.drop_records`. A removed included turn becomes
  a dropped turn. The stable-prefix boundary is the count of kept stable-prefix records.
- Consent runs before the final guard, on `read` and on `assemble`. The guard then measures
  and labels exactly the context returned, and nothing runs after it.
- One counter on the read path: `compose.pipeline.render.read_counter()`, which is the active
  reader tokenizer or the packer estimate `len // 4 + 1`. The bundle packer, the overlays,
  the gate, the consent filter and the guard all use it. The library `EstimatingCounter`
  (`ceil(len / 4)`) is no longer used on the read path.