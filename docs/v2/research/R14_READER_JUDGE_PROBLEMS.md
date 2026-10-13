# R14 DEEP-READ: reader failures, distractors, judge errors and gold defects in `clean-jina-full`

Package DEEP-READ, round R14, 2026-10-14. CPU only, no model call. Reference run `clean-jina-full` (1,540 LoCoMo cat 1-4 questions, 81.4% = 1,253 correct, Qwen3.5-9B reader and guarded judge, jina reranker, window 2/4). Every new item carries the tag `R14`; items that confirm or sharpen an existing one say `R14-confirmed`. Machine-readable twin: `r14_reader_judge.json` (21 items with ids, rules, solutions, tests). Per-question features (ids, labels, numbers, no text): `evals/analysis/r14_reader_judge_features.csv`. Code: `evals/analysis/r14_reader_judge.py` (features), `evals/analysis/r14_build_report.py` (tables and items).

Licence: this file quotes no LoCoMo text. Examples use invented names (Ilse, Bram, Noor, Tomasz). Question ids are `conv-NN/Q-I`.

## 0. Headlines

1. **The funnel.** 287 wrong. 169 of them (59%) have every evidence turn in the reader's context. Of those 169, the existing labels put 140 at stage d (reader), 23 at f (gold error) and 6 at e (judge). The other 118 wrong rows lack gold in context (retrieval, covered by earlier packages). Accuracy given all gold in context is 87.2% (1,323 questions); without it 45.6%.
2. **Judge errors are in both directions and do not cancel by category.** All 287 WRONG verdicts were read by hand: **10 clear, 4 likely, 9 borderline false negatives** (central about 17). For CORRECT verdicts, 201 rule-flagged rows plus 70 random unflagged rows were read: **17 clear, 12 likely, 5 borderline false positives** (central about 30 after adding 3 for the unflagged tail). Net effect: **the 81.4% headline is about 0.9 points high (central 80.5%, range 79.1 to 81.7)**. By category the error is concentrated in multi-hop: **cat1 66.7% becomes about 61.1%** under strict all-items scoring, cat4 rises 0.7, cat3 0.8, cat2 -0.3. Excluding the 26 errata rows (gold_error and needs_image) the raw figure is 82.6%.
3. **The date guard closed the biggest old judge hole.** 89 questions (5.8%) are credited by the deterministic date check with no LLM call; none looked wrong on reading. Weekday-before date-form false negatives were 7 of 24 in the forensic run; none of the 23 hand false negatives is of that form now. What is left is list/containment/annotation/geography.
4. **The judge is not noisy, it is systematically lenient on lists.** Identical normalised answers got different verdicts in 4 of 1,696 groups across six full runs (0.24%). Re-running will not average the errors away.
5. **Position effects: none.** Single-evidence questions with the gold in context: early third 89.7%, late third 90.2% (p=0.90); logistic z for gold position -0.4. The context is chronological in 1,540 of 1,540 cases. **Context length: weak negative** (z -1.3 to -2.2 per sd). **Multi-session evidence: strong** (z -4.4): 89.9% for one session, 72.9% for two, 62.5% for three.
6. **Distractor use is the largest single reader mechanism:** 59 wrong in-context answers draw their new content from a non-gold line (heuristic; hand check 13 of 18 true, so about 43). In 19 of 59 the distractor is a window neighbour of the gold; in 32 it is itself a retrieval hit in the final top 10; in 11 it outranks the gold.
7. **New stratum: sibling evidence** (3 of 18 hand-checked distractor rows): the answer is true text from another turn and the gold names only one. A reader error and a data limit at once.
8. **Gold errors: the errata is close to complete on the wrong side.** Reading all 287 wrong rows found only 3 new certain defects and 5 unverified speaker-attribution candidates (previous estimate: 16-24 unread). The bigger distortion is on the credited side: **39 questions name one speaker while every evidence turn is spoken by the other; 23 of the 24 credited ones are not in the errata** and depend on ignoring the named speaker.

## 1. Method and rules (every count is labelled)

