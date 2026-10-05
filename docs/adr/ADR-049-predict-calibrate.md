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

1. **Context.** Up to `PREDICT_CALIBRATE_KNOWLEDGE_K` (20) live semantic statements at or above
   `PREDICT_CALIBRATE_KNOWN_MIN_TRUST` (0.5), best content-word overlap with the session first.
   Never summaries, cues, list cards, flagged or quarantined records, low-trust records (one that
   pre-states a fact must not suppress the true one), or anything derived from this session's turns.
2. **Predict** (`predict_episode` prompt and role, falls back to `extract`): from the known
   statements plus the session's date and opening turn (first `PREDICT_CALIBRATE_CUE_CHARS`
   characters), the facts the session most likely states, one per line. The model never sees the
   rest of the session.
3. **Calibrate** (`calibrate` v2 prompt and role, falls back to `extract`; `ExtractedFacts`
   output): the dated transcript against the known statements and the prediction; returns the facts
   the transcript states that the known statements do not. The prediction is a guess, not memory: a
   fact it guessed right is still returned, as are the facts it missed or got wrong.
4. **Deterministic guard: in memory, not predicted.** A returned fact is dropped only when it is
   already in memory: one known statement covers its content words (at least
   `PREDICT_CALIBRATE_COVERED`, 0.8) and, for a fact that is not a `state` and has a date, that
   statement happened on an overlapping date (its `happened:` label, else the day of its
   `valid_from`). The same event on another date is a new occurrence and is stored, so count and
   list questions ("how many times did Melanie go camping?") keep every occurrence. A predicted line
   never suppresses storage: a correct guess about a fact memory does not hold is stored.
5. **Prompt inputs.** Every input (cue, known statements, prediction lines, transcript turns) has
   its whitespace collapsed to one line and the engine's markers escaped (`escape_markers`), so a
   stored newline cannot forge a prompt section such as "Session opening".
6. **Store** each new fact through the write door like a mined fact: role `assistant`, channel
   `calibration`, tags `surprise_fact`, `calibrated:<session>` and `kind:<kind>`. Parents = the
   session's turns (erasure cascades); trust capped at the least-trusted turn (E1).

Two calls per session. Taint repair clears this stage's marker only when the stage is enabled.

## Consequences

- An ablation switch, not a replacement for `mine_facts`; whether it helps LoCoMo is to be measured.
- Simplifications against Nemori: the prediction cue is the opening turn (Nemori uses a generated
  episode title), and knowledge selection is lexical, not embedding-based.
- The stored set is "new to memory", not "unpredicted": the prediction steers the calibrator but
  cannot drop a fact (amended 2026-10-06 after review: predicted-line coverage dropped correct
  guesses that memory never held, an undated word match dropped repeat events, and an untrusted
  record could pre-empt a true fact).
- Tests: `tests/unit/test_predict_calibrate.py` (fake LLM: known facts are not re-stored, new ones
  are even when predicted; a repeat event on a new date is stored, on the same date it is not; a
  low-trust record suppresses nothing; prompt inputs are single lines with markers escaped; once per
  session; erasure cascade; off by default; skipped without a role), plus prompt goldens for
  `predict_episode` and `calibrate`.
