# G24: pre-registration of the GLiNER2 read-planner tuning

Task #89 (follow-up to the real-model check of 2026-10-06, task #42). Status: **registered
2026-10-06, before any model was run on the set.** This file and the frozen set
`G24_gliner2_planner_set.json` are committed before `gliner2_planner_eval.py run` is executed
for the first time. Results go in `G24_gliner2_planner_results.md`; this file is not edited
after that, except to append a dated amendment section.

## 1. Why

With `read.planner: decision` and `decision.provider: gliner2`, `Engine._plan_read_mode` asks
GLiNER2 to choose between the three `_READ_MODES` options. On 30 LoCoMo questions it chose
`retrieve` every time (0/10 count/list questions went to `compose`) with median confidence 1.0,
so `read.planner_min_confidence` cannot filter it. This experiment asks whether a different
classification setup makes the planner agree with the read modes the engine's own rules and
the `plan` prompt intend, measured offline, and ships a setup only if it beats the current one
on data it was not tuned on.

## 2. The set (frozen)

- Source: LoCoMo-10 `locomo10.json` (sha256 recorded in the fixture), all 1,986 questions,
  de-duplicated by lower-cased text.
- **Labels are derived deterministically** by `gliner2_planner_eval.label`, in this order:
  1. `compose` if `query_shape.is_count(q)`, or `is_aggregation(q)` and not `is_temporal(q)`
     (counts, lists and sets; "how many months passed" is a duration, not a count);
  2. `replay` if `is_ordering(q)` (H16: evidence shown in time order), or the replay cue fires:
     *why / reason / motivate / inspire / feel / react / say / tell / mention*. The cue is a
     transcription of the `plan` prompt's replay intent ("why something happened, how someone
     felt, what was said around an event") and of the engine's replay option ("exact wording
     or what was said around an event"); it is not an engine rule;
  3. `retrieve` otherwise (one specific fact, including plain date questions).
  The rule that fired is stored per item (`rule`).
- Sample: 34 compose, 33 replay, 33 retrieve (n = 100), drawn with `random.Random(20261006)`
  per mode; within each mode, alternate items go to `tune` and `heldout` (51 / 49).
- **No relabelling.** The labels are a proxy (LoCoMo has no routing gold). Questions the rules
  label badly (e.g. "What martial arts has John done?" is a set but labelled `retrieve`) stay
  as they are; disagreements of that kind are reported, not fixed.

## 3. Setups and procedure

- Baselines: `current` (the engine's options and the provider's call, unchanged),
  `rules (no planner)` (`read(mode="auto")` without a planner: compose on `is_aggregation`,
  else replay) and `constant retrieve`.
- Tuning is free on the `tune` half only: label names and descriptions, the task name and
  prompt, hand-written few-shot examples (never LoCoMo questions), multi- vs single-label,
  shape hints appended to the question, and the **hybrid** (`query_shape` rules first, GLiNER2
  only for questions the rules leave open). Models: `fastino/gliner2-base-v1`; `-large-v1`
  only if disk allows.
- Selection: the setup with the best `tune` accuracy (ties: macro recall, then the earlier
  entry). The selected setup, `current` and the best non-hybrid setup are then run once on
  `heldout`.

## 4. Metric and decision rule

- Primary: accuracy on `heldout` against the frozen labels. Secondary: per-mode recall,
  macro recall, confusion matrix, median confidence.
- **Ship** the selected setup as the `decision.provider: gliner2` planner default only if its
  `heldout` accuracy is strictly higher than `current`'s. Otherwise record the negative
  result and leave the engine unchanged. `read.planner` stays `rules` by default either way.

## 5. Known limits, stated in advance

- The hybrid's rules overlap the labelling rules (compose and `is_ordering` replay come from
  the same predicates), so its agreement on those items is partly by construction. Its
  informative part is the remainder (replay-cue and retrieve items), which the results report
  separately.
- n = 49 held out; one run; one machine. A difference of a few questions is noise.