- **[measured]** computed by script from `results.jsonl`, `forensics.jsonl` (context turn ids and order), `locomo10.json` (evidence ids, speakers, captions), `locomo_problem_labels_clean_jina_full.txt` and `locomo_errata.json`. **[heuristic]** a deterministic rule, stated here. **[hand]** one annotator (this pass) reading question, gold and answer. Hand tiers: *clear* (any grader agrees), *likely* (most graders), *borderline* (coin flip).
- `all_gold_in_ctx`: every evidence id that exists in the conversation (3 rows have a malformed id, 5 have none) is among the context turn ids. Stage d (reader) = wrong with the label prefix `d:`; this equals "wrong and all gold in ctx" minus the 23 f and 6 e rows.
- **Answer source** (heuristic, wrong or right): content stems of the answer (stopwords, digits dropped, light suffix stripping) that are not in the question and not in the gold answer are "novel". Source = `refusal` (regex of `memspine_evals.refusal.is_refusal`); `no_novel` (none); `distractor` (at least 2 and at least 40% of novel stems occur only in non-gold context lines, and more than the novel stems found in gold turns); `ungrounded` (at least 50% of novel stems occur in no context line); `from_gold_turn` (at least 50% occur in a gold turn); else `mixed`.
- **Judge false-negative rules** (flag WRONG rows): single-day date equivalence (gold parsed to a day or "weekday before D"; first/any answer date equal), range membership (week/weekend/month/year gold), normalised gold containment (answer at most 45 words, no negation or refusal), gold stem recall at least 0.8, answer stems a subset of gold stems, `check_conventions` v1/v2, number-in-answer. **False-positive rules** (flag CORRECT rows): `list_item_missing` (gold has 2 or more items; fewer than all have 60% of their stems in the answer), `list_half_or_less`, `low_gold_recall` (below 0.34 with at least 2 gold stems), `hedged_or`, `long_answer` (60 words), month/year/date mismatch, number mismatch or "among many", polarity mismatch, refusal credited.
- **Estimation of judge error**: weights clear 1.0 / likely 0.75 (FN) or 0.7 (FP) / borderline 0.4 (FN) or 0.3 (FP); +3 FP for the unflagged tail (0 of 70 sampled; the rule-of-three 95% bound is 4.3%, up to about 40 unseen cannot be excluded). Range: FP 17-45, FN 10-23.
- **Regression**: logistic regression of `correct` on standardised covariates among all-gold-in-context rows, bootstrap standard errors (200 resamples).
- Single annotator. The hand sets are a judgement and should be adjudicated once more before a number is published.

## 2. (a) Reader failures: the 169 wrong rows with all gold in context

| class (label) | n | rows | what the reader does | existing handle |
|---|---:|---|---|---|
| wrong_detail | 46 | d | topical answer, wrong or generic detail (median gold-token recall 0.0; 28 are distractor-sourced) | C2, grounded_det, S19/S52-54 |
| inference_open_domain | 26 | d (all cat3) | refuses or answers the opposite on hypothetical, "likely", world-knowledge questions | C3, C4, deep_v2 (S55) |
| list_incomplete | 16 | d | one or two of several items | list_mode, contract, S06/S11 |
| refusal_premise_denial | 14 | d | "not mentioned" or denies the premise although a line supports it | I1 retry, I61 premise |
| date_duration_arithmetic | 10 | d | subtraction by the reader | duration_solve (no screen) |
| count_wrong | 9 | d | "at least N", one event counted twice | count_verify (single rejected) |
| date_wrong_event | 8 | d | right operation on the wrong event line | date_repair, I79 |
| image_caption_misuse | 6 | d | caption of another turn, or photo-only fact | C7, SOL-ASSET |
| distractor_line, speaker_confusion, date_offset_small | 3 + 1 + 1 | d | | |
| gold_error (f) | 23 | f | reader right, gold wrong | errata |
| judge (e) | 6 | e | reader right, judge wrong | conventions |

Generic patterns across those classes (counts [measured] unless stated):

