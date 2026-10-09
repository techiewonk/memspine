# LoCoMo reader / composition / judge gaps (qs-eq06-roff)

Run analysed: `evals/runs/qa-full-qs-eq06-roff--memspine` (Qwen3-Embedding-0.6B + hybrid search, +-2-turn window, Qwen3.5-9B Q4_K_M reader and judge, thinking off). Compared with `qs-eq06-rjina`, `qs-ebge-roff`, `qs-ejina-rjina`. CPU-only analysis of `results.jsonl` and `trace.jsonl` plus `data/locomo10.json`. No model call, no harness run, no existing file edited. Categories follow LoCoMo numbering: cat1 multi-hop (282 q), cat2 temporal (321), cat3 open-domain (96), cat4 single-hop (841).

## 0. Headline findings

1. **The read_fail bucket (182 questions, 11.8%) is not mostly reader failure.** Reading all 182 by hand: 26 (14.3%) are gold/question errors (the question names the wrong speaker, or a year/date that contradicts the dataset) and 29 (15.9%) are judge false negatives (the answer states the gold fact). So **55 of 182 (30%) need no reader fix**; the real reader/composition failures are 127 (8.2% of all questions).
2. **The reader was run with the harness `default` prompt**, not a dated one. `prompt_sha256 = 83fc53c6...` is exactly `DEFAULT_QA_PROMPT` ("Answer using only the context... say you do not know"). The context carries `[YYYY-MM-DD]` prefixes and `[= Fri 2023-07-14]` annotations, but the reader is never told what they mean, and is told to refuse. 92 of the 182 read_fail answers (51%) are refusals or "the context does not specify" hedges; 226 of 1,540 answers overall (14.7%) are refusal-like.
3. **Biggest single class is open-domain inference (35, 19%)**: world-knowledge hops (Paris->France, Fort Wayne->Indiana, "UNO", "Mafia", July 2 -> Independence Day) and "would/likely" questions where the reader says "I do not know". All 32 cat3 read_fails fall here.
4. **Temporal read_fails (cat2, 62)** split into elapsed-time arithmetic that nothing resolves ("for 3 years", "a month now", "four months now": 15), gold/question date errors (10), judge misses on LoCoMo's "The Friday before X" gold style (10), refusals on implicit dates (10), unresolved phrases / wrong event (12).
5. **Judge**: false-positive rate is small but real (>= 8 refusals credited, plus lenient partial-list credit), false-negative rate is larger (about 9-12% of wrong verdicts). Net the judge under-credits by roughly +0.5 to +2 points. On near-identical answers it flips verdict 5.7% of the time.
6. **Rerank vs no-rerank (74.4 vs 71.3) is not a "same evidence, different prompt" comparison.** Jina-reranked contexts are 58% smaller (672 vs 1,571 tokens, 17 vs 39 turns) and the contexts differ in 1,539 of 1,540 questions. Where gold is fully present in both (1,184 q), the reranked run refuses 135 times vs 101, and refusal explains 40 of the 71 "roff right / rjina wrong" flips.
7. Run-to-run noise from the reader+judge at temperature 0 is small (about 0.4% of verdicts), well below sampling error (SE of a paired difference between these two runs is about 0.85 points; the gap is 3.1).

Estimated recoverable from reader/judge/composition work: **about +2.9 to +6.0 points (45-95 questions) on this retrieval, central estimate +4.3 (about 66 q)**, before regressions. Section 6 has the fix table.

## 1. Re-deriving read_fail and rebuilding the context

`error_analysis.classify` logic (all gold evidence ids in `retrieved_ids`, score 0), re-run over the 1,540 rows:

| outcome | n | % |
|---|---|---|
| correct | 1,145 | 74.4 |
| retrieval_miss | 115 | 7.5 |
| partial | 96 | 6.2 |
| read_fail | 182 | 11.8 |
| no_gold | 2 | 0.1 |

read_fail by category: cat1 17 of 282 (6.0%), cat2 62 of 321 (19.3%), cat3 32 of 96 (33.3%), cat4 71 of 841 (8.4%). This reproduces the known split exactly.

**Context rebuild.**
- *Order is exactly recoverable.* The trace's `E_t` list is the assembled-record order and its `score` field is just `1/(rank+1)` of that position (not a relevance score). In all 1,540 rows `retrieved_ids` is in ascending chronological turn order, so the reader saw one chronological block (the engine's replay mode sorts it).
- *Text is reconstructed, not byte-exact.* The engine record content is `"<Speaker>: <turn text>"` (image captions appended as `[image: ...]`), passed through `memspine.core.temporal_resolve.annotate` against the session date (`resolve_relative_dates: true`, `relative_dates_anchored: false`, calendar weeks), then the harness prefixes `[YYYY-MM-DD] `. I rebuilt exactly that from the dataset turns. The trace stores only a sha256 that I could not match without the engine, so the rebuild is unverified byte-wise; by the harness character-count heuristic it is 6.8% longer than the logged `context_tokens`, which is consistent with a different token estimator rather than missing text. No context was truncated (`context_truncated` is false in every row of both main runs), so "in `retrieved_ids`" means "in the reader's prompt" for all 182.
- Context size for the 182: 728-2,305 tokens, median about 1,500 (budget 4,096).

## 2. Taxonomy of the 182 read_fail answers (all 182 read by hand)

Each row was read against the gold evidence turns. One primary class per question. Single annotator; borderline calls noted in the class definition.

| code | class | n | % of 182 | % of all 1,540 | cat1 | cat2 | cat3 | cat4 |
|---|---|---|---|---|---|---|---|---|
| INF | Open-domain inference refusal or miss | 35 | 19.2 | 2.3 | 2 | 1 | 32 | 0 |
| JFN | Judge false negative (answer states the gold fact) | 29 | 15.9 | 1.9 | 1 | 10 | 0 | 18 |
| GOLD | Gold / question error | 26 | 14.3 | 1.7 | 1 | 10 | 0 | 15 |
| REF | Refusal despite explicit (or trivially derivable) evidence | 22 | 12.1 | 1.4 | 1 | 10 | 0 | 11 |
| DARITH | Elapsed-time / duration arithmetic | 16 | 8.8 | 1.0 | 0 | 15 | 0 | 1 |
| IMG | Answer lives in an image caption (ignored, or title not in text) | 12 | 6.6 | 0.8 | 1 | 1 | 0 | 10 |
| DIST | Wrong event or fact picked (distractor line) | 11 | 6.0 | 0.7 | 1 | 5 | 0 | 5 |
| DET | Right line, wrong or vague detail (paraphrase, picked other clause) | 11 | 6.0 | 0.7 | 0 | 0 | 0 | 11 |
| DANC | Date anchoring: unresolved or wrong absolute date | 7 | 3.8 | 0.5 | 0 | 7 | 0 | 0 |
| LIST | List / count incompleteness or over-count | 7 | 3.8 | 0.5 | 7 | 0 | 0 | 0 |
| HOP | Multi-hop link not made (needs two lines joined) | 5 | 2.7 | 0.3 | 2 | 3 | 0 | 0 |
| CONF | Conflicting memories in context | 1 | 0.5 | 0.1 | 1 | 0 | 0 | 0 |
| | **Total** | **182** | 100 | 11.8 | 17 | 62 | 32 | 71 |

