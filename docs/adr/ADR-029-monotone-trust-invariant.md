# ADR-029 — Trust-horizon invariant for shared memory (`integrity.*`, opt-in)

> Renamed 2026-09-30 from "monotone trust invariant (MTI)": the phrase "monotone trust" is used by
> LoopHarness (arXiv 2608.27141) for a single-agent write gate. The name now says what the invariant
> bounds, the grant-hop *horizon* of admitted poison. Config keys and this file name are unchanged.

- **Status:** proposed (awaiting maintainer approval)
- **Date:** 2026-09-29
- **Decision id:** D-56 (extends E1 Memory Firewall and R2 shared grants)
- **Phase:** v0.3 research track · **Tier:** core, default OFF

## Context

Shared reads capped foreign trust at a flat `TRUST_RETRIEVED_CAP = 0.3`, but that left three gaps:

- **Trust never reached ranking.** `ScoringPolicy.composite_score` never reads `trust`, so the cap changed nothing about what ranks first.
- **Writes took no parents.** `Engine.write` had no parent argument, so every agent answer re-entered memory at its role's base trust. A foreign record paraphrased by a grantee was *laundered* back to 0.5 and re-shared: a model-free construction reaches every agent of a 5-agent chain.
- **Corroboration was Sybil-prone.** Promotion checked independence only between the corroborator and the held record, so one principal, even with a single message id, could promote its own quarantined payload.

Paper A (research repo, `paper_aamas27/`) proves that a finite propagation radius exists iff the per-hop trust gain is < 1. It needs three things from the engine: provenance-carrying deposits, per-grant attenuation, and trust-aware admission.

## Decision

Add an `integrity` config block, **disabled by default**. When disabled, every path is byte-identical to the pre-ADR engine; this is guarded by the full suite.

| Key | Effect when `enabled` |
|---|---|
| `attenuation` (`product` / `min`), `kappa`, `edge_kappa` | A foreign record's view trust is `trust × κ` (or `min(trust, κ)`) for its grant edge |
| `write(..., derived_from=[ids])` | Always records `source.parents`. When enabled, it also caps trust at `min(base, view(parent)…) × derivation_decay` (MTI-D). An unreadable parent counts as 0.0 (fail closed) |
| `admission_threshold` θ, `trust_weighted_ranking` | `search`/`shared_search` drop records with view trust < θ and rank by score × view trust |
| `principal_bound_corroboration` | Corroborators need a `source.principal` that differs from the held record's and from every earlier corroborator's, recorded in the transition payload |
| `merge_reinforcement_gate` | A less-trusted dedup duplicate never reinforces the kept record |
| `verification_bonus` | **Baseline emulation only** (≥ 1 breaks the invariant on purpose, to measure prior-art rules) |
| EXPOSE event | `shared_search` appends a reader-side `memory.expose` event listing foreign ids returned (in the reader's namespace, never the grantor's) |

It also adds two operator-level forensic calls:
- `audit_taint(..., cross_namespace=True)` follows declared parents across grants and lists exposed readers.
- `rollback_taint(seed)` archives the seed and every content-derived descendant through DECAY_TRANSITION deltas. Merge survivors are returned for review, not archived.

Related changes made in the same pass:
- **EI-1 (bug fix, default-visible):** `search` no longer returns the namespace's own `memory_type="shared"` bookkeeping (grant and subscription records), which occupied top-k slots. `shared_search` already hid foreign ones. Guarded by `test_search_never_returns_grant_bookkeeping`.
- **G8:** `write_ex(...)` returns `WriteOutcome(record, action, occurrence_id)`, so callers can tell a dedup merge from a new record. `write()` is unchanged.
- **G9:** `assemble(..., shared=True)` assembles over grants via `shared_search`. With `integrity` on, the trust-weighted scores are rescaled uniformly, so the top candidate sits at its unweighted score: abstention (θ_abstain) keeps judging relevance, while trust already had its own gate.
- The M11 log vocabulary gains `EVENT_EXPOSE`.

`SourceInfo` gains `principal` and `parents`. Both are stored inside the existing JSON `source` column, so **no migration** is needed.

## Consequences

- **Evidence:**
  - Unit and property tests (2,000 random graph/trajectory pairs, 0 ceiling violations).
  - Engine tests.
  - A 135-run model-free grid: the bound held 108/108, the refined prediction was exact 108/108, and the invariant-off arm reached the whole team 27/27.
  - Full suite: 703 passed, 15 skipped. 2 integration failures are pre-existing or flaky; they fail at HEAD or pass in isolation.
- **Known limits:**
  - Principal-bound independence reads prior corroborations from the log, so under `event_log.mode: ephemeral` it degrades to "differs from the held record".
  - Conservative parents drain benign trust (tracked as MG-2).
  - Soft admission is not implemented as an admission mode. The B6 wrapper (below) labels low-trust
    records that were already admitted; below-θ content is still dropped.

## Amendment (2026-09-30): enforcement, not assumption

Paper A's assumptions A1 (complete mediation) and A2 (declared parents) were caller obligations. These
opt-in keys make the engine enforce them. All default off; `integrity.enabled: false` stays byte-identical.

| Key / call | Effect |
|---|---|
| `implicit_parents: turn \| session` (B0) | Reads made with a `session_id` are recorded, and become the parents of that session's next write (`turn`: consumed per write; `session`: until `end_session`). Omitting `derived_from` no longer launders trust |
| `live_reevaluation` (B4′) | Candidates are re-checked against their current parents at read time, so quarantine, rollback or revocation of an ancestor propagates to descendants. Radii can only shrink |
| `untrusted_wrap_below` (B6) | Assembly renders records below this view trust as labelled data |
| `authorize(ids, namespace, threshold)` (B6) | Allows an action only if every evidence record is readable and its current view trust is at least `threshold`. Fails closed; returns the weakest link |
| `send()` (B5) | Agent-to-agent messages go through the write door, so A1 holds for messages sent this way |
| `repair_taint(seed)`, `verify_integrity()` | Roll back, then re-derive the benign descendants. Offline hash-chain, fingerprint and MTI verification over the log |

Evidence: the unit tests in `tests/unit/test_engine_integrity.py` and `test_engine_repair.py`. For the
real-LLM guard study (Qwen3-32B agents, `authorize` as the action gate), see `paper_aamas27/results/llm_guard.json`
in the research repository. Retrieval-only surfaces added later on the read path (anticipatory cues,
C8′; replay and full-context `read()`, C7′) apply the same gates.
- **Not changed:** the default profile, firewall verdicts, quarantine semantics, and grant enforcement (`grant_allows` remains the single decision point).
