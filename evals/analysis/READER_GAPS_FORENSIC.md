# LoCoMo reader gaps, forensic pass on the fixed run (qa-full-qs-eq06-fix)

Follow-up to `READER_GAPS.md`. Scope: every `read_fail` question (all gold evidence turns in the reader's context, verdict WRONG) of the fixed run, the baseline-to-fixed transitions, the retry, the judge, and gold errors. Analysis only: no engine edit, no model call, no eval run.

Tags used throughout: **[M]** = measured from the run files or by a script over them; **[H]** = hand classification by one annotator (me), read from `per_question.jsonl` (`question`, `gold_answer`, `answer`, `context_text`, `gold_turns`) and, for the retry, `results.jsonl` `meta.first_answer`/`meta.retry_answer`; **[E]** = estimate (judgement, not measured). Single annotator; borderline calls are flagged.

Runs (all `evals/runs/<run>--memspine/report/per_question.jsonl`, 1,540 questions, Qwen3.5-9B Q4_K_M reader and judge):

| run | what it is | acc [M] | read_fail | refusal-like answers [M] | mean context |
|---|---|---|---|---|---|
| `qa-full-qs-eq06-roff-fx` (baseline) | `default` prompt, rubric judge, window 2/2, no retry | 74.5% (1,147) | 180 | 236 | 1,571 tok, 39.3 lines |
| `qa-full-qs-eq06-fix` (fixed) | `grounded` prompt, refusal retry, guarded judge, window 2/4, anchored dates, date-mention leg | 80.1% (1,234) | 148 | 30 | 2,071 tok, 52.9 lines |
| `qa-full-qs-eq06-rq4b4-fx` | baseline reader/judge, Qwen3-Reranker-4B | 70.8% (1,090) | 206 | 298 | 806 tok, 19.7 lines |

Refusal-like = `refusal.is_refusal` (the retry's own regex). The third run is only used in section 8; its reader prompt hash is the baseline's (`83fc53c6...`), so it has none of the fixes except the engine side.

Per category, baseline to fixed [M]: single-hop 87.3 to 90.8, multi-hop 56.0 to 62.8, temporal 70.7 to 76.6, open-domain 29.2 to 49.0.

## 0. Headlines

1. **Of the 148 read_fail rows, only about 100 are real reader failures** [H]. 21 are gold or question errors, 3 more cannot be answered from text (answer only in the photo), and 24 are judge false negatives (19 clear, 5 borderline). So 48 of 148 (32%) need no reader fix. Same shape as the earlier analysis (55 of 182 = 30%).
2. **The fixes recovered 84 of the 180 baseline read_fail** [M] and the prompt change removed refusals almost entirely (236 to 30 refusal-like answers; refusal-like answers the judge credited 25 to 1). But **49 questions that were correct in the baseline are now read_fail** [M], another 18 became partial or retrieval_miss. Net gain +87 questions = 154 gained, 67 lost.
3. **The remaining read_fail has moved from "refuses" to "answers something else".** Only 13 of the 148 final answers are refusal-like [M]. The big classes now are: judge false negatives 24, open-domain inference 23, gold errors 21, wrong-line distractor 18, wrong or vague detail 16, duration arithmetic 13, list/count 10, caption ignored 8.
4. **Answers got much shorter** (mean 32.4 to 18.1 words, median 20 to 14) [M] while the context grew 32% (1,571 to 2,071 tokens) [M]. 14 of the 49 regressions are list, detail or caption losses (LIST 5, DET 6, IMG 3) [H]; DIST adds 9 more. This is the cost side of the grounded prompt's "short, direct answer" plus the larger window.
5. **The guarded judge still fails on LoCoMo's relative-date gold.** Of 20 fixed-run answers that state exactly the date the gold phrase resolves to ("The Friday before 23 January 2022" = 2022-01-21), the judge accepted 13 and rejected 7 [M, script]. And it still over-credits partial lists: roughly 45-55 multi-hop "correct" answers omit at least one gold list item [E]. The two errors are of the same size and cancel in the headline but not per category (section 4).
6. **Retry**: fired on 107 questions (6.9%), 30 retries returned a refusal again and were discarded, 77 were accepted; 31 of the 107 end correct, 27 of them were wrong in the baseline [M]. Open-domain fires on 44% of its questions.

## 1. Taxonomy of the 148 fixed-run read_fail (all read by hand) [H]

Primary class per question. Classes of `READER_GAPS.md` kept; new class marked NEW. Last column = how many of the class's questions were already read_fail in the baseline (persisting) versus newly broken [M].

| code | class | n | % of 148 | cat1 multi | cat2 temp | cat3 open | cat4 single | persisting / regressed from correct |
|---|---|---|---|---|---|---|---|---|
| JFN | Judge false negative (answer states the gold fact) | 24 (19 clear, 5 borderline) | 16.2 | 4 | 9 | 1 | 10 | 9 / 15 |
| INF | Open-domain inference or world-knowledge miss | 23 | 15.5 | 1 | 1 | 20 | 1 | 21 / 2 |
| GOLD | Gold or question error | 21 (2 borderline) | 14.2 | 1 | 9 | 0 | 11 | 17 / 4 |
| DIST | Wrong line or event picked (distractor) | 18 | 12.2 | 2 | 9 | 0 | 7 | 9 / 9 |
| DET | Right line, wrong or vague detail | 16 | 10.8 | 0 | 0 | 0 | 16 | 10 / 6 |
| DARITH | Elapsed-time, duration or two-event arithmetic | 13 | 8.8 | 0 | 12 | 0 | 1 | 9 / 4 |
| LIST | List or count incomplete or over-inclusive | 10 | 6.8 | 10 | 0 | 0 | 0 | 5 / 5 |
| IMG | Answer is in an image caption that is in context, reader ignores or mixes captions | 8 | 5.4 | 0 | 0 | 0 | 8 | 5 / 3 |
| IMGX (NEW) | Answer only visible in the photo, not in any stored text | 3 | 2.0 | 1 | 0 | 0 | 2 | 2 / 1 |
| REF | Refusal or premise-denial despite evidence | 4 | 2.7 | 1 | 3 | 0 | 0 | 4 / 0 |
| HOP | Two lines must be joined, link not made | 4 | 2.7 | 3 | 1 | 0 | 0 | 4 / 0 |
| DANC | Implicit or unresolved date anchoring | 3 | 2.0 | 1 | 2 | 0 | 0 | 3 / 0 |
| CONF | Conflicting memories merged | 1 | 0.7 | 1 | 0 | 0 | 0 | 1 / 0 |
| | **Total** | **148** | 100 | 25 | 46 | 21 | 56 | 99 / 49 |

(The persisting column sums to 99, not 96: 3 fixed read_fail rows were `partial` or `retrieval_miss` in the baseline.) Compared with the earlier 182: REF 22 to 4, INF 35 to 23, DANC 7 to 3, DIST 11 to 18, DET 11 to 16, LIST 7 to 10, IMG 12 to 8+3, JFN 29 to 24. Refusal is no longer a class that matters; distractors, detail loss and list loss grew because the reader now commits to an answer.

Rank distribution of the gold line [M]: for the 130 rows with a ranked gold turn, the best gold turn is within the final top 5 in 116 (89%) and top 10 in all 130 (`gold_turns[].ranks.final`). The evidence is almost always a top hit and the reader still misses or mixes it; this is what makes "mark which lines were hits" a candidate lever (section 7).

### 1.1 Worked examples (question id, gold, final answer)

**JFN (24; 19 clear, 5 borderline).** Clear:
- `conv-42/3-7` gold "The Friday before 23 January, 2022"; answer "last Friday, which corresponds to January 21, 2022" (the line is annotated `[= the Friday before 2022-01-23 (Fri 2022-01-21)]`) judged WRONG.
- `conv-50/9-25` gold "on the Friday before 22 August, 2023"; answer "Friday, 2023-08-18" WRONG (baseline answer "Fri 2023-08-18" CORRECT, 1.0).
- `conv-42/3-64`, `conv-26/0-41`, `conv-42/3-48`, `conv-50/9-17`, `conv-48/7-37`: same pattern, each answer is the exact resolved date. `7-37`'s answer text is identical to the baseline's, which was CORRECT.
- `conv-42/3-183` and `3-95` gold "Xenoblade Chronicles", answer "Xeonoblade Chronicles" (the transcript's own typo, `D27:23`).
- `conv-50/9-127` gold "Shinjuku"; answer "Shibuya Crossing and Shinjuku" (superset; the line says "excited to explore Shinjuku").
- `conv-47/6-59` gold "On the night of October 30 to 31, 2022"; answer "Last night (Sunday, 2022-10-30)" WRONG.
- `conv-48/7-81` gold "in October 2023"; answer "They have not yet agreed on a specific date; Jolene proposed planning for next month (October 2023)" WRONG (hedge plus gold).
- `conv-50/9-60`, `conv-49/8-154`, `conv-41/2-142`, `conv-26/0-135`, `conv-42/3-167`, `conv-26/0-70`, `conv-50/9-64` (answer contains the gold fact plus extra items, extra detail, a hedge or a paraphrase).
Borderline (5): `conv-26/0-2` (counseling vs "psychology, counseling certification"), `conv-42/3-148` ("fun and rewarding" vs "happy to share"), `conv-43/4-113` ("Minnesota Wolves" vs "The Wolves"), `conv-48/7-101` ("2022" vs "a year ago"), `conv-50/9-1` (mansion and luxury sports car vs "mansion in Japan, Ferrari 488 GTB").

**INF (23): 12 world-knowledge lookups, 11 hypothetical or "likely".**
- World: `conv-42/3-73` Fort Wayne to Indiana ("Not mentioned", retry fired and did not help; the question also says 2021 where the line is 2022); `conv-47/6-16` UNO and `6-17` Mafia (the reader quotes the description and says the name is not given); `conv-48/7-75` Exploding Kittens; `conv-49/8-57` Lake Tahoe, answer "Nevada" (gold California); `conv-43/4-70` Star Wars filming locations in Ireland; `conv-47/6-35` answer "Nuuk" where gold is "Greenland" (the baseline's longer answer said both and was credited).
- Hypothetical: `conv-26/0-22` Dr. Seuss books ("No, it is not mentioned ... she lists classics"); `conv-48/7-70` "is the friend no longer alive" gold "likely yes", answer "No" (the baseline answered Yes and was credited, so this is a regression caused by the committed-answer behaviour); `conv-50/9-13` Hollywood Bowl; `conv-41/2-17` degree; `conv-42/3-12` asthma (no line states it).

**GOLD (21)**: listed with turn evidence in section 5.

**DIST (18).**
- `conv-30/1-14` "When did Jon start expanding his studio's social media presence?" gold April 2023 (`D8:13`, 2023-04-03); final answer June 2023, from a later line (baseline answered 2023-04-03, correct).
- `conv-30/1-20` Gina accepted for the internship, final "2023-05-10" from a different line, gold 2023-05-27 (baseline right).
- `conv-42/3-44` "win a lot of money in a tournament" gold September 2022; answer "Friday, 2022-07-10" (the fourth-tournament line; baseline included the right window).
- `conv-42/3-54` second movie script, answer "June 4, 2022" instead of the Sunday before 25 October.
- `conv-44/5-79` "Roasted Chicken" vs "Chicken Pot Pie" (gold line `D10:13`); `conv-49/8-115` "easy exercises and swimming" vs "watercolor painting"; `conv-50/9-8` shop opening "May 9-15" vs 1 May; `conv-50/9-53` "Dave supports Calvin" vs "friends and team".
- `conv-44/5-37`: accident "2022-09-22" from a different line; gold line says `[= 2023-10-17..2023-10-23]`.

**DET (16).** `conv-42/3-113` "action and sci-fi" vs "fantasy and sci-fi" (the line states fantasy); `conv-43/4-117` team belief vs "facing tough opponents"; `conv-44/5-72` and `conv-44/5-108` (picks one clause of a two-clause line); `conv-47/6-112` "storytelling and characters" vs "shaping the world with choices"; `conv-47/6-147` "practice a little first" vs "gamepad and sense of timing"; `conv-50/9-113` "long drives to relax" vs "wind blowing through his hair"; `conv-50/9-96` "silver Corvette" (caption) vs "classic muscle car" (text; a regression).

**DARITH (13).**
- Single-line durations (4): `conv-47/6-4` "I've been playing for a month now" in a line dated 2022-03-27, gold February 2022 (answer "2022-03-27", retry fired); `conv-47/6-39` "left my IT job after 3 years" (2022-08-06), gold 2019, answer "around 2022-04"; `conv-26/0-68` "Seven years now" gold "Since 2016", answer "Seven years" (baseline credited the same wording); `conv-48/7-134` "I got her last year", answer "about 3 years", gold one year.
- Two-event spans (9): `conv-41/2-63` weeks between Coco and Shadow (answer 3, gold 2); `conv-43/4-16` (answer 6 weeks, gold 3; baseline right); `conv-44/5-59` (1 year 7 months vs 4 months); `conv-50/9-62` workshop length (retry produced "one day"); `conv-49/8-39`, `conv-47/6-62`, `conv-44/5-7`, `conv-42/3-43`, `conv-50/9-61`.

**LIST (10).** `conv-42/3-52` three vs two; `conv-47/6-20` "at least three" vs two (retry fired, first answer said four); `conv-50/9-47` three vs two; `conv-42/3-53` once vs twice (the first walk is only in a caption); `conv-47/6-22` beneficiaries: answer omits "homeless" (regression, the baseline listed all three); `conv-49/8-69` omits "partner gets pregnant" (regression); `conv-49/8-78` "told his extended family" only (regression); `conv-44/5-21` only agility classes; `conv-42/3-75` two of four desserts; `conv-47/6-26` countries, adds Japan.

**IMG (8)**: caption is in the context line, answer is wrong. `conv-49/8-106` "forest scene" (answer: "watercolors"); `conv-49/8-108` "sunsets over the ocean" (answer: "Landscapes"); `conv-49/8-109` caption shows skis, answer "Kayaking" (regression); `conv-49/8-144`; `conv-50/9-115` "a shiny orange car" (answer: mansion into a recording studio); `conv-49/8-104`, `conv-44/5-119`, `conv-43/4-152`.
**IMGX (3)**: `conv-42/3-91` "dragons" (no text or caption contains it; the question's evidence turn `D9:14` only says "this series ... adventures, magic, and great characters"), `conv-42/3-92` "red and purple lighting" (caption says "a gaming room with a computer and a gaming chair"), `conv-26/0-23` (the book title appears on a cover in the photo).

**REF (4).** `conv-48/7-44` denies because the friend is not named Anna (gold 9 April 2023); `conv-49/8-67` "plan it for next month [= 2023-12]" not linked to the beach; `conv-50/9-40` retry fabricated "Classic rock" where `D20:8` says Tupac and Dr. Dre; `conv-26/0-49` (denies "together", the line `D12:15` says "We had a blast last year [= 2022] at the Pride fest").
**HOP (4).** `conv-26/0-71` (two lines, "Becoming Nicole" and "that book you recommended"); `conv-42/3-41` tart caption plus "Yesterday" line; `conv-42/3-23`; `conv-48/7-77` (answer Rio, gold Phuket).
**DANC (3).** `conv-42/3-20` ("I just got a new addition ... Max", line dated 2022-05-20; retry guessed "March 2022"); `conv-43/4-48`; `conv-48/7-71` (answers "Friday 2023-09-01" for "last week [= 08-27..09-02]").
**CONF (1).** `conv-44/5-41` merges "two are Jack Russell mixes" with the Lab/Chihuahua line.

## 2. Transitions, baseline to fixed

### 2.1 Overall flips [M]

| baseline \ fixed | correct | wrong |
|---|---|---|
| correct (1,147) | 1,080 | 67 |
| wrong (393) | 154 | 239 |

Baseline outcome class to fixed outcome class: read_fail 180 to {correct 84, read_fail 96}; retrieval_miss 115 to {correct 35, still miss 76, partial 3, read_fail 1}; partial 96 to {correct 33, partial 60, read_fail 2, miss 1}; correct 1,147 to {correct 1,080, partial 12, read_fail 49, retrieval_miss 6}.

### 2.2 What explains the 154 gains [M, heuristic attribution]

| cause | n |
|---|---|
| baseline answer was a refusal (the grounded prompt and the retry now answer) | 85 |
| different answer (date format, more context via window 2/4, anchored dates, better pick) | 68 |
| near-identical answer, only the verdict changed (judge) | 1 |
| of the 154: retry fired on the question | 27 |
| of the 154: all gold evidence in the fixed context / in the baseline context | 103 / 84 (net +19 from the window 2/4 and date-mention leg) |

The last row matters: 103 of the 154 gains have every gold turn in the fixed context against 84 in the baseline context, so about 19 gains (an eighth) come from composition (the 2/4 neighbour window and the date-mention temporal leg bringing in missing gold) and the rest from reader, prompt and judge behaviour. 49 gains were answered correctly with some gold turn missing from the fixed context (multi-turn golds answered from one line).

### 2.3 What explains the 67 losses [M for the counts; H for the causes]

By mechanism: 62 different answer, 3 retry answer wrong, 1 fixed refuses, 1 near-identical answer with a different verdict (`conv-48/7-37`). Only 1 of the 67 is a pure judge change on the same text, but 15 of the 49 read_fail losses are answers I judge correct that the guarded judge marked WRONG (JFN below): sensitivity to wording of an equivalent answer, not a changed reader.

The 49 baseline-correct that are now read_fail, by my class label [H]: JFN 15, DIST 9, DET 6, LIST 5, DARITH 4, GOLD 4, IMG 3, INF 2, IMGX 1. Categories: temporal 19, single-hop 18, multi-hop 9, open-domain 3. Their mean answer length ratio fixed/baseline is 0.87 [M]; their context grew from 1,590 to 2,113 tokens [M].

Causes:
- **Judge or wording (15 JFN + 4 GOLD where the baseline was credited for an answer the transcript contradicts)**: the fixed answers state a resolved date or a short fact, and the guarded judge marks them WRONG; the baseline's wordier or differently formatted answer happened to be credited. These are mostly noise of the judge on equivalent answers, not reader regressions [H].
- **Shorter answers (14: LIST 5, DET 6, IMG 3)**: the baseline enumerated items or quoted the caption; the fixed "short, direct answer" returns one item or the wrong caption (`conv-47/6-22`, `conv-49/8-69`, `conv-49/8-78`, `conv-44/5-21`, `conv-50/9-96`, `conv-49/8-109`, `conv-49/8-104`) [H].
- **Distractor from a bigger window (9 DIST)**: the reader picks a later or earlier line about the same topic (`conv-30/1-14`, `1-20`, `conv-42/3-44`, `3-54`, `conv-50/9-8`, `9-53`, `conv-49/8-115`) [H]. The context for these rows holds 53 lines against 39. This is an association, not an ablation: the run changed prompt, window, anchored dates and retry together.
- **Committed answers on hypotheticals (2 INF)**: `conv-48/7-70` Yes became No [H].
- **Retry (3 of the 67; 2 of the 49)**: the retry produced a wrong guess where the baseline answer was credited; these are also counted in the class list above [M].
- **Duration arithmetic (4)**: `conv-43/4-16` (6 weeks vs 3 weeks), `conv-26/0-68` ("Seven years" vs "Since 2016"), `conv-42/3-43`, `conv-50/9-62`: the baseline's longer or differently worded answer was credited, the short answer does a wrong subtraction or leaves the year out [H].

### 2.4 Which of the earlier 182 labelled failures are fixed [M]

Using the `READER_GAPS.md` appendix ids and the old class labels (180 of its 182 ids are the read_fail of the baseline here):

| old class | n | now correct | still wrong |
|---|---|---|---|
| JFN | 29 | 22 | 7 |
| INF | 35 | 15 | 20 |
| REF | 22 | 12 | 10 |
| GOLD | 26 | 10 | 16 |
| DARITH | 16 | 6 | 10 |
| IMG | 12 | 4 | 8 |
| DIST | 11 | 3 | 8 |
| DET | 11 | 3 | 8 |
| DANC | 7 | 6 | 1 |
| LIST | 7 | 1 | 6 |
| HOP | 5 | 2 | 3 |
| CONF | 1 | 0 | 1 |
| total | 182 | 84 | 98 |

Caveat [E]: these 182 were selected as failures of one run, and the guards and prompts were designed by reading them. The 84 recovered are in-sample, and part of any recovery of a failure-selected set is regression to the mean (reader and judge noise is about 0.4-0.6% of verdicts, 6-9 questions, per `READER_GAPS.md` section 4). Report the gain on a conversation-level holdout before claiming it (section 3.5). The 10 old GOLD rows now "correct" are cases where the new judge or answer happened to match a wrong gold, not real fixes.

## 3. Retry analysis

### 3.1 Measured [M]

- Fired on 107 of 1,540 questions (6.9%). Reader calls +107 (+7%). By category: open-domain 42 of 96 (44%), temporal 27 of 321 (8%), multi-hop 20 of 282 (7%), single-hop 18 of 841 (2%).
- The first-pass `grounded` prompt produced 107 refusal-like answers (baseline `default` prompt: 236 over the same questions), so the prompt alone removed about 129 refusals, and the retry then acted on the remaining 107.
- Retry result: 77 accepted (non-refusal), 30 refused again (first answer kept). Final verdicts: accepted 30 correct, 47 wrong; kept-refusal 1 correct (a false positive, `conv-47/6-64`), 29 wrong. So **31 of 107 end CORRECT (29%)**; among accepted retries 39% correct.
- Baseline status of the 107: 99 wrong, 8 correct. Of the 31 correct: 27 were wrong in the baseline (gain), 4 correct in both. Of the 76 wrong: 72 wrong in both, 4 were correct in the baseline (potential harm: these 4 are refusals that the baseline answered).
- 18 of the 107 first answers contain the gold string although they are refusal-like (hedged answers, e.g. `conv-26/0-94`, `conv-41/2-41`, `conv-50/9-136`); under the guarded judge some might have been credited without the retry. So the retry's causal gain is between about 20 and 27 questions [E], +1.3 to +1.8 points, for +7% reader calls.
- Among the 148 read_fail rows the retry fired on 32 and **never rescued one** (by construction, the rescued ones are not read_fail): INF 15, GOLD 5 (the reader is right to refuse, wrong gold), DARITH 3, REF 3, IMGX 2, HOP 1, DANC 1, JFN 1, LIST 1.

### 3.2 Behaviour worth knowing [H]

- The retry instruction ("do not reply that it is unknown ... give your most likely answer") produces fabricated guesses on questions that really have no text answer: `conv-50/9-40` "Classic rock", `conv-50/9-62` "one day", `conv-42/3-20` "March 2022", `conv-50/9-137` "after he bought his new mansion". Those would have been wrong either way; they are only a risk for a metric that rewards abstention (cat 5 is not in these 1,540).
- The retry works best on open-domain: 15 of its 42 fires end correct (temporal 6 of 27, multi-hop 5 of 20, single-hop 5 of 18) [M]. The open-domain fires that stay read_fail return a second non-answer because the 9B reader does not make the world-knowledge hop (section 7).

## 4. Judge audit (fixed run, guarded judge) [M for scans, H for reading]

Reviewed verdicts: all 148 read_fail (all WRONG), a seeded sample of 24 other wrongs (retrieval_miss and partial), 1 refusal-like CORRECT (the only one in the run), 42 random non-refusal CORRECT (seed 7, stratified 12/10/12/8 by single-hop/multi-hop/temporal/open-domain) and 25 low-gold-token-recall CORRECT: 240 verdicts, plus two full-population scripts (relative-date equivalence over all 24 weekday-before golds; gold-contained-in-wrong-answer over all 306 wrongs; list-item coverage over all 177 multi-hop corrects). The judge never sees the context, so it cannot tell a gold error from a reader error.

### 4.1 False negatives (marked WRONG, answer states the gold fact)

| source | n | notes |
|---|---|---|
| read_fail, hand-read | 24 (19 clear + 5 borderline) [H] | 8 date answers (7 weekday-before golds + `6-59`), 2 typo cases (`3-95`, `3-183`), 3 supersets (`9-127`, `0-70`, `9-64`), hedge-plus-gold (`7-81`, `0-135`), paraphrase (`3-167`, `9-60`, `8-154`, `2-142`) |
| date-gold, scripted | 7 of 20 equivalent answers rejected (35%) [M] | all 7 are in read_fail; baseline's rubric judge rejected 4 of its equivalent answers; so the guard added to the prompt did not fix the pattern |
| 24 random other wrongs | 0 clear, 4 borderline [H] | `8-82`, `3-70`, `1-44`, `8-51`; all "partial list" or "adjacent paraphrase" that are inconsistent with the lenient credit given to other partial lists |
| gold string inside a WRONG non-refusal answer, scripted | 10 of 306 [M] | only 2-3 are real FNs (`9-127`, `9-91`, `6-72`); the others are right to fail (answer contradicts) |
| near-identical answers, verdict flip baseline vs fixed | 3 of 256 pairs (1.2%) [M] | `6-59`, `7-37` (WRONG now), `8-90` (CORRECT now) |

Estimate [E]: false negatives 24 (hand) + 0-26 unseen among the 158 non-read_fail wrongs (central about 8) = **about 24-50 of 1,540 (1.6-3.2 points), 8-16% of the 306 WRONG-with-answer verdicts**. Concentrated in cat2 temporal (date gold) and cat4 superset answers.

### 4.2 False positives (marked CORRECT, answer misses the gold)

| source | n | notes |
|---|---|---|
| refusal credited | 1 of 1,234 [M] | `conv-47/6-64`. Baseline rubric judge: 25 of 1,147 corrects were refusal-like [M]. The guarded prompt plus fewer refusals removed this error |
| wrong fact credited | 0 of 67 sampled [H] | none seen in the 42 random + 25 low-recall corrects (`conv-26/0-81` is credited on the first word "No" although the stated reason differs from gold: counted as lenient) |
| partial list credited | 42 random corrects: 4 (4 of the 10 multi-hop samples: `6-2`, `5-51`, `0-61`, `3-11`); 25 low-recall corrects: 4 clear (`0-60` clarinet only, `0-65`, `2-26`, `2-44`) + 2 borderline [H] | gold lists with one omitted item are routinely credited |
| scripted list-item scan of all 177 multi-hop corrects | 74 have at least one gold item not found in the answer (shown truncated) [M]; by reading, about 45-55 are genuine omissions and the rest paraphrase [E] | `0-60`, `0-61`, `2-25` (Spain only; gold Spain, England), `2-40` (Kyle only), `3-46` (Catan only; gold Chess, Catan), `3-63` (3 of 5 games), `4-65`, `5-35`, `7-24`, `8-7`, `9-5`, `9-11`, `9-28` ... |

Estimate [E]: false positives about **45-60 of 1,234 corrects (3.6-4.9%), 2.9-3.9 points, almost all multi-hop** (about 25-30% of multi-hop corrects). This is higher than the earlier estimate (1-3% of cat1+cat3) because the fixed reader gives more answers that partially match.

### 4.3 Net effect and the category view

Over-credit (about +2.9 to +3.9 points, multi-hop) and under-credit (about -1.6 to -3.2 points, temporal and single-hop) nearly cancel in the headline (net 0 to +2 points over-stated) [E]. They do not cancel per category: under strict all-items scoring multi-hop would drop by up to 16-20 points (45-55 of 282; the lenient convention is the usual LoCoMo one, so this is a grading-convention gap, not an error in the run), while temporal 76.6% is probably 2-3 points too low. The category ordering of the fixes (multi-hop +6.8, temporal +5.9) is therefore not reliable at that resolution.

Proxy checks [M]: a token-recall >= 0.5 rule gives 79.4% on the fixed run (judge 80.1%) and 73.9% on the baseline (judge 74.5%); judge and recall rule agree on 88.2% of verdicts (84.2% baseline). Containment of the normalised gold string gives only 40.8%, token-F1 >= 0.5 33.6%, so LoCoMo cannot be graded by string match; the aggregate agreement of the crude rule with the LLM judge is high, the 12% disagreement is where the audit above lives.

### 4.4 Methodological risks of same-model reader and judge [H/E]

1. **Shared blind spots.** Qwen3.5-9B Q4_K_M reads and grades. The judge fails on the same operation the reader fails on: weekday and date arithmetic ("the Friday before 23 January" vs 2022-01-21), world-knowledge hops, long-list completeness. Where both are wrong in the same direction the error is invisible. The date FNs are exactly the 7 questions where the reader did the arithmetic right and the judge could not verify it.
2. **Self-preference and verbosity.** The same model rates answers in its own style; the baseline's longer, hedged answers were credited more often than short exact ones in several pairs (`conv-50/9-25`, `conv-50/9-17`, `conv-48/7-37`, `conv-49/8-154`), and the fixed prompt shortened answers by 44% [M]. A "judge change" and a "reader-style change" are confounded in the headline.
3. **Circularity.** The prompt, the retry, the judge guards and the window were chosen by reading the same 182 baseline failures that now report the gain (section 2.4). No holdout.
4. **Context-blind judge.** The judge sees question, gold and answer only, so 21 gold errors are labelled WRONG without the possibility of review, and 25 refusals that matched nothing were credited in the baseline.
5. **Resolution.** Run-to-run verdict noise at temperature 0 is about 0.4-0.6% (6-9 questions); one run per arm; the judge errors above are 2-3 points each in opposite directions.

### 4.5 Proposed judge protocol [E, not implemented]

1. **Deterministic first pass** (no model call): (a) relative-date equivalence for gold of the form "[the] weekday before|after D Month YYYY", "the week before ...", "the weekend of ..." (resolve the gold to a date or range, accept an answer that contains an in-range date in ISO or long form); (b) normalised containment of every gold list item for superset answers; (c) edit-distance 1-2 match for proper nouns (Xenoblade/Xeonoblade); (d) an explicit refusal rule that fails refusals unless gold is a refusal. This alone would settle about 12-17 of the 24 FNs (7 date + 2 typo + 3-6 superset or paraphrase).
2. **Second judge on disagreement only**: an independent family or larger model (a 32B+ open model or a capped cloud judge), run only on rows where (deterministic result, 9B verdict) disagree and on all rows with gold lists of 3 or more items. At about 230 disagreements per run (12% of 1,540) the cost is small. Report both judges and Cohen's kappa; headline = the second judge on the disputed rows.
3. **Strict list scoring as a separate column**: item recall for list golds (fraction of gold items present), reported next to the binary verdict. Binary stays for comparability; item recall shows the 45-60 partial credits.
4. **A small human-checked set**: 150 questions, stratified: all 21 GOLD, 24 JFN, 20 partial-list corrects, 40 random corrects, 45 random wrongs. One person labels once with transcript access; every judge change is scored on it (precision, recall of CORRECT, per category). Freeze the labels; hash them into the manifest.
5. **Holdout discipline**: tune prompts and guards on conv-26, 30, 41, 42 (cat1-4 about 40%) and report on conv-43, 44, 47, 48, 49, 50. Paired bootstrap CIs for run comparisons; do not interpret differences under 1.5 points between single runs.
6. **Errata file** for gold errors (section 5), applied as a reported variant ("adjusted") never as a silent edit.

## 5. Gold and dataset errors [H, evidence from `locomo10.json`, `gold_turns`]

21 read_fail rows where the gold contradicts the transcript or the question names the wrong person. Turn ids are `D<session>:<turn>`.

| # | question id | cat | what is wrong | evidence |
|---|---|---|---|---|
| 1 | conv-26/0-5 | temp | gold "The Sunday before 25 May 2023"; the line says Saturday | `D2:1` "last Saturday [= Sat 2023-05-20]". Reader answer 2023-05-20 is right. |
| 2 | conv-26/0-94 | single | question says Melanie's bowl; it is Caroline's | `D4:5` speaker Caroline |
| 3 | conv-30/1-9 | multi | question names Jean and John; transcript has Gina and Jon | `D2:5` (Gina), `D15:1` (Jon) |
| 4 | conv-41/2-69 | single | question says December 2023; donation is December 2022 | `D2:1` dated 22 Dec 2022 "yesterday [= Wed 2022-12-21]" |
| 5 | conv-42/3-24 (borderline) | temp | gold "The weekend after 3 June 2022"; line says "two weekends later" than the hiking trip | `D14:20` |
| 6 | conv-43/4-6 | temp | gold June 2023; the 40-point game was "last week" of a 16 July line | `D3:1` `[= 2023-07-09..2023-07-15]` |
| 7 | conv-43/4-60 | temp | gold "December 11, 2023"; event was the Friday before | `D23:3` `[= Fri 2023-12-08]` |
| 8 | conv-43/4-86 | single | "What did John share with the person he skyped"; Tim skyped | `D5:1` speaker Tim |
| 9 | conv-43/4-135 | single | "How does Tim stay motivated"; gold is John's line | `D18:6` speaker John; Tim's own line gives the 25/5 method |
| 10 | conv-43/4-136 | single | "What did Tim say about his injury"; it is John's | `D18:10` speaker John |
| 11 | conv-43/4-164 | single | "What language does Tim know besides German"; John knows Spanish | `D27:6` speaker John |
| 12 | conv-43/4-165 | single | "What book did Tim get in Italy"; John got it | `D27:4` speaker John |
| 13 | conv-43/4-166 | single | "John's favourite book series"; the line is Tim's | `D27:19` speaker Tim |
| 14 | conv-44/5-4 | temp | gold "week of April 3rd to 9th"; line dated 16 Apr 2023 says "last week" = 04-09..04-15 | `D3:18` `[= 2023-04-09..2023-04-15]` |
| 15 | conv-47/6-31 | temp | gold "19 days"; leaving 11 July, return 20 July is 9-10 days | `D16:9`, `D16:13` |
| 16 | conv-47/6-72 (borderline) | single | "What instrument is John learning as of 27 March 2022" gold Drums; the learner line is James's | `D3:2` speaker James, `D3:3` John "I play drums too" |
| 17 | conv-48/7-174 | single | "Why did Jolene have to reschedule"; it was Deborah | `D26:15` speaker Deborah |
| 18 | conv-49/8-24 | temp | "significant event in Sam's life"; gold is Evan's (met a Canadian woman) | `D5:1` speaker Evan |
| 19 | conv-49/8-74 | temp | gold "Thursday before December 17, 2023"; line says Tuesday | `D20:3` `[= Tue 2023-12-12]` |
| 20 | conv-49/8-80 | temp | gold "January 9, 2023"; line is dated 10 Jan **2024** | `D24:3` |
| 21 | conv-50/9-137 | single | "When did Calvin first get interested in cars"; line is Dave's | `D26:6` speaker Dave |

In most of the 21 the reader's answer is right relative to the transcript, or it correctly refuses (from reading the answers; not every row individually re-verified). Four of the 21 were credited in the baseline (`conv-43/4-166` by a refusal; `conv-44/5-4`, `conv-42/3-24` and `conv-47/6-72` by lenient matching), which is why they count as "regressions".

Not text-answerable (3 IMGX): `conv-42/3-91`, `conv-42/3-92`, `conv-26/0-23` (the evidence turn does not contain the stated fact; the dataset image does).

**Ceiling impact.** Measured in the 148 read_fail only: 21 + 3 = 24 questions (1.6% of all). Adjusted accuracy dropping those 24 from the denominator: 1,234 / 1,516 = 81.4% (+1.3 points) [M]; an upward-biased adjustment because gold-error questions that the system happened to get right stay in. The earlier analysis found 26 of 182 (14%) gold errors; the 158 other wrongs (retrieval_miss and partial) are unread here, and if they carry a similar share (10-15%) that is another 16-24 questions [E]. Total unattainable on text about 40-50 questions, so **the attainable ceiling for this reader/judge pairing is about 96.5-97.5%**, not 100.

## 6. Reading the remaining 148 as a recoverable budget [E]

| bucket | n | recoverable by system work | notes |
|---|---|---|---|
| judge or metric only (JFN) | 24 | 0 system, 14-20 by judge protocol | changes the number, not the system |
| unattainable (GOLD, IMGX) | 24 | 0 | report separately |
| attainable by reader, composition or helpers | 100 | 35-55 | INF 23, DIST 18, DET 16, DARITH 13, LIST 10, IMG 8, REF 4, HOP 4, DANC 3, CONF 1 |

So the plausible further gain on this retrieval with the same 9B is about +2.3 to +3.6 points from system work and +0.9 to +1.3 from judge work [E], before regressions. Section 7 gives the per-class options.

## 7. Solution options per class [E]

1 question = 0.065 points. "Recovered" is of that class's count in the fixed run; ranges are low-high; options in one row overlap and the totals are not additive. Cost: M = model calls, C = code size, L = latency. Risk notes the earlier failure mode (ADR-054: prompt-only predictions have had the wrong sign; the grounded prompt itself lost 14 LIST, DET, IMG rows).

| class (n) | option | what | expected recovered | cost | risk |
|---|---|---|---|---|---|
| JFN (24) | A. deterministic pre-judge (section 4.5 step 1) | date equivalence for "weekday before D", "week before", "weekend of"; containment for superset; fuzzy proper noun; refusal rule | 12-17 | C: small (`judge.py`), no model | changes metric definition; re-judge old runs; superset rule can credit shotgun answers |
| JFN | B. second judge on disputed rows only | independent/larger model, about 230 rows per run | 14-20 (and corrects some FPs) | M: about 230 calls per run; or a cloud call | judge drift across versions; need pinned model/prompt hashes |
| JFN | C. add worked LoCoMo date examples to the guarded prompt | 3-4 few-shot lines with weekday arithmetic | 3-6 | none | the guarded prompt already has one example and still fails 35% (measured); not recommended alone |
| GOLD (21) + IMGX (3) | A. errata file | per-question tag `gold_error` and `needs_image`, hashed; report both headlines | 0 (changes denominator, +1.3 points adjusted) | C: small | credibility: publish both numbers with the file |
| INF world (12) | A. world-knowledge clause in the QA prompt | "use general knowledge to name the place/game/country when the memory gives the clue" | 3-6 | none | prompt-only; may raise hallucination on cat5-style abstention |
| INF world | B. larger or different reader (27-32B) | config change | 4-8 | M: slower/costlier reader | cost; changes comparability of all prior numbers |
| INF hypothetical (11) | C. route "would/likely/might" to an inference prompt that commits to Yes/No/likely with the supporting line (`routed` variant exists) | 3-5 | none | `dated_infer` measured +1.3 on temporal and -0.3 elsewhere before; retry already fires here and does not rescue |
| DIST (18) | A. mark and order retrieval hits | put the top-k retrieved lines first (or tag them `*`), neighbours after; the gold turn is in the final top 5 in 116 of 130 ranked read_fail rows (89%), 13 of 15 for DIST | 4-7 | C: small (`read.present_order`/render); no model | breaks the chronological reading that helps date and ordering questions; test per shape |
| DIST | B. per-shape window | window 2/4 only for temporal or multi-turn questions, 1/1 otherwise; the +32% context is associated with 9 DIST regressions | 3-5 | C: small | the window also produced 49 of 154 gains; must be per shape, not global |
| DIST | C. answer verification pass | second call: "does the cited line support the answer date/event"; keep or re-ask (`--verify-answer` exists) | 3-5 | M: +1 call per question (or per date question only, about 30%) | adds latency; the 9B verifier shares the reader's blind spots |
| DET (16) | A. answer-style clause | "answer in one sentence that includes the specific detail from the line" instead of "short, direct answer" | 4-7 (also helps LIST, IMG regressions) | none | answers lengthen; judge verbosity effects; prompt-only, screen first |
| DET | B. quote-then-answer | the reader first copies the supporting line, then answers (cheap CoT) | 3-6 | M: more output tokens | `dated3` style lost 3.6 points through terse outputs; check |
| DARITH (13) | A. duration annotator in `temporal_resolve` | "for N years", "N months now", "a month now", "last year", "since" to `[= since YYYY]` or `[= started about YYYY-MM]`; covers the 4 single-line cases (`6-4`, `6-39`, `0-68`, `7-134`) | 3-4 | C: moderate, unit tests | policy "a wrong resolution is worse than none" |
| DARITH | B. elapsed helper | planner or reader extracts the two anchor lines/dates, code subtracts and writes the span into the context (`core/elapsed.py`); covers the 9 two-event cases | 4-6 | M: +1 extraction call on "how long/how many weeks/months" questions only (about 5% of questions); C: moderate | wrong event extraction gives a confident wrong number |
| LIST (10) | A. list/count clause | "list every matching item across the memories; count distinct events once" for `how many`/`what ... has` questions | 3-5 | none | `dated3` loses elsewhere; apply only on list-shaped questions |
| LIST | B. event dedup in the context | collapse lines with the same date and same lemma; add a "Counts" hint | 1-3 | C: moderate | over-merging distinct events |
| LIST | C. structured fact memory | extracted facts with set semantics (deposit-time `activities`, `books` lists) | 4-6 | M: extraction at deposit; C: large | new failure mode: missed extraction; large change |
| IMG (8) | A. prompt line + caption rendering | "`[image: ...]` text describes a photo shared in that turn and is evidence"; render as its own sentence (`Photo: ...`) | 3-5 | none | cheap; also reduces mixing of captions from other lines (render date or turn id next to the caption) |
| REF/HOP/DANC/CONF (12) | A. implicit-date rule | when the question asks "when" and the line has no relative phrase, annotate the line date as the event date; coreference of "the book you recommended" via the previous 2 lines | 2-4 | C: small | wrong when an event is retrospective; apply to past-tense verbs only |
| all | B. second-stage retry for the 30 retries that refused again | a different instruction ("use general knowledge and the line's date; name the most likely entity") | 3-6 | M: +30 calls per run | more fabricated guesses; do not restrict the first retry (open-domain is where it pays: 15 of 42) |

Totals, non-overlapping central estimates [E]: judge protocol 14-20 (metric), DIST 6, DET 5, DARITH 6, LIST 4, IMG 4, INF 6, other 3: about 34-44 questions from system work = +2.2 to +2.9 points, upper range 55 (+3.6) with a bigger reader and structured memory. Most promising per unit cost: (1) judge protocol A+B (no system change, 14-20 q on the metric); (2) retrieval-hit marking plus a per-shape window plus an answer-style clause that restores detail and lists (about 12-20 q combined, no model calls); (3) duration annotator and elapsed helper (6-9 q, deterministic).

## 8. The Qwen3-Reranker-4B run (qa-full-qs-eq06-rq4b4-fx), brief [M]

70.8% (1,090 correct), read_fail 206, retrieval_miss 137, partial 105. Contexts are 806 tokens / 19.7 lines against 1,571 / 39.3 for the baseline. It uses the baseline's reader prompt and rubric judge (no grounded prompt, no retry): refusal-like answers 298 vs 236, and 20 of them are credited by the rubric judge. The extra read_fail (206 vs 180) is consistent with `READER_GAPS.md` section 4: thinner contexts lack the neighbour lines that supply antecedents, and the unfixed reader answers "I do not know" more often. I did not hand-classify these 206; a fixed-reader run on this retrieval would be the clean comparison.

## 9. Caveats

- One annotator; borderline JFN, GOLD, DET and INF calls (about 20 rows) could move between classes without changing the ranking. Borderline rows flagged in the appendix.
- The run changed prompt, retry, judge, window, anchored dates and a retrieval leg at once; section 2 attributes gains and losses by answer behaviour, not by ablation. The only separable pieces are the retry (section 3) and the refusal removal (236 to 107 first-pass).
- The false-positive estimate in 4.2 rests on a script flag (74 of 177) plus hand reading of truncated answers; it is the least certain number in this document (range 30-60).
- Rank statistics use `gold_turns[].ranks.final` as logged; it is the position in the final list passed to the composer, not a relevance score.
- No model was called; nothing in this document was verified by re-running the reader.

## Appendix A. Classification of all 148 fixed-run read_fail rows

Columns: # = row order in `per_question.jsonl` among read_fail; b = baseline status (c = correct in baseline, f = read_fail in baseline, o = other wrong); R = retry fired; ? = borderline label.

| # | question id | cat | class | b | R | question | gold |
|---|---|---|---|---|---|---|---|
| 0 | conv-26/0-2 | cat3 | JFN? | c |  | What fields would Caroline be likely to pursue in her educaton? | Psychology, counseling certification |
| 1 | conv-26/0-5 | cat2 | GOLD | f |  | When did Melanie run a charity race? | The sunday before 25 May 2023 |
| 2 | conv-26/0-22 | cat3 | INF | f | R | Would Caroline likely have Dr. Seuss books on her bookshelf? | Yes, since she collects classic children |
| 3 | conv-26/0-23 | cat1 | IMGX | f |  | What books has Melanie read? | "Nothing is Impossible", "Charlotte's We |
| 4 | conv-26/0-33 | cat2 | DIST | f |  | When did Caroline go to a pride parade during the summer? | The week before 3 July 2023 |
| 5 | conv-26/0-41 | cat2 | JFN | c |  | When did Caroline join a new activist group? | The Tuesday before 20 July 2023 |
| 6 | conv-26/0-49 | cat2 | REF | f |  | When did Caroline and Melanie go to a pride fesetival together? | 2022 |
| 7 | conv-26/0-68 | cat2 | DARITH | c |  | How long has Melanie been practicing art? | Since 2016 |
| 8 | conv-26/0-70 | cat1 | JFN | c |  | What transgender-specific events has Caroline attended? | Poetry reading, conference |
| 9 | conv-26/0-71 | cat1 | HOP | f | R | What book did Melanie read from Caroline's suggestion? | "Becoming Nicole" |
| 10 | conv-26/0-94 | cat4 | GOLD | f | R | What is Melanie's hand-painted bowl a reminder of? | art and self-expression |
| 11 | conv-26/0-135 | cat4 | JFN | f |  | What setback did Melanie face in October 2023? | She got hurt and had to take a break fro |
| 12 | conv-26/0-138 | cat4 | DET | c |  | What kind of painting did Caroline share with Melanie on October 13, 2 | An abstract painting with blue streaks o |
| 13 | conv-26/0-147 | cat4 | DET | f |  | How did Melanie feel after the accident? | Grateful and thankful for her family |
| 14 | conv-26/0-151 | cat4 | DET | f |  | What did Melanie do after the road trip to relax? | Went on a nature walk or hike |
| 15 | conv-30/1-9 | cat1 | GOLD | f |  | Which city have both Jean and John visited? | Rome |
| 16 | conv-30/1-14 | cat2 | DIST | c |  | When did Jon start expanding his studio's social media presence? | April, 2023 |
| 17 | conv-30/1-20 | cat2 | DIST | c |  | When did Gina get accepted for the design internship? | 27 May, 2023 |
| 18 | conv-30/1-43 | cat4 | DIST | c |  | What do the dancers in the photo represent? | They are performing at the festival |
| 19 | conv-41/2-17 | cat3 | INF | f |  | What might John's degree be in? | Political science, Public administration |
| 20 | conv-41/2-63 | cat2 | DARITH | f |  | How many weeks passed between Maria adopting Coco and Shadow? | two weeks |
| 21 | conv-41/2-69 | cat4 | GOLD | f |  | What did Maria donate to a homeless shelter in December 2023? | old car |
| 22 | conv-41/2-85 | cat4 | DIST | f |  | What event did John volunteer at last weekend? | career fair at a local school |
| 23 | conv-41/2-99 | cat4 | DET | f |  | What did John and the veterans do during the small party? | share stories and make connections |
| 24 | conv-41/2-142 | cat4 | JFN | c |  | What recognition did Maria receive at the homeless shelter in August 2 | a medal for volunteering |
| 25 | conv-42/3-4 | cat3 | INF | f |  | What pets wouldn't cause any discomfort to Joanna? | Hairless cats or pigs,since they don't h |
| 26 | conv-42/3-7 | cat2 | JFN | f |  | When did Joanna finish her first screenplay? | The Friday before 23January, 2022 |
| 27 | conv-42/3-12 | cat3 | INF | f | R | What underlying condition might Joanna have based on her allergies? | asthma |
| 28 | conv-42/3-20 | cat2 | DANC | f | R | When did Nate adopt Max? | May 2022 |
| 29 | conv-42/3-23 | cat1 | HOP | o |  | Which of Joanna's screenplay were rejected from production companies? | first screenplay on drama and romance, t |
| 30 | conv-42/3-24 | cat2 | GOLD? | c |  | When is Nate hosting a gaming party? | The weekend after 3June, 2022. |
| 31 | conv-42/3-26 | cat2 | DIST | f |  | When did Nate win his third tourney? | The week before 3June, 2022 |
| 32 | conv-42/3-41 | cat2 | HOP | f |  | When did Joanna make a chocolate tart with raspberries? | 5 October, 2022 |
| 33 | conv-42/3-43 | cat2 | DARITH | c |  | How long did it take for Joanna to finish writing her book? | four months |
| 34 | conv-42/3-44 | cat2 | DIST | c |  | When did Nate win a lot of money in a video game tournament? | September 2022 |
| 35 | conv-42/3-48 | cat2 | JFN | c |  | When did Nate go to a convention and meet new people? | The Friday before 9October, 2022. |
| 36 | conv-42/3-52 | cat1 | LIST | f |  | How many of Joanna's writing have made it to the big screen? | two |
| 37 | conv-42/3-53 | cat1 | LIST | f |  | How many times has Nate taken his turtles on a walk? | Twice. |
| 38 | conv-42/3-54 | cat2 | DIST | c |  | When was Joanna's second movie script shown on the big screens? | The Sunday before 25October, 2022. |
| 39 | conv-42/3-64 | cat2 | JFN | c |  | When did Nate win a big Valorant tourney? | The Saturday before 7November, 2022 |
| 40 | conv-42/3-73 | cat3 | INF | f | R | What state did Joanna visit in summer 2021? | Indiana |
| 41 | conv-42/3-75 | cat1 | LIST | o |  | What are Nate's favorite desserts? | coconut milk icecream, dairy-free chocol |
| 42 | conv-42/3-91 | cat4 | IMGX | f | R | What is Nate's favorite book series about? | dragons |
| 43 | conv-42/3-92 | cat4 | IMGX | c | R | What kind of lighting does Nate's gaming room have? | red and purple lighting |
| 44 | conv-42/3-95 | cat4 | JFN | f |  | What is Nate's favorite video game? | Xenoblade Chronicles |
| 45 | conv-42/3-113 | cat4 | DET | f |  | What is Nate's favorite genre of movies? | Fantasy and sci-fi |
| 46 | conv-42/3-143 | cat4 | DET | c |  | How did Joanna feel when someone wrote her a letter after reading her  | Touched |
| 47 | conv-42/3-148 | cat4 | JFN? | f |  | How did Nate feel about sharing his love for dairy-free desserts with  | Happy to share |
| 48 | conv-42/3-167 | cat4 | JFN | c |  | What does Joanna recommend to make a living room comfy like hers? | couch for multiple people, fluffy blanke |
| 49 | conv-42/3-183 | cat4 | JFN | f |  | What game is Nate currently playing and recommends to others on Novemb | "Xenoblade Chronicles" |
| 50 | conv-43/4-6 | cat2 | GOLD | f |  | In which month's game did John achieve a career-high score in points? | June 2023 |
| 51 | conv-43/4-8 | cat3 | INF | f | R | Which outdoor gear company likely signed up John for an endorsement de | Under Armour |
| 52 | conv-43/4-15 | cat3 | INF | f |  | Who is Anthony? | likely John's friend, colleague or famil |
| 53 | conv-43/4-16 | cat2 | DARITH | c |  | After how many weeks did Tim reconnect with the fellow Harry Potter fa | three weeks |
| 54 | conv-43/4-32 | cat3 | INF | f | R | Which US states might Tim be in during September 2023 based on his pla | California or Florida |
| 55 | conv-43/4-40 | cat2 | INF | f | R | Has Tim been to North Carolina and/or Tennesee states in the US? | Yes |
| 56 | conv-43/4-48 | cat1 | DANC | f |  | When did John get an ankle injury in 2023? | around November 16, 2023 |
| 57 | conv-43/4-51 | cat3 | INF | f |  | What kind of yoga for building core strength might John benefit from? | Hatha Yoga |
| 58 | conv-43/4-60 | cat2 | GOLD | f |  | When did John achieve a career-high assist performance? | December 11, 2023 |
| 59 | conv-43/4-66 | cat3 | INF | f | R | What is a Star Wars book that Tim might enjoy? | Star Wars: Jedi Apprentice by Judy Blund |
| 60 | conv-43/4-70 | cat3 | INF | f | R | Which Star Wars-related locations would Tim enjoy during his visit to  | Skellig Michael, Malin Head, Loop Head,  |
| 61 | conv-43/4-76 | cat4 | DIST | f |  | What kind of picture did Tim share as part of their Harry Potter book  | MinaLima's creation from the Harry Potte |
| 62 | conv-43/4-86 | cat4 | GOLD | f |  | What did John share with the person he skyped about? | Characters from Harry Potter |
| 63 | conv-43/4-106 | cat4 | DIST | f |  | Where are John and his teammates planning to explore on a team trip? | a new city |
| 64 | conv-43/4-113 | cat4 | JFN? | f | R | Which basketball team does Tim support? | The Wolves |
| 65 | conv-43/4-117 | cat4 | DET | f |  | What motivates John's team to get better, according to John? | facing tough opponents |
| 66 | conv-43/4-135 | cat4 | GOLD | f |  | How does Tim stay motivated during difficult study sessions? | Visualizing goals and success |
| 67 | conv-43/4-136 | cat4 | GOLD | f | R | What did Tim say about his injury on 16 November, 2023? | The doctor said it's not too serious |
| 68 | conv-43/4-151 | cat4 | DET | f |  | What is the topic of discussion between John and Tim on 11 December, 2 | Academic achievements and sports success |
| 69 | conv-43/4-152 | cat4 | IMG | f |  | What kind of game did John have a career-high in assists in? | basketball |
| 70 | conv-43/4-164 | cat4 | GOLD | f |  | What language does Tim know besides German? | Spanish |
| 71 | conv-43/4-165 | cat4 | GOLD | f | R | What book did Tim get in Italy that inspired him to cook? | a cooking book |
| 72 | conv-43/4-166 | cat4 | GOLD | c |  | What is John's favorite book series? | Harry Potter |
| 73 | conv-44/5-4 | cat2 | GOLD | c |  | When did Audrey make muffins for herself? | The week of April 3rd to 9th |
| 74 | conv-44/5-7 | cat2 | DARITH | f |  | How many years passed between Audrey adopting Pixie and her other thre | three years |
| 75 | conv-44/5-21 | cat1 | LIST | c |  | What are the classes that Audrey took for her dogs to? | Positive reinforcement training class fo |
| 76 | conv-44/5-37 | cat2 | DIST | f |  | When did Audrey get into an accident in the park? | between October 19 and 24, 2023 |
| 77 | conv-44/5-41 | cat1 | CONF | f |  | What are the breeds of Audrey's dogs? | Mongrel mixed with Lab for Pepper and Pa |
| 78 | conv-44/5-59 | cat2 | DARITH | f |  | How long has it been since Andrew adopted his first pet, as of Novembe | 4 months |
| 79 | conv-44/5-72 | cat4 | DET | f |  | Why did Audrey think positive reinforcement training is important for  | To have pets learn how to behave in a po |
| 80 | conv-44/5-79 | cat4 | DIST | f |  | What is Audrey's favorite recipe that she shares with Andrew on 3 July | Chicken Pot Pie |
| 81 | conv-44/5-108 | cat4 | DET | c |  | How does Audrey describe her dogs' response to snow? | They definitely prefer nice, sunny days  |
| 82 | conv-44/5-119 | cat4 | IMG | c |  | What did Audrey share to show ways to keep dogs active in the city? | photography of a basket full of stuffed  |
| 83 | conv-47/6-0 | cat3 | INF | f | R | What are John's suspected health problems? | Obesity |
| 84 | conv-47/6-4 | cat2 | DARITH | f | R | When did John resume playing drums in his adulthood? | February 2022 |
| 85 | conv-47/6-16 | cat3 | INF | f | R | What is the game with different colored cards that was John talking ab | UNO |
| 86 | conv-47/6-17 | cat3 | INF | f | R | What is the board game where you have to find the imposter that John m | Mafia |
| 87 | conv-47/6-20 | cat1 | LIST | f | R | How many charity tournaments has John organized till date? | two |
| 88 | conv-47/6-22 | cat1 | LIST | c |  | Who or which organizations have been the beneficiaries of John's chari | animal shelter, homeless, children's hos |
| 89 | conv-47/6-26 | cat1 | LIST | c |  | Which countries has James visited? | Italy, Mexico, Turkey, Canada, Greenland |
| 90 | conv-47/6-31 | cat2 | GOLD | f |  | How many days did James plan to spend on his trip in Canada? | 19 days |
| 91 | conv-47/6-35 | cat3 | INF | c |  | What additional country did James visit during his trip to Canada? | Greenland |
| 92 | conv-47/6-39 | cat2 | DARITH | f |  | When did John start his job in IT? | 2019 |
| 93 | conv-47/6-41 | cat2 | DIST | c |  | When did James meet Samantha? | August 9, 2022 |
| 94 | conv-47/6-59 | cat2 | JFN | c |  | When did John and his gaming friends organize the charity tournament? | On the night of October 30 to 31, 2022 |
| 95 | conv-47/6-62 | cat2 | DARITH | f |  | How long did James and Samantha date for before deciding to move in to | nearly three months |
| 96 | conv-47/6-72 | cat4 | GOLD? | c |  | What instrument is John learning to play as of 27 March, 2022? | Drums |
| 97 | conv-47/6-112 | cat4 | DET | f |  | What aspect of "The Witcher 3" does John find immersive? | shaping the world with choices |
| 98 | conv-47/6-147 | cat4 | DET | f |  | What did John suggest James practice before playing FIFA 23 together? | Control with a gamepad and timing |
| 99 | conv-48/7-37 | cat2 | JFN | c |  | When did Jolene take Seraphim to the park? | Sunday before 2 March, 2023 |
| 100 | conv-48/7-44 | cat2 | REF | f | R | When did Deborah go to an art show with Anna? | on 9 April, 2023 |
| 101 | conv-48/7-70 | cat3 | INF | c | R | Is the friend who wrote Deborah the motivational quote no longer alive | likely yes |
| 102 | conv-48/7-71 | cat2 | DANC | f |  | When did Deborah go to a community meetup? | last week of August 2023 |
| 103 | conv-48/7-75 | cat3 | INF | f | R | What card game is Deborah talking about? | Exploding Kittens |
| 104 | conv-48/7-77 | cat1 | HOP | f |  | Where did Jolene and her partner find a cool diving spot? | Phuket |
| 105 | conv-48/7-81 | cat2 | JFN | f |  | When did the Deboran and Jolene agree to go surfing? | in October 2023 |
| 106 | conv-48/7-87 | cat1 | INF | f |  | Which countries has Deborah traveled to? | Thailand, Brazil |
| 107 | conv-48/7-101 | cat4 | JFN? | c |  | When did Jolene buy her pet snake? | A year ago |
| 108 | conv-48/7-115 | cat4 | DET | c |  | What activity does Deborah incorporate into her daily routine after go | spending time with loved ones |
| 109 | conv-48/7-134 | cat4 | DARITH | f |  | For how long has Jolene had Seraphim as a pet? | one year |
| 110 | conv-48/7-151 | cat4 | DET | c |  | What did Deborah and her husband use to play to bond and make memories | video games |
| 111 | conv-48/7-174 | cat4 | GOLD | o | R | Why did Jolene have to reschedule their meeting with Deborah on Septem | Jolene already had plans |
| 112 | conv-49/8-24 | cat2 | GOLD | f |  | What significant event happened in Sam's life towards the end of summe | He fell in love with a Canadian woman |
| 113 | conv-49/8-39 | cat2 | DARITH | f |  | How many months lapsed between Sam's first and second doctor's appoint | three months |
| 114 | conv-49/8-57 | cat3 | INF | f |  | Which US state was Sam travelling in during October 2023? | California |
| 115 | conv-49/8-67 | cat2 | REF | f | R | When did Evan and Sam planned a trip to the beach together? | December, 2023 |
| 116 | conv-49/8-69 | cat1 | LIST | c |  | Which two significant life events occur in Evan's life in December 202 | his partner gets pregnant and they get m |
| 117 | conv-49/8-74 | cat2 | GOLD | f |  | When did Evan's son fall off his bike? | Thursday before December 17, 2023. |
| 118 | conv-49/8-78 | cat1 | LIST | c |  | Who did Evan tell about his marriage? | To Sam, to his friends from work, and to |
| 119 | conv-49/8-80 | cat2 | GOLD | f |  | When did Evan have a drunken night with his friends? | January 9, 2023 |
| 120 | conv-49/8-104 | cat4 | IMG | c |  | What food did Sam share a photo of on 19 August, 2023? | bowl of spinach, avocado, and strawberri |
| 121 | conv-49/8-106 | cat4 | IMG | f |  | What did Evan start painting years ago due to being inspired by a frie | forest scene |
| 122 | conv-49/8-108 | cat4 | IMG | f |  | What type of landscapes does Evan love painting the most? | sunsets over the ocean |
| 123 | conv-49/8-109 | cat4 | IMG | c |  | What fun activity did Evan mention doing in July 2023? | skiing |
| 124 | conv-49/8-115 | cat4 | DIST | c |  | What activity does Evan do to keep himself busy while healing his knee | Watercolor painting |
| 125 | conv-49/8-144 | cat4 | IMG | f |  | What did Evan share with Sam after their hiking trip? | a photo of a man standing on a rock look |
| 126 | conv-49/8-154 | cat4 | JFN | c |  | Why did Evan apologize to his partner? | for a drunken night |
| 127 | conv-50/9-1 | cat1 | JFN? | f |  | What items did Calvin buy in March 2023? | mansion in Japan, luxury car Ferrari 488 |
| 128 | conv-50/9-7 | cat3 | INF | f | R | Does Dave's shop employ a lot of people? | Yes |
| 129 | conv-50/9-8 | cat2 | DIST | c |  | When did Dave start his car maintenance shop? | May 1, 2023 |
| 130 | conv-50/9-13 | cat3 | INF | f | R | Would Calvin enjoy performing at the Hollywood Bowl? | Yes; because he enjoys the rush of perfo |
| 131 | conv-50/9-17 | cat2 | JFN | c |  | When did Calvin have a car incident? | on the Friday before 21 June, 2023 |
| 132 | conv-50/9-25 | cat2 | JFN | c |  | When did Dave host a card-playing night with his friends? | on the Friday before 22 August, 2023 |
| 133 | conv-50/9-40 | cat1 | REF | f | R | What was the artists Calvin used to listen to when he was a kid? | Tupac and Dr. Dre |
| 134 | conv-50/9-47 | cat1 | LIST | f |  | How many car shows has Dave attended? | two |
| 135 | conv-50/9-53 | cat1 | DIST | c |  | Who supports Calvin in tough times? | friends and team |
| 136 | conv-50/9-54 | cat1 | DIST | f |  | What does help Calvin stay connected to the creative process? | Calvin stays connected to the creative p |
| 137 | conv-50/9-60 | cat1 | JFN | c |  | What gifts has Calvin received from his artist friends? | gold chain, custom-made guitar with an o |
| 138 | conv-50/9-61 | cat2 | DARITH | f | R | How long did Dave's work on the Ford Mustang take? | nearly two months |
| 139 | conv-50/9-62 | cat2 | DARITH | c | R | How long was the car modification workshop in San Francisco? | two weeks |
| 140 | conv-50/9-64 | cat1 | JFN | c |  | What activities has Dave participated in with his friends? | weekly visits to local parks, countrysid |
| 141 | conv-50/9-91 | cat4 | INF | f |  | What car brand does Calvin own that he is proud of? | Ferrari |
| 142 | conv-50/9-96 | cat4 | DET | c |  | What type of car did Dave work on during the workshop? | classic muscle car |
| 143 | conv-50/9-109 | cat4 | DIST | f |  | What is Calvin excited about after the tour? | exploring and growing his brand |
| 144 | conv-50/9-113 | cat4 | DET | f |  | What activity did Calvin enjoy during his summer drives? | feeling the wind blowing through his hai |
| 145 | conv-50/9-115 | cat4 | IMG | f |  | What project did Calvin work on to chill out? | A shiny orange car |
| 146 | conv-50/9-127 | cat4 | JFN | f |  | What specific location in Tokyo does Calvin mention being excited to e | Shinjuku |
| 147 | conv-50/9-137 | cat4 | GOLD | f | R | When did Calvin first get interested in cars? | at an early age |
