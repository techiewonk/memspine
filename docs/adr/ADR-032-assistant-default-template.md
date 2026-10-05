# ADR-032: `assistant` is the default template for `Engine()`

- **Status:** accepted (maintainer request, 2026-10-05)
- **Date:** 2026-10-05
- **Decision id:** D-58 (amends D-12 template layering; keeps D-11 config layering)

## Context

The paid LoCoMo ablations of 3–4 Oct 2026 measured every v0.3 read-path feature one change at a
time (`paper_spine/evaluation/LOCOMO_RESULTS_2026-10-04.md`). The setup was Qwen3-32B as reader and
judge, LoCoMo categories 1–4, 1,540 questions. The read-side winners stack:

| Configuration | Accuracy | Context |
|---|---|---|
| `base` / `profile="simple"` | 70.7% | 1,555 tokens |
| combo-A (the `assistant` template) | 78.27% ± 0.26 (3 runs) | 1,574 tokens |

combo-A needs no extra model calls. Users who call `Engine()` with no template got schema defaults,
which are the weakest measured configuration for conversational memory.

## Decision

- `Engine()` with no `template` loads **`assistant`**. The shipped default lives in
  `config/constants.DEFAULT_TEMPLATE`. The `memspine` CLI resolves config the same way.
- **`profile="simple"` is unchanged.** `Engine(template="base")` gives exactly the previous
  behaviour.
- **The schema defaults are unchanged.** `MemspineConfig()` is still `simple`, and the golden guard
  `tests/unit/test_simple_profile_golden.py` still pins them.
- What `assistant` sets:
  - `read.default_mode: replay`;
  - `read.resolve_relative_dates`;
  - `read.order_by_time_for_ordering`;
  - `read.assembly.relative_floor: 0.3`;
  - relevance-first scoring (recency, importance and utility weights at 0);
  - `read.record_access: false`;
  - `prompts.selection.chat: {condition: dated}`, which selects the `chat@dated` prompt.
- Reranking is **off by default and opt-in per requirement** (`read.rerank: "off"`, decision
  2026-10-05). All reranker code stays in memspine (fastembed, flashrank, qwen3, litellm incl.
  Cohere); a deployment enables one only when it needs it. Cohere was measured (80.5%) but is not
  the default choice.

## Consequences

- **Behaviour change for callers that relied on "no template":**
  - `Engine.read()` without a mode now uses `replay`, not `auto`.
  - Relative dates are annotated (`[= 2023-07-14]`) in assembled context.
  - Scoring is relevance-first.
  - Reads no longer refresh `last_accessed_at`.
  - `Engine.chat_messages()` renders the dated prompt.

  Callers that want the old behaviour pass `template="base"`.
- **Workloads that are not chat** may prefer another template: `coding`, `personal`, `voice`,
  `multi_agent` or `regulated_financial`. The benchmark evidence is for multi-session conversation
  only.
- **Tests.** The engine test suite exercises mechanics on schema defaults. An autouse fixture in
  `tests/conftest.py` pins the pre-ADR default (no template) for every test, except tests marked
  `shipped_default`, which check the real default
  (`tests/unit/config/test_assistant_template.py`).
- **Evaluation runs are unaffected.** The harness's memspine arm always names its template.
- **Reversible.** Set `constants.DEFAULT_TEMPLATE` back to `None`, or to any template.