| pattern | evidence | status |
|---|---|---|
| Over-refusal and premise denial | 25 refusal-like answers, 0 credited, 11 with all gold in context; cat3 accuracy 42.7% (46.9% with gold in ctx); the retry fired on 25 rows and rescued 0 | **R14-READ-1** (H) |
| Wrong detail from the right region | 46 rows | **R14-READ-2** (H) |
| Incomplete list / counting | 25 rows; 47 of the 169 have a list-type gold; how_many: 26 in-ctx at 65.4% | **R14-READ-3** (H) |
| Date arithmetic and relative-date resolution | 19 rows; how_long 34 in-ctx at 58.8% (15 wrong); gold "number" 50% (n=36), relative-phrase gold 50% (n=40), absolute-day gold 89% (n=82) | **R14-READ-4** (H) |
| Image-caption misuse | 6 rows; caption-only gold 67.9% (n=28) vs 87.6% | **R14-READ-5** (L) |
| Answer about the wrong speaker | 1 reader row; gold-side effect in R14-GOLD-2 | **R14-READ-6** (L) |
| Ungrounded answers | 37 wrong of 225 (16%) vs 10 of 447 for answers with no novel content (2%) | **R14-READ-7** (M) |
| Verbosity vs a short gold | 49 of 169 wrong have an answer more than 8x the gold length vs 124 of 1,154 correct (29% vs 11%); accuracy 57.6% above 16x | **R14-READ-8** (M) |
| Answering from a distractor turn | section 3 | **R14-DIST-1** (H) |
| Gold early vs late (lost in the middle) | null | **R14-POS-1** (L) |
| Context length | weak negative | **R14-LEN-1** (M) |
| Combining two or more turns | strong | **R14-MH-1** (M) |

### 2.1 Structural effects (all rows with every gold turn in context)

| factor | result | model |
|---|---|---|
| gold position in the (chronological) context, single-evidence | quintiles 90.3 / 90.4 / 91.5 / 90.9 / 89.9% (n 168-208 each) | z = -0.4 with category, context size, gold rank controlled |
| number of context turns | 41-50: 87.6%; 51-60: 87.7%; above 60: 83.6% (n=269); at most 40: 91.4% | z -1.3 (turns), -2.2 (tokens, single-evidence) |
| best gold rank in the final candidate list | rank 3 or better 90.8% (n=1,101) vs worse 74.8% (n=107), gold errors and judge rows dropped | z -2.6 to -3.5 |
| evidence turns needed | 1: 90.0% (n=1,076); 2: 78.5% (177); 3: 68.2% (44); 4: 64.7% (17) | |
| evidence sessions needed | 1: 89.9% (n=1,146); 2: 72.9% (144); 3: 62.5% (24); 4 or more: 3 of 9 | z = -4.4 |
| answer / gold word ratio | up to 8x: 87-92%; 8-16x: 78.9% (114); above 16x: 57.6% (59) | difficulty correlate |
| question shape | how_long 58.8%, how_many 65.4%, yes/no 76.0%, list 91.7%, when 91.8%, why 95.4% | |

Reading the table: ordering and position are not the lever at 2k tokens (R2-4 also rejected hits-first, 86.7% vs 88.4%). The rank association is real but, given the rejected presentation change, it points at retrieval rank (reranker v2, pool protection) rather than composition. Cross-session joins are the clearest reader-side structural weakness.

## 3. (b) Distractors

Heuristic hits: **59 wrong in-ctx rows are `distractor` sourced** (207 rows of that source in all, 28.5% wrong, against 2.2% for `no_novel`, 9.5% for `from_gold_turn`, 16.4% for `ungrounded`). Where the distractor line sits (largest-overlap non-gold line per row): same session as a gold turn 24, within 4 turns of a gold turn (a window neighbour) 19, in the final top 10 (a retrieval hit) 32, outranks the best gold 11, never seen in the final list (window filler) 22.

