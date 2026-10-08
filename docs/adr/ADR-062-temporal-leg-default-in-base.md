# ADR-062: Temporal leg on by default in the `base` template

- **Status:** accepted
- **Date:** 2026-10-08
- **Decision id:** D-86. Plan v3.2 row W4b. Decided by the project owner on 2026-10-08.

## Context

`read.temporal_leg` adds a retrieval leg that ranks live records by event time when the
question names a date or span. It has been off by default since it shipped.

The free LoCoMo screen of 2026-10-07 used the frozen engine:
- 1,540 questions, categories 1–4, retrieval only;
- the local `bge-small` embedder, with 0 model calls.

It moved evidence coverage from **75.1 to 76.8 (+1.7)**:
- 28 questions gained evidence and 2 lost it;
- the paired sign test is significant.

The leg adds no model calls and does nothing on questions without a date or span.

Rule U5 asks for two independent corpora before any default change. The LongMemEval-S
stratified screen (72 questions) is still running. The owner chose to change the default
now, on the LoCoMo evidence.

## Decision

`base.yaml` sets `read.temporal_leg: true`. Every template that extends `base` inherits
it, including `assistant`, the `Engine()` default.

The schema default stays `False`. The `core` template therefore remains the bare
reference, and every off-golden test that boots `core` is unchanged.

Not changed:
- `read.temporal_relative` stays off;
- `read.temporal_leg_event_dates` stays off.

Neither has its own screen result.

## Consequences

- Questions with a date or span get an event-time leg fused by RRF. Other questions read exactly as before.
- **Revert trigger:** this default is reverted if either result shows a significant coverage loss:
  - the LongMemEval-S screen;
  - the ConvoMem screen (chain 3, `v32f-lme72-temporal-leg`, `v32f-convomem-temporal-leg`).
- **QA untested:** answer accuracy (QA) with the leg on has not been measured, because QA runs are paid. The combo-A 79.3 figure was measured without the leg. Papers must not credit the leg with a QA gain until a QA run exists.
