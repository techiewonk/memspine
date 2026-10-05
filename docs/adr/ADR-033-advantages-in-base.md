# ADR-033: The measured read-path advantages are the defaults of `base` (`profile="simple"`)

- **Status:** accepted (maintainer request, 2026-10-05)
- **Date:** 2026-10-05
- **Decision id:** D-59. Amends D-12 (template layering) and ADR-032 / D-58 (the default template).

## Context

ADR-032 put the measured combo-A read settings into a separate `assistant` template and made it the
default for `Engine()`. `base` (`profile="simple"`) kept the bare configuration, and every other
template (`coding`, `personal`, `voice`, `multi_agent`, `regulated_financial`) extends `base`. So
only `assistant` users got the gains.

Measured gain on LoCoMo categories 1–4, 1,540 questions, Qwen3-32B as reader and judge: **70.7% →
78.3% ± 0.26** (3 runs). It comes at the same ~1.6K-token context and with no extra model calls.

The maintainer asked that `simple` and `assistant` be the same, with the advantages on by default.

## Decision

`base` carries the combo-A settings, so every template inherits them:
- `read.default_mode: replay`;
- `read.resolve_relative_dates`;
- `read.order_by_time_for_ordering`;
- `read.assembly.relative_floor: 0.3`;
- relevance-first scoring (recency, importance and utility weights 0);
- `read.record_access: false`;
- `prompts.selection.chat: {condition: dated}` (the `chat@dated` answer prompt).

The other templates change as follows:

| Template | After this ADR |
|---|---|
| `assistant` | extends `base` and only names the profile. It stays the `Engine()` default (ADR-032) |
| `core` (new) | the bare configuration, i.e. the old `base` |
| `coding`, `personal`, `voice`, `multi_agent`, `regulated_financial` | inherit the advantages from `base` |

What does not change:
- **`MemspineConfig()` schema defaults.** They stay the minimal core; the golden guard
  `tests/unit/test_simple_profile_golden.py` pins them.
- **Reranking.** It stays off and opt-in per requirement.

## Consequences

- **Behaviour change for `template="base"` / `profile="simple"` users.** The changes are the same
  ones ADR-032 listed for `Engine()`:
  - replay reads;
  - `[= date]` annotations;
  - relevance-first scoring;
  - no access recording;
  - the dated answer prompt.

  Use `template="core"` for the previous behaviour.
- **Other templates change too.** Coding, voice and regulated workloads now get relevance-first
  scoring as well. The benchmark evidence covers multi-session conversation only, so a deployment
  that relies on recency weighting should set it back in its own config.
- **Tests and evaluation.** The test suite and the eval harness's reference arms use
  `template="core"`, so the suite still tests the mechanics it was written for. Published runs
  (which named `base` when `base` was bare) stay reproducible with `core`.
- **Reversible.** Move the `read` and `prompts` blocks from `base.yaml` back into `assistant.yaml`.