Hand check of 18 random flagged rows: 13 true (72%): the answer restates a similar remark of another turn or another event (e.g. Ilse's sushi photo versus Ilse's salad photo; the reader returns the first). 5 are other things: a count, a premise error, two refusals, a vague detail from the right turn. Estimated true distractor use: about 43 (Wilson interval 29-52). Three of the 13 are **sibling evidence** (R14-DIST-2): the answer is true text from a second turn that also answers the question, and the gold names only the first.

Solutions already in the tree: hits-first and `hit_blocks` (rejected, R2-4), `*` markers (neutral), `--verify-answer` and `slot_verify` (unscreened), SOL-GAP-DISTRACTOR (not built). New: provenance binding (the answer cites a turn id; code checks speaker, date and head noun phrase), a per-shape window (19 of 59 are window neighbours; the 2/4 window produced 49 of the 154 gains in the forensic run, so it must be per shape), accept-set errata for sibling evidence.

## 4. (c) Judge errors in both directions

### 4.1 False negatives (WRONG, answer states the gold fact): 23 hand rows

clear 10: `conv-26/0-70`, `conv-26/0-135`, `conv-42/3-183`, `conv-43/4-30`, `conv-43/4-174`, `conv-48/7-101`, `conv-48/7-145`, `conv-50/9-60`, `conv-50/9-64`, `conv-50/9-127`. Likely 4: `conv-42/3-173`, `conv-47/6-26`, `conv-47/6-30`, `conv-47/6-35`. Borderline 9: `conv-30/1-17`, `conv-42/3-134`, `conv-42/3-148`, `conv-48/7-8`, `conv-49/8-154`, `conv-49/8-18`, `conv-49/8-37`, `conv-49/8-54`, `conv-50/9-1`.

Mechanisms: the hedged longer list that ends in a "therefore" list holding every gold item (4-30); superset lists (7-145, 9-64, 0-70); a year written as "N years ago [= about YYYY]" in the context and as the year in the answer (7-101); a spelling copied from the transcript (3-183); the gold fact with a different but correct date in a question whose month premise is off (0-135); the gold fact with extra date detail (4-174); city or county given for a country or state gold (6-26, 6-30, 6-35); paraphrase (3-173). Deterministic rules (date, containment, recall, conventions v1/v2, numeral) flag 9 wrong rows and 6 of the 14 clear or likely ones: **recall 43%**. The 7 stage-e labels already in the data are all in the hand set except one (4-55, reclassified as a gold ambiguity).

Central FN estimate 16.6 (range 10-23): 1.1 points (0.6-1.5).

### 4.2 False positives (CORRECT, answer misses a required item): 34 hand rows

clear 17: `conv-26/0-23`, `0-60`, `0-78`; `conv-41/2-6`, `2-30`, `2-32`, `2-36`; `conv-42/3-46`, `3-59`, `3-63`, `3-75`; `conv-43/4-52`, `4-65`; `conv-47/6-40`; `conv-49/8-7`, `8-50`; `conv-50/9-30`. Likely 12 and borderline 5 are in the CSV (`hand_fp`). Pattern: a gold of 2 or more items (or a named title) answered with one item or a generic description; 25 of the 29 clear/likely are cat1. The calibration set labels "CORRECT with extra items" but has no "CORRECT with missing items" rule, so the judge's habit of crediting the first correct item is uncalibrated. The 70 random unflagged corrects held no FP.

The rules: `list_item_missing` flagged 136 rows, 27 clear/likely (20% precision); other flags (hedged_or 31, long_answer 26, month/year/date mismatch 10) found 0-1 each. Recall of the FP rules on the hand FPs: 29 of 29 by construction (the hand set was read from the flags), real recall unknown because only 70 unflagged rows were sampled.

Central FP estimate about 30 (range 17-45): 1.9 points (1.1-2.9). This is relative to the strict all-items convention; LoCoMo practice often accepts partial lists, so report both.

### 4.3 Net effect on the headline

