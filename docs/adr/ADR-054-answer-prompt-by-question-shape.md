# ADR-054: Choose the answer prompt by question shape (opt-in)

- **Status:** accepted
- **Date:** 2026-10-06
- **Decision id:** feat/routed-qa (improvement master table items C1, C2, H11). Builds on the
  B2 prompt selection (`prompts.selection`) and the H3 query-shape rules
  (`core/query_shape.py`).

## Context

The 2026-10-06 LoCoMo prompt arms showed that one answer prompt does not fit every question:

- `dated_infer` gained on temporal questions (+1.3) and lost slightly elsewhere (-0.3).
- `dated3` lost 3.6 points against `chat@dated`. Its "short answer only" made answers terse
  (6.2 words on average against 15.3), and the rubric judge credits fuller answers that contain
  the gold item. Its conditional refusal clause ("Not mentioned" if nothing bears on it) raised
  refusals about 7x on Qwen3, mostly on "would / likely" questions.

## Decision

1. **`is_inference`** joins `core/query_shape.py`: a "would / likely / might / could" word
   before the question mark. It is a pure function. A date question (`is_temporal`) takes
   precedence over it.
2. **Harness `--qa-prompt routed` (C1, H11).** Each question gets one of three variants:
   - temporal questions: `dated`, with the refusal replaced by relative-date inference ("say you
     do not know only when nothing bears on it") and "Answer dates as DD Month YYYY (or the
     granularity the question asks)";
   - inference questions: `dated`, with the refusal replaced by "give the most plausible answer
     and say 'likely'" and no refusal;
   - every other question: the `dated` text verbatim.

   All variants keep `dated`'s one-sentence answer. The reader's `describe()` records
   `qa_prompt: routed`, the router version and one hash per variant. Each row records
   `meta["qa_variant"]`. Every existing `--qa-prompt` keeps its text, its `describe()` and its
   rows byte for byte.
3. **Engine parity (C2).** A `prompts.selection.chat_by_shape` entry maps a shape (`temporal`,
   `inference`) to a `chat` condition. `Engine.chat_messages` consults it through
   `PromptRegistry.select_for_question`. Two new chat variants carry the H11 wording:
   `chat@temporal` and `chat@inference`. An explicit per-call `condition` still wins. An unknown
   shape is a config error at start, and so is a condition no `chat` prompt declares (without
   that check `select()` would silently fall back to the base prompt). Unset, `chat_messages`
   renders exactly what it rendered before.

## Consequences

- Nothing changes by default. Routing is a run flag in the harness and a config entry in the
  engine.
- Only routing changes, not reading or retrieval. The predicted gain (+0.5 to +0.7 for C1, +0.2
  to +0.4 for H11) still has to be measured. Prompt-only predictions have had the wrong sign
  before (dated3).
- The shape rules are English-only regular expressions, like the other `query_shape` rules.
