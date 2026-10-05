# ADR-049: Nemori predict-calibrate as an opt-in consolidation stage

- **Status:** accepted (research-grade, opt-in)
- **Date:** 2026-10-06
- **Decision id:** master absorb list #62 (EP-nemori). See `SOTA_COMPARISON_2026-10-05.md`:
  Nemori reports a drop of 14.8 to 21.3 points without its predict-calibrate step.

## Context

Mining (`mine_facts`) stores every fact a session states, including facts memory already holds.
Nemori's predict-calibrate principle stores only the *prediction error*: predict an episode from
existing memory, compare the prediction with what actually happened, keep the difference. The
claim is fewer, sharper facts; it is unverified on our harness.

## Decision

`memories.episodic.policies.consolidation.predict_calibrate: false`. When on, a sleep stage
`predict_calibrate` runs after `mine_facts`. It is inserted into the cycle only when enabled, so
the default sleep output is unchanged. Once per consolidated session (done marker, like the other
derived stages):

1. **Context.** Up to `PREDICT_CALIBRATE_KNOWLEDGE_K` (20) live semantic statements, best
   content-word overlap with the session first. Never summaries, cues, list cards, flagged or
   quarantined records, or anything derived from this session's turns.
2. **Predict** (`predict_episode` prompt and role, falls back to `extract`): from the known
   statements plus the session's date and opening turn (first `PREDICT_CALIBRATE_CUE_CHARS`
   characters), the facts the session most likely states, one per line. The model never sees the
   rest of the session.
3. **Calibrate** (`calibrate` prompt and role, falls back to `extract`; `ExtractedFacts` output):
   the prediction against the dated transcript; returns only the facts the prediction missed or got
   wrong.
4. **Deterministic guard.** A returned fact whose content words are covered (at least
   `PREDICT_CALIBRATE_COVERED`, 0.8) by one predicted line or one known statement is dropped, so a
   predicted fact is never re-stored even when the model echoes it.
5. **Store** each surprise through the write door like a mined fact: role `assistant`, channel
   `calibration`, tags `surprise_fact`, `calibrated:<session>` and `kind:<kind>`. Parents = the
   session's turns (erasure cascades); trust capped at the least-trusted turn (E1).

Two calls per session. Taint repair clears this stage's marker only when the stage is enabled.

## Consequences

- An ablation switch, not a replacement for `mine_facts`; whether it helps LoCoMo is to be measured.
- Simplifications against Nemori: the prediction cue is the opening turn (Nemori uses a generated
  episode title), and knowledge selection is lexical, not embedding-based.
- Tests: `tests/unit/test_predict_calibrate.py` (fake LLM: predicted facts are not re-stored, novel
  ones are; once per session; erasure cascade; off by default; skipped without a role), plus prompt
  goldens for `predict_episode` and `calibrate`.
