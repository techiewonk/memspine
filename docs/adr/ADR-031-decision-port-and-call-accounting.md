# ADR-031 — Decision port (`services/decision`) and model-call accounting (`Engine.model_calls()`)

- **Status:** proposed (awaiting maintainer approval; the default is unchanged)
- **Date:** 2026-10-02
- **Decision id:** D-57 (extends D-07/D-22 LLM roles and D-28 `[ner]`)

## Context

Two needs came out of the v0.3 read-path work and the evaluation harness.

- **Cheap calibrated choices.** Some read-path decisions are too blunt for a rule but too expensive
  for an LLM call per query. The first one is routing `read(mode="auto")` to compose, replay or
  retrieve. An encoder that picks one of several *described* options in one forward pass, without
  generating tokens, fits that gap. GLiNER2 (fastino-ai/GLiNER2, Apache-2.0, 205M–340M parameters,
  CPU-first) has a schema-driven classification head and is already the `[ner]` extra (D-28).
- **Measured cost.** The evaluations report model calls per loop stage. Before this change the
  harness's memspine adapter reported 0 write calls "by observation", although mining, anticipation,
  reflection and LLM extraction do call models. Cost must be measured, not assumed.

## Decision

**1. A decision port.** `services/decision` defines `DecisionProvider`:
`async choose(text, options: Mapping[label, description]) -> (label, confidence)`.
- `decision.provider: off | gliner2` (default `off`), `decision.model` (default
  `fastino/gliner2-base-v1`).
- The GLiNER2 adapter imports `GLiNER2` (with `AutoExtractor` as a fallback name) and loads the
  checkpoint lazily under a lock, in a worker thread. A load failure is cached, so it is not retried
  on every read. It classifies through `create_schema().classification(...)` + `extract(...)` when the
  model exposes them, and through `classify_text(...)` otherwise. A label outside the options raises.
- `Engine.start()` validates the provider: when gliner2 is not installed it raises
  `MissingServiceError(extra="ner")`, or under `strict_services: false` logs once and leaves the
  provider off (D-10).
- The first consumer is `read.planner: decision`. Any provider failure falls back to the rules
  planner: the provider is an enhancer, never a gate.

**2. Call accounting.** `LLMRouter.for_role(role)` returns a counting wrapper (`_Counted`). Each
`chat` call through it increments a per-role counter in the router. `Engine.model_calls()` returns
those counts since `start()`, for the read path, the write path and the sleep cycle. `Engine.llm(role)`
returns the same wrapper. `LLMRouter.provider(role)` returns the bare provider, for wiring checks
only. The wrapper delegates other attributes to the provider, refuses dunder and `_inner` lookups
(so `copy.deepcopy` and `pickle` work), and has a `__repr__`.

The decision provider is not an LLM role and its calls are not counted in `model_calls()`.

## Consequences

- `profile="simple"` is unchanged: `decision.provider` is `off` and `read.planner` is `rules`. The
  golden guard `tests/unit/test_simple_profile_golden.py` pins these defaults.
- The harness's memspine adapter reads write, query and build (sleep) cost from `model_calls()`.
  The `c0-1 --memspine-build-sleep` hook charges sleep-cycle calls to the synthesise (K) stage.
- **Not verified:** the gliner2 package is not installed in the development environment, so the
  real classification call has not been run. The import and `from_pretrained` match the NER adapter
  (`memories/semantic/entities.py`) and graphiti's `GLiNER2Client`. The classification call shape is
  handled defensively, and `parse_choice` fails loudly on an unknown result shape. A run with the
  real model is still needed before `read.planner: decision` is used in a reported result.
- **Open question for the maintainer:** should decision-provider calls get their own counter, so the
  read-path cost report includes encoder passes as well as LLM calls?
- Tests: `tests/unit/services/decision/test_decision.py` (fake gliner2 module: import fallback,
  default checkpoint, cached load failure, load lock, start-time validation, planner fallback) and
  `tests/unit/services/test_llm_call_counting.py` (deepcopy/pickle, per-role counts,
  `model_calls()` after write, read and sleep through a real `LLMRouter` with stub providers).