| | raw | judge-adjusted (central) | range |
|---|---:|---:|---|
| all 1,540 | 81.4% | 80.5% | 79.1-81.7 |
| cat1 multi-hop (282) | 66.7% | 61.1% | |
| cat2 temporal (321) | 83.2% | 82.9% | |
| cat3 open-domain (96) | 42.7% | 43.5% | |
| cat4 single-hop (841) | 90.0% | 90.7% | |
| errata-excluded (1,514) | 82.6% | about 81.7% | 80.2-83.0 (derived) |

The shift is inside the cluster-bootstrap CI (+-2.2) but it is concentrated in the category where most claimed gains live. Per-category deltas under about 5 points on cat1 should not be interpreted without the strict column.

### 4.4 Judge repeat noise and the date guard

- 1,696 groups of identical normalised answers judged by the LLM across clean-jina-full, full-persp-loc, r7-multprot/post/protect-full and qa-full-qs-eq06-fix: 4 groups (0.24%) with both verdicts. The errors are systematic.
- 89 deterministic date-guard credits (all CORRECT, no LLM call): read in full, no suspect found. The guard credits any answer whose first date equals the gold day, so an answer with the right date for the wrong event would pass; none seen, risk is not zero.

## 5. Gold and dataset defects

- The errata has 52 entries (23 gold_error, 20 evidence_label_error, 4 needs_image, 1 premise_error, 1 bad_evidence_id, 3 cat-5 rows not in this run). Excluding gold_error + needs_image: 26 rows, 3 credited, **82.6%**.
- New from reading all 287 wrong rows: `conv-43/4-55` (gold month not entailed by its evidence turn), `conv-26/0-135` (premise month differs; the reader's answer is right, counted as an FN above), `conv-49/8-25` (gold needs a for-N-years computation the annotator leaves unresolved). Five speaker-attribution candidates not in the errata (heuristic, unverified): `conv-42/3-54`, `conv-48/7-116`, `7-155`, `7-179`, `conv-50/9-147`.
- **Speaker-swapped questions** (R14-GOLD-2): 39 rows where the question names exactly one speaker and every evidence turn is spoken by the other: 24 credited (23 not in the errata), 15 wrong (10 in the errata). A perspective feature that enforces attribution loses the credited rows. Report them as their own stratum.

## 6. Items (R14), with existing handles

`H/M/L` = priority. Full records (rules, question ids, solutions with sources, cost, risk, tests) in `r14_reader_judge.json`. Sources named from memory and not re-fetched this round.

| id | layer | problem | count | existing MemSpine handle | best next step | pri |
|---|---|---|---:|---|---|---|
| R14-READ-1 | reader | over-refusal / premise denial vs committed inference | 40 | C3, C4, I1, `query_contract`+`contract_rules` (S06 +1/-0, S11 0), `deep_v2` (S55), `grounded_generic_infer` | typed commit route gated by contract, guard cat5 | H |
| R14-READ-2 | reader | wrong detail from the right region | 46 | C2, grounded_det, SOL-BUNDLES (S19, S52-54), `slot_verify` | quote-then-answer on "what kind/which" shapes | H |
| R14-READ-3 | reader | incomplete lists, wrong counts | 25 | list_mode, `count_verify` (single rejected +1/-16), `slot_verify`, `obligations` (S56), S06/S11 | screen `count_verify two_call` and `evidence_table` | H |
| R14-READ-4 | reader | date/duration arithmetic, wrong event | 19 | `duration_solve`, `date_repair`, milestones, I79, S62 | first screen of `duration_solve`+milestones | H |
| R14-READ-5 | reader | image-caption misuse | 6 | C7, SOL-ASSET | caption as own line with turn metadata | L |
| R14-READ-6 | reader | wrong speaker | 1 | SOL-PERSPECTIVE, I59 owner_check | only with the GOLD-2 stratum | L |
| R14-READ-7 | reader | ungrounded answers | 37 | `slot_verify` diagnosed, `defect_diagnosis` | screen diagnosed mode; measure false defects on 188 correct ungrounded | M |
| R14-READ-8 | reader | verbosity vs short gold | 49 | contract output templates, strict slot_verify | contract-fixed shapes | M |
| R14-DIST-1 | reader | answering from a distractor turn | 59 | R2-4 (rejected), `--verify-answer`, SOL-GAP-DISTRACTOR | provenance binding + per-shape window | H |
| R14-DIST-2 | data | sibling evidence: answer true, gold names another turn | 3 (est. 7-10) | none | accept-set errata tag | M |
| R14-POS-1 | compose | lost-in-the-middle | 0 | `present_order` (rejected) | re-measure at 8k+ tokens only | L |
| R14-POS-2 | retrieve | gold rank above 3 | 29 | reranker v2 (S16), pool protect, family fusion | track accuracy by rank bucket in every screen | M |
| R14-LEN-1 | compose | context length | 0 | replay window, exact_budget (S52/S53) | 2x2 per-shape window | M |
| R14-MH-1 | reader | combining two or more turns | 52+ | obligations join (S56), agentic, deep_v2 (S55), evidence table | decomposition arm on cat1 | M |
| R14-JUDGE-1 | judge | false negatives after the date guard | 23 | conventions v1/v2 (flag recall 43%), second_view | conventions v3 + second judge on disagreement | H |
| R14-JUDGE-2 | judge | false positives, missing list items | 34 | `list_recall` column (A3) | strict item-recall column; extend calibration | H |
| R14-JUDGE-3 | judge | headline error | n/a | A1, A5, A10 | triple reporting with CIs | H |
| R14-JUDGE-4 | judge | repeat noise | 4 groups | A16 | none (confirmed) | L |
| R14-JUDGE-5 | judge | unparsed relative golds | 20 | date guard | relative_year convention | L |
| R14-GOLD-1 | data | gold/question defects not in errata | 8 | locomo_errata.json | verify the 5 candidates | L |
| R14-GOLD-2 | data | speaker-swapped questions | 39 | SOL-PERSPECTIVE | separate stratum | M |

R14-confirmed against the existing lists: A2 (the date part is fixed, 7 of 24 FNs gone), A3 (FP list problem measured again: 34 hand rows, 29 in multi-hop), A5 (errata near-complete on the wrong side), C1 (distractors still the biggest mechanism; window neighbours 19 of 59), C3/C4 (cat3 reader rows all inference, 26), C5 (how_long 58.8%), C6 (lists, 47 of 169), C10 (retry rescues 0 of 25 here), I58/I77 (conventions reach 6 of 14), R2-4 (position null here).

## 7. What this means for the next screens

1. Fix the **measurement** first: add the strict item-recall column and the 57-row calibration extension (23 FN + 34 FP ids in the CSV). Without it a multi-hop delta below 5 points is uninterpretable.
2. Screen the unscreened built handles in this order of expected size: `duration_solve`+milestones (R14-READ-4), `count_verify two_call` + `evidence_table` (R14-READ-3), `slot_verify diagnosed` (R14-READ-7), `deep_v2`/`obligations` (S55/S56, R14-READ-1 and MH-1).
3. Treat position and answer length as non-levers; treat gold rank, context size and cross-session joins as the structural levers.
4. Do not enforce speaker attribution without the speaker-swap stratum.

## 8. Caveats

- One annotator; 193 flagged plus 70 random unflagged CORRECT rows were read, not all 1,253. The FP tail among 933 unflagged rows is bounded by the sample only (0 of 70, 95% upper bound 4.3%).
- The `distractor` rule has 72% precision on an 18-row check; the interval is wide.
- Regression coefficients are descriptive (observational, single run); gold rank and difficulty are correlated.
- Citations (Liu et al. 2023, Shi et al. 2023, Yoran et al. 2023, Dhuliawala et al. 2023, Gao et al. 2022, Press et al. 2022, Zhou et al. 2022, Wang et al. 2022, Xu et al. 2023, Yu et al. 2023, Zheng et al. 2023, Verga et al. 2024, Fatemi et al. 2024, Levy et al. 2024, Peysakhovich and Lerer 2023) are given from memory and were not re-fetched; check before they enter a paper.