Sub-splits:
- INF = 20 world-knowledge lookups (city->country/state, game/genre names, holiday from a date) + 15 hypothetical/"would/likely" questions. Three INF rows are outside cat3 (one cat2, two cat1).
- GOLD = 15 where the question names a person who did not say it (e.g. "Melanie's hand-painted bowl" is Caroline's; "Jean and John" for Gina and Jon; "Tim ... language besides German" is John's line) + 11 where the question or gold date contradicts the transcript (question says December 2023, line is 2022-12-17; gold "January 9, 2023", line is 2024-01-10; gold "Thursday" when the transcript says Tuesday).
- JFN = 10 LoCoMo "The Friday before 14 August 2023" gold style answered with the correct resolved date, 12 answers that contain the gold fact plus extras or a hedge, 7 paraphrase / typo / borderline (gold "Xenoblade", source and answer both say "Xeonoblade").
- DANC = bare "on Friday" never annotated (3), bare "weekend" (1), "the other day" (1), hallucinated date (2).

### Worked examples per class

**INF (open-domain)**
- `conv-48/7-5` "In what country did Jolene's mother buy her the pendant?" gold *In France*. Context: "she gave it to me in 2010 in Paris." Answer: "Paris ... the specific country (France) is not explicitly mentioned." Reader knows Paris is in France and still refuses to say so.
- `conv-42/3-73` "What state did Joanna visit in summer 2021?" gold *Indiana*. Context: "last summer ... near Fort Wayne". Answer: "I do not know."
- `conv-41/2-39` "Around which US holiday did Maria get into a car accident?" gold *Independence Day*. Context: line dated 2023-07-03, "yesterday [= Sun 2023-07-02]". Answer: "July 2023 ... does not specify a particular US holiday".
- `conv-26/0-22` "Would Caroline likely have Dr. Seuss books?" gold *Yes, since she collects classic children's books*. Context has "lots of kids' books - classics". Answer: "I do not know" (the `default` prompt tells it to).
- `conv-42/3-12` "What underlying condition might Joanna have based on her allergies?" gold *asthma* (no line says it). Answer: "no information".

