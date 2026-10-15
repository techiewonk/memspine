# Dev-set gap reasoning, 2026-10-10

Scope: every question answered wrong by R0 (fixed reranker, grounded prompt) or R3 (+ hit markers, detail prompt,
duration resolver) on the two development conversations conv-26 and conv-30 (233 questions; runs `rs-r0-ref-i2`,
`rs-r3-all-i2`). 42 questions, each read with its gold evidence, stage ranks, context and judge verdict. Purpose: find
the causes and fix them before spending held-out runs (user request).

## R0's 35 wrong answers by cause

| Cause | n | Questions | Fix |
|---|---|---|---|
| Judge marks a correct exact date wrong against relative gold ("The Friday before 15 July 2023" vs "Friday, 2023-07-14") | 5 | 0-29, 0-41, 0-45, 0-79, (0-135 month framing) | **A2 deterministic date check** (`--judge-date-check`): credits 4 of them, no false positives on 10 full-run flips checked by hand |
| Gold label wrong or answer only in the photo | 5-6 | 0-5, 1-9, 1-57 (advice is Jon's line), 0-23 (title only in the photo), 1-43, (0-151) | errata file (+2 today: 1-57, 0-23); report with and without |
| Strong vector hit lost in RRF fusion (vector rank 7-23, absent from BM25, so its fused score loses to two-leg consensus) | 5 | 0-3 (vec 9), 0-19 (vec 7), 0-59 (vec 12), 0-66 (vec 23), 1-3 (vec 23) | **`read.rerank_balanced`** (existing): the reranker pool takes each leg's best hits in turn |
| Category-vs-instance list questions (up to 6 gold turns across sessions; question names a category) | 8 | 0-11, 0-15, 0-34, 0-38, 0-69, 0-70, 1-23, 1-44 | needs design work (B1: decomposition or mined list memory); not attempted today |
| Reader with evidence in context | 11 | 0-22 (inference: "would she have Dr. Seuss books"), 0-71 (reference "that book you recommended"), 0-151 ("we did it yesterday" refers to a photo), 1-20 / 1-48 (wrong line), 1-71 (state), 1-74 (misread), 0-35, 0-40 (count), 0-43 / 0-78 (detail) | **`grounded_v2` prompt**: references to earlier lines, captions as evidence, yes/no inference with general knowledge; keeps short answers |

Verified against the dataset: conv-30/1-20 gold (27 May 2023) is correct - session 12 is 27 May; the reader used another
line, so it is a distractor case, not a gold error.

## What R3 changed

Gains (0-29, 0-35, 0-41, 0-45, 0-43, 0-78, 1-71) came mostly from echoing "the Friday before X" phrasing that the judge
accepts - the date check gives R0 the same credit without a prompt change - plus more detail on two list questions.
Losses (0-36 wrong anchor date, 0-85 distractor, 0-94, 0-142, 1-31, 1-54, 1-75) came from longer answers and line
confusion. Net zero; `grounded_v2` keeps R0's short answers and adds only the three targeted rules.

## Offline rescoring with the date check (no new runs)

| Run | Before | After | Flipped |
|---|---|---|---|
| rs-r0-ref-i2 (dev, R0) | 85.0% | 86.7% | 4 |
| rs-r3-all-i2 (dev, R3) | 85.0% | 85.4% | 1 |
| qa-full-qs-eq06-fix (all 1,540) | 80.1% | 80.6% | 7 |
| qa-full-qs-eq06-roff-fx (baseline, 1,540) | 74.5% | 74.7% | 3 |

## Screen running

2 dev conversations, all with `--judge-date-check`: `rerank_balanced`; `grounded_v2`; both.
