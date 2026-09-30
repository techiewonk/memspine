# ADR-030 — Relevance-first scoring mode (`scoring.mode`, opt-in)

- **Status:** proposed (awaiting maintainer approval; the default is unchanged)
- **Date:** 2026-09-30

## Context

The M1 composite score is the equal-weight mean of recency, relevance and importance, plus a utility
modifier. So relevance is one third of a record's rank. On the standard LoCoMo benchmark (snap-research
@ `3eb6f2c`, retrieval mode, no model calls), this blend demotes the best-matching turn:

| memspine scoring (conv-26, R@1 / R@5) | R@1 | R@5 |
|---|---|---|
| default `blend` | 0.102 | 0.426 |
| relevance only (other weights 0) | 0.294 | 0.619 |
| `relevance_first` | 0.289 | 0.614 |

On the full set, relevance-only lifts R@1 from 0.084 to 0.304. Event and ingest times cluster in such a
corpus, so recency is noise there, and importance is a heuristic. Zeroing the other weights discards
them entirely, which is wrong for live agents, where freshness matters.

## Decision

Add `ScoringOptions.mode`:
- `blend` is the default and is unchanged.
- `relevance_first` scores `relevance + tie_break_weight × nudge`. Here `nudge ∈ [0, 1]` combines recency,
  importance and utility, and `tie_break_weight` defaults to 0.05. Relevance decides the order; the other
  signals only separate near-equal matches.

The mode is set like every scoring option: `read.scoring` or per-type policies. `profile="simple"` and all
current defaults are byte-identical.

## Consequences

- Benchmark arms can declare `scoring.mode: relevance_first` instead of zeroing weights by hand.
- **Open question for the maintainer:** should the `agent` profile, or a QA template, default to it? The
  evidence above supports it for retrieval-heavy workloads. It has not been measured on live, time-varying
  agent memory, where the blend was designed to help.
- Tests: `tests/unit/core/policies/test_scoring.py` covers relevance order under `relevance_first`, recency
  tie-breaking, and default `blend`.
