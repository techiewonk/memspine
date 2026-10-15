# GTEMP: temporal regression of the GC graph base

Evidence: full LoCoMo, arm `scr-gc3-graph-jina` (run `v2-P2REF-full`, 79.35%) against the J reference
(`clean-jina-full`, 81.36%). Counts and question ids: `evals/analysis/gtemp_diagnosis.json`, made by
`evals/analysis/gtemp_diagnose.py` (reads local forensics, writes ids and counts only; the text-bearing
per-question dump goes to the gitignored `evals/runs/_analysis/`).

## What the forensics show

Same commit, so not code drift. Per category (P2REF vs clean, wins/losses): cat1 +23/-30, cat2 +8/-30,
cat4 +23/-26. The loss is spread, not one bug, but three mechanisms are visible:

1. **Derived records take the slots turns had.** In P2REF about half of the 10 reranked hits are derived
   (graph edge facts, entity summaries, session or episode text); the J reference has none. The turn
   windows then shrink: mean turns in the context 26 (P2REF) vs 51 (clean) on cat2, 43 vs 59 on cat1,
   33 vs 48 on cat4. The share is the same on broken and unbroken questions (about 5 of 10), so it costs
   a fixed probability per question rather than failing a few.
2. **Derived records carry the wrong date.** An edge fact without a stated date takes `valid_from` of its
   last parent turn, so it renders as `[session-close date] fact`. Of 8,735 derived context records,
   4,575 are undated facts, 1,616 dated facts, 1,473 entity summaries, 1,071 composite window texts. 138
   carry a run-date anchor (a relative phrase resolved against the build wall clock, year 2025/2026).
   The reader then answers with the session date or the run-date year instead of the event date.
3. **Reader noise.** The reader sees derived text in front of the evidence; on non-temporal questions it
   merges facts that disagree with the turns (lists).

## Causes of the broken questions (clean right, P2REF wrong)

Rule order: a = a gold turn present in the clean context is missing from P2REF; b = gold present and the
P2REF answer's date or year occurs only in derived text; c1 = gold present, at least 4 derived hits in the
top 10; c2 = gold present, other; c0 = gold absent in both.

| Category | a displaced | b derived date led | c1 derived heavy | c2 other | c0 gold absent both |
|---|---|---|---|---|---|
| cat1 (30) | 11 | 0 | 7 | 3 | 9 |
| cat2 (30) | 6 | 11 | 10 | 1 | 2 |
| cat4 (26) | 2 | 0 | 11 | 11 | 2 |

Derived-induced (a + b) is 11 of 30 on cat1 and 17 of 30 on cat2; c1 is plausibly the same displacement
seen at the reader. Lifting every derived hit to a source turn (`read.lift_before_rerank`) was not
chosen: a session-granularity fact lists the whole session as parents, so it would pick turns by rank,
not by match.

## Fix (all default off, projection only)

* `read.raw_turn_floor` (existing, W19): widen the search by the number of derived hits, so every turn
  hit the J reference has stays, with its window. Addresses mechanism 1.
* `read.derived_dates` (new): a derived record without a happened date is shown with the date of its
  best source turn (word overlap, later turn on a tie); the `ts_defaulted` tag is cleared on the copy so
  the dated render and the relative-date annotation use the said date. On a temporal-shaped question a
  derived record that still has no date is dropped. Wired in
  `compose/pipeline/windows.py:_assemble_core` before `_decorate`; code in
  `read/pipeline/derived.py:redate_derived`.
* `read.derived_cap` (new, N > 0): at most N derived records in a replay read, placed after the turns
  (persona and lead blocks stay in front). Wired at the end of `read/pipeline/router.py:_read_routed`;
  code in `derived.py:place_derived`. Tap events: `read.derived_dates`, `read.derived_cap`.

Graph legs keep finding turns; only the derived text shown is limited.

## Offline estimate (no re-pack: the windows need the run snapshot)

Restoring the clean context for the a and b questions, and answering at the clean rate given gold in
context (cat1 0.75, cat2 0.87, cat4 0.92), recovers about 8 of 30 on cat1, 15 of 30 on cat2 and 2 of 26
on cat4: roughly 25 of the 86 broken. c1 (7 + 10 + 11) is upside not counted. The questions the graph
base fixed (+23/+8/+23) are not simulated and may lose some. The gap to the J reference is 2.0 points,
so expect a recovery of about 1 to 1.5 points, the size of reader noise; hence a targeted slice, not a
full run.

## Screen

Arm `evals/arms/v2-GTEMP-graph-fixed.json` = `scr-gc3-graph-jina` + `read.raw_turn_floor` +
`read.derived_dates` + `read.derived_cap=3`. Queue row G1 (B11, GC, NONINF, set f, 150 min, ref
`v2-S02-i2`), slices `fail_temporal` and `fail_mix_120` (`SCREEN_OVERRIDES["G1"]`, map regenerated).
Pass: temporal targets recovered without losing controls. Not launched.