**JFN (judge false negative)**
- `conv-26/0-45` gold "The Friday before 14 August 2023", answer "Friday, August 11, 2023" -> WRONG. The Jina run's near-identical answer "Friday, 2023-08-11" was graded CORRECT.
- `conv-42/3-7` gold "The Friday before 23 January, 2022", answer "last Friday ... 2022-01-21" -> WRONG.
- `conv-41/2-130` "Which activity apart from yoga?" gold *weight training*, answer "kickboxing and weight training" -> WRONG (superset).
- `conv-42/3-183` gold "Xenoblade Chronicles", answer "Xeonoblade Chronicles" (the transcript's own typo) -> WRONG.
- `conv-48/7-25` and `7-90` (same Sapiens + Avalanche answer; gold lists exactly those two) -> WRONG in this run, CORRECT in another run with a 99%-identical answer.

**GOLD (gold or question error)**
- `conv-26/0-15` "What is Melanie's hand-painted bowl a reminder of?" The line is Caroline's. The reader correctly says so and refuses.
- `conv-41/2-68` "What workout class did Maria start in December 2023?" gold *aerial yoga*; the line is dated 2022-12-17. Reader: "I do not know."
- `conv-49/8-80` gold "January 9, 2023"; the line is dated 2024-01-10 ("Yesterday [= Tue 2024-01-09]"). Answer "Tue 2024-01-09" is right per the transcript.
- `conv-43/4-6` "In which month's game did John achieve a career-high score?" gold *June 2023*; the annotated line says "Last week [= 2023-07-03..2023-07-09] I scored 40 points". Answer "July" follows the text.
- `conv-43/4-136` "What did Tim say about his injury on 16 November?" It was John's injury.

**REF (refusal despite evidence)**
- `conv-26/0-44` "When is Melanie's daughter's birthday?" gold *13 August*. Context: "Last night [= Sun 2023-08-13] ... we celebrated my daughter's birthday". Answer: "The context does not contain the answer".
- `conv-48/7-44` "When did Deborah go to an art show with Anna?" gold *9 April 2023*. Context: "Checked out an art show with a friend today [= Sun 2023-04-09]". Answer: "I do not know."
- `conv-49/8-83` "What type of car did Evan get after his old Prius broke down?" gold *new Prius*; line: "my new Prius". Answer: "I do not know."
- `conv-43/4-113` "Which basketball team does Tim support?" gold *The Wolves*; line: "The Wolves are solid and LeBron's skills ... are amazing." Answer: "I do not know."
- Implicit date: `conv-42/3-20` "When did Nate adopt Max?" gold *May 2022*; the line ("I just got a new addition ... Max") is dated 2022-05-20. Answer: "I do not know."

**DARITH (elapsed time)**
- `conv-41/2-31` "When did John get his dog Max?" gold *2013*. Line (2023-06-03): "he was ... part of our family for 10 years". Answer: "does not contain the answer."
- `conv-47/6-4` "When did John resume playing drums?" gold *February 2022*. Line (2022-03-27): "I've been playing for a month now". Answer: "I do not know."
- `conv-42/3-8` "When did Nate get his first two turtles?" gold *2019*. Line (2022-01-23): "I've had them for 3 years now". Refused.
- `conv-43/4-56` "When did Tim start playing the violin?" gold *August 2023*. Line (2023-12-06): "playing for about four months now". Answer "December 2023".
- `conv-48/7-134` "For how long has Jolene had Seraphim?" gold *one year*; line "I got her last year [= 2022]". Answer "about 3 years" (pulled "together three years" from another line).

**IMG (caption-only evidence)**
- `conv-49/8-144` "What did Evan share with Sam after their hiking trip?" gold *a photo of a man standing on a rock looking out over a valley*. That sentence is the caption inside the same context line. Answer: "I do not know."
- `conv-50/9-115` "What project did Calvin work on to chill out?" gold *A shiny orange car* (caption `a shiny orange car with the hood open`). Answer: "transforming a Japanese mansion into a recording studio".
- `conv-42/3-92` "What kind of lighting does Nate's gaming room have?" gold *red and purple lighting*; the caption only says "a gaming room with a computer and a gaming chair". Not answerable from the stored text.
- `conv-43/4-152` "What kind of game ... career-high in assists?" gold *basketball* (caption). Answer: "their big game against their rival".
- `conv-26/0-26` "When did Melanie read the book 'nothing is impossible'?" gold *2022*. The title appears only on the book cover in the image; the text says "This book I read last year [= 2022]". Unresolvable without the title.

**DIST (picked the wrong event/line)**
- `conv-42/3-26` "When did Nate win his third tourney?" gold *The week before 3 June 2022*. Correct line is annotated `[= 2022-05-23..2022-05-29]`; the answer cites "between 2022-09-26 and 2022-10-02" from a different line.
- `conv-50/9-12` "When was Calvin's concert in Tokyo?" gold *last week of May 2023*; answer "2023-10-19".
- `conv-44/5-37` "When did Audrey get into an accident in the park?" annotated `[= 2023-10-16..2023-10-22]`; answer "2023-09-22".
- `conv-42/3-29` "When is Joanna going to make Nate's ice cream?" annotated `this weekend [= 2022-06-25..2022-06-26]`; answer "2022-11-05".
- `conv-41/2-85` "What event did John volunteer at last weekend?" (underspecified; the reader picked the latest 5K, not the 2023-04-07 career fair).

**DET (wrong or vague detail)**
- `conv-42/3-113` "What is Nate's favorite genre of movies?" Line: "I love fantasy and sci-fi movies". Answer: "action and sci-fi" (from another line).
- `conv-50/9-113` "What activity did Calvin enjoy during his summer drives?" Line: "The wind blowing through my hair and the rush of freedom ... I've pondered the intricacies of life". Answer picks only "pondering the intricacies of life".
- `conv-47/6-147` "What did John suggest James practice before FIFA 23?" Line: "all you need is a gamepad and a sense of timing". Answer: "practice a little first".
- `conv-43/4-106` "Where are John and his teammates planning to explore?" Line: "explore a new city". Answer: "still deciding on the destination".

**DANC (date anchoring)**
- `conv-42/3-31` "I won my fourth video game tournament on Friday!" in a line dated 2022-07-10 (a Sunday). "on Friday" has no `last`/`this`, so the annotator leaves it; answer "Friday, 2022-07-10". Gold: the Friday before = 2022-07-08.
- `conv-43/4-68` "On Friday, I got great news" (line 2024-01-07). Answer: "Friday ... exact date is not stated".
- `conv-48/7-38` "I hosted a class for them on Friday" (line 2023-03-13). Answer: "2023-03".
- `conv-47/6-66` "I had a super fun weekend" (line 2022-11-07), gold *November 5-6, 2022*. Answer: "[2022-11-07]".
- `conv-44/5-57` "adopt another dog the other day" (line 2023-11-22) -> answer is the said date. The annotator deliberately skips "the other day".

**LIST (list or count)**
- `conv-42/3-52` "How many of Joanna's writing have made it to the big screen?" gold *two*, answer "three" (counted a repeat).
- `conv-47/6-20` "How many charity tournaments has John organized?" gold *two*, answer *three*.
- `conv-50/9-47` "How many car shows has Dave attended?" gold *two*, answer *three*.
- `conv-42/3-53` "How many times has Nate taken his turtles on a walk?" gold *Twice*, answer "only once" (the first is only in an image caption).
- `conv-50/9-1` "What items did Calvin buy in March 2023?" gold mansion + Ferrari; the answer gives the car and hedges the mansion.

**HOP (multi-hop link)**
- `conv-26/0-71` "What book did Melanie read from Caroline's suggestion?" Both lines are in context ("I loved 'Becoming Nicole'" on 2023-07-12; "that book you recommended a while ago" on 2023-10-13). Answer: "never specifies the title".
- `conv-42/3-2` "When did Joanna first watch 'Eternal Sunshine'?" The line "I first watched it around 3 years ago [= ≈ 2019]" refers to the movie only by pronoun; the reader refuses because the title is not repeated in the line.
- `conv-42/3-41` "When did Joanna make a chocolate tart with raspberries?" Needs "tried my newest dairy-free recipe" + photo caption "a tart with raspberries".
- `conv-48/7-77` "Where did Jolene and her partner find a cool diving spot?" Needs the Phuket retreat line and the scuba line.

**CONF (conflict)**: `conv-44/5-41` breeds of Audrey's dogs: two lines disagree ("Two ... are Jack Russell mixes" vs "Pepper and Panda are Lab mixes"); answer merges both.

### Refusal prevalence

Refusal-like answer (regex over "do not know / does not contain / no mention / not specified"): 92 of the 182 read_fail answers (INF 28, REF 19, GOLD 16, DARITH 14, IMG 5, HOP 4, JFN 4, DIST 1, DANC 1). Across the whole run 226 of 1,540 answers (14.7%) are refusal-like, but only 15 of the 201 caught by a stricter regex are marked correct.

## 3. Judge audit

The judge sees only question, gold and answer (`RUBRIC_BINARY_PROMPT`, JSON `{"label": ...}`); it never sees the context. Rule text: CORRECT only if the answer states the same fact as gold (longer is fine); dates compared at the gold's precision "including relative forms that resolve to it"; WRONG if it says it does not know unless gold says so.

**Sample.** Marked correct: all 15 refusal-like correct answers plus 26 random others (8/8/8/6 per category) = 41. Marked wrong: all 182 read_fail answers (read in section 2) plus a random 24 of the 211 retrieval_miss/partial wrongs. 247 verdicts reviewed. Judge output formatting is clean: 1,039+106 CORRECT and 368+27 WRONG tokens, no unparsable verdicts; no empty answers in the run (so the "7 empty answers credited" issue from the thinking test did not occur here).

**False positives (marked correct, should be wrong).**
- Of the 15 refusal-like correct answers, 8 are clear false positives: `conv-26/0-59` (religious), `conv-30/1-43` (dancers), `conv-41/2-32` (colleagues' outdoor activities), `conv-43/4-166` (favorite book series), `conv-48/7-115` (daily routine), `conv-50/9-48` (Dave's first weekend of October), `conv-50/9-62` (workshop length), `conv-50/9-147` (Calvin's tools). The other 7 state the gold fact inside a hedge (e.g. `conv-30/1-13` lists the 2023-03-16 open date; `conv-49/8-10` answers "camping trip") and are fair.
- Of the 26 random non-refusal correct answers: 0 clear false positives, 3 lenient partial credits on gold lists (`conv-47/6-54` programming events lacks the online competition, `conv-30/1-23` omits the video presentation, `conv-49/8-81` stress relievers matched by approximation).
- Estimate: clear FP about 0.7% of the 1,145 correct (8) plus lenient list/paraphrase credit of roughly 1-3% (cat1 and cat3 only). **Over-credit about 0.8-2.3 points.**

**False negatives (marked wrong, should be correct).**
- read_fail: 29 of 182 (15.9%) by my reading (listed in section 2).
- Random 24 non-read_fail wrongs: 1 clear (`conv-49/8-32` "recurring frustration": answer lists "losing his keys ... every week" first) and 1 borderline (`conv-42/3-1` shared interests), i.e. about 4-8% of 211, or 8-17 questions.
- Estimate: 37-46 false negatives of 395 wrongs (9-12%). **Under-credit about 2.4-3.0 points.** Net bias after FP: the judge under-credits by about +0.5 to +2.2 points.

**Failure patterns (ranked by count in this run).**
1. *LoCoMo relative-date gold* ("The Friday before 14 August 2023", "The week before 25 August 2023", "The weekend of 24 June 2022"): 10 read_fail rows are a correct resolved absolute date marked wrong; the same wording flips in other runs. The prompt says "including relative forms that resolve to it" but the 9B model does the weekday arithmetic poorly.
2. *Hedge-then-answer*: the rule "WRONG if it says it does not know" fires on answers that also contain the gold (`conv-48/7-2` "passed away a few years ago", `conv-48/7-21` states 2023-01-28 then notes no completion date, `conv-48/7-171` Nintendo console, `conv-48/7-187` chocolate chip). Inconsistent with the 7 hedged answers it did credit.
3. *Superset / extra-item answers* are sometimes WRONG (`conv-41/2-130`, `conv-41/2-150` list contains all five gold items plus three more) and sometimes right.
4. *Refusals credited when the answer mentions nearby facts* (8 FPs above).
5. *Typos in gold or source*: `Xenoblade` vs `Xeonoblade`.
6. *Phrase sensitivity*: on 1,153 cross-run answer pairs that are at least 80% similar but not identical, 66 (5.7%) get different verdicts, i.e. an item-level flip probability of about 3% on borderline wording; identical text flips 6 of 3,066 pairs (0.2%).

## 4. Cross-run comparison (roff vs rjina) and run-to-run noise

**Contexts differ almost everywhere.** `retrieved_ids` are identical in only 1 of 1,540 questions between roff and rjina (14 for roff vs ebge, 0 for roff vs ejina-rjina; 490 for rjina vs ejina-rjina, which have different embedders but identical post-rerank contexts). Mean context: roff 1,571 tokens / 39.3 turns, rjina 672 tokens / 16.7 turns. Mean Jaccard of the turn sets 0.42. So "identical evidence" pairs have to be defined as "all gold evidence present in both": 1,184 questions.

On those 1,184: accuracy roff 85.5%, rjina 83.0%; refusal-like answers 101 vs 135; 71 questions roff right / rjina wrong, 42 the reverse.

Explanation of the 113 flips (gold fully present in both):

| flip direction | n | loser refuses | near-identical answers (judge sensitivity) | different content |
|---|---|---|---|---|
| roff right, rjina wrong | 71 | 40 | 8 | 23 |
| roff wrong, rjina right | 42 | 12 | 8 | 22 |

- The main driver is **context size**: shrinking the context from 39 to 17 turns removes the neighbour turns that give pronouns and references their antecedent, and the reader answers "I do not know" more often (+34 refusals when gold is present). Examples: `conv-50/9-111` Ratatouille (roff right, rjina "I do not know", context 243 tokens); `conv-41/2-62` "how many dogs adopted": roff "two (Coco and Shadow)", rjina refuses.
- Some rjina wins come from the reverse effect: with a short context the answer is short and clean (`conv-48/7-187` roff hedges, rjina "Chocolate chip cookies."; `conv-41/2-130` roff gives kickboxing plus weight training and is marked wrong, rjina gives weight training only). The verbose-hedge pattern of large contexts is itself a judge-FN source.
- **Prompt noise / order / truncation are not the cause.** Same prompt in all four runs; contexts are chronological in all runs; neither main run truncated a context.
- **Judge sensitivity** accounts for roughly 16 of the 113 flips (the same answer reworded): `conv-26/0-45` ("Friday, August 11, 2023" WRONG vs "Friday, 2023-08-11" CORRECT); `conv-50/9-127`; `conv-41/2-56` ("Last weekend [= 2023-08-05..2023-08-06]" WRONG vs "last weekend, which corresponds to ... 2023-08-05..2023-08-06" CORRECT).

**Noise floor.**
- Reader nondeterminism at temperature 0: among the 490 identical-context pairs (rjina vs ejina-rjina), 25 answers differ in text (5.1%), but only 2 verdicts differ (0.4%).
- Judge nondeterminism on identical text: 6 flips in 3,066 cross-run pairs (0.2%).
- Combined verdict noise is about 0.4-0.6% of questions, about 6-9 questions, about +-0.5 points.
- Sampling error for one run is about 1.1 points (binomial, n=1,540); for the paired roff-vs-rjina difference the SE is about 0.85 points (171 discordant pairs). The 3.1-point gap is about 3.7 SE; the bge-small vs Qwen3 gap (74.4 vs 71.9, 179 discordant pairs) is about 2.9 SE. Differences below about 1.5 points between single runs are not interpretable.
- A separate think-on vs think-off pair exists (`qa-q35-think-on/off`, 152 questions each: 59.2% vs 73.0%, with the think-on max_tokens 4096); on the 22 of those that are in our read_fail set the thinking run gets 4 right and the no-thinking run 0, so thinking is not obviously useless on reasoning-type failures, but the run is too small to conclude from.

## 5. Where the harness and engine already have a lever

- `--qa-prompt default` (used here) vs `dated`, `dated2` (said-vs-happened rule), `dated_infer` (INFER_RULE: infer "would/likely"), `dated_world` (world knowledge + latest value wins + do not confuse speakers), `dated_noabstain`, `dated3`/`evermemos_cot` (reasoning then `Answer:`), `routed` (temporal / inference / plain variants, ADR-054).
- Prior measurements recorded in ADR-054: `dated_infer` +1.3 on temporal and -0.3 elsewhere; `dated3` -3.6 (terse answers, 7x more refusals); `routed` predicted +0.5 to +0.7 but unmeasured; "prompt-only predictions have had the wrong sign before".
- Engine: `read.resolve_relative_dates` (on in this run), `read.relative_dates_anchored` (G13; renders "the Friday before 2023-07-15" to match LoCoMo gold; **off in this run**), `read.relative_week: preceding_7_days`, `read.render: dated` (the `[YYYY-MM-DD Day]` prefix; the harness adds its own `[YYYY-MM-DD]`), `read.order_by_time_for_ordering` (a no-op here: contexts are always chronological), `present_order`, `gap_markers`.
- Harness: `--verify-answer` (+1 judge-backend call per question, checks the answer is supported by the context).
- `temporal_resolve.annotate` explicitly does not resolve: bare weekdays ("on Friday"), bare "weekend", "the other day", "recently", "a few days ago" (anchored only), "for N years", "N months now".

## 6. Fix table with estimated gain

Estimates are in questions out of the 1,540 (1 q = 0.065 points). Ranges are low-high flips assuming the class is attacked by that fix alone; fixes overlap, so the total is not the sum. "Measured?" says whether a number exists.

| class (n) | fix | exists? | est. flips | notes / risk |
|---|---|---|---|---|
| JFN (29) | Judge prompt: (a) add worked LoCoMo examples of "The Friday/week/weekend before X"; (b) credit answers that contain every gold item despite extras or a hedge; (c) deterministic pre-judge: normalise dates, token-F1/containment of gold in answer -> CORRECT, refusal regex with no gold-token overlap -> WRONG | new code (`judge.py`, `judge_prompts.py`) | +18 to +26 gross; the refusal guard removes about 8-15 undeserved credits; **net +8 to +16** | fixes the audit's FP and FN together; changes the number reported, so re-judge old runs (`rejudge.py` exists) and report both |
| JFN date-style subset (10) | `read.relative_dates_anchored: true` so the context and answer say "the Friday before 2023-07-15", the gold's own form | exists (G13), unmeasured on this stack | 6 to 9 of the 10 | also helps DANC; engine-only, no extra call |
| GOLD (26) | Not a system fix. Report an adjusted metric: flag the 26 as `gold_error` (question speaker or date contradicts the dataset) and exclude or report separately; a lenient-speaker prompt rule ("if the named person did not say it but their conversation partner did, answer from that line") would pass about 4-8 of the 15 speaker cases | new flag in `failure_buckets.py`; prompt rule new | 0 for the system, +4 to +8 with the speaker rule | the speaker rule conflicts with cat5 abstention (wrong-speaker adversarial questions), so only enable for cat1-4 reporting |
| REF (22) | `--qa-prompt dated_noabstain` or `dated_infer`; or a **refusal retry**: when the answer matches the refusal regex and the context is non-empty, re-ask once with the no-abstain prompt and keep the second answer if it is not a refusal | prompts exist; retry is new code in the reader wrapper (like `verify.py`) | 8 to 13 | retry costs about 13% extra reader calls (226 refusal-like answers); risk is wrong guesses on true abstentions, which cat1-4 scoring does not penalise but cat5 would |
| INF (35) | (a) `dated_world` / `dated_infer` / `routed` (inference variant: "give the most plausible answer and say 'likely'"); (b) larger reader (27-32B class) for the world-knowledge half; (c) judge rule to accept "likely yes/no" forms | prompts exist; reader swap is a config change | prompts 8 to 14 (world 5-8, hypothetical 3-6); bigger reader adds 4 to 8 | hypothetical gold is subjective ("Likely no; since this one went badly") and only 3 of the 35 are right in any other run, so the ceiling is low; part of this class needs retrieval of the right extra turn |
| DARITH (16) | Extend `temporal_resolve` to cover duration phrases: "for N years" / "N years now" / "a month now" / "N months now" -> `[= since YYYY]` or `[= started ≈ YYYY-MM]` relative to the line date; for two-event questions ("how long between X and Y", "how many weeks") add a deterministic elapsed-time helper (planner extracts the two dated lines, code subtracts) and put the computed span into the context | new code in `temporal_resolve.py` + a `core/elapsed.py` helper; `core/query_shape.is_temporal` can route | 8 to 11 | the single-line duration cases (`conv-41/2-31`, `conv-47/6-4`, `conv-47/6-39`, `conv-42/3-8`, `conv-48/7-34`, `conv-48/7-134`, `conv-43/4-56`, `conv-47/6-15`) are about 8 and are cheap; two-event arithmetic (7) needs the helper |
| DANC (7) | Annotator patch: bare weekday ("on Friday" -> most recent such weekday up to and including the anchor), bare "weekend", "the other day" (anchored: "a few days before <d>"); `dated2` prompt for said-vs-happened | new rules in `temporal_resolve.py` (+ unit tests) | 4 to 6 | "a wrong resolution is worse than none" is the stated policy; "on Friday" with past-tense verb is safe |
| LIST (7) | Count/list prompt clause from `dated3` ("merge repeated mentions of the same event, count distinct events") applied only to `how many` / `what ... has` questions; optional event-dedup in the context builder (collapse lines with the same date + same lemma) | prompt clause exists inside `dated3` (not separable); extraction of the clause is new; event-dedup new | 2 to 3 | `dated3` as a whole lost 3.6 points; take only the counting clause |
| IMG (12) | Make captions first-class: store `[image: ...]` text as its own record or at least keep it as a separate sentence ("Photo: ..."); prompt line "image captions are evidence"; 3-4 are not answerable from stored text (`conv-42/3-92`, `conv-26/0-23`, `conv-26/0-26`) | deposit tweak new; prompt line new | 4 to 6 | check how the dataset loader renders `blip_caption` first |
| DIST (11) | Smaller, cleaner context for date questions (rerank helps here but hurts elsewhere: use it only for `is_temporal`); `--verify-answer` to reject answers whose date is not in the cited line; `dated2` | `--verify-answer` exists, per-shape rerank is new config | 3 to 5 | `verify` costs +1 call per question |
| DET (11) | "Quote the specific detail from the context rather than paraphrasing" clause (in `dated3`, extract it); larger reader; answer-length floor so that short answers still name the detail | clause new (separable form); reader swap config | 4 to 6 | 9 of the 11 are right in at least one other run, so this class is mostly noise a better reader/prompt removes |
| HOP (5) | Pre-resolve references in neighbour lines (coreference rewrite at deposit: "that book you recommended" -> keeps), or retrieve with the question's own entities as the second query; or raise the neighbour window for cat1 | planner exists (`gliner2_planner_eval.py` hints) but not wired for this | 1 to 2 | small class |
| CONF (1) | `present_order: recorded` / latest wins | exists | 0 to 1 | |

**Totals.** Gross (non-overlapping central estimates): JFN-net 12 + REF 10 + INF 12 + DARITH 9 + DANC 5 + LIST 2 + IMG 5 + DIST 4 + DET 5 + HOP 1 = about 65 q = **+4.2 points** (range 45-95 q, +2.9 to +6.0). That would put this retrieval at about 78.5% with the same 9B reader/judge, if the 26 gold errors are excluded from the denominator the baseline is 1,145/1,514 = 75.6% and the same fixes give about 79.9%.

**Order of work by gain per effort.**
1. Judge: pre-judge guards + date-gold examples + `relative_dates_anchored: true` (about +12 to +20 q, no model calls, only re-judging old runs).
2. Prompt: switch the reader from `default` to a dated/infer/no-abstain variant and add the refusal retry (about +15 to +25 q).
3. `temporal_resolve` duration and bare-weekday rules + elapsed helper (about +12 to +17 q).
4. Reader size for INF/DET (+8 to +14 q).
5. Caption records, counting clause, per-shape rerank (+8 to +14 q).

## 7. Caveats

- One annotator (this analysis); borderline calls (JFN vs DET, REF vs INF) could move 10-15 rows between classes without changing the ranking.
- Fix estimates are judgement calls from the labelled counts and from the "right in at least one other run" check (JFN 18 of 29, DET 9 of 11, REF 10 of 22, INF 3 of 35, DARITH 1 of 16, DANC 0 of 7); they are not measured. ADR-054 warns that prompt-only predictions have had the wrong sign before: treat every prompt line as a hypothesis to screen on the 182 + a random 300 correct (regression check), not a promise.
- The context is rebuilt from the dataset and engine rules, not read from the run; the logged sha256 could not be matched.
- Single run per arm, no repeated seeds: noise numbers come from the overlap between different runs, not repeated runs of the same arm.

## Appendix A. Classification of all 182 read_fail questions

Code key: INF, JFN, GOLD, REF, DARITH, IMG, DIST, DET, DANC, LIST, HOP, CONF (section 2).

| # | question id | cat | class | question | gold |
|---|---|---|---|---|---|
| 0 | conv-26/0-5 | cat2 | GOLD | When did Melanie run a charity race? | The sunday before 25 May 2023 |
| 1 | conv-26/0-22 | cat3 | INF | Would Caroline likely have Dr. Seuss books on her bookshelf? | Yes, since she collects classic children's bo |
| 2 | conv-26/0-23 | cat1 | IMG | What books has Melanie read? | "Nothing is Impossible", "Charlotte's Web" |
| 3 | conv-26/0-26 | cat2 | IMG | When did Melanie read the book "nothing is impossible"? | 2022 |
| 4 | conv-26/0-27 | cat3 | INF | Would Caroline pursue writing as a career option? | LIkely no; though she likes reading, she want |
| 5 | conv-26/0-29 | cat2 | JFN | When did Melanie go to the pottery workshop? | The Friday before 15 July 2023 |
| 6 | conv-26/0-33 | cat2 | DIST | When did Caroline go to a pride parade during the summer? | The week before 3 July 2023 |
| 7 | conv-26/0-44 | cat2 | REF | When is Melanie's daughter's birthday? | 13 August |
| 8 | conv-26/0-45 | cat2 | JFN | When did Caroline attend a pride parade in August? | The Friday before 14 August 2023 |
| 9 | conv-26/0-49 | cat2 | REF | When did Caroline and Melanie go to a pride fesetival together? | 2022 |
| 10 | conv-26/0-57 | cat2 | JFN | When did Caroline encounter people on a hike and have a negative experience? | The week before 25 August 2023 |
| 11 | conv-26/0-71 | cat1 | HOP | What book did Melanie read from Caroline's suggestion? | "Becoming Nicole" |
| 12 | conv-26/0-76 | cat1 | JFN | When did Melanie go on a hike after the roadtrip? | 19 October 2023 |
| 13 | conv-26/0-77 | cat3 | INF | Would Melanie go on another roadtrip soon? | Likely no; since this one went badly |
| 14 | conv-26/0-81 | cat3 | INF | Would Caroline want to move back to her home country soon? | No; she's in the process of adopting children |
| 15 | conv-26/0-94 | cat4 | GOLD | What is Melanie's hand-painted bowl a reminder of? | art and self-expression |
| 16 | conv-26/0-104 | cat4 | REF | What book did Caroline recommend to Melanie? | "Becoming Nicole" |
| 17 | conv-26/0-133 | cat4 | IMG | What precautionary sign did Melanie see at the café? | A sign stating that someone is not being able |
| 18 | conv-26/0-135 | cat4 | JFN | What setback did Melanie face in October 2023? | She got hurt and had to take a break from pot |
| 19 | conv-26/0-140 | cat4 | IMG | What did the posters at the poetry reading say? | "Trans Lives Matter" |
| 20 | conv-26/0-147 | cat4 | DET | How did Melanie feel after the accident? | Grateful and thankful for her family |
| 21 | conv-26/0-151 | cat4 | DIST | What did Melanie do after the road trip to relax? | Went on a nature walk or hike |
| 22 | conv-30/1-9 | cat1 | GOLD | Which city have both Jean and John visited? | Rome |
| 23 | conv-30/1-21 | cat2 | REF | When did Jon start reading "The Lean Startup"? | May, 2023 |
| 24 | conv-30/1-63 | cat4 | GOLD | What kind of professional experience did Gina get accepted for on May 23, 2023? | fashion internship |
| 25 | conv-30/1-73 | cat4 | DET | How does Gina describe the feeling that dance brings? | magical |
| 26 | conv-41/2-17 | cat3 | INF | What might John's degree be in? | Political science, Public administration, Pub |
| 27 | conv-41/2-31 | cat2 | DARITH | When did John get his dog Max? | In 2013 |
| 28 | conv-41/2-39 | cat3 | INF | Around which US holiday did Maria get into a car accident? | Independence Day |
| 29 | conv-41/2-56 | cat2 | JFN | When did John participate in a 5K charity run? | first weekend of August 2023 |
| 30 | conv-41/2-63 | cat2 | DARITH | How many weeks passed between Maria adopting Coco and Shadow? | two weeks |
| 31 | conv-41/2-68 | cat4 | GOLD | What type of workout class did Maria start doing in December 2023? | aerial yoga |
| 32 | conv-41/2-69 | cat4 | GOLD | What did Maria donate to a homeless shelter in December 2023? | old car |
| 33 | conv-41/2-85 | cat4 | DIST | What event did John volunteer at last weekend? | career fair at a local school |
| 34 | conv-41/2-99 | cat4 | JFN | What did John and the veterans do during the small party? | share stories and make connections |
| 35 | conv-41/2-105 | cat4 | DIST | How does John plan to honor the memories of his beloved pet? | By considering adopting a rescue dog |
| 36 | conv-41/2-120 | cat4 | JFN | What natural disaster affected John's old area on 7 July, 2023? | Flood |
| 37 | conv-41/2-125 | cat4 | JFN | What does John appreciate about the veteran's hospital visit? | the resilience of the veterans and their insp |
| 38 | conv-41/2-130 | cat4 | JFN | Which activity has John done apart from yoga at the studio? | weight training |
| 39 | conv-41/2-150 | cat4 | JFN | What activities does John's family enjoy doing together? | going for hikes, hanging out at the park, hav |
| 40 | conv-42/3-2 | cat2 | HOP | When did Joanna first watch "Eternal Sunshine of the Spotless Mind? | 2019 |
| 41 | conv-42/3-4 | cat3 | INF | What pets wouldn't cause any discomfort to Joanna? | Hairless cats or pigs,since they don't have f |
| 42 | conv-42/3-7 | cat2 | JFN | When did Joanna finish her first screenplay? | The Friday before 23January, 2022 |
| 43 | conv-42/3-8 | cat2 | DARITH | When did Nate get his first two turtles? | 2019 |
| 44 | conv-42/3-12 | cat3 | INF | What underlying condition might Joanna have based on her allergies? | asthma |
| 45 | conv-42/3-20 | cat2 | REF | When did Nate adopt Max? | May 2022 |
| 46 | conv-42/3-25 | cat2 | JFN | When did Joanna hike with her buddies? | The weekend after 3June, 2022. |
| 47 | conv-42/3-26 | cat2 | DIST | When did Nate win his third tourney? | The week before 3June, 2022 |
| 48 | conv-42/3-29 | cat2 | DIST | When is Joanna going to make Nate's ice cream for her family? | The weekend of 24June, 2022. |
| 49 | conv-42/3-31 | cat2 | DANC | When did Nate win his fourth video game tournament? | The Friday before 10July, 2022. |
| 50 | conv-42/3-35 | cat2 | JFN | When did Nate take time off to chill with his pets? | The weekend of 22August, 2022. |
| 51 | conv-42/3-41 | cat2 | HOP | When did Joanna make a chocolate tart with raspberries? | 5 October, 2022 |
| 52 | conv-42/3-43 | cat2 | DARITH | How long did it take for Joanna to finish writing her book? | four months |
| 53 | conv-42/3-52 | cat1 | LIST | How many of Joanna's writing have made it to the big screen? | two |
| 54 | conv-42/3-53 | cat1 | LIST | How many times has Nate taken his turtles on a walk? | Twice. |
| 55 | conv-42/3-60 | cat3 | INF | What Console does Nate own? | A Nintendo Switch; since the game "Xenoblade  |
| 56 | conv-42/3-73 | cat3 | INF | What state did Joanna visit in summer 2021? | Indiana |
| 57 | conv-42/3-85 | cat3 | INF | What kind of job is Joanna beginning to preform the duties of because of her mov | filmmaker. |
| 58 | conv-42/3-91 | cat4 | IMG | What is Nate's favorite book series about? | dragons |
| 59 | conv-42/3-92 | cat4 | IMG | What kind of lighting does Nate's gaming room have? | red and purple lighting |
| 60 | conv-42/3-95 | cat4 | REF | What is Nate's favorite video game? | Xenoblade Chronicles |
| 61 | conv-42/3-102 | cat4 | JFN | Which dairy-free dessert flavors does Nate enjoy? | chocolate and mixed berry |
| 62 | conv-42/3-113 | cat4 | DET | What is Nate's favorite genre of movies? | Fantasy and sci-fi |
| 63 | conv-42/3-116 | cat4 | DET | Which activity helps Nate escape and stimulates his imagination? | watching fantasy and sci-fi movies |
| 64 | conv-42/3-119 | cat4 | GOLD | What does Nate feel he could do when out in cool places like Whispering Falls? | write a whole movie |
| 65 | conv-42/3-140 | cat4 | JFN | What inspired Joanna's new script in July 2022? | Woodhaven's interesting past and people |
| 66 | conv-42/3-148 | cat4 | JFN | How did Nate feel about sharing his love for dairy-free desserts with Joanna? | Happy to share |
| 67 | conv-42/3-183 | cat4 | JFN | What game is Nate currently playing and recommends to others on November 7, 2022 | "Xenoblade Chronicles" |
| 68 | conv-43/4-6 | cat2 | GOLD | In which month's game did John achieve a career-high score in points? | June 2023 |
| 69 | conv-43/4-8 | cat3 | INF | Which outdoor gear company likely signed up John for an endorsement deal? | Under Armour |
| 70 | conv-43/4-15 | cat3 | INF | Who is Anthony? | likely John's friend, colleague or family |
| 71 | conv-43/4-28 | cat3 | INF | Which popular music composer's tunes does Tim enjoy playing on the piano? | John Williams |
| 72 | conv-43/4-32 | cat3 | INF | Which US states might Tim be in during September 2023 based on his plans of visi | California or Florida |
| 73 | conv-43/4-40 | cat2 | INF | Has Tim been to North Carolina and/or Tennesee states in the US? | Yes |
| 74 | conv-43/4-48 | cat1 | DIST | When did John get an ankle injury in 2023? | around November 16, 2023 |
| 75 | conv-43/4-51 | cat3 | INF | What kind of yoga for building core strength might John benefit from? | Hatha Yoga |
| 76 | conv-43/4-56 | cat2 | DARITH | When did Tim start playing the violin? | August 2023 |
| 77 | conv-43/4-60 | cat2 | GOLD | When did John achieve a career-high assist performance? | December 11, 2023 |
| 78 | conv-43/4-66 | cat3 | INF | What is a Star Wars book that Tim might enjoy? | Star Wars: Jedi Apprentice by Judy Blundell a |
| 79 | conv-43/4-68 | cat2 | DANC | What day did Tim get into his study abroad program? | Januarty 5, 2024 |
| 80 | conv-43/4-70 | cat3 | INF | Which Star Wars-related locations would Tim enjoy during his visit to Ireland? | Skellig Michael, Malin Head, Loop Head, Ceann |
| 81 | conv-43/4-74 | cat4 | GOLD | What aspects of the Harry Potter universe will be discussed in John's fan projec | characters, spells, magical creatures |
| 82 | conv-43/4-76 | cat4 | DET | What kind of picture did Tim share as part of their Harry Potter book collection | MinaLima's creation from the Harry Potter fil |
| 83 | conv-43/4-86 | cat4 | GOLD | What did John share with the person he skyped about? | Characters from Harry Potter |
| 84 | conv-43/4-106 | cat4 | DET | Where are John and his teammates planning to explore on a team trip? | a new city |
| 85 | conv-43/4-113 | cat4 | REF | Which basketball team does Tim support? | The Wolves |
| 86 | conv-43/4-117 | cat4 | DET | What motivates John's team to get better, according to John? | facing tough opponents |
| 87 | conv-43/4-128 | cat4 | REF | What type of meal does John often cook using a slow cooker? | honey garlic chicken with roasted veg |
| 88 | conv-43/4-135 | cat4 | GOLD | How does Tim stay motivated during difficult study sessions? | Visualizing goals and success |
| 89 | conv-43/4-136 | cat4 | GOLD | What did Tim say about his injury on 16 November, 2023? | The doctor said it's not too serious |
| 90 | conv-43/4-151 | cat4 | REF | What is the topic of discussion between John and Tim on 11 December, 2023? | Academic achievements and sports successes |
| 91 | conv-43/4-152 | cat4 | IMG | What kind of game did John have a career-high in assists in? | basketball |
| 92 | conv-43/4-164 | cat4 | GOLD | What language does Tim know besides German? | Spanish |
| 93 | conv-43/4-165 | cat4 | GOLD | What book did Tim get in Italy that inspired him to cook? | a cooking book |
| 94 | conv-44/5-5 | cat2 | GOLD | When did Audrey see a hummingbird? | first week of May 2023 |
| 95 | conv-44/5-7 | cat2 | DARITH | How many years passed between Audrey adopting Pixie and her other three dogs? | three years |
| 96 | conv-44/5-37 | cat2 | DIST | When did Audrey get into an accident in the park? | between October 19 and 24, 2023 |
| 97 | conv-44/5-41 | cat1 | CONF | What are the breeds of Audrey's dogs? | Mongrel mixed with Lab for Pepper and Panda.  |
| 98 | conv-44/5-57 | cat2 | DANC | When did Andrew adopt Scout? | few days before November 2023 |
| 99 | conv-44/5-59 | cat2 | DARITH | How long has it been since Andrew adopted his first pet, as of November 2023? | 4 months |
| 100 | conv-44/5-72 | cat4 | JFN | Why did Audrey think positive reinforcement training is important for pets? | To have pets learn how to behave in a positiv |
| 101 | conv-44/5-79 | cat4 | DIST | What is Audrey's favorite recipe that she shares with Andrew on 3 July, 2023? | Chicken Pot Pie |
| 102 | conv-47/6-0 | cat3 | INF | What are John's suspected health problems? | Obesity |
| 103 | conv-47/6-4 | cat2 | DARITH | When did John resume playing drums in his adulthood? | February 2022 |
| 104 | conv-47/6-7 | cat3 | INF | In which state is the shelter from which James adopted the puppy? | Connecticut. |
| 105 | conv-47/6-15 | cat2 | DARITH | When did James start playing Civilization VI? | March 2022 |
| 106 | conv-47/6-16 | cat3 | INF | What is the game with different colored cards that was John talking about with J | UNO |
| 107 | conv-47/6-17 | cat3 | INF | What is the board game where you have to find the imposter that John mentions to | Mafia |
| 108 | conv-47/6-20 | cat1 | LIST | How many charity tournaments has John organized till date? | two |
| 109 | conv-47/6-30 | cat3 | INF | Which country did James book tickets for in July 2022? | Canada |
| 110 | conv-47/6-31 | cat2 | GOLD | How many days did James plan to spend on his trip in Canada? | 19 days |
| 111 | conv-47/6-34 | cat1 | INF | Which countries did James visit in July 2022? | Canada, Greenland |
| 112 | conv-47/6-37 | cat2 | GOLD | When did John spend time with his sister and dogs? | July 21, 2022 |
| 113 | conv-47/6-39 | cat2 | DARITH | When did John start his job in IT? | 2019 |
| 114 | conv-47/6-61 | cat2 | GOLD | What was James' big moment with Samantha in October 2023? | They decided to live together and rented an a |
| 115 | conv-47/6-62 | cat2 | DARITH | How long did James and Samantha date for before deciding to move in together? | nearly three months |
| 116 | conv-47/6-64 | cat2 | DARITH | How long did John practice chess for before winning the chess tournament? | nearly four months |
| 117 | conv-47/6-65 | cat2 | DANC | When did James and his family visit Mark and Josh? | November 7, 2022 |
| 118 | conv-47/6-66 | cat2 | DANC | When did John work with a game developer on a project? | November 5-6, 2022 |
| 119 | conv-47/6-93 | cat4 | IMG | What did John receive for achieving second place in the tournament? | money and a trophy |
| 120 | conv-47/6-97 | cat4 | DET | What disagreement do James and John have about their football teams? | debating on which team will perform better in |
| 121 | conv-47/6-103 | cat4 | JFN | What new hobby did James become interested in on 9 July, 2022? | Extreme sports |
| 122 | conv-47/6-112 | cat4 | DET | What aspect of "The Witcher 3" does John find immersive? | shaping the world with choices |
| 123 | conv-47/6-147 | cat4 | DET | What did John suggest James practice before playing FIFA 23 together? | Control with a gamepad and timing |
| 124 | conv-48/7-2 | cat2 | JFN | When did Deborah`s mother pass away? | a few years before 2023 |
| 125 | conv-48/7-5 | cat3 | INF | In what country did Jolene's mother buy her the pendant? | In France |
| 126 | conv-48/7-18 | cat3 | INF | In what country did Jolene buy snake Seraphim? | In France |
| 127 | conv-48/7-21 | cat2 | JFN | When do Jolene and her partner plan to complete the game "Walking Dead"? | Saturday after 27 January, 2023 |
| 128 | conv-48/7-25 | cat4 | JFN | What are Jolene's favorite books? | Sapiens, Avalanche by Neal Stephenson |
| 129 | conv-48/7-28 | cat3 | INF | In what country was Jolene during summer 2022? | Colombia |
| 130 | conv-48/7-34 | cat2 | DARITH | Which year did Jolene and her partner start dating? | 2020 |
| 131 | conv-48/7-38 | cat2 | DANC | When did Deborah start the yoga class in the neighborhood? | Friday before 13 March, 2023 |
| 132 | conv-48/7-40 | cat3 | INF | Does Deborah live close to the beach or the mountains? | beach |
| 133 | conv-48/7-44 | cat2 | REF | When did Deborah go to an art show with Anna? | on 9 April, 2023 |
| 134 | conv-48/7-71 | cat2 | DANC | When did Deborah go to a community meetup? | last week of August 2023 |
| 135 | conv-48/7-75 | cat3 | INF | What card game is Deborah talking about? | Exploding Kittens |
| 136 | conv-48/7-77 | cat1 | HOP | Where did Jolene and her partner find a cool diving spot? | Phuket |
| 137 | conv-48/7-81 | cat2 | REF | When did the Deboran and Jolene agree to go surfing? | in October 2023 |
| 138 | conv-48/7-87 | cat1 | INF | Which countries has Deborah traveled to? | Thailand, Brazil |
| 139 | conv-48/7-90 | cat4 | JFN | What are Jolene's favorite books? | Sapiens, Avalanche by Neal Stephenson |
| 140 | conv-48/7-100 | cat4 | REF | What project did Jolene finish last week before 23 January, 2023? | an electrical engineering project |
| 141 | conv-48/7-108 | cat4 | REF | What new outlook did Jolene gain after her mini retreat on 9 February, 2023? | A confidence boost |
| 142 | conv-48/7-116 | cat4 | GOLD | According to Jolene, what does exercise help her to feel? | connected to her body |
| 143 | conv-48/7-123 | cat4 | GOLD | What did Jolene and Anna discuss while watching the sunset by the sea? | They realized they inspire each other |
| 144 | conv-48/7-134 | cat4 | DARITH | For how long has Jolene had Seraphim as a pet? | one year |
| 145 | conv-48/7-171 | cat4 | JFN | What was the video game console that Jolene's parents got her at age 10? | nintendo game console |
| 146 | conv-48/7-187 | cat4 | JFN | What kind of cookies did Jolene used to bake with someone close to her? | Chocolate chip cookies |
| 147 | conv-49/8-4 | cat2 | REF | Which hobby did Sam take up in May 2023? | painting |
| 148 | conv-49/8-17 | cat2 | JFN | When did Sam's friends mock him for being overweight? | Friday before 27 July 2023 |
| 149 | conv-49/8-24 | cat2 | GOLD | What significant event happened in Sam's life towards the end of summer 2023? | He fell in love with a Canadian woman |
| 150 | conv-49/8-27 | cat3 | INF | What electronic device could Evan gift Sam to help him keep up with his fitness  | fitness tracker |
| 151 | conv-49/8-39 | cat2 | DARITH | How many months lapsed between Sam's first and second doctor's appointment? | three months |
| 152 | conv-49/8-45 | cat2 | HOP | Which places in Canada was Evan visiting in July 2023? | Banff, Rocky Mountains |
| 153 | conv-49/8-57 | cat3 | INF | Which US state was Sam travelling in during October 2023? | California |
| 154 | conv-49/8-67 | cat2 | REF | When did Evan and Sam planned a trip to the beach together? | December, 2023 |
| 155 | conv-49/8-74 | cat2 | GOLD | When did Evan's son fall off his bike? | Thursday before December 17, 2023. |
| 156 | conv-49/8-80 | cat2 | GOLD | When did Evan have a drunken night with his friends? | January 9, 2023 |
| 157 | conv-49/8-83 | cat4 | REF | What type of car did Evan get after his old Prius broke down? | new Prius |
| 158 | conv-49/8-102 | cat4 | REF | What dish did Sam make on 18 August, 2023 that turned out flavorful? | grilled dish with salmon and vegetables |
| 159 | conv-49/8-106 | cat4 | IMG | What did Evan start painting years ago due to being inspired by a friend's gift? | forest scene |
| 160 | conv-49/8-108 | cat4 | IMG | What type of landscapes does Evan love painting the most? | sunsets over the ocean |
| 161 | conv-49/8-127 | cat4 | JFN | Why had Evan been going through a tough time lately? | Lost their job due to downsizing |
| 162 | conv-49/8-144 | cat4 | IMG | What did Evan share with Sam after their hiking trip? | a photo of a man standing on a rock looking o |
| 163 | conv-50/9-0 | cat2 | REF | When did Calvin first travel to Tokyo? | between 26 March and 20 April 2023 |
| 164 | conv-50/9-1 | cat1 | LIST | What items did Calvin buy in March 2023? | mansion in Japan, luxury car Ferrari 488 GTB |
| 165 | conv-50/9-7 | cat3 | INF | Does Dave's shop employ a lot of people? | Yes |
| 166 | conv-50/9-12 | cat2 | DIST | When was Calvin's concert in Tokyo? | last week of May 2023 |
| 167 | conv-50/9-13 | cat3 | INF | Would Calvin enjoy performing at the Hollywood Bowl? | Yes; because he enjoys the rush of performing |
| 168 | conv-50/9-28 | cat1 | LIST | What does Calvin do to relax? | take long drives in his car, embrace nature,  |
| 169 | conv-50/9-40 | cat1 | REF | What was the artists Calvin used to listen to when he was a kid? | Tupac and Dr. Dre |
| 170 | conv-50/9-47 | cat1 | LIST | How many car shows has Dave attended? | two |
| 171 | conv-50/9-50 | cat2 | REF | When did Calvin buy his second Ferrari? | first week of October 2023 |
| 172 | conv-50/9-54 | cat1 | LIST | What does help Calvin stay connected to the creative process? | Calvin stays connected to the creative proces |
| 173 | conv-50/9-61 | cat2 | DARITH | How long did Dave's work on the Ford Mustang take? | nearly two months |
| 174 | conv-50/9-91 | cat4 | REF | What car brand does Calvin own that he is proud of? | Ferrari |
| 175 | conv-50/9-109 | cat4 | DIST | What is Calvin excited about after the tour? | exploring and growing his brand |
| 176 | conv-50/9-113 | cat4 | DET | What activity did Calvin enjoy during his summer drives? | feeling the wind blowing through his hair |
| 177 | conv-50/9-115 | cat4 | IMG | What project did Calvin work on to chill out? | A shiny orange car |
| 178 | conv-50/9-127 | cat4 | JFN | What specific location in Tokyo does Calvin mention being excited to explore? | Shinjuku |
| 179 | conv-50/9-136 | cat4 | REF | When did Dave sell the car he restored last year? | Last year |
| 180 | conv-50/9-137 | cat4 | GOLD | When did Calvin first get interested in cars? | at an early age |
| 181 | conv-50/9-151 | cat4 | GOLD | What hobby did Calvin take up recently? | Photography |
