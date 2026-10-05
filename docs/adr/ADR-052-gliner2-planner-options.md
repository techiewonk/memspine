# ADR-052: Decision planner: rules first, then plain-label GLiNER2 options

- **Status:** accepted
- **Date:** 2026-10-06
- **Decision id:** G24 (task #89), following the real-model check of 2026-10-06 (task #42).
  Builds on H24 (decision port), G2b (`read.planner_min_confidence`) and H3/H16 (`query_shape`).

## Context

With `read.planner: decision` and `decision.provider: gliner2`, `Engine._plan_read_mode` gave
GLiNER2 three options named after the read modes (`compose`, `replay`, `retrieve`) with
one-sentence descriptions. On real LoCoMo questions `fastino/gliner2-base-v1` chose `retrieve`
for almost everything, with median confidence 1.0. Count and list questions were not sent to
`compose`, and the confidence gate could not catch this. It was also worse than having no
planner: once the planner returned `retrieve`, the `is_aggregation` fallback of
`read(mode="auto")` no longer applied.

## Decision

1. **Rules first.** `query_shape.rule_read_mode` settles counts and sets (`is_count`, or
   `is_aggregation` on a question that is not a date or duration question) as `compose`, and
   ordering questions (`is_ordering`) as `replay`. When it returns a mode, the provider is not
   called and `planner_min_confidence` does not apply, because a rule decision is not a guess.
2. **Plain labels for the rest.** The provider chooses among `count or list`,
   `reason or feeling` and `single fact`, each described by its cue words
   (`Engine._READ_OPTIONS`). Each label maps to `compose` / `replay` / `retrieve`. A label
   outside the options returns None, and the rules then apply as before.

The setup was chosen offline under a pre-registration (`evals/prereg/G24_gliner2_planner.md`).
The set is 100 LoCoMo questions labelled by deterministic rules and frozen before any model
run. 26 setups were tuned on 51 questions, and the selected one was read once on 49 held-out
questions. Held-out accuracy: **0.939** for the shipped setup against **0.388** for the old
options. The best single-model setup (no rules) scored 0.735. On the held-out questions the
rules leave open, it scored 0.889 against 0.593 for the old options. The rule was "ship only
if it beats current on held-out accuracy", and it did. `fastino/gliner2-large-v1` did no better
on the tune half, so the default checkpoint is unchanged.

## Consequences

- Only `read.planner: decision` changes. `read.planner` still defaults to `rules`,
  `decision.provider` to `off`, and `MemspineConfig()` / `template="core"` are unchanged. No
  config key was added.
- A routed count or list question now reads by `compose` again, as it did with no planner.
- GLiNER2 confidences stay near 1.0, so `planner_min_confidence` is still a weak filter for
  the provider's choices.
- The labels come from the same predicates as part of the hybrid, so its agreement on compose
  and ordering items holds by construction. The informative number is the accuracy on the
  questions the rules leave open, which the results table reports separately. Answer quality
  downstream of routing was not measured.
- The eval (`evals/gliner2_planner_eval.py`) checks in a test that the engine's options are
  the shipped setup's, so the two cannot drift apart silently.
