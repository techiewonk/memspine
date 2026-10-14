# Why gold evidence does not reach the reader on LoCoMo: forensic analysis of three full runs

> Licence note (2026-10-14): quoted benchmark text (questions, gold answers, turns) was removed from this file and replaced by `[omitted]`; ids, ranks and analysis are unchanged. The text-bearing original is kept locally, untracked, in `evals/runs/_analysis/legacy_text/`.

Scope: LoCoMo, 1,540 non-adversarial questions (single-hop 841, multi-hop 282, temporal 321, open-domain 96), branch `feat/local-qwen-stack`. Analysis only: nothing in the engine, harness or run folders was changed; no GPU, Ollama or eval run was used (only CPU re-embedding with Qwen3-Embedding-0.6B and a CPU BM25 for the offline what-if simulations).

Runs analysed (per-question stage logs, `evals/runs/<run>--memspine/report/per_question.jsonl`):

| label | run | config in one line | accuracy |
|---|---|---|---|
| **fix** | `qa-full-qs-eq06-fix` | grounded prompt, refusal retry, judge guards, replay window 2 before / 4 after, `relative_dates_anchored`, `temporal_leg_mentions`, no reranker | 80.1% |
| **base** | `qa-full-qs-eq06-roff-fx` | same engine, replay window +-2, no reranker | 74.5% |
| **rr** | `qa-full-qs-eq06-rq4b4-fx` | base + Qwen3-Reranker-4B (4-bit), pool = fused top-10 | 70.8% |

**Conventions.** MEASURED = read directly from the logged stage lists, or an exact replay of them (the fusion and the replay-window context were re-computed from the logged legs and reproduce the logged `fused` list and `retrieved_turns` in 1,540 / 1,540 questions for fix and for base, and the reranker context in 1,540 / 1,540 for rr). SIMULATED = an offline what-if built on those lists (fusion, window and budget logic as the engine; BM25 and embeddings re-computed on CPU; the CPU replica matches the logged vector top-30 at 97.8% and the BM25 top-30 at 97.1%, and its baseline is within +-3 questions of the real one). ESTIMATED = judgement extrapolating from measured numbers, always labelled. The cause labels in section 2 come from stated rules (heuristics); the ranks and counts under them are measured. 'Gold' = the dataset evidence turns; 8 gold ids of the 2,356 do not exist in the dataset and are dropped (2,348 gold turns remain). `conv-NN qid` are the run's own ids.

## 0. Summary

**Gold-turn funnel (fix run, 2348 gold turns).** 1404 (59.8%) are themselves one of the 10 fused hits; 427 (18.2%) reach the context only because the neighbour window of some other hit covers them (262 of those were in a leg's top-30 but outside the fused top-10, 165 were in no leg at all); **517 (22.0%) never reach the context: 280 lost at recall (in no leg's top-30) and 237 lost at fusion (in a leg's top-30, outside the fused top-10).** These 517 turns sit in 281 questions; 158 of them were answered wrong (94 primary gap recall + 64 fusion), the other 123 were answered correctly anyway (partial gold is often enough). The reader gap (148 questions: all gold in context, still wrong) is out of scope here.

**What the data says**

1. Retrieval loss is a *multi-evidence / indirect-evidence* problem, not a single-turn matching problem. Lost gold turns by question category: single-hop 31, multi-hop 347, open-domain 96, temporal 43. 87% of lost gold turns belong to questions with 2+ gold turns, 50% to questions with 4+.
2. Largest cause: **list / aggregation questions whose gold turns are instances of a category the question names generically** ("hobbies", "activities", "places", "books"): 164 lost turns (32% of the 517); 52% of all such gold turns are lost (22% overall). The turn says "kayaking" / "Valorant" / "Name of the Wind"; question and turn share no content word and the embedding does not bridge category to instance.
3. Next: **the gold turn does not literally contain the answer** (112 turns, 22%: implicit, paraphrased, or a weakly related / noisy gold label) and **open-domain inference** (96 turns, 19%: the evidence is an implication). About half of each is lost whatever the retrieval setting, and a 'perfect decomposition' oracle cannot target them.
4. Fusion loss (237 turns) is structural to RRF over a vector leg and an OR-of-every-token BM25 leg: 195 of 237 fusion-lost turns are in the vector leg only, 37 in BM25 only, and **only 1 in both**. The fused top-10 holds on average 5.9 two-leg items per question, 42% of which sit at vector rank 11 or worse (17% at 21-30), and 34% of BM25's own top-10 hits share *no content word* with the question. Re-weighting, rrf_k, reserved slots and the existing query-probe options do not fix it (sections 3 and 7); only a larger candidate pool does.
5. The neighbour window is already near its useful maximum: window 2 before / 4 after rescues 427 gold turns (+55 gold turns and +29 fully covered questions over the base run's symmetric +-2). Of the 517 lost turns, 388 (75%) sit in a session with no hit at all, so no window shape can reach them (section 5).
6. The reranker run loses accuracy not because it mis-scores gold (it keeps 97.4% of gold hits) but because pool=10 makes it a pure deleter: min-max normalisation plus `relative_floor 0.3` removes ~55% of the non-gold hits *together with their windows*; 74% of the gold turns lost to the reranker were window neighbours of a dropped hit (section 6).

**Headroom (fix run; questions with ALL gold in context; baseline 1,251 of 1,540; 158 questions are wrong with incomplete gold). SIMULATED, exact fusion / window / budget logic:**

| change | questions fully covered | of the 158 wrong+incomplete, newly covered | avg context tokens (est.) |
|---|---|---|---|
| baseline (fix) | 1,251 | 0 | 2,189 |
| `candidate_pool` x2 (pool 20), window 2/4, budget 4096 | 1,330 (+79) | 39 | 3,967 |
| pool 30, tiered window: ranks 1-10 window 2/4, ranks 11-30 window 0/1 (window-by-rank needs code) | 1,341 (+90) | 50 | 3,447 |
| pool 20, tiered window (2/4 then 0/1) | 1,308 (+57) | 32 | 2,812 |
| ORACLE reranker: best 10 of the fused top-30, window 2/4 (upper bound for pool 30 + a perfect reranker) | 1,332 (+81) | 43 | 2,177 |
| BM25 indexed like the engine `english` analyzer (stop words + stemming) and speaker names dropped from the query | 1,274 (+23) | 34 | 2,104 |

Reader accuracy given complete gold is 82.6% overall in the rr run (89% single-hop, 82% multi-hop, 72% temporal, 25% open-domain), so expected extra correct answers are about 0.8 x the newly covered wrong questions, ESTIMATED. Larger contexts may dilute the reader; the logs cannot show that (risk, section 8).

## 1. The funnel: where each gold turn ends up (MEASURED)

Gold-turn state per run. "hit" = gold turn is one of the 10 fused hits (rr: one of the hits that survive the floor); "window" = in context only through a neighbour window, split by what the turn was before (fusion: in a leg top-30; recall: in no leg top-30); "lost" = not in the context.

| run | gold turns | hit | window (was fusion-lost) | window (was recall-lost) | lost at fusion | lost at recall | lost at assembly (floor) | in context |
|---|---|---|---|---|---|---|---|---|
| base (+-2 window) | 2348 | 1401 | 231 | 142 | 267 | 307 | 0 | 75.6% |
| fix (2 before / 4 after) | 2348 | 1404 | 262 | 165 | 237 | 280 | 0 | 78.0% |
| rr (reranker, +-2) | 2348 | 1383 | 196 | 125 | 302 | 324 | 18 | 72.6% |

By question category (fix run, gold turns):

| category | gold turns | hit | window only | lost at fusion | lost at recall |
|---|---|---|---|---|---|
| single-hop | 895 | 704 (79%) | 160 (18%) | 21 (2%) | 10 (1%) |
| multi-hop | 882 | 355 (40%) | 180 (20%) | 171 (19%) | 176 (20%) |
| temporal | 374 | 284 (76%) | 47 (13%) | 29 (8%) | 14 (4%) |
| open-domain | 197 | 61 (31%) | 40 (20%) | 16 (8%) | 80 (41%) |

Question level (all gold of the question in the stage): fix run funnel from `run_summary.json`, plus primary-gap counts (earliest stage that lost a gold turn):

| category | questions | all gold in some leg top-30 | all gold in fused top-10 | all gold in context | accuracy | gap: recall | gap: fusion | gap: reader |
|---|---|---|---|---|---|---|---|---|
| single-hop | 841 | 94% | 78% | 96% | 91% | 9 | 12 | 56 |
| multi-hop | 282 | 50% | 22% | 38% | 63% | 50 | 30 | 25 |
| temporal | 321 | 90% | 78% | 88% | 77% | 10 | 19 | 46 |
| open-domain | 92 | 50% | 34% | 53% | 47% | 25 | 3 | 21 |

Base vs fix: the vector and BM25 lists are identical in all 1,540 questions (same embedder, same index); the fused top-10 changed in 76 questions (the new date-mention leg, 187 questions now carry a temporal leg vs 169), moving 4 gold turns into the hits. Almost the whole retrieval gain of the fix run is the 2/4 window: 57 fewer lost gold turns (55 turns newly covered by the window: 31 that were fusion-lost and 24 that were recall-lost; the changed hits add a net 2).

## 2. Task 1: why gold turns are lost (517 turns, fix run)

### 2.1 How turns are classified

Every lost turn gets exactly one cause by the first rule that matches (rules are heuristics over tokens; "content words" exclude stop words and the two speaker names; "stem" uses a Porter stemmer; "answer tokens" are the content words of the gold answer string):

1. **T** question category is temporal.
2. **I** question category is open-domain.
3. **N** (non-temporal, non-open) none of the gold-answer tokens occurs in the turn or its image caption: the answer is not literally there.
4. **H** the question shares no stemmed content word with the turn, the answer tokens are in the turn, and the question has 2+ gold turns (category -> instance). (If only an adjacent turn carries the question words it is still counted here: adjacency is a tag, below.)
5. **P** same as H but a single gold turn.
6. **M** the question shares a stemmed but not an exact content word with the turn.
7. **O** remaining: the turn shares an exact content word with the question and is still out-ranked.

### 2.2 Summary of causes

| cause | lost turns | lost at recall | lost at fusion | share | lost-rate (all gold turns with this label) | by category sh / mh / tp / od | questions (dominant cause), all incomplete | ... of which wrong (158) |
|---|---|---|---|---|---|---|---|---|
| **H** Category-to-instance gap (list / aggregation question) | 164 | 95 | 69 | 32% | 52% of 315 | 0 / 164 / 0 / 0 | 94 | 47 |
| **N** Answer not literally in the gold turn (paraphrase / implicit / noisy label) | 112 | 64 | 48 | 22% | 35% of 323 | 3 / 109 / 0 / 0 | 54 | 28 |
| **I** Open-domain inference (evidence is an implication) | 96 | 80 | 16 | 19% | 49% of 197 | 0 / 0 / 0 / 96 | 40 | 28 |
| **T** Temporal / date-dependent evidence | 43 | 14 | 29 | 8% | 11% of 374 | 0 / 0 / 43 / 0 | 36 | 29 |
| **O** Lexical overlap exists but the turn is outranked or crowded out | 63 | 13 | 50 | 12% | 7% of 900 | 15 / 48 / 0 / 0 | 31 | 11 |
| **M** Morphology-only overlap (BM25 without stemming misses it) | 27 | 9 | 18 | 5% | 25% of 108 | 2 / 25 / 0 / 0 | 14 | 5 |
| **P** Single-hop paraphrase gap | 12 | 5 | 7 | 2% | 9% of 131 | 11 / 1 / 0 / 0 | 12 | 9 |
| **all** | 517 | 280 | 237 | 100% | 22% of 2348 | 31 / 347 / 43 / 96 | 281 | 157 |

Question-level attribution = the most frequent cause among the question's lost turns (ties go to the earlier letter in the table order).

Non-exclusive tags measured over all 2,348 gold turns (lift = lost-rate with tag / overall lost-rate 22.0%; tags that discriminate are bold):

| tag | gold turns | lost | lost-rate | lift | share of the 517 lost |
|---|---|---|---|---|---|
| **no stemmed content-word overlap with the question** (vocabulary mismatch) | 793 | 353 | 45% | 2.02 | 68% |
| no exact content-word overlap (BM25 cannot match at all) | 954 | 404 | 42% | 1.92 | 78% |
| stem-only overlap (BM25 without stemming misses it) | 161 | 51 | 32% | 1.44 | 10% |
| **question has 2+ gold turns** | 1226 | 451 | 37% | 1.67 | 87% |
| **question has 4+ gold turns** | 527 | 260 | 49% | 2.24 | 50% |
| **gold of the question spans 2+ sessions** | 1048 | 434 | 41% | 1.88 | 84% |
| **gold answer tokens not in the turn** (non-temporal) | 447 | 174 | 39% | 1.77 | 34% |
| **short backchannel / anaphoric turn** (<=14 words, "yeah", "that", "it" opener) | 59 | 21 | 36% | 1.62 | 4% |
| turn carries an image caption | 904 | 209 | 23% | 1.05 | 40% |
| answer tokens ONLY in the caption | 67 | 15 | 22% | 1.02 | 3% |
| question overlap ONLY via the caption | 55 | 12 | 22% | 0.99 | 2% |
| relative-time phrase in the turn ("yesterday", "last week") | 752 | 159 | 21% | 0.96 | 31% |
| question names exactly one person, turn spoken by the other | 83 | 19 | 23% | 1.04 | 4% |
| question names exactly one person, turn spoken by that person | 1963 | 406 | 21% | 0.94 | 79% |
| question names both or no speaker | 302 | 92 | 30% | 1.38 | 18% |
| an adjacent turn (+-1) holds the question words | 1289 | 157 | 12% | 0.55 | 30% |

Reading: vocabulary mismatch, many gold turns per question, cross-session evidence and "answer not literally there" are what predict loss. **Image captions, relative-time phrases and speaker identity do not** (lift ~1.0): the image-caption class of the earlier simulation (RETRIEVAL_GAPS C1) is a label, not a cause. Evidence that exists *only* in the caption is 3% of the lost turns (15). Speaker confusion (Caroline vs Melanie) is also not a driver: the lost rate for a turn spoken by the non-named person is 23% vs 22% overall.

### 2.3 Evidence per cause, with examples

Rank notation: `vec 14 (cos 0.60)` = rank and cosine in the logged vector top-30; `vec ~127` = rank among *all* turns of the conversation from the CPU replica (the turn is outside the logged top-30; cos shown with the 30th-place cosine); `BM25 ~242` likewise from the CPU BM25; "own RRF" is the turn's fusion score (normalised like the log) against the score of the 10th fused item.

#### H. Category-to-instance gap: 164 lost turns (95 recall, 69 fusion), all multi-hop

MEASURED. The question asks for a set ("What kind of hobbies does Evan pursue?", "[omitted]") and each gold turn names one member of the set. 52% of such gold turns are lost. Median full-conversation vector rank of the recall-lost ones is 110 (conversations have 300-700 turns); only 23 of 95 are within rank 60. In 87 of 164 cases (53%) another gold turn of the same question *is* a hit: the query reaches one or two instances and not the rest. Worst questions: "What kind of hobbies does Evan pursue?" (8 gold turns), "What things has Nate recommended to Joanna?" (6), "[omitted]" (7), "What new hobbies did Sam consider trying?" (10).

- **conv-44 5-35** (multi-hop, lost at recall, 3 gold turns). Q: "What are the names of Andrew's dogs?" Gold answer: "Toby, Scout, Buddy".
  - gold D28:8 (session 28): "[omitted]"
  - ranks: vec ~72 (cos 0.51; 30th = 0.56); BM25 ~610; in no leg top-30.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-50 9-29** (multi-hop, lost at recall, 4 gold turns). Q: "What are Dave's hobbies other than fixing cars?" Gold answer: "[omitted]".
  - gold D8:8 (session 8): "[omitted]"
  - ranks: vec ~182 (cos 0.47; 30th = 0.58); BM25 ~289; in no leg top-30.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-42 3-30** (multi-hop, lost at recall, 4 gold turns). Q: "What kind of writings does Joanna do?" Gold answer: "Screenplays,books, online blog posts, journal".
  - gold D2:3 (session 2): "[omitted]"
  - ranks: vec ~37 (cos 0.54; 30th = 0.55); BM25 ~194; in no leg top-30.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-26 0-61** (multi-hop, lost at fusion, 2 gold turns). Q: "What musical artists/bands has Melanie seen?" Gold answer: "Summer Sounds, Matt Patterson".
  - gold D11:3 (session 11): "[omitted]"
  - ranks: vec 14 (cos 0.51); BM25 ~52; own RRF 0.41 vs 10th fused 0.48.
  - out-ranked by: "[omitted]"; "Melanie: Cool! What type of music do you play?".
- **conv-49 8-50** (multi-hop, lost at fusion, 8 gold turns). Q: "What kind of hobbies does Evan pursue?" Gold answer: "[omitted]".
  - gold D8:30 (session 8): "[omitted]"
  - ranks: vec 4 (cos 0.64); BM25 ~266; own RRF 0.48 vs 10th fused 0.48.
  - out-ranked by: "Evan: What other hobbies have you found for yourself?"; "[omitted]".

#### N. Answer not literally in the gold turn: 112 lost turns (64 recall, 48 fusion), 109 multi-hop

MEASURED + judgement. None of the gold-answer tokens occurs in the turn or its caption (lost-rate 35% of such turns). Three sub-types are mixed here and cannot be separated mechanically: (a) the turn implies the answer in other words ("lost a friend" for a family-members-who-died question), (b) the answer lives in the *previous* turn and the gold turn is its reply ("Thanks! The turtles might be small..."), (c) the gold label is only weakly related (e.g. a pottery question whose gold turn is "I appreciate our friendship too, Caroline"; a 'How long did it take Jon to open his studio' question whose gold is "[omitted]"). I did not hand-label these, so the share of type (c) is not quantified; it is visibly non-trivial in the examples below. This is the part of the problem that retrieval engineering reaches least.

- **conv-49 8-11** (multi-hop, lost at recall, 19 gold turns). Q: "[omitted]" Gold answer: "Weight problem".
  - gold D16:3 (session 16): "[omitted]"
  - ranks: vec ~44 (cos 0.52; 30th = 0.55); BM25 ~105; in no leg top-30.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-44 5-3** (multi-hop, lost at recall, 8 gold turns). Q: "[omitted]" Gold answer: "[omitted]".
  - gold D23:3 (session 23): "[omitted]"
  - ranks: vec ~82 (cos 0.46; 30th = 0.50); BM25 ~74; in no leg top-30.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-48 7-19** (multi-hop, lost at recall, 2 gold turns). Q: "How many times has Jolene been to France?" Gold answer: "two times".
  - gold D1:8 (session 1): "[omitted]"
  - ranks: vec ~57 (cos 0.49; 30th = 0.51); BM25 ~261; in no leg top-30.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-26 0-7** (multi-hop, lost at fusion, 2 gold turns). Q: "What is Caroline's relationship status?" Gold answer: "Single".
  - gold D3:13 (session 3): "[omitted]"
  - ranks: vec 26 (cos 0.53); BM25 ~382; own RRF 0.35 vs 10th fused 0.48.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-43 4-50** (multi-hop, lost at fusion, 2 gold turns). Q: "[omitted]" Gold answer: "The Alchemist".
  - gold D18:2 (session 18): "[omitted]"
  - ranks: vec ~54 (cos 0.40; 30th = 0.43); BM25 30; own RRF 0.34 vs 10th fused 0.50.
  - out-ranked by: "[omitted]"; "[omitted]".

#### I. Open-domain inference: 96 lost turns (80 recall, 16 fusion)

MEASURED. Of 197 open-domain gold turns 96 are lost (49%), 83% of the lost ones at recall; only 55% of open-domain questions have all gold in context and, with all gold in context, the reader is right on 25% (rr run oracle). The questions are 'Would X ...', 'What might ...', 'How old is Jolene?': the evidence is a set of weakly related turns (studies, finals, an internship for age) and no single turn answers. Typical example below.

- **conv-44 5-53** (open-domain, lost at recall, 4 gold turns). Q: "[omitted]" Gold answer: "[omitted]".
  - gold D3:1 (session 3): "[omitted]"
  - ranks: vec ~138 (cos 0.47; 30th = 0.56); BM25 ~60; in no leg top-30.
  - out-ranked by: "Andrew: No, no pets right now. But I do love animals."; "[omitted]".
- **conv-48 7-36** (open-domain, lost at recall, 10 gold turns). Q: "How old is Jolene?" Gold answer: "likely no more than 30; since she's in school".
  - gold D24:2 (session 24): "[omitted]"
  - ranks: vec ~332 (cos 0.45; 30th = 0.60); BM25 ~132; in no leg top-30.
  - out-ranked by: "Jolene: How old is Luna?"; "Jolene: How did you two meet?".
- **conv-26 0-69** (open-domain, lost at fusion, 3 gold turns). Q: "[omitted]" Gold answer: "Thoughtful, authentic, driven".
  - gold D7:4 (session 7): "[omitted]"
  - ranks: vec 17 (cos 0.65); BM25 ~59; own RRF 0.40 vs 10th fused 0.48.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-26 0-59** (open-domain, lost at fusion, 2 gold turns). Q: "Would Caroline be considered religious?" Gold answer: "Somewhat, but not extremely religious".
  - gold D14:19 (session 14): "[omitted]"
  - ranks: vec 12 (cos 0.47); BM25 ~305; own RRF 0.42 vs 10th fused 0.48.
  - out-ranked by: "[omitted]"; "[omitted]".

#### T. Temporal / date-dependent evidence: 43 lost turns (14 recall, 29 fusion)

MEASURED. Only 43 of 374 temporal gold turns are lost (11%), the best category after single-hop. The loss is concentrated in questions that name an explicit date or month (18 of 57 such gold turns lost = 32%, vs 8% when the question has no explicit date): the gold turn is a relative statement ("Yesterday I met artists in Boston" said on 4 Oct) whose own text has no date, so a date in the question has nothing to match; 26 of the 43 lost turns contain a relative-time phrase. Only 187 questions had a temporal leg in the fix run (the leg fires only for absolute dates). Several lost turns are the second or third gold turn of a comparison question ("Which pet did Jolene adopt first": 21 of 43 belong to 2+ gold questions).

- **conv-50 9-57** (temporal, lost at recall, 1 gold turn). Q: "Which hobby did Dave pick up in October 2023?" Gold answer: "photography".
  - gold D27:2 (session 27): "[omitted]"
  - ranks: vec ~63 (cos 0.50; 30th = 0.52); BM25 ~457; in no leg top-30.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-43 4-20** (temporal, lost at recall, 4 gold turns). Q: "[omitted]" Gold answer: "Seattle".
  - gold D5:2 (session 5): "[omitted]"
  - ranks: vec ~201 (cos 0.32; 30th = 0.42); BM25 ~84; in no leg top-30.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-50 9-68** (temporal, lost at fusion, 1 gold turn). Q: "[omitted]" Gold answer: "last week of October 2023".
  - gold D28:20 (session 28): "[omitted]"
  - ranks: vec 8 (cos 0.60); BM25 ~134; own RRF 0.45 vs 10th fused 0.50.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-50 9-31** (temporal, lost at fusion, 2 gold turns). Q: "[omitted]" Gold answer: "San Francisco".
  - gold D14:1 (session 14): "[omitted]"
  - ranks: vec ~31 (cos 0.52; 30th = 0.52); BM25 ~38; own RRF 0.33 vs 10th fused 0.33.
  - out-ranked by: "[omitted]"; "[omitted]".

#### O. Overlap present but out-ranked / crowded out: 63 lost turns (13 recall, 50 fusion)

MEASURED. These turns do share a content word with the question, so they are the 'fair fight' cases: 50 of 63 are lost at fusion, in one leg only. For the fusion-lost ones the median own RRF score is 0.43 against 0.48 for the 10th fused item (a one-leg item at vector or BM25 rank ~8-25 cannot beat items present in both legs). They are one to a few slots from the cut; section 7 gives per-cause recovery under a larger pool.

- **conv-50 9-152** (single-hop, lost at recall, 1 gold turn). Q: "What new item did Dave buy recently?" Gold answer: "A vintage camera".
  - gold D30:5 (session 30): "[omitted]"
  - ranks: vec ~201 (cos 0.43; 30th = 0.50); BM25 ~100; in no leg top-30.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-44 5-15** (multi-hop, lost at recall, 2 gold turns). Q: "[omitted]" Gold answer: "Not being able to find pet friendly spots.".
  - gold D7:8 (session 7): "[omitted]"
  - ranks: vec ~139 (cos 0.52; 30th = 0.58); BM25 ~197; in no leg top-30.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-41 2-42** (multi-hop, lost at fusion, 2 gold turns). Q: "What area was hit by a flood?" Gold answer: "West County".
  - gold D14:21 (session 14): "[omitted]"
  - ranks: vec ~141 (cos 0.28; 30th = 0.34); BM25 10; own RRF 0.44 vs 10th fused 0.48.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-50 9-20** (multi-hop, lost at fusion, 3 gold turns). Q: "[omitted]" Gold answer: "His Dad".
  - gold D12:2 (session 12): "[omitted]"
  - ranks: vec 20 (cos 0.57); BM25 ~45; own RRF 0.38 vs 10th fused 0.46.
  - out-ranked by: "[omitted]"; "[omitted]".

#### M. Morphology-only overlap: 27 lost turns (9 recall, 18 fusion)

MEASURED. The question shares only a stem with the turn (research / researching, camp / camping, endorsement / endorsements, dog / dogs, paint / painting). BM25 without stemming scores zero, so the turn is in the vector leg only and loses fusion to two-leg items. A simulation with Porter stemming (section 7) recovers 9 net questions; this is a small, cheap cause.

- **conv-47 6-8** (multi-hop, lost at recall, 3 gold turns). Q: "How many pets does James have?" Gold answer: "Three dogs.".
  - gold D1:14 (session 1): "[omitted]"
  - ranks: vec ~31 (cos 0.51; 30th = 0.52); BM25 ~433; in no leg top-30.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-50 9-5** (multi-hop, lost at recall, 3 gold turns). Q: "What are Dave's dreams?" Gold answer: "[omitted]".
  - gold D5:5 (session 5): "[omitted]"
  - ranks: vec ~81 (cos 0.50; 30th = 0.53); BM25 ~265; in no leg top-30.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-48 7-1** (multi-hop, lost at fusion, 3 gold turns). Q: "[omitted]" Gold answer: "mother, father, her friend Karlie".
  - gold D6:4 (session 6): "[omitted]"
  - ranks: vec 12 (cos 0.57); BM25 ~429; own RRF 0.42 vs 10th fused 0.83.
  - out-ranked by: "[omitted]"; "[omitted]".

#### P. Single-hop paraphrase gap: 12 lost turns

MEASURED. Rare: only 12 single-hop-style gold turns are lost this way ("What injury did Evan suffer from in August 2023?" -> "Twisted my knee last Friday": the question word 'injury' never appears, the embedding finds other injury turns first).

- **conv-49 8-110** (single-hop, lost at recall, 1 gold turn). Q: "What injury did Evan suffer from in August 2023?" Gold answer: "Twisted knee".
  - gold D9:2 (session 9): "[omitted]"
  - ranks: vec ~41 (cos 0.48; 30th = 0.50); BM25 ~425; in no leg top-30.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-26 0-4** (multi-hop, lost at recall, 1 gold turn). Q: "What is Caroline's identity?" Gold answer: "Transgender woman".
  - gold D1:5 (session 1): "[omitted]"
  - ranks: vec ~189 (cos 0.42; 30th = 0.50); BM25 ~353; in no leg top-30.
  - out-ranked by: "[omitted]"; "[omitted]".
- **conv-42 3-122** (single-hop, lost at fusion, 1 gold turn). Q: "What did Nate do for Joanna on 25 May, 2022?" Gold answer: "get her a stuffed animal".
  - gold D13:9 (session 13): "[omitted]"
  - ranks: vec ~93 (cos 0.65; 30th = 0.69); BM25 ~69; own RRF 0.29 vs 10th fused 0.32.
  - out-ranked by: "[omitted]"; "Nate: Can't wait to see it, Joanna! I'm here to support you.".

### 2.4 Recall-lost turns: how far outside the legs are they?

MEASURED on the CPU replica (full ranking of all turns in the conversation; the replica's top-30 agrees with the logged one at 97.8%, so ranks 31-40 below can be bf16 noise). The 280 recall-lost turns:

| full-conversation cosine rank of the gold turn | turns | share |
|---|---|---|
| <=30 (replica/bf16 noise) | 2 | 1% |
| 31-60 | 66 | 24% |
| 61-100 | 52 | 19% |
| 101-200 | 71 | 25% |
| >200 | 89 | 32% |

Median vector rank 126, p25 62, p75 229 (conversations hold 300-700 turns). Cosine gap to the 30th logged hit: median 0.067; 111 of 280 within 0.05, 39 within 0.02. BM25 (full ranking): median rank 242; best of vector and BM25 within 60 for only 84 turns (30%), within 100 for 146 (52%). **Widening the legs (fetch 30 -> 60 or 100) would reach at most a quarter to a half of them and would then have to survive fusion; the rest need a different retrieval behaviour (decomposition, derived facts, entity / session routing) or are not retrievable by question similarity at all (causes N, I).**

Per cause, recall-lost turns (MEASURED, CPU replica): median full vector rank and share within rank 60:

| cause | recall-lost turns | median vector rank | within vector rank 60 | median BM25 rank |
|---|---|---|---|---|
| H | 95 | 110 | 24% | 285 |
| N | 64 | 102 | 36% | 196 |
| I | 80 | 180 | 10% | 248 |
| T | 14 | 132 | 21% | 264 |
| O | 13 | 60 | 54% | 77 |
| M | 9 | 81 | 22% | 313 |
| P | 5 | 147 | 40% | 381 |

### 2.5 Fusion-lost turns: margin and mechanism

MEASURED. 237 gold turns are in a leg's top-30 and outside the fused top-10. Leg membership: vector only 195, BM25 only 37, both 1, in the temporal leg 4.

| leg rank of the gold turn | 1-5 | 6-10 | 11-15 | 16-20 | 21-25 | 26-30 |
|---|---|---|---|---|---|---|
| vector (n=196) | 22 | 42 | 44 | 40 | 24 | 24 |
| BM25 (n=38) | 11 | 9 | 7 | 2 | 0 | 9 |

RRF margin (normalised fusion score of the 10th fused item minus the gold turn's own score): median 0.072, p25 0.037, p75 0.120; typical 10th score 0.48, typical gold turn 0.41. That is the gap between 'one leg at rank ~12' and 'both legs at ranks ~15'.

**Why single-leg items lose.** RRF with k=60 scores a one-leg item at rank r as 1/(60+r) (at most 0.0164), and a two-leg item at ranks (a, b) as 1/(60+a)+1/(60+b). An item at rank 28 in *both* legs (0.0227) already beats the vector leg's rank-1 item (0.0164) when that item is absent from BM25. The BM25 leg is an OR of every question token with no stop words and no stemming, so it matches common words ("did", "the", "what", the two speaker names) and puts 28th-ranked noise into two-leg status.

MEASURED noise: of all BM25 top-10 hits over the 1,540 questions, 5230 of 15400 (34%) share *no* content word with the question (they match on stop words / names only). In the fused top-10, 2152 of the 13997 non-gold slots (15%) are such BM25-only noise items sitting outside the vector top-10; 947 questions (61%) have at least one, 1.4 on average.

Examples where a high-ranked gold turn lost to lower-ranked two-leg items (fused top-10 listed as `(turn, vector rank, BM25 rank)`):

- **conv-43 4-50** Q: "[omitted]". Gold D19:20 ("[omitted]"The Alchemist"[omitted]"): vector rank 3, not in BM25 top-30. Fused top-10: (19:6,2,1), (4:10,8,6), (22:10,14,8), (18:13,26,2), (27:4,24,5), (5:12,9,23), (22:14,18,16), (17:2,11,26), (11:23,17,21), (11:25,1,-).
- **conv-48 7-84** Q: "[omitted]". Gold D17:10 ("[omitted]"): vector rank 2, not in BM25 top-30. Fused top-10: (13:7,7,3), (13:9,13,1), (13:1,3,16), (22:6,17,4), (22:10,20,9), (22:8,14,15), (7:9,11,21), (14:13,29,6), (3:1,9,30), (7:1,21,27).
- **conv-49 8-25** Q: "[omitted]". Gold D5:7 ("[omitted]"): vector rank 2, not in BM25 top-30. Fused top-10: (12:2,7,10), (7:11,17,2), (14:1,15,6), (17:4,8,19), (17:2,9,21), (11:2,23,12), (7:2,10,27), (25:2,22,16), (17:5,27,15), (7:1,24,23).
- **conv-49 8-37** Q: "[omitted]". Gold D8:1 ("[omitted]"): vector rank 3, not in BM25 top-30. Fused top-10: (8:6,2,2), (14:1,7,3), (11:1,14,10), (17:5,22,5), (10:2,12,14), (4:6,16,13), (7:3,18,15), (17:3,20,17), (7:2,11,30), (3:1,28,19).

## 3. Task 4: which leg is weak; does RRF lose items a leg ranked high?

Gold turns (fix run) by leg membership (MEASURED; "either" includes the temporal leg):

| category | gold turns | in vector top-30 | in BM25 top-30 | in either | in both | vector only | BM25 only | vector rank<=10 | BM25 rank<=10 | fused hit |
|---|---|---|---|---|---|---|---|---|---|---|
| all | 2348 | 1766 (75%) | 1235 (53%) | 1903 (81%) | 1121 (48%) | 645 (27%) | 114 (5%) | 1405 (60%) | 956 (41%) | 1404 (60%) |
| single-hop | 895 | 769 (86%) | 641 (72%) | 840 (94%) | 586 (65%) | 183 (20%) | 55 (6%) | 669 (75%) | 533 (60%) | 704 (79%) |
| multi-hop | 882 | 586 (66%) | 287 (33%) | 626 (71%) | 247 (28%) | 339 (38%) | 40 (5%) | 386 (44%) | 171 (19%) | 355 (40%) |
| temporal | 374 | 325 (87%) | 257 (69%) | 341 (91%) | 247 (66%) | 78 (21%) | 10 (3%) | 287 (77%) | 217 (58%) | 284 (76%) |
| open-domain | 197 | 86 (44%) | 50 (25%) | 96 (49%) | 41 (21%) | 45 (23%) | 9 (5%) | 63 (32%) | 35 (18%) | 61 (31%) |

Question level, ALL gold turns of the question present:

| category | questions | all gold in vector top-30 | all in BM25 top-30 | all in either | all fused hits | all in context |
|---|---|---|---|---|---|---|
| single-hop | 841 | 721 (86%) | 603 (72%) | 790 (94%) | 658 (78%) | 810 (96%) |
| multi-hop | 282 | 124 (44%) | 36 (13%) | 141 (50%) | 62 (22%) | 108 (38%) |
| temporal | 320 | 277 (87%) | 220 (69%) | 290 (91%) | 249 (78%) | 284 (89%) |
| open-domain | 89 | 42 (47%) | 22 (25%) | 46 (52%) | 31 (35%) | 49 (55%) |

Reading. **The vector leg is the strong leg in every category** (75% of all gold turns in its top-30 vs 53% for BM25; 4.9% of gold turns are found by BM25 alone, 27.5% by the vector alone). BM25 is weakest exactly where the loss is: multi-hop (32% of gold turns in the BM25 top-30; only 36 of 282 questions have all gold there) and open-domain (25%). It is not a recall engine for LoCoMo but a precision signal for single-hop (72%) and temporal (69%). The numbers for multi-hop (50% of questions with all gold in some leg) are reproduced: 141 of 282.

**Does RRF lose items a leg ranked highly? Yes, mostly for the vector leg.** 186 gold turns have vector rank <=10 but are not fused hits (64 lost, 122 rescued only by a window); 66 have vector rank <=5, 26 rank <=3 (5 of those lost for good). For BM25, 32 gold turns with BM25 rank <=10 are not hits (15 with rank <=5). For the 66 vector-rank-<=5 turns that are not hits (22 lost + 44 window-rescued), the fused top-10 contains on average 7.6 items whose vector rank is *worse* (or absent), 6.4 of them present in both legs. The cause is agreement-by-noise, as quantified above.

Do re-weighting or other fusion settings fix it? SIMULATED with exact RRF on the logged legs (questions fully covered; baseline 1,251; "net" = gained minus lost questions; "wrong-set" = newly covered among the 158 wrong+incomplete):

| fusion variant | fully covered | net | gained / lost | wrong-set newly covered |
|---|---|---|---|---|
| baseline RRF k=60, equal weights | 1,251 | 0 | - | 1 |
| rrf_k 10 | 1,251 | 0 | +15 / -15 | 8 |
| rrf_k 30 | 1,251 | 0 | 0 / 0 | 1 |
| rrf_k 100 | 1,250 | -1 | 0 / -1 | 1 |
| vector weight 2 | 1,235 | -16 | +22 / -38 | 14 |
| BM25 weight 2 | 1,176 | -75 | +14 / -89 | 9 |
| vector leg only | 1,216 | -35 | +47 / -82 | 28 |
| BM25 leg only | 1,115 | -136 | +30 / -166 | 17 |
| reserve top-3 of each leg, fill with RRF | 1,247 | -4 | +11 / -15 | 8 |
| reserve top-5 of each leg | 1,221 | -30 | +32 / -62 | 20 |
| third BM25 leg on content words only (`read.core_terms_leg`-like) | 1,229 | -22 | +28 / -50 | 15 |
| same, stemmed | 1,249 | -2 | +44 / -46 | 27 |

Nothing here moves the total, because every re-ordering of ten slots trades one question's gold for another's. Only more slots help (section 7). The `rrf_k`, weights and reserved-slot knobs are not worth a run.

## 4. Task 2: multi-hop evidence structure and what decomposition could recover

MEASURED. 282 multi-hop questions with 882 gold turns. Number of gold turns per question: 1: 5, 2: 136, 3: 56, 4: 45, 5: 20, 6: 8, 7: 5, 8+: 7. Sessions spanned: 1: 13, 2: 164, 3: 56, 4: 28, 5+: 21. 269 of 282 questions need evidence from 2+ sessions.

| group | questions | all gold in some leg | all gold fused hits | all gold in context | >=1 gold in context | gold turns in context | answered correctly |
|---|---|---|---|---|---|---|---|
| all multi-hop | 282 | 141 (50%) | 62 (22%) | 108 (38%) | 247 (88%) | 61% | 177 (63%) |
| 1-2 gold turns | 141 | 96 (68%) | 54 (38%) | 75 (53%) | 120 (85%) | 69% | 95 (67%) |
| 3 gold turns | 56 | 23 (41%) | 7 (12%) | 20 (36%) | 50 (89%) | 65% | 36 (64%) |
| 4-5 gold turns | 65 | 20 (31%) | 1 (2%) | 13 (20%) | 59 (91%) | 57% | 41 (63%) |
| 6-99 gold turns | 20 | 2 (10%) | 0 (0%) | 0 (0%) | 18 (90%) | 46% | 5 (25%) |
| 1 session | 13 | 6 (46%) | 3 (23%) | 6 (46%) | 10 (77%) | 67% | 8 (62%) |
| 2 sessions | 164 | 101 (62%) | 54 (33%) | 87 (53%) | 140 (85%) | 69% | 114 (70%) |
| 3 sessions | 56 | 22 (39%) | 4 (7%) | 12 (21%) | 51 (91%) | 58% | 33 (59%) |
| 4+ sessions | 49 | 12 (24%) | 1 (2%) | 3 (6%) | 46 (94%) | 50% | 22 (45%) |

Reading. The ~50% of multi-hop questions with all gold in some leg is confirmed (141 of 282). Coverage falls steeply with the number of gold turns (2 turns: 68% in some leg; 6+ turns: 10%) and with sessions spanned (2 sessions: 62%; 4+: 24%), but 88% of multi-hop questions still get *at least one* gold turn into the context: the query finds one or two members of the evidence set and misses the rest. 75 questions have exactly one fused-hit gold turn while other gold turns are lost. Answer accuracy (63%) is higher than all-gold coverage (38%) because list questions are graded on partial answers.

By question form (regex classification of the question text):

| group | questions | all gold in some leg | all gold fused hits | all gold in context | >=1 gold in context | gold turns in context | answered correctly |
|---|---|---|---|---|---|---|---|
| list / aggregation (plural noun) | 127 | 61 (48%) | 25 (20%) | 43 (34%) | 111 (87%) | 59% | 75 (59%) |
| other wh- | 106 | 55 (52%) | 23 (22%) | 42 (40%) | 95 (90%) | 62% | 75 (71%) |
| count / duration | 26 | 16 (62%) | 8 (31%) | 11 (42%) | 22 (85%) | 64% | 11 (42%) |
| both / in common | 11 | 2 (18%) | 2 (18%) | 4 (36%) | 8 (73%) | 52% | 7 (64%) |
| yes / no | 8 | 4 (50%) | 3 (38%) | 5 (62%) | 7 (88%) | 68% | 6 (75%) |
| when | 4 | 3 (75%) | 1 (25%) | 3 (75%) | 4 (100%) | 86% | 3 (75%) |

**Is each gold turn retrievable by its own sub-question? (SIMULATED oracle, upper bound).** No human or LLM decomposition is available offline, so I built the best-case sub-question for every multi-hop gold turn whose text literally contains a gold-answer token (611 of 882 turns): "What did <person> say about <the answer words found in that turn>?". This is an *oracle*: it already knows the instance, which a real decomposer would have to guess or discover. Each sub-question is embedded and BM25-searched on its own and fused (RRF k=60) like the real query.

| state of the gold turn today | turns with an oracle sub-question | own sub-question: fused rank <=1 | <=3 | <=5 | <=10 |
|---|---|---|---|---|---|
| hit | 250 | 138 (55%) | 201 (80%) | 209 (84%) | 217 (87%) |
| win_fusion | 77 | 40 (52%) | 55 (71%) | 61 (79%) | 67 (87%) |
| win_recall | 52 | 23 (44%) | 32 (62%) | 37 (71%) | 42 (81%) |
| lost_fusion | 118 | 56 (47%) | 80 (68%) | 86 (73%) | 94 (80%) |
| lost_recall | 114 | 35 (31%) | 69 (61%) | 80 (70%) | 91 (80%) |

Of the 347 lost multi-hop gold turns, 232 can be targeted this way and 185 (80%) are in the top-10 of their own sub-question; 115 (33%) have no answer token in the turn, so even a perfect decomposer has no handle on them (cause N). Question level, union of today's context and the top-k of every oracle sub-question:

| oracle sub-questions retrieve | multi-hop questions with all gold in context (today 108) | gain | context cost |
|---|---|---|---|
| top-1 of each sub-question | 141 of 282 | +33 | 1 hits (each with its window) per targeted gold turn |
| top-3 of each sub-question | 161 of 282 | +53 | 3 hits (each with its window) per targeted gold turn |
| top-5 of each sub-question | 169 of 282 | +61 | 5 hits (each with its window) per targeted gold turn |
| top-10 of each sub-question | 180 of 282 | +72 | 10 hits (each with its window) per targeted gold turn |

ESTIMATE for a real decomposer: an LLM planner that does not know the answers recovers a fraction of this. Two reference points: (a) independence model with the single-hop per-gold-turn rates (fused-top-10 hit rate 78.7%, in-context 96.5%): expected multi-hop questions fully covered 142 (hits only) to 253 (with windows) of 282, but this overstates because list-instance turns are harder than single-hop turns (their lost rate is about 60%); (b) 25-50% of the oracle gain: **+18 to +36 multi-hop questions fully covered (108 -> ~126-144 of 282), i.e. roughly +15 to +30 correct answers at the 0.82 conditional reader accuracy**. Cost: 1 LLM call (the local 9B: ~1-3 s) plus 3-6 extra retrieval legs (~+100 ms).

## 5. Task 3: the neighbour window

MEASURED. In the fix run 427 gold turns (18.2%) reach the context only through a window (base: 373, 15.9%). By offset from the nearest hit that covers them (negative = before the hit): -2: 86, -1: 79, +1: 116, +2: 91, +3: 17, +4: 38. 149 of the 427 are covered by more than one hit. Offsets within +-2 account for 372 turns; the +3 / +4 positions added by the fix run account for 55.

Questions that have all their gold in context *only* because of windows: 251 of 1,540 (hits alone cover all gold for only 1,000 questions by simulation, windows raise it to 1,251).

**Where the lost gold sits relative to the hits.** Of the 517 lost turns, **388 (75%) are in a session in which no hit exists**; no window shape can help them. The other 129 lie in a session that has a hit, at offset (nearest hit): 3-4 turns before: 32; 5-8 before: 26; 9+ before: 28; 5-6 after: 9; 7-8 after: 12; 9+ after: 22. A bigger window can therefore reach at most 129 more gold turns, 79 of them within +-8.

SIMULATED window shapes on the fix run's logged fused top-10 (same session only, engine greedy order, budget 4096 estimated tokens; "full" = questions with all gold in context; baseline 1,251):

| window (before / after) | full | net | avg est. tokens | avg turns |
|---|---|---|---|---|
| 0 / 0 (hits only) | 1,000 | -251 | 458 | 10 |
| 1 / 2 | 1,186 | -65 | 1,408 | 33 |
| 2 / 2 (base run) | 1,222 | -29 | 1,670 | 39 |
| **2 / 4 (fix run)** | 1,251 | 0 | 2,189 | 53 |
| 2 / 6 | 1,259 | +8 | 2,637 | 65 |
| 3 / 5 | 1,260 | +9 | 2,643 | 64 |
| 2/4 + bridge: all turns between two hits of the same session (gap <= 8) | 1,251 | 0 | 2,192 | 53 |
| 2/4 + bridge (gap <= 20) | 1,255 | +4 | 2,261 | 55 |
| 2/4 + whole session when it holds 2+ hits | 1,270 | +19 | 3,182 | 79 |
| 2/4 + whole session when it holds 3+ hits | 1,257 | +6 | 2,507 | 61 |

**The window is saturated.** Beyond 2/4, 2 extra turns after the hit buy +8 questions for +450 tokens; session-level expansion buys +19 for +1,000 tokens. The remaining headroom is in finding more hits (more distinct sessions), not in widening the window around the same ones. With a larger pool the window must shrink by rank to stay in budget (section 7: tiered windows).

## 6. Task 5: how the reranker run loses gold (rr vs base)

MEASURED. Mechanism, verified on the logs:

1. Pool = the fused top-10 (`candidate_pool=1`); the fused list is identical to the base run in 1,540 / 1,540 questions. The reranker can only reorder and delete; **no question gained a gold turn** (full-gold questions: base 1219, rr 1183, lost 36, gained 0).
2. The engine replaces the fusion score by the *min-max normalised* reranker score (best hit = 1.0, worst = 0.0), then `assembly.relative_floor 0.3` drops every hit below 0.3: re-computing this rule reproduces the logged rr context in 1,540 / 1,540 questions (window +-2 around the surviving hits). Average hits surviving: 4.98 of 10 (distribution 1..9 hits: 102, 166, 207, 215, 203, 192, 177, 149, 129); the context shrinks from 39.3 to 19.7 turns and 1,571 to 806 tokens.
3. The reranker is a good gold detector: it keeps 1364 of 1400 gold hits (97.4%) but only 6310 of 14000 non-gold hits (45%). Drop rate by fused rank: 1: 10%, 2: 29%, 3: 40%, 4: 46%, 5: 55%, 6: 59%, 7: 62%, 8: 65%, 9: 66%, 10: 69%.
4. The damage is collateral. 70 gold turns that were in the base context are not in the rr context: only 18 (26%) were themselves hits that the floor dropped (their raw P(yes) median 0.14: the reranker really thought they were irrelevant, or they are label noise); 52 (74%) were window neighbours of a dropped hit (that hit's raw P(yes) median 0.07). Typical case: the hit is the question turn ("Which pet did you adopt?"), the answer is two turns later; scored alone the question turn is irrelevant, so hit and window go.
5. Effect on answers: 36 questions lost full gold coverage (none gained); on those, correct answers fell 23 -> 7. Overall rr is 1090 correct vs 1147 for base (base-only 107, rr-only 50); the 36 lost-coverage questions explain 16 of the net 57 loss. The remainder happens in questions whose gold coverage did not change or was already partial; the logs do not isolate why (candidates: a context half as long, dropped dated context the reader uses for temporal questions).
6. The assembly line of the rr run's primary gaps (11 questions) is exactly this: 18 gold hits that survived the rerank but fell under the floor.
7. Cost of the reranker as run: mean retrieval latency 388 ms vs 143 ms (fix) for 10 documents with the 4B 4-bit model; a 30-document pool would be ~3x the reranking part (~+0.7 s).

What this means for the retrieval-gap question: the reranker run *cannot* fix recall or fusion (nothing outside the fused 10 is ever scored). It becomes useful only with (a) `candidate_pool` 2-3, (b) a floor that does not act on min-max scores (`assembly.relative_floor: 0` or `rerank_blend` / `rerank_gate`), (c) `rerank_keep` set so the context size is controlled by count, not by a score range, and (d) ideally `rerank_context` so a question turn is scored together with its reply. See section 7 for the upper bound (ORACLE best-10-of-30: +81 questions at unchanged tokens).

## 7. What each lever recovers (SIMULATED on the logged lists)

Method: for every question the logged vector top-30, BM25 top-30 and temporal leg are re-fused (RRF k=60) exactly as the engine does (reproduces `fused` 1,540 / 1,540); the replay window and the 4,096-token budget are applied as in `Engine._read_routed` (reproduces `retrieved_turns` 1,540 / 1,540). BM25 and vector variants use the CPU replica (BM25: own implementation of Tantivy scoring, 97% top-30 agreement; vector: Qwen3-Embedding-0.6B fp32, 97.8% agreement). "Full" = questions with every gold turn in the context (baseline 1,251); "net" = questions gained minus lost; "wrong-set" = how many of the 158 wrong+incomplete questions have all gold in context (the baseline replica counts 1); tokens are an estimate (chars/4 + date prefix) and run ~6% above the logged mean (2,071). Churn matters: a variant with +40 / -30 has a net of +10 but reshuffles 70 questions.

### 7.1 More hits (pool size) and how the window is spent

| configuration | full | net (gained / lost) | wrong-set | avg tokens |
|---|---|---|---|---|
| baseline: pool 10, window 2/4 | 1,251 | - | 1 | 2,189 |
| pool 15, window 2/4 | 1,303 | +52 (+52 / -0) | 27 | 3,172 |
| pool 20, window 2/4, budget 4096 | 1,330 | +79 (+79 / -0) | 39 | 3,967 (budget binds) |
| pool 20, window 2/4, budget 8192 | 1,336 | +85 | 44 | 4,115 |
| pool 30, window 2/4, budget 4096 | 1,342 | +91 | 47 | 4,400 (budget binds hard) |
| pool 30, window 2/4, budget 8192 | 1,380 | +129 | 66 | 5,884 |
| pool 20, window 1/2 (flat) | 1,280 | +29 (+68 / -39) | 39 | 2,694 |
| pool 30, window 1/2 (flat) | 1,328 | +77 (+107 / -30) | 59 | 3,919 |
| pool 20, tiered: ranks 1-10 window 2/4, ranks 11-20 window 0/1 | 1,308 | +57 (+57 / -0) | 32 | 2,812 |
| pool 20, tiered: ranks 1-10 2/4, ranks 11-20 hit only | 1,300 | +49 (+49 / -0) | 28 | 2,513 |
| pool 30, tiered: ranks 1-10 2/4, ranks 11-30 window 0/1 | 1,341 | +90 (+90 / -0) | 50 | 3,447 |
| pool 30, tiered: ranks 1-10 2/4, ranks 11-30 hit only | 1,326 | +75 (+75 / -0) | 41 | 2,861 |
| pool 30, tiered: ranks 1-5 2/4, 6-10 1/2, 11-30 0/1 | 1,324 | +73 (+86 / -13) | 50 | 3,093 |
| ORACLE reranker: best 10 of the fused top-20, window 2/4 | 1,305 | +54 | 29 | 2,179 |
| ORACLE reranker: best 10 of the fused top-30 | 1,332 | +81 | 43 | 2,177 |
| ORACLE reranker: best 10 of the fused top-60 | 1,377 | +126 | 71 | 2,171 |
| ORACLE reranker: best 15 of the fused top-30 | 1,345 | +94 | 51 | 3,167 |

Reading. More slots are the only fusion-side lever with a large effect, and tokens are the price: a flat 2/4 window at pool 30 costs ~4,400 tokens and the 4,096 budget starts cutting evidence. Rank-tiered windows (full window for the first 10 hits, a one-turn tail for hits 11-30) recover most of the gain at ~3,450 tokens with no losses. A reranker (oracle bound) delivers +81 at the *current* token count, which is why a reranker over pool 30 is the efficient route if its real accuracy is close to the oracle; the rr run shows it keeps 97% of gold hits when it sees them, so 60-80% of the oracle (ESTIMATED: +50 to +65 questions) is plausible.

### 7.2 Lexical, vector-side and query-side options (pool 10, window 2/4 unless stated)

| option | exists as | full | net (gained / lost) | wrong-set | note |
|---|---|---|---|---|---|
| BM25 with Porter stemming | `read.lexical_analyzer: english` (also stop words) | 1,260 | +9 (+36 / -27) | 20 |  |
| BM25 stop words removed | `english` | 1,251 | 0 (+34 / -34) | 22 | pure churn |
| BM25 stop words + stemming (what `english` indexes) | `read.lexical_analyzer: english` | 1,264 | +13 (+55 / -42) | 34 |  |
| ... and speaker names dropped from the query | new code (or `core_terms_leg` as a replacement, below) | 1,274 | +23 (+56 / -33) | 34 | names are in every turn |
| ... plus pool 20 | pool = `read.candidate_pool: 2` | 1,353 | +102 (+108 / -6) | 63 | 3,860 tokens |
| BM25 indexes date words of the day said | `read.lexical_dates: true` | 1,261 | +10 (+15 / -5) | 13 | temporal 284 -> 290 |
| BM25 also indexes the dates of resolved relative phrases ("yesterday" -> that date) | new code (rule resolver exists) | 1,243 | -8 (+9 / -17) | 6 | date words match too many turns |
| third BM25 leg on content words only (names and stop words out) | `read.core_terms_leg: true` | 1,229 | -22 (+28 / -50) | 15 | harmful |
| same with stemming | `core_terms_leg` + `english` | 1,249 | -2 (+44 / -46) | 27 |  |
| embed "[date] + previous turn + turn" (and BM25 on the same text) | new code (write-time enrichment) | 1,241 / 1,244 | -10 / -7 | 21 / 29 | at window 2/2: +2 / +5 and 22% fewer tokens; at pool 20: +65 / +68 vs +85 plain |
| embed "previous turn + turn" | new code | 1,240 | -11 (+36 / -47) | 20 | pool 20: +72 |
| embed "previous + turn + next" | new code | 1,242 | -9 | 25 | pool 20: +49 |
| embed the turn text without the image caption | new code | 1,253 | +2 (+14 / -12) | 10 | captions neither help nor hurt |
| caption as its own vector unit (turn score = max of text-only and caption-only) | new code | 1,251 | 0 (+15 / -15) | 10 | pool 20: +81 vs +85 plain |
| statement-form probe (rule rewrite of the question) | `read.statement_probe: true` | 1,237 | -14 (+17 / -31) | 14 | fires on 1,162 questions |
| multi-part question split | `read.multi_intent_split: true` | 1,251 | 0 | 1 | splits only 5 of 1,540 questions |
| pseudo-relevance feedback probe | `read.prf_expansion: true` | 1,175 | -76 (+22 / -98) | 14 | harmful |
| statement probe + PRF | both | 1,215 | -36 | 24 |  |
| neighbours of the top-2 hits as extra legs | `read.cluster_expand: true` | 1,185 | -66 (+27 / -93) | 20 | harmful (30 neighbours: -73) |
| at most N hits per session from a 4x pool | `read.session_cap: 1 / 2 / 3` | 1,186 / 1,248 / 1,250 | -65 / -3 / -1 | 19 / 7 / 1 | no gain |
| per-session top-1 / top-2 retrieval, +-1 window | new code | 857 / 1,155 | -394 / -96 | - | 27.7 / 55.4 hits per question |

Reading. **Every existing rule-based query-expansion option (statement probe, PRF, cluster expansion, session cap, multi-intent split) is neutral or harmful on LoCoMo in this simulation**, and write-time context enrichment of the embedding (previous turn, date, caption handling) is neutral: it frees tokens (the enriched turns embed closer to their neighbours, so the top-10 concentrates in fewer places) but does not add gold. The only lexical options with a positive net are `lexical_analyzer: english` (+13), `lexical_dates` (+10), and names dropped (+23 combined), all small and churn-heavy. Caveat: the probes were simulated from the engine's own functions (`statement_form`, `feedback_terms`, `split_intents`) with probes embedded as queries (instruction prefix) and fused as vector + BM25 legs; a different embedding of the probe text could change the sign for `statement_probe` but is unlikely to rescue PRF or cluster expansion.

### 7.3 Per-cause recovery (gold turns, MEASURED-SIM)

| cause | lost turns | A: pool 20 tiered | B: pool 30 tiered | C: english-like BM25, names dropped | D: ORACLE rerank 10 of 30 | E: B + C |
|---|---|---|---|---|---|---|
| H Category-to-instance gap (list / aggregation q | 164 | 26 | 46 | 26 | 39 | 64 |
| N Answer not literally in the gold turn (paraphr | 112 | 21 | 37 | 23 | 30 | 55 |
| I Open-domain inference (evidence is an implicat | 96 | 7 | 14 | 14 | 8 | 25 |
| T Temporal / date-dependent evidence | 43 | 18 | 22 | 17 | 23 | 29 |
| O Lexical overlap exists but the turn is outrank | 63 | 26 | 35 | 27 | 35 | 48 |
| M Morphology-only overlap (BM25 without stemming | 27 | 9 | 13 | 15 | 12 | 18 |
| P Single-hop paraphrase gap | 12 | 3 | 6 | 6 | 5 | 7 |
| **all** | 517 | 110 | 173 | 128 | 152 | 246 |
| gold turns newly *lost* (were in context) | - | 0 | 0 | 86 | 4 | 30 |

Reading. Pool 30 with a thin tail recovers a third of the lost turns (173 of 517) with zero losses; the English BM25 variant recovers 128 but loses 86 other turns (a reshuffle, not a gain); the oracle reranker recovers 152 at constant tokens. Combining pool 30 with the lexical change (E) recovers 246 of 517 (48%): 76% of cause O, 67% of T and M, 49% of N, 39% of H and 26% of I (30 turns churn out). **About half of the lost turns is the ceiling of these retrieval-only fixes; the rest needs decomposition or derived memory (H) or cannot be reached by similarity to the question (N, I).**

## 8. Solution options per cause

Legend. **Exists** = a config key in `src/memspine/config/schema.py` / `docs/USAGE.md` (verified); **new code** = needs implementation. "Recoverable" is gold turns / fully-covered questions; MEASURED-SIM refers to section 7, ESTIMATE is judgement. Reader conversion: multiply covered questions by ~0.8 (single-hop 0.89, multi-hop 0.82, temporal 0.72, open-domain 0.25). Cost is per question at read time unless stated; risk includes reader dilution from longer contexts, which these logs cannot measure.

### H. Category-to-instance gap (164 turns, 26% of lost; 40 wrong questions)

| option | exists / needs | expected recovery | cost | risk |
|---|---|---|---|---|
| H1. Larger pool with a tiered window: `candidate_pool: 3` + window by rank (full window for hits 1-10, 0/1 beyond) | `read.candidate_pool` exists (max 10; pool 30 = 3). Window-by-rank = new code (small; the 2-before / 4-after window exists only in `memspine-fixes`) | MEASURED-SIM: 46 of 164 turns; +90 questions fully covered overall, 50 of the 158 wrong ones; multi-hop 108 -> 161 (pool 30 tiered) | tokens 2.2k -> 3.4k; ~0 ms latency | reader dilution; budget 4096 must be raised for flat windows |
| H2. Rerank a pool of 30, keep 10-15: `candidate_pool: 3`, `rerank: qwen3`, `rerank_keep: 10`, floor off | all exist (`read.rerank_keep`, `assembly.relative_floor: 0`; also `rerank_blend`, `rerank_gate`, `rerank_context`, `rerank_date_prefix`) | MEASURED-SIM oracle: 39 of 164 turns, +81 questions at constant tokens; ESTIMATE real: 60-80% of that | +0.7 s (30 docs, 4B 4-bit, as measured 245 ms for 10); GPU memory for reranker | the floor/min-max bug must be removed first (section 6); pointwise scoring cannot see that a reply answers the previous turn (use `rerank_context: 1-2`) |
| H3. Instance-aware decomposition: LLM planner writes evidence-seeking sub-queries, each retrieves its own top-k, fused as RRF legs | exists: `read.planner: llm`, `planner_version: v2/v3` (needs the `plan` LLM role), `read.compose_rewrites`, `aggregate_in_replay` + `aggregate_top_k` | MEASURED-SIM oracle upper bound: multi-hop 108 -> 180 (+72) with answer-aware sub-queries; ESTIMATE real +18 to +36 multi-hop questions | 1 LLM call per question (~1-3 s local 9B) + 3-6 retrieval legs (~+100 ms) | a decomposer that does not know the instances must guess them ("hobbies" -> painting, hiking, ...); wrong sub-queries add noise |
| H4. Derived list memory: mine atomic facts with topic tags and keep person x topic list cards (hobbies: painting, kayaking, ...) read through `read.cards: header` | exists: `consolidation.mine_facts`, `mine_multiview`, `list_cards`, `read.cards`, `read.list_cards_only_aggregate`; needs the `extract` LLM role and a sleep run | not measurable from these logs. ESTIMATE: directly targets the cause (instances collected regardless of wording); upper range is cause H + part of N; depends on miner recall | LLM per session at ingest (one-off, ~19-35 sessions x 10 conversations); no read-time cost beyond the card block | miner errors and wrong dates (documented: cards cost temporal accuracy in the 2026-10-05 smoke test); cards consume read budget; evaluating needs an LLM run |
| H5. Two-stage retrieval: round 1 finds some instances, round 2 queries "also: <category> <instance>" for each | `read.second_round` exists but seeds from names and dates only; instance-seeded round 2 = new code | not simulated; the oracle in section 4 is its upper bound (80% of targetable lost turns in the top-10 of an instance query) | 1 extra search (~50 ms) or 1 LLM call | drift to the instances already found |

### N. Answer not literally in the gold turn (112 turns, 22%; 35 wrong questions)

| option | exists / needs | expected recovery | cost | risk |
|---|---|---|---|---|
| N1. Audit the gold labels for this class (evaluation hygiene, not engine work) | new tooling; 112 turns, start with the 35 wrong questions | changes the denominator, not accuracy: any turn that does not support the answer should not count as lost | one day of reading | none; reduces false "retrieval failure" counts |
| N2. Pool / reranker as H1-H2 | exist | MEASURED-SIM: pool 30 tiered recovers 37 of 112, oracle rerank 30 | as H1 / H2 | as H1 / H2 |
| N3. HyDE-style answer hypothesis or answer-free rewrite as an extra leg | `read.compose_rewrites` is the nearest (answer-free rewrites from the `query_rewrite` role); true HyDE = new code | ESTIMATE +5 to +15 questions; implicit answers ("lost a friend") are what a hypothesis can bridge | 1 LLM call per question | hallucinated hypotheses retrieve confident noise |
| N4. Reply-aware retrieval: index a turn together with the turn it answers | new code; MEASURED-SIM of the embedding variants: net -11 to +2 | no gain at pool 10 (section 7.2) | re-index | top-10 concentrates in fewer places |
| N5. Bigger / different embedder (Qwen3-Embedding-4B is in the local HF cache) | config only (`embedding.model`, 2560-d); untested here (the 4B snapshot would not load offline) | unknown; recall-lost turns have a median vector rank of 127, so a better embedder is unlikely to pull most of them into the top-30 | ~7x embedding time, more GPU | earlier arms: bge-small and Jina lost 2.5 points to the 0.6B model |

### I. Open-domain inference (96 turns, 19%; 28 wrong questions)

| option | exists / needs | expected recovery | cost | risk |
|---|---|---|---|---|
| I1. Pool / reranker as H1-H2 | exist | MEASURED-SIM: 14 of 96 (pool 30 tiered), 8 (oracle rerank) | as H1 / H2 | reader accuracy is 25% even with all gold, so conversion is ~0.25 |
| I2. Evidence-seeking sub-queries for yes/no and "would X" questions ("Is X religious?" -> "church", "faith") | exists: `planner: llm`, `planner_version: v2` (needs `plan` role) | ESTIMATE +3 to +8 questions covered, +1 to +2 correct | 1 LLM call | little downstream conversion |
| I3. Profile / reflective memory so dispositions are stored as statements | exists: `consolidation.reflect_profile`, reflective memory; needs LLM at sleep | not measurable here | LLM at sleep | profile errors read as facts |
| I4. Deprioritise: fix the reader prompt for inference questions instead | prompt change (outside this document) | the 148 reader failures include 21 open-domain ones with all gold present | none | - |

### T. Temporal / date-dependent evidence (43 turns, 8%; 29 wrong questions)

| option | exists / needs | expected recovery | cost | risk |
|---|---|---|---|---|
| T1. Let the temporal leg fire on relative and partial dates: `read.temporal_relative`, `temporal_infer_year` | exist (off in these runs); `temporal_leg_mentions` is already on in the fix run | ESTIMATE 5-12 turns; the temporal leg fires on only 187 of the 1,540 questions today | ~0 | the leg lists the first N turns of a day unscored (all session turns share one timestamp), so it adds noise |
| T2. Date words in BM25: `read.lexical_dates: true` | exists (rebuilds the lexical index) | MEASURED-SIM: +10 questions net (+15 / -5); temporal 284 -> 290 | index +10% | small |
| T3. Index the resolved date of "yesterday" / "last week" at write time | new code (the rule resolver exists; `annotate` is used only at read time on hits) | MEASURED-SIM of the crude version (resolved date words appended to the BM25 text): -8 net; needs a smarter way than date words (e.g. a date field filtered by the temporal leg, `consolidation.mine_event_dates` + `temporal_leg_event_dates`) | sleep stage or write step | noise from date words shared by many turns |
| T4. Question-aware reranking with dates: `rerank_date_prefix: true` | exists (only with a reranker) | ESTIMATE 3-8 | negligible | - |

### O. Overlap present but out-ranked (63 turns, 12%; 11 wrong questions)

| option | exists / needs | expected recovery | cost | risk |
|---|---|---|---|---|
| O1. Pool / reranker as H1-H2 | exist | MEASURED-SIM: pool 30 tiered 35 of 63, oracle rerank 35 | as H1 / H2 | as H1 / H2 |
| O2. BM25 noise control: `lexical_analyzer: english`, drop speaker names from the query | `english` exists; name stripping = new code | MEASURED-SIM +13 / +23 questions, 27 of 63 turns, but 86 gold turns churn out | index rebuild | churn: net small, effects per conversation uneven |
| O3. Weights, rrf_k, reserved slots | exist (`leg_weights`, `rrf_k`, `short_query_lexical_weight`) | MEASURED-SIM: 0 to negative (section 3) | - | do not run |

### M / P. Morphology-only overlap (27 turns) and single-hop paraphrase (12 turns), 5% and 2%

| option | exists / needs | expected recovery | cost | risk |
|---|---|---|---|---|
| M1. `read.lexical_analyzer: english` | exists | MEASURED-SIM: recovers 15 of 27 M turns and 6 of 12 P turns (config C); overall +13 net | index rebuild | churn |
| M2. Larger pool | exists | 13 of 27 and 6 of 12 (pool 30 tiered) | as H1 | as H1 |

### Cross-cutting: fusion and context assembly

| option | exists / needs | expected recovery | cost | risk |
|---|---|---|---|---|
| X1. Fix the reranker floor before any reranker run: `assembly.relative_floor: 0` (or `rerank_blend` / `rerank_gate`, or `rerank_keep`) | exists | would keep the 18 floor-dropped gold hits and most of the 52 lost neighbours in the rr run (ESTIMATE: the 36 lost full-coverage questions) | - | - |
| X2. Spend hits on distinct locations: 1.87 of the 10 hits per question have a window that overlaps a higher-ranked hit | new code: skip a candidate whose turn already lies inside the 2/4 window of a chosen hit and take the next fused item (hit selection aware of the window) | MEASURED-SIM: 1,266 fully covered (+15; +28 / -13), 13 of the wrong set, 2,631 tokens (+440) | ~0 ms | needs fused items 11+ (pool > 10); modest gain |
| X3. Window 2/4 as a config key on this branch | `read.replay_window_before/after` exist in `memspine-fixes` (`schema.py:507`), not in the `feat/local-qwen-stack` ReadConfig, where `replay_window` is only a call argument (docs/PIPELINE_TREE.md 2.10) | already measured in the fix run: +29 questions over +-2 | - | - |
| X4. Budget: raise `--budget` from 4096 once the pool grows, and move truncation from the tail (latest turns) to the lowest-ranked windows | `--budget` is a harness argument; ranked truncation = new code | pool 30 flat 2/4 loses 38 of its 129 possible questions to the 4,096 budget (1,342 vs 1,380) | - | the reader context window |
| X5. Gold-label audit of the multi-hop list questions | tooling | affects metrics only | - | - |

## 9. Comparison with the earlier simulation-based analysis (`evals/analysis/RETRIEVAL_GAPS.md`)

| claim in RETRIEVAL_GAPS.md | status | what the real logs show |
|---|---|---|
| Gold: 57.8% itself a fused hit, 22.2% at fused ranks 11-60, 16.8% rescued by the window, ~20% in no leg | confirmed | base run: 59.7% hits, 21.2% in a leg but outside the fused top-10 (498 turns), 19.1% in no leg (449), window-rescued 15.9% (373); fix run: 59.8% / 21.3% / 19.0% / 18.2% |
| The top-10 cut-off (class B) is the largest class: 36% of missing gold turns; 117 of 142 vector-only, 23 BM25-only | confirmed (and larger) | fusion-lost is 237 of 517 lost turns in the fix run (46%; base 267 of 574): 195 vector-only, 37 BM25-only, 1 both. At question level `primary_gap` shows recall (94) above fusion (64) only because recall is checked first when a question has both kinds |
| Class A "near-window": window too small, 3-5 turns from a hit (14% of missing) | resolved by the fix | the 2/4 window rescued 55 more turns; what is left near a hit is 32 turns 3-4 before and 9 turns 5-6 after (section 5) |
| Image captions are a class (C1: 23% of wrong questions) | corrected | captions are not a cause: lift 1.05, answer-only-in-caption is 15 of 517 lost turns (3%); embedding without captions or with captions as their own vector unit changes nothing (+2, 0) |
| 69% of missing gold turns share no content word with the question; speaker names carry no signal | confirmed | 68% (stemmed) / 78% (exact) of the 517 lost turns; speaker identity is not a driver (lift 1.04) |
| Window 2 before / 4 after beats symmetric 3 at equal cost | confirmed and realised | fix run uses 2/4: +29 fully covered questions over the base +-2 in the replay; window is now saturated (2/6: +8, session-level: +19) |
| Pool 20 (`candidate_pool: 2`) recovers ~58 questions; pool 25 ~72 | confirmed in order of magnitude | pool 20: +79 questions fully covered (39 of the wrong set) but the 4,096 budget already binds (3,967 tokens); budget 8192: +85. A tiered window gets +57 at 2.8k tokens |
| Contextual enrichment ("[session date] + previous turn" in embedded and BM25 text) recovers ~18 net questions | NOT confirmed | re-embedded on CPU: -7 to -10 at window 2/4 (-10 vector only), +2 to +5 at the old window 2/2 with 22% fewer tokens; at pool 20 it is no better than the plain index (+65-68 vs +85). Prev+cur and prev+cur+next variants behave the same |
| Lexical weight 2, core_terms_leg, cohesion, sentence, entity_expand are net harmful | confirmed where retested | BM25 weight 2: -75; core_terms leg: -22 (-2 stemmed); vector weight 2: -16; rrf_k 10/30/100: 0, 0, -1. Newly tested existing options, all neutral or harmful: statement_probe -14, prf_expansion -76, cluster_expand -66, session_cap 1/2/3: -65/-3/-1 |
| Reranker with pool 10 can only delete; min-max + `relative_floor 0.3` drops 53% of hits (Jina) | confirmed for Qwen3-Reranker-4B | context is a subset of the base context in 1,540 / 1,540; 4.98 of 10 hits survive; gold hits kept 97.4%, non-gold 45%; 74% of lost gold (52 of 70 turns) were window neighbours; 36 questions lost full gold, 0 gained |
| Part of the reranker's cost happens with all gold still present (-29 of -47) | consistent | rr: the 36 lost-coverage questions explain -16 of the net -57 correct; the other -41 happen where coverage is unchanged or already partial |
| Multi-hop: only ~35% of questions have complete evidence in context | confirmed | 38% in the fix run (108 / 282); 50% have all gold in some leg (141 / 282) |
| C4 date-dependent turns: relative phrases need the session date to match | partly confirmed, smaller | 43 temporal lost turns; 26 contain a relative phrase; lost-rate is 32% when the question names an explicit date vs 8% otherwise; `lexical_dates` +10 net, crude resolved-date words -8 |
| Reader conditional accuracy 0.83 / 0.78 / 0.30 / 0.91 | consistent | rr run: 82% multi-hop, 72% temporal, 25% open-domain, 89% single-hop with all gold in context |

**New findings the simulation could not see:** (1) 34% of BM25 top-10 hits have no content-word overlap with the question and fill 15% of the non-gold fused slots; (2) vector rank <=5 turns are routinely lost to two-leg items at vector rank 15-30 (66 turns); (3) every rule-based query-expansion option in the engine is neutral or harmful here; (4) the lost turns are concentrated in questions with many gold turns (50% in questions with 4+), so counting questions understates the problem; (5) 75% of lost gold is in sessions that have no hit.

## Appendix A. All 517 lost gold turns (fix run)

Columns: `st` R = lost at recall (in no leg top-30), F = lost at fusion. `vec` / `bm25` = rank in the logged top-30, or `~n` = rank over the whole conversation from the CPU replica. `margin`: F rows = own normalised RRF score / score of the 10th fused item; R rows = cosine below the 30th logged vector hit. `cause` letters as in section 2. Sorted by cause, then question. Text truncated; pipes replaced by `/`.

| # | q | cat | cause | st | vec | bm25 | margin | question | gold turn | out-ranked by (top fused non-gold) |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 26 0-7 | mh | H | R | ~86 | ~343 | -0.04 | What is Caroline'[omitted]'[omitted]'[omitted]'ve come so far, but there's more to do. Yo… |
| 2 | 26 0-11 | mh | H | R | ~245 | ~34 | -0.11 | Where did Caroline move from 4 years ago? | D4:3: Caroline: [omitted]aroline: [omitted]… |
| 3 | 26 0-15 | mh | H | R | ~136 | ~290 | -0.07 | What activities does Melanie partake in? | D1:12: Melanie: [omitted]… | Melanie: [omitted] … |
| 4 | 26 0-15 | mh | H | R | ~36 | ~228 | -0.00 | What activities does Melanie partake in? | D1:18: Melanie: [omitted]… | Melanie: [omitted] … |
| 5 | 26 0-15 | mh | H | R | ~61 | ~94 | -0.03 | What activities does Melanie partake in? | D9:1: Melanie: [omitted]… | Melanie: [omitted] … |
| 6 | 26 0-19 | mh | H | F | 7 | ~329 | 0.46/0.47 | What do Melanie'[omitted]'ll do… |
| 7 | 26 0-24 | mh | H | F | 17 | ~363 | 0.40/0.47 | What does Melanie do to destress? | D5:4: Melanie: Wow, Caroline! That'[omitted]'s like the… | Melanie: [omitted]… |
| 8 | 26 0-32 | mh | H | R | ~43 | ~314 | -0.03 | [omitted]? | D8:17: Caroline: [omitted]… | Melanie: [omitted]… |
| 9 | 26 0-38 | mh | H | R | ~40 | ~133 | -0.01 | [omitted]? | D1:18: Melanie: [omitted]… | Melanie: [omitted] … |
| 10 | 26 0-38 | mh | H | F | 29 | ~178 | 0.34/0.48 | [omitted]? | D6:4: Melanie: [omitted] … | Melanie: [omitted] … |
| 11 | 26 0-38 | mh | H | R | ~122 | ~152 | -0.07 | [omitted]? | D8:6: Melanie: [omitted] … | Melanie: [omitted] … |
| 12 | 26 0-38 | mh | H | R | ~31 | ~44 | -0.00 | [omitted]? | D9:1: Melanie: [omitted]… | Melanie: [omitted] … |
| 13 | 26 0-61 | mh | H | F | 14 | ~52 | 0.41/0.48 | What musical artists/bands has Melanie seen? | D11:3: Melanie: [omitted]… | Melanie: [omitted]? … |
| 14 | 26 0-66 | mh | H | F | 23 | ~106 | 0.37/0.47 | What does Melanie do with her family on hikes? | D16:4: Melanie: [omitted]… | Melanie: [omitted] … |
| 15 | 30 1-3 | mh | H | F | 23 | ~294 | 0.37/0.48 | What do Jon and Gina both have in common? | D1:2: Jon: [omitted]'[omitted]'s make a difference and show 'em what we got. We … |
| 16 | 30 1-3 | mh | H | R | ~147 | ~99 | -0.09 | What do Jon and Gina both have in common? | D1:3: Gina: [omitted]… | Gina: Yeah Jon! Let'[omitted]'em what we got. We … |
| 17 | 30 1-3 | mh | H | R | ~202 | ~242 | -0.14 | What do Jon and Gina both have in common? | D1:4: Jon: Sorry to hear that! I'[omitted]'cause I'[omitted]'d b… | Gina: Yeah Jon! Let'[omitted]'em what we got. We … |
| 18 | 30 1-3 | mh | H | F | ~162 | 5 | 0.47/0.48 | What do Jon and Gina both have in common? | D2:1: Gina: [omitted]… | Gina: Yeah Jon! Let'[omitted]'em what we got. We … |
| 19 | 30 1-17 | mh | H | F | 21 | ~126 | 0.38/0.72 | [omitted]? | D6:8: Gina: [omitted]… | Gina: [omitted]… |
| 20 | 30 1-23 | mh | H | F | 27 | ~59 | 0.35/0.49 | How did Gina promote her clothes store? | D13:4: Gina: [omitted] … | Gina: [omitted]… |
| 21 | 41 2-3 | mh | H | F | 26 | ~610 | 0.35/0.49 | [omitted]? | D2:1: Maria: [omitted]… | Maria: [omitted]… |
| 22 | 41 2-3 | mh | H | R | ~206 | ~497 | -0.09 | [omitted]? | D3:5: John: [omitted]… | Maria: [omitted]… |
| 23 | 41 2-6 | mh | H | F | 11 | ~328 | 0.43/0.48 | Where has Maria made friends? | D14:10: Maria: [omitted]… | John: [omitted]… |
| 24 | 41 2-6 | mh | H | R | ~79 | ~498 | -0.03 | Where has Maria made friends? | D19:1: Maria: [omitted]… | John: [omitted]… |
| 25 | 41 2-6 | mh | H | R | ~405 | ~456 | -0.14 | Where has Maria made friends? | D2:1: Maria: [omitted]… | John: [omitted]… |
| 26 | 41 2-11 | mh | H | R | ~57 | ~557 | -0.03 | [omitted]? | D21:19: Maria: [omitted]… | Maria: [omitted] … |
| 27 | 41 2-11 | mh | H | R | ~118 | ~74 | -0.08 | [omitted]? | D6:5: Maria: [omitted]… | Maria: [omitted] … |
| 28 | 41 2-15 | mh | H | F | 7 | ~457 | 0.46/0.46 | What writing classes has Maria taken? | D9:1: Maria: [omitted]… | Maria: Looks fun! What other classes have you done? |
| 29 | 41 2-25 | mh | H | F | 5 | ~260 | 0.47/0.48 | What European countries has Maria been to? | D8:15: Maria: [omitted]… | Maria: [omitted]… |
| 30 | 41 2-28 | mh | H | R | ~50 | ~556 | -0.02 | [omitted]? | D12:5: John: [omitted]… | John: [omitted] … |
| 31 | 41 2-28 | mh | H | R | ~146 | ~285 | -0.06 | [omitted]? | D9:8: John: [omitted]… | John: [omitted] … |
| 32 | 41 2-32 | mh | H | F | 5 | ~180 | 0.47/0.48 | [omitted]? | D18:2: John: Hey Maria, thanks for your kind words. It'[omitted]'m finding some comfort in the … | John: [omitted]… |
| 33 | 41 2-35 | mh | H | F | 9 | ~517 | 0.44/0.48 | What states has Maria vacationed at? | D18:3: Maria: [omitted]… | Maria: [omitted] … |
| 34 | 41 2-40 | mh | H | R | ~120 | ~536 | -0.05 | What are the names of John'[omitted]'s birthday… | Maria: [omitted]… |
| 35 | 41 2-47 | mh | H | R | ~33 | ~561 | -0.01 | What exercises has John done? | D10:1: John: Hey Maria, I'[omitted]'s … | Maria: [omitted]… |
| 36 | 41 2-47 | mh | H | F | 8 | ~214 | 0.45/0.46 | What exercises has John done? | D1:4: John: Woah, Maria, that sounds cool! I'[omitted]'s giving me so much energy. | Maria: [omitted]… |
| 37 | 41 2-57 | mh | H | F | 12 | ~64 | 0.42/0.48 | What causes has John done events for? | D6:12: John: [omitted]… | Maria: [omitted]… |
| 38 | 42 3-1 | mh | H | R | ~470 | ~203 | -0.22 | What kind of interests do Joanna and Nate share? | D10:9: Joanna: [omitted]… | Nate: [omitted]… |
| 39 | 42 3-1 | mh | H | R | ~53 | ~440 | -0.02 | What kind of interests do Joanna and Nate share? | D4:9: Nate: [omitted]. | Nate: [omitted]… |
| 40 | 42 3-5 | mh | H | F | 23 | ~409 | 0.37/0.47 | What are Joanna's hobbies? | D2:25: Joanna: [omitted]… | Nate: [omitted]. |
| 41 | 42 3-11 | mh | H | F | 13 | ~250 | 0.42/0.47 | What is Joanna allergic to? | D4:4: Joanna: [omitted]… | Joanna: [omitted]… |
| 42 | 42 3-27 | mh | H | F | 6 | ~207 | 0.46/0.48 | What places has Joanna submitted her work to? | D2:7: Joanna: [omitted] - … | Joanna: [omitted]… |
| 43 | 42 3-30 | mh | H | F | 17 | ~213 | 0.40/0.48 | What kind of writings does Joanna do? | D17:14: Joanna: [omitted]! [image: a photo of a… | Joanna: [omitted]… |
| 44 | 42 3-30 | mh | H | R | ~37 | ~194 | -0.01 | What kind of writings does Joanna do? | D2:3: Joanna: [omitted]… | Joanna: [omitted]… |
| 45 | 42 3-34 | mh | H | R | ~229 | ~519 | -0.12 | [omitted]? | D3:17: Joanna: I just watched "[omitted]" and it was amazing! It'[omitted]'m so glad you're excited. I've never really t… |
| 46 | 42 3-42 | mh | H | F | 21 | ~263 | 0.38/0.47 | What movies have both Joanna and Nate seen? | D10:1: Joanna: [omitted] "[omitted]" Tr… | Joanna: [omitted]… |
| 47 | 42 3-42 | mh | H | R | ~242 | ~242 | -0.14 | What movies have both Joanna and Nate seen? | D22:8: Nate: I watched "[omitted]" [omitted]… | Joanna: [omitted]… |
| 48 | 42 3-50 | mh | H | F | 10 | ~327 | 0.44/0.47 | [omitted]? | D13:9: Nate: [omitted]! [image: a photo of a stuffed… | Nate: [omitted]… |
| 49 | 42 3-56 | mh | H | R | ~205 | ~340 | -0.08 | What animal do both Nate and Joanna like? | D5:6: Nate: I'[omitted]'[omitted]… | Joanna: [omitted] … |
| 50 | 42 3-58 | mh | H | R | ~568 | ~517 | -0.29 | What things has Nate reccomended to Joanna? | D10:11: Joanna: [omitted]… | Joanna: [omitted] … |
| 51 | 42 3-58 | mh | H | R | ~356 | ~615 | -0.20 | What things has Nate reccomended to Joanna? | D9:12: Nate: [omitted]… | Joanna: [omitted] … |
| 52 | 42 3-58 | mh | H | R | ~371 | ~51 | -0.20 | What things has Nate reccomended to Joanna? | D9:14: Nate: [omitted]! [image… | Joanna: [omitted] … |
| 53 | 42 3-63 | mh | H | R | ~69 | ~447 | -0.05 | What video games does Nate play? | D27:1: Nate: [omitted]… | Nate: [omitted]. |
| 54 | 42 3-70 | mh | H | F | 9 | ~490 | 0.44/0.47 | What activities does Nate do with his turtles? | D25:23: Nate: Yeah, it'[omitted]'m not sure I'll ever understand why watching my… |
| 55 | 42 3-74 | mh | H | R | ~416 | ~515 | -0.24 | [omitted]? | D15:14: Nate: [omitted]… | Nate: [omitted]! |
| 56 | 42 3-74 | mh | H | R | ~271 | ~526 | -0.17 | [omitted]? | D19:15: Nate: Good idea! How about this series? [image: a photo of a stack of books sitting on top of a woo… | Nate: [omitted]! |
| 57 | 42 3-74 | mh | H | R | ~534 | ~123 | -0.28 | [omitted]? | D23:26: Joanna: [omitted]… | Nate: [omitted]! |
| 58 | 42 3-74 | mh | H | R | ~521 | ~428 | -0.27 | [omitted]? | D3:17: Joanna: I just watched "[omitted]" [omitted]… | Nate: [omitted]! |
| 59 | 42 3-81 | mh | H | F | 22 | ~401 | 0.37/0.49 | What recipes has Joanna made? | D19:8: Joanna: [omitted]… | Joanna: [omitted]… |
| 60 | 42 3-81 | mh | H | R | ~83 | ~41 | -0.07 | What recipes has Joanna made? | D21:11: Joanna: [omitted] … | Joanna: [omitted]… |
| 61 | 42 3-81 | mh | H | F | 16 | ~430 | 0.40/0.49 | What recipes has Joanna made? | D22:1: Joanna: [omitted]… | Joanna: [omitted]… |
| 62 | 42 3-82 | mh | H | R | ~35 | ~72 | -0.01 | What recipes has Nate made? | D3:4: Nate: [omitted]… | Joanna: [omitted]… |
| 63 | 42 3-83 | mh | H | R | ~127 | ~129 | -0.05 | [omitted]? | D18:8: Nate: [omitted] … | Nate: [omitted]. |
| 64 | 43 4-2 | mh | H | F | 5 | ~298 | 0.47/0.48 | What items does John collect? | D27:20: John: [omitted] … | John: [omitted] … |
| 65 | 43 4-4 | mh | H | R | ~50 | ~123 | -0.06 | What books has Tim read? | D1:14: Tim: It'[omitted]'re f… | Tim: [omitted]… |
| 66 | 43 4-7 | mh | H | F | 14 | ~202 | 0.41/0.47 | Which geographical locations has Tim been to? | D14:16: Tim: [omitted]… | John: [omitted]… |
| 67 | 43 4-7 | mh | H | R | ~61 | ~525 | -0.03 | Which geographical locations has Tim been to? | D3:2: Tim: [omitted]… | John: [omitted]… |
| 68 | 43 4-11 | mh | H | R | ~96 | ~385 | -0.06 | What sports does John like besides basketball? | D3:25: John: [omitted]… | Tim: [omitted]… |
| 69 | 43 4-14 | mh | H | R | ~36 | ~481 | -0.01 | What kind of writing does Tim do? | D2:1: Tim: [omitted]… | Tim: [omitted]… |
| 70 | 43 4-25 | mh | H | F | 28 | ~155 | 0.35/0.48 | [omitted]? | D7:7: John: You'[omitted]'s awesome to see how sports can unite people. B… |
| 71 | 43 4-25 | mh | H | R | ~261 | ~235 | -0.11 | [omitted]? | D7:9: John: [omitted]'[omitted]'s awesome to see how sports can unite people. B… |
| 72 | 43 4-35 | mh | H | F | 5 | ~333 | 0.47/0.48 | What outdoor activities does John enjoy? | D12:6: John: [omitted]… | John: [omitted] … |
| 73 | 43 4-38 | mh | H | R | ~49 | ~155 | -0.03 | [omitted]? | D13:1: Tim: Hey John! It'[omitted]'[omitted]… |
| 74 | 43 4-38 | mh | H | F | 25 | ~440 | 0.36/0.46 | [omitted]? | D18:1: Tim: Hey John! Hope you'[omitted]'[omitted]… |
| 75 | 43 4-41 | mh | H | F | 11 | ~287 | 0.43/0.49 | What kind of fiction stories does Tim write? | D15:3: Tim: [omitted]im: [omitted]… |
| 76 | 43 4-41 | mh | H | R | ~85 | ~526 | -0.07 | What kind of fiction stories does Tim write? | D16:1: Tim: Hey John, long time no see! Hope you'[omitted]'s happ… | Tim: [omitted]… |
| 77 | 43 4-52 | mh | H | R | ~159 | ~322 | -0.12 | [omitted]? | D20:2: John: [omitted]… | John: [omitted]… |
| 78 | 43 4-61 | mh | H | R | ~37 | ~363 | -0.02 | What books has John read? | D17:9: John: [omitted]… | John: [omitted]… |
| 79 | 43 4-65 | mh | H | R | ~45 | ~412 | -0.02 | What fantasy movies does Tim like? | D8:16: Tim: Yeah, "[omitted]" is special to me. It was the first movie from… | Tim: That'[omitted]'s something special about being… |
| 80 | 44 5-2 | mh | H | F | 17 | ~301 | 0.40/0.48 | [omitted]? | D13:1: Andrew: [omitted]… | Andrew: [omitted]… |
| 81 | 44 5-2 | mh | H | F | 22 | ~107 | 0.37/0.48 | [omitted]? | D19:15: Andrew: [omitted]'[omitted]'s it going? Since we last talked, a few new t… |
| 82 | 44 5-3 | mh | H | F | 27 | ~499 | 0.35/0.48 | [omitted] … | D13:1: Andrew: [omitted]… | Audrey: [omitted]… |
| 83 | 44 5-3 | mh | H | R | ~69 | ~140 | -0.03 | [omitted] … | D4:2: Andrew: [omitted]… | Audrey: [omitted]… |
| 84 | 44 5-12 | mh | H | R | ~65 | ~78 | -0.05 | [omitted]? | D17:1: Andrew: [omitted]ndrew: [omitted]… |
| 85 | 44 5-15 | mh | H | R | ~62 | ~146 | -0.02 | [omitted]? | D10:5: Audrey: [omitted] … | Audrey: [omitted]… |
| 86 | 44 5-18 | mh | H | F | 23 | ~262 | 0.37/0.49 | Where did Audrey get Pixie from? | D11:4: Audrey: [omitted]… | Audrey: [omitted] … |
| 87 | 44 5-23 | mh | H | R | ~33 | ~179 | -0.00 | [omitted]? | D2:12: Andrew: [omitted]. | Andrew: [omitted] … |
| 88 | 44 5-23 | mh | H | R | ~209 | ~156 | -0.14 | [omitted]? | D5:3: Andrew: [omitted] … | Andrew: [omitted] … |
| 89 | 44 5-23 | mh | H | F | 30 | ~446 | 0.34/0.50 | [omitted]? | D5:5: Andrew: [omitted]'[omitted]'s doing great in his new home. Still getting used … |
| 90 | 44 5-23 | mh | H | R | ~221 | ~453 | -0.14 | [omitted]? | D5:7: Andrew: I'[omitted]'s doing great in his new home. Still getting used … |
| 91 | 44 5-35 | mh | H | F | 12 | ~292 | 0.42/0.48 | What are the names of Andrew'[omitted]'s a bundle of … | Andrew: Thanks, I think that'[omitted]'ll take good ca… |
| 92 | 44 5-35 | mh | H | R | ~72 | ~610 | -0.05 | What are the names of Andrew'[omitted]'Scout'[omitted]'s what I need to hear. I'll take good ca… |
| 93 | 44 5-39 | mh | H | R | ~227 | ~352 | -0.16 | What did Audrey get wtih having so many dogs? | D2:15: Audrey: You'll love them! They'[omitted]'t get bored at all. [imag… |
| 94 | 44 5-49 | mh | H | F | 17 | ~408 | 0.40/0.48 | What does Audrey view her pets as? | D23:18: Audrey: I think i'[omitted]'t imagine my life without them - my … | Audrey: [omitted]… |
| 95 | 44 5-50 | mh | H | R | ~141 | ~71 | -0.07 | [omitted]? | D17:4: Audrey: [omitted]… | Audrey: [omitted].… |
| 96 | 44 5-51 | mh | H | R | ~108 | ~279 | -0.05 | [omitted]? | D18:10: Audrey: [omitted]… | Audrey: [omitted]… |
| 97 | 44 5-54 | mh | H | F | 28 | ~96 | 0.35/0.48 | What activity do Audrey'[omitted]'[omitted]udrey: [omitted]… |
| 98 | 47 6-2 | mh | H | R | ~161 | ~583 | -0.08 | [omitted]? | D1:36: James: [omitted]! | James: That'[omitted]'s having a good time in the … |
| 99 | 47 6-2 | mh | H | F | 30 | ~370 | 0.34/0.47 | [omitted]? | D21:15: James: Well, how about we go to McGee'[omitted]'[omitted] … |
| 100 | 47 6-5 | mh | H | R | ~110 | ~314 | -0.07 | What are John and James'[omitted]'[omitted]… | John: [omitted]? |
| 101 | 47 6-8 | mh | H | R | ~50 | ~429 | -0.04 | How many pets does James have? | D1:12: James: [omitted],… | James: [omitted]… |
| 102 | 47 6-8 | mh | H | F | 14 | ~169 | 0.41/0.48 | How many pets does James have? | D5:1: James: [omitted]… | James: [omitted]… |
| 103 | 47 6-27 | mh | H | F | 16 | ~107 | 0.40/0.48 | What kind of classes has James joined? | D13:6: James: [omitted] … | James: [omitted]… |
| 104 | 47 6-38 | mh | H | R | ~72 | ~35 | -0.03 | What happened to John'[omitted]'[omitted]'[omitted]'m super stoke… |
| 105 | 47 6-40 | mh | H | R | ~47 | ~311 | -0.04 | What kind of tricks do James'[omitted]'s… | James: [omitted]… |
| 106 | 47 6-40 | mh | H | F | 28 | ~184 | 0.35/0.48 | What kind of tricks do James'[omitted]'[omitted]! [image… | James: [omitted]… |
| 107 | 47 6-40 | mh | H | F | 26 | ~162 | 0.35/0.48 | What kind of tricks do James's pets know? | D17:16: James: [omitted]… | James: [omitted]… |
| 108 | 47 6-56 | mh | H | F | 5 | ~157 | 0.47/0.71 | Which of James'[omitted]'[omitted]… | James: [omitted]… |
| 109 | 48 7-13 | mh | H | F | 12 | ~377 | 0.42/0.49 | What places give Deborah peace? | D19:17: Deborah: [omitted]'[omitted]'[omitted]… |
| 110 | 48 7-14 | mh | H | F | 12 | ~133 | 0.42/0.49 | What were Deborah's mother'[omitted]'[omitted]… |
| 111 | 48 7-14 | mh | H | F | 7 | ~490 | 0.46/0.49 | What were Deborah's mother'[omitted]'[omitted]… |
| 112 | 48 7-20 | mh | H | F | 15 | ~551 | 0.41/0.49 | [omitted]? | D20:1: Jolene: [omitted].… | Jolene: [omitted]. |
| 113 | 48 7-82 | mh | H | R | ~41 | ~344 | -0.02 | [omitted]? | D2:13: Deborah: [omitted]… | Deborah: [omitted]… |
| 114 | 48 7-84 | mh | H | F | 26 | ~530 | 0.35/0.73 | [omitted]? | D17:12: Jolene: [omitted]… | Jolene: [omitted]… |
| 115 | 48 7-84 | mh | H | F | 22 | ~531 | 0.37/0.73 | [omitted]? | D4:5: Jolene: [omitted]… | Jolene: [omitted]… |
| 116 | 48 7-86 | mh | H | R | ~185 | ~75 | -0.08 | What gifts has Deborah received? | D23:22: Deborah: [omitted]… | Deborah: [omitted]… |
| 117 | 48 7-88 | mh | H | R | ~66 | ~220 | -0.07 | [omitted]? | D12:1: Deborah: [omitted]… | Deborah: [omitted]… |
| 118 | 48 7-88 | mh | H | R | ~279 | ~523 | -0.17 | [omitted]? | D15:1: Deborah: [omitted]'[omitted]'ve been focusing on teaching yoga an… |
| 119 | 48 7-88 | mh | H | R | ~90 | ~187 | -0.08 | [omitted]? | D28:11: Deborah: That beach is super special to me. It'[omitted]'ve been focusing on teaching yoga an… |
| 120 | 48 7-88 | mh | H | R | ~126 | ~218 | -0.11 | [omitted]? | D29:1: Deborah: Hey Jolene, I'[omitted]'ve been focusing on teaching yoga an… |
| 121 | 49 8-7 | mh | H | F | 20 | ~361 | 0.38/0.49 | What new hobbies did Sam consider trying? | D10:8: Sam: Wow Evan, that'[omitted]'s catch up soon and … |
| 122 | 49 8-7 | mh | H | F | 3 | ~261 | 0.48/0.49 | What new hobbies did Sam consider trying? | D20:6: Sam: I used to love hiking, but it'[omitted]'s catch up soon and … |
| 123 | 49 8-7 | mh | H | R | ~97 | ~127 | -0.05 | What new hobbies did Sam consider trying? | D7:2: Sam: [omitted].… | Evan: [omitted] … |
| 124 | 49 8-7 | mh | H | R | ~240 | ~370 | -0.10 | What new hobbies did Sam consider trying? | D7:4: Sam: The cooking class has been great, I'[omitted]'s catch up soon and … |
| 125 | 49 8-7 | mh | H | R | ~154 | ~118 | -0.07 | What new hobbies did Sam consider trying? | D7:6: Sam: [omitted]… | Evan: [omitted] … |
| 126 | 49 8-14 | mh | H | R | ~44 | ~285 | -0.02 | What is Evan'[omitted]'s ma… | Evan: [omitted]… |
| 127 | 49 8-21 | mh | H | F | 18 | ~72 | 0.39/0.48 | [omitted]… | D23:3: Evan: [omitted]… | Sam: [omitted]? |
| 128 | 49 8-32 | mh | H | F | 6 | ~62 | 0.46/0.47 | [omitted]? | D21:20: Evan: [omitted]'[omitted]'m doing my b… |
| 129 | 49 8-37 | mh | H | F | 29 | ~123 | 0.34/0.73 | [omitted]… | D18:6: Sam: Exactly, it'[omitted]'s great to hear! Feeling more energized after me… |
| 130 | 49 8-37 | mh | H | F | 3 | ~148 | 0.48/0.73 | [omitted]… | D8:1: Sam: Hey Evan, some big news: I'[omitted]'m determined. [… | Evan: [omitted]… |
| 131 | 49 8-42 | mh | H | F | 19 | ~71 | 0.39/0.77 | How did Evan get into painting? | D1:14: Evan: Yep, it'[omitted]'s alwa… |
| 132 | 49 8-42 | mh | H | F | 23 | 29 | 0.71/0.77 | How did Evan get into painting? | D1:16: Evan: [omitted]! | Sam: [omitted]… |
| 133 | 49 8-48 | mh | H | R | ~75 | ~136 | -0.06 | [omitted]? | D21:9: Sam: Wish I could feel the same about love, but I'[omitted]'[omitted] … |
| 134 | 49 8-50 | mh | H | R | ~81 | ~252 | -0.04 | What kind of hobbies does Evan pursue? | D1:6: Evan: [omitted]! | Evan: What other hobbies have you found for yourself? |
| 135 | 49 8-50 | mh | H | R | ~60 | ~355 | -0.03 | What kind of hobbies does Evan pursue? | D25:10: Evan: [omitted].… | Evan: What other hobbies have you found for yourself? |
| 136 | 49 8-50 | mh | H | R | ~188 | ~83 | -0.08 | What kind of hobbies does Evan pursue? | D25:8: Evan: [omitted]… | Evan: What other hobbies have you found for yourself? |
| 137 | 49 8-50 | mh | H | F | 9 | ~209 | 0.44/0.48 | What kind of hobbies does Evan pursue? | D6:1: Evan: [omitted]… | Evan: What other hobbies have you found for yourself? |
| 138 | 49 8-50 | mh | H | F | 4 | ~266 | 0.48/0.48 | What kind of hobbies does Evan pursue? | D8:30: Evan: [omitted]. | Evan: What other hobbies have you found for yourself? |
| 139 | 49 8-50 | mh | H | R | ~152 | ~381 | -0.07 | What kind of hobbies does Evan pursue? | D9:6: Evan: [omitted]… | Evan: What other hobbies have you found for yourself? |
| 140 | 49 8-54 | mh | H | R | ~98 | ~415 | -0.06 | [omitted]? | D9:2: Evan: [omitted]'[omitted]'[omitted] … |
| 141 | 49 8-62 | mh | H | R | ~109 | ~272 | -0.06 | What health scares did Sam and Evan experience? | D14:2: Evan: [omitted]… | Sam: [omitted]… |
| 142 | 49 8-64 | mh | H | F | 16 | ~41 | 0.40/0.50 | [omitted]? | D14:1: Sam: [omitted]… | Sam: [omitted]… |
| 143 | 49 8-72 | mh | H | R | ~60 | ~172 | -0.01 | [omitted]? | D22:1: Sam: [omitted]… | Evan: [omitted]. |
| 144 | 49 8-77 | mh | H | R | ~72 | ~285 | -0.03 | [omitted]? | D23:15: Evan: [omitted]… | Evan: [omitted]… |
| 145 | 49 8-77 | mh | H | F | 16 | ~135 | 0.40/0.47 | [omitted]? | D23:23: Evan: [omitted]… | Evan: [omitted]… |
| 146 | 49 8-77 | mh | H | R | ~126 | ~155 | -0.05 | [omitted]? | D23:25: Evan: We'[omitted]'re really exc… | Evan: [omitted]… |
| 147 | 49 8-77 | mh | H | R | ~316 | ~456 | -0.13 | [omitted]? | D24:9: Evan: [omitted]. … | Evan: [omitted]… |
| 148 | 50 9-3 | mh | H | R | ~193 | ~322 | -0.10 | Which bands has Dave enjoyed listening to? | D23:9: Dave: The Fireworks headlined the festival. | Calvin: [omitted]? |
| 149 | 50 9-19 | mh | H | R | ~270 | ~209 | -0.17 | [omitted]? | D12:7: Calvin: Aww, that'[omitted]'m actually going to Tokyo next month after the … |
| 150 | 50 9-23 | mh | H | R | ~91 | ~176 | -0.03 | What is Dave's main passion? | D3:12: Dave: [omitted]… | Dave: [omitted]… |
| 151 | 50 9-23 | mh | H | F | 7 | ~290 | 0.46/0.48 | What is Dave'[omitted]'s a step towards my other … | Dave: [omitted]… |
| 152 | 50 9-23 | mh | H | R | ~160 | ~309 | -0.05 | What is Dave'[omitted]'[omitted]… | Dave: [omitted]… |
| 153 | 50 9-29 | mh | H | R | ~139 | ~37 | -0.09 | What are Dave'[omitted]'s cool that you'[omitted]'ve been getti… | Calvin: [omitted]… |
| 154 | 50 9-29 | mh | H | R | ~334 | ~77 | -0.17 | What are Dave'[omitted]'[omitted]… | Calvin: [omitted]… |
| 155 | 50 9-29 | mh | H | R | ~221 | ~282 | -0.12 | What are Dave's hobbies other than fixing cars? | D5:9: Dave: [omitted]… | Calvin: [omitted]… |
| 156 | 50 9-29 | mh | H | R | ~182 | ~289 | -0.11 | What are Dave'[omitted]'[omitted]… | Calvin: [omitted]… |
| 157 | 50 9-45 | mh | H | F | 19 | ~495 | 0.39/0.48 | What shared activities do Dave and Calvin have? | D21:3: Calvin: [omitted]… | Calvin: [omitted]… |
| 158 | 50 9-45 | mh | H | R | ~270 | ~485 | -0.11 | What shared activities do Dave and Calvin have? | D21:4: Dave: [omitted]… | Calvin: [omitted]… |
| 159 | 50 9-46 | mh | H | R | ~116 | ~333 | -0.04 | What is Dave'[omitted]'s therapeutic to… | Dave: [omitted]… |
| 160 | 50 9-46 | mh | H | R | ~73 | ~95 | -0.02 | What is Dave'[omitted]'s been my goal since I was a kid and it'[omitted]'m passionate about. Doing… |
| 161 | 50 9-56 | mh | H | R | ~32 | ~68 | -0.00 | Which cities did Dave travel to in 2023? | D14:1: Dave: Hey Cal, how'[omitted]'t wait to see y… |
| 162 | 50 9-56 | mh | H | F | 11 | ~44 | 0.29/0.33 | Which cities did Dave travel to in 2023? | D26:2: Dave: [omitted]… | Dave: [omitted]… |
| 163 | 50 9-58 | mh | H | F | 13 | ~218 | 0.42/0.46 | Which events in Dave'[omitted]'ve alway… |
| 164 | 50 9-58 | mh | H | F | 9 | ~548 | 0.44/0.46 | Which events in Dave'[omitted]'ve alway… |
| 165 | 26 0-7 | mh | N | F | 26 | ~382 | 0.35/0.48 | What is Caroline'[omitted]'m really lucky to have them. They'[omitted]'ve known th… | Melanie: Wow, Caroline. We'[omitted]'s more to do. Yo… |
| 166 | 26 0-34 | mh | N | R | ~251 | ~138 | -0.13 | [omitted]? | D3:3: Caroline: [omitted]… | Melanie: Wow, Caroline. We'[omitted]'s more to do. Yo… |
| 167 | 26 0-37 | mh | N | F | 20 | ~184 | 0.38/0.47 | What did Melanie paint recently? | D9:17: Melanie: [omitted]… | Melanie: [omitted] … |
| 168 | 26 0-38 | mh | N | F | 17 | ~308 | 0.40/0.48 | [omitted]? | D3:14: Melanie: [omitted]. [image: a photo of a man an… | Melanie: [omitted] … |
| 169 | 26 0-38 | mh | N | R | ~89 | ~137 | -0.06 | [omitted]? | D8:4: Melanie: [omitted]… | Melanie: [omitted] … |
| 170 | 26 0-40 | mh | N | F | ~39 | 15 | 0.27/0.56 | [omitted]? | D6:16: Melanie: [omitted]'[omitted]' faces so happy at the beach was the best! We… |
| 171 | 26 0-43 | mh | N | R | ~178 | ~161 | -0.13 | What kind of art does Caroline make? | D11:12: Caroline: [omitted]… | Caroline: [omitted] … |
| 172 | 26 0-47 | mh | N | F | ~50 | 5 | 0.47/0.48 | [omitted]? | D12:1: Caroline: Hey Mel! How're ya doin'[omitted]'s face light up when they saw the support … |
| 173 | 26 0-48 | mh | N | R | ~207 | ~364 | -0.14 | [omitted]? | D12:14: Melanie: I appreciate our friendship too, Caroline. You'[omitted]'s been super busy here. So much since we ta… |
| 174 | 26 0-75 | mh | N | R | ~103 | ~310 | -0.06 | How many children does Melanie have? | D18:1: Melanie: [omitted] … | Caroline: [omitted]… |
| 175 | 30 1-27 | mh | N | R | ~36 | ~312 | -0.01 | [omitted]? | D14:14: Gina: Way to go, Jon! Don'[omitted]'s a pic from … | Gina: [omitted]… |
| 176 | 30 1-31 | mh | N | R | ~42 | ~348 | -0.03 | How long did it take for Jon to open his studio? | D15:13: Jon: [omitted]! [image: a photo of a man… | Gina: [omitted]… |
| 177 | 30 1-31 | mh | N | F | ~32 | 30 | 0.34/0.48 | How long did it take for Jon to open his studio? | D1:2: Jon: [omitted] … | Gina: [omitted]… |
| 178 | 30 1-44 | sh | N | R | ~273 | ~189 | -0.25 | [omitted]? | D1:26: Jon: Yeah, they'[omitted]'ve been practicing hard and will defin… | Jon: [omitted]… |
| 179 | 41 2-32 | mh | N | R | ~229 | ~230 | -0.09 | [omitted]? | D16:2: Maria: Hey John! Cool that it'[omitted]'m busy … | John: [omitted]… |
| 180 | 41 2-44 | mh | N | R | ~412 | ~543 | -0.15 | [omitted]? | D28:5: John: [omitted]… | Maria: [omitted]… |
| 181 | 41 2-49 | mh | N | R | ~617 | - | -0.32 | [omitted]? | D25:19: John: Yeah, it'[omitted]'[omitted]… |
| 182 | 42 3-1 | mh | N | R | ~220 | ~104 | -0.10 | What kind of interests do Joanna and Nate share? | D20:2: Joanna: [omitted]'[omitted]'m a big fan of them and thought it would be a fun… |
| 183 | 42 3-1 | mh | N | R | ~175 | ~190 | -0.07 | What kind of interests do Joanna and Nate share? | D3:4: Nate: [omitted]… | Nate: [omitted]… |
| 184 | 42 3-51 | mh | N | R | ~32 | ~142 | -0.00 | When did Nate get Tilly for Joanna? | D24:2: Joanna: [omitted]… | Nate: Glad to hear it! What made you name her Tilly? |
| 185 | 42 3-55 | mh | N | R | ~78 | ~241 | -0.02 | What is Joanna inspired by? | D4:6: Joanna: [omitted]. | Nate: [omitted]… |
| 186 | 42 3-55 | mh | N | F | ~90 | 28 | 0.35/0.47 | What is Joanna inspired by? | D7:6: Joanna: [omitted]… | Nate: [omitted]… |
| 187 | 42 3-56 | mh | N | R | ~101 | ~241 | -0.04 | What animal do both Nate and Joanna like? | D26:9: Joanna: [omitted]… | Joanna: [omitted] … |
| 188 | 42 3-58 | mh | N | R | ~364 | ~596 | -0.20 | What things has Nate reccomended to Joanna? | D2:14: Nate: [omitted] … | Joanna: [omitted] … |
| 189 | 42 3-59 | mh | N | F | 9 | ~493 | 0.44/0.71 | What does Joanna do to remember happy memories? | D15:9: Joanna: [omitted]. [image: … | Joanna: [omitted]… |
| 190 | 42 3-61 | mh | N | F | 22 | ~533 | 0.37/0.48 | What mediums does Nate use to play games? | D22:2: Nate: [omitted]… | Nate: [omitted]… |
| 191 | 42 3-62 | mh | N | F | 18 | ~232 | 0.39/0.48 | How many letters has Joanna recieved? | D14:1: Joanna: [omitted]… | Joanna: Awww! How long have you had them? |
| 192 | 42 3-62 | mh | N | F | 5 | ~426 | 0.47/0.48 | How many letters has Joanna recieved? | D18:5: Joanna: [omitted] … | Joanna: Awww! How long have you had them? |
| 193 | 42 3-67 | mh | N | R | ~43 | ~178 | -0.02 | What pets does Nate have? | D28:23: Nate: [omitted]… | Nate: [omitted]… |
| 194 | 42 3-67 | mh | N | R | ~57 | ~118 | -0.04 | What pets does Nate have? | D8:3: Nate: [omitted] … | Nate: [omitted]… |
| 195 | 42 3-69 | mh | N | R | ~59 | ~106 | -0.05 | How many turtles does Nate have? | D8:3: Nate: [omitted] … | Nate: [omitted]… |
| 196 | 42 3-70 | mh | N | F | 14 | ~478 | 0.41/0.47 | What activities does Nate do with his turtles? | D25:21: Nate: [omitted]'[omitted]'m not sure I'll ever understand why watching my… |
| 197 | 42 3-74 | mh | N | R | ~529 | ~94 | -0.28 | [omitted]? | D15:15: Joanna: [omitted]ate: [omitted]! |
| 198 | 42 3-74 | mh | N | R | ~305 | ~76 | -0.19 | [omitted]? | D19:16: Joanna: [omitted]! | Nate: [omitted]! |
| 199 | 42 3-78 | mh | N | F | 14 | ~70 | 0.41/0.82 | [omitted]? | D19:1: Nate: [omitted] … | Nate: [omitted]… |
| 200 | 42 3-79 | mh | N | R | ~63 | ~118 | -0.05 | How many screenplays has Joanna written? | D12:13: Nate: [omitted]? | Joanna: [omitted]… |
| 201 | 42 3-79 | mh | N | R | ~130 | ~394 | -0.10 | How many screenplays has Joanna written? | D12:14: Joanna: Yep! I chose to write about this because it'[omitted]'s about loss, identity, a… | Joanna: [omitted]… |
| 202 | 42 3-79 | mh | N | F | 13 | ~416 | 0.42/0.48 | How many screenplays has Joanna written? | D5:1: Joanna: [omitted]. … | Joanna: [omitted]… |
| 203 | 42 3-80 | mh | N | F | 8 | ~52 | 0.45/0.48 | How many tournaments has Nate won? | D17:1: Nate: [omitted]… | Nate: [omitted]… |
| 204 | 42 3-80 | mh | N | F | 11 | ~41 | 0.43/0.48 | How many tournaments has Nate won? | D22:2: Nate: [omitted]… | Nate: [omitted]… |
| 205 | 42 3-134 | sh | N | R | ~291 | ~297 | -0.19 | What recipe Nate offer to share with Joanna? | D16:10: Nate: [omitted]? | Nate: [omitted]… |
| 206 | 43 4-11 | mh | N | F | 19 | ~96 | 0.39/0.48 | What sports does John like besides basketball? | D1:7: John: [omitted]! [image: a … | Tim: [omitted]… |
| 207 | 43 4-11 | mh | N | F | 26 | ~160 | 0.35/0.48 | What sports does John like besides basketball? | D2:14: John: [omitted]… | Tim: [omitted]… |
| 208 | 43 4-11 | mh | N | R | ~205 | ~196 | -0.12 | What sports does John like besides basketball? | D3:1: John: [omitted]… | Tim: [omitted]… |
| 209 | 43 4-17 | mh | N | R | ~52 | ~72 | -0.03 | How many games has John mentioned winning? | D22:4: John: [omitted]… | John: [omitted] … |
| 210 | 43 4-17 | mh | N | R | ~32 | ~394 | -0.01 | How many games has John mentioned winning? | D3:3: John: [omitted]… | John: [omitted] … |
| 211 | 43 4-17 | mh | N | F | 11 | ~441 | 0.43/0.48 | How many games has John mentioned winning? | D5:2: John: [omitted]… | John: [omitted] … |
| 212 | 43 4-18 | mh | N | R | ~45 | ~161 | -0.03 | What authors has Tim read books from? | D1:14: Tim: It'[omitted]'re f… | Tim: [omitted]… |
| 213 | 43 4-18 | mh | N | R | ~31 | ~378 | -0.00 | What authors has Tim read books from? | D26:36: Tim: I'[omitted]'s coming out called "[omitted]". It's b… | Tim: [omitted]… |
| 214 | 43 4-26 | mh | N | F | 29 | ~84 | 0.34/0.49 | Which TV series does Tim mention watching? | D17:1: John: Hey Tim! Great to chat again. So much has happened! | Tim: [omitted] -… |
| 215 | 43 4-26 | mh | N | R | ~209 | - | -0.09 | Which TV series does Tim mention watching? | D17:11: John: Yeah, I saw "That"! It'[omitted]'s a great … | Tim: [omitted] -… |
| 216 | 43 4-30 | mh | N | F | 11 | ~229 | 0.43/0.48 | Which cities has John been to? | D27:36: John: Yeah! That'[omitted]'[omitted]. … | Tim: [omitted]… |
| 217 | 43 4-49 | mh | N | F | 9 | ~73 | 0.44/0.47 | How many times has John injured his ankle? | D18:2: John: Hey Tim! That'[omitted]'s been a tough week for me with… | John: [omitted]… |
| 218 | 43 4-50 | mh | N | F | ~54 | 30 | 0.34/0.50 | [omitted]? | D18:2: John: Hey Tim! That'[omitted]'s been a tough week for me with… | John: [omitted]… |
| 219 | 43 4-65 | mh | N | F | 23 | ~380 | 0.37/0.48 | What fantasy movies does Tim like? | D26:28: Tim: [omitted]'[omitted]'[omitted]… |
| 220 | 43 4-65 | mh | N | R | ~58 | ~400 | -0.05 | What fantasy movies does Tim like? | D8:18: Tim: [omitted]'[omitted]'[omitted]… |
| 221 | 44 5-2 | mh | N | F | 30 | ~45 | 0.34/0.48 | [omitted]? | D23:1: Andrew: Hey Audrey, it'[omitted]'s it going? Since we last talked, a few new t… |
| 222 | 44 5-3 | mh | N | R | ~82 | ~74 | -0.04 | [omitted] … | D23:3: Andrew: Friday night'[omitted]'m planning to check out… | Audrey: [omitted]… |
| 223 | 44 5-3 | mh | N | F | 14 | ~68 | 0.41/0.48 | [omitted] … | D6:1: Andrew: [omitted] … | Audrey: [omitted]… |
| 224 | 44 5-17 | mh | N | F | 5 | ~76 | 0.47/0.48 | [omitted]? | D24:13: Audrey: I can'[omitted]'s gonna be awesome! | Audrey: It'[omitted]'s going great. We're… |
| 225 | 44 5-24 | mh | N | R | ~91 | ~197 | -0.05 | Did Audrey and Andrew grow up with a pet dog? | D13:10: Audrey: [omitted]… | Audrey: [omitted]… |
| 226 | 44 5-24 | mh | N | R | ~86 | ~37 | -0.04 | Did Audrey and Andrew grow up with a pet dog? | D2:16: Andrew: It'[omitted]'s a photo of my family… | Audrey: [omitted]… |
| 227 | 44 5-26 | mh | N | R | ~55 | ~304 | -0.04 | What is the biggest stressor in Andrew'[omitted]'[omitted]'[omitted]'[omitted]… |
| 228 | 44 5-27 | mh | N | F | 5 | ~371 | 0.47/0.49 | How does Andrew feel about his current work? | D10:16: Andrew: Thanks! I'[omitted]'m stil… | Audrey: [omitted]… |
| 229 | 44 5-27 | mh | N | F | 4 | ~49 | 0.48/0.49 | How does Andrew feel about his current work? | D12:3: Andrew: [omitted]… | Audrey: [omitted]… |
| 230 | 44 5-31 | mh | N | R | ~235 | ~366 | -0.16 | What has Andrew done with his dogs? | D14:27: Andrew: [omitted]! | Andrew: Thanks, I think that'[omitted]'ll take good ca… |
| 231 | 44 5-56 | mh | N | R | ~41 | ~74 | -0.01 | [omitted]? | D28:12: Andrew: [omitted] … | Andrew: [omitted]… |
| 232 | 44 5-60 | mh | N | R | ~30 | ~175 | -0.00 | How many dogs does Andrew have? | D12:1: Andrew: [omitted]'[omitted]'s been amazing hav… |
| 233 | 44 5-60 | mh | N | F | 15 | ~476 | 0.41/0.47 | How many dogs does Andrew have? | D24:2: Andrew: [omitted]'[omitted]'s been amazing hav… |
| 234 | 44 5-60 | mh | N | F | 27 | ~487 | 0.35/0.47 | How many dogs does Andrew have? | D28:6: Andrew: No, we haven'[omitted]'s been amazing hav… |
| 235 | 47 6-3 | mh | N | R | ~129 | ~667 | -0.07 | Do both James and John have pets? | D1:12: James: [omitted],… | John: [omitted]… |
| 236 | 47 6-24 | mh | N | R | ~135 | ~424 | -0.11 | What kind of games has James tried to develop? | D13:7: John: [omitted]… | John: [omitted]? |
| 237 | 47 6-24 | mh | N | R | ~41 | ~194 | -0.01 | What kind of games has James tried to develop? | D27:2: James: [omitted]… | John: [omitted]? |
| 238 | 48 7-19 | mh | N | R | ~57 | ~261 | -0.02 | How many times has Jolene been to France? | D1:8: Jolene: [omitted]… | Jolene: [omitted]… |
| 239 | 48 7-82 | mh | N | F | 28 | ~322 | 0.35/0.47 | [omitted]? | D2:11: Deborah: This is one of the places where I do it. [image: a photo of a living room with a televisio… | Deborah: [omitted]… |
| 240 | 48 7-86 | mh | N | R | ~95 | ~343 | -0.04 | What gifts has Deborah received? | D23:20: Deborah: [omitted]… | Deborah: [omitted]… |
| 241 | 48 7-188 | sh | N | R | ~196 | ~46 | -0.13 | [omitted]? | D29:27: Deborah: It's okay, maybe we can try it together sometime! | Deborah: [omitted]… |
| 242 | 49 8-7 | mh | N | F | 7 | ~348 | 0.46/0.49 | What new hobbies did Sam consider trying? | D21:19: Sam: I do sketch occasionally, but I haven'[omitted]'ll… | Evan: [omitted] … |
| 243 | 49 8-11 | mh | N | R | ~149 | ~417 | -0.13 | [omitted]? | D10:6: Sam: It'[omitted]'s tough w… |
| 244 | 49 8-11 | mh | N | R | ~135 | ~81 | -0.12 | [omitted]? | D13:2: Sam: Hey Evan! It'[omitted]'s tough w… |
| 245 | 49 8-11 | mh | N | F | 8 | ~61 | 0.45/0.48 | [omitted]? | D14:1: Sam: Hey Evan! I'[omitted]'s tough w… |
| 246 | 49 8-11 | mh | N | F | 23 | ~77 | 0.37/0.48 | [omitted]? | D15:1: Sam: Morning, Evan. I'[omitted]'s tough. My fami… | Evan: [omitted]… |
| 247 | 49 8-11 | mh | N | R | ~44 | ~105 | -0.03 | [omitted]? | D16:3: Sam: Thanks, Evan! Appreciate your support. It'[omitted]'s tough w… |
| 248 | 49 8-11 | mh | N | R | ~45 | ~268 | -0.03 | [omitted]? | D24:12: Sam: Thanks, Evan! Haven'[omitted]'s probably a good idea to get some advi… | Evan: [omitted]… |
| 249 | 49 8-11 | mh | N | F | 19 | ~435 | 0.39/0.48 | [omitted]? | D24:14: Sam: I'[omitted]'s tough w… |
| 250 | 49 8-11 | mh | N | F | 17 | ~397 | 0.40/0.48 | [omitted]? | D24:20: Sam: [omitted]. | Evan: [omitted]… |
| 251 | 49 8-11 | mh | N | F | ~90 | 10 | 0.44/0.48 | [omitted]? | D25:3: Sam: [omitted]'[omitted]'s tough w… |
| 252 | 49 8-11 | mh | N | R | ~408 | ~176 | -0.26 | [omitted]? | D5:5: Evan: [omitted]'[omitted]'s tough w… |
| 253 | 49 8-11 | mh | N | R | ~112 | ~43 | -0.10 | [omitted]? | D6:2: Sam: [omitted]… | Evan: [omitted]… |
| 254 | 49 8-11 | mh | N | F | 30 | ~450 | 0.34/0.48 | [omitted]? | D8:1: Sam: Hey Evan, some big news: I'[omitted]'m determined. [… | Evan: [omitted]… |
| 255 | 49 8-14 | mh | N | R | ~34 | ~446 | -0.00 | What is Evan'[omitted]'s great to hear! Feeling more energized after me… |
| 256 | 49 8-28 | mh | N | R | ~183 | ~242 | -0.09 | [omitted]? | D11:7: Sam: [omitted]van: [omitted]… |
| 257 | 49 8-36 | mh | N | R | ~164 | ~207 | -0.07 | [omitted]? | D23:26: Sam: [omitted]… | Evan: [omitted] … |
| 258 | 49 8-44 | mh | N | R | ~45 | ~36 | -0.05 | What kind of subjects does Evan enjoy painting? | D20:13: Evan: [omitted] … | Sam: [omitted]? |
| 259 | 49 8-49 | mh | N | F | 11 | ~67 | 0.43/0.47 | Who was injured in Evan'[omitted]'[omitted]… | Sam: That'[omitted]'s the s… |
| 260 | 49 8-49 | mh | N | R | ~38 | ~395 | -0.02 | Who was injured in Evan'[omitted]'[omitted]… |
| 261 | 49 8-49 | mh | N | R | ~364 | ~284 | -0.18 | Who was injured in Evan'[omitted]'s hard seeing someone we care about hurt. Look af… | Sam: That'[omitted]'s the s… |
| 262 | 49 8-49 | mh | N | F | 8 | ~39 | 0.45/0.47 | Who was injured in Evan'[omitted]'[omitted]… |
| 263 | 49 8-49 | mh | N | R | ~197 | ~200 | -0.09 | Who was injured in Evan'[omitted]'ve hit a sore spot l… | Sam: That'[omitted]'s the s… |
| 264 | 49 8-50 | mh | N | R | ~363 | ~163 | -0.14 | What kind of hobbies does Evan pursue? | D4:8: Evan: [omitted]… | Evan: What other hobbies have you found for yourself? |
| 265 | 49 8-54 | mh | N | R | ~31 | ~422 | -0.00 | [omitted]? | D11:2: Evan: Hey Sam! That'[omitted]'[omitted] … |
| 266 | 49 8-64 | mh | N | R | ~34 | ~83 | -0.01 | [omitted]? | D7:2: Sam: [omitted].… | Sam: [omitted]… |
| 267 | 49 8-81 | mh | N | F | ~74 | 9 | 0.44/0.80 | What is a stress reliever for Evan? | D8:18: Evan: [omitted]… | Evan: [omitted]… |
| 268 | 49 8-82 | mh | N | R | ~221 | ~51 | -0.08 | What is a stress reliever for Sam? | D16:17: Sam: [omitted]… | Evan: [omitted], … |
| 269 | 50 9-11 | mh | N | F | ~102 | 7 | 0.46/0.48 | What mishaps has Calvin run into? | D6:1: Calvin: [omitted]. | Calvin: [omitted]… |
| 270 | 50 9-24 | mh | N | F | 12 | ~43 | 0.42/0.48 | Can Dave work with engines? | D20:1: Dave: [omitted]… | Dave: [omitted]… |
| 271 | 50 9-30 | mh | N | R | ~87 | ~245 | -0.04 | What kind of music does Dave listen to? | D2:10: Dave: [omitted]… | Dave: [omitted]… |
| 272 | 50 9-59 | mh | N | F | 11 | ~389 | 0.43/0.47 | How many Ferraris does Calvin own? | D2:1: Calvin: [omitted]… | Dave: [omitted]… |
| 273 | 50 9-63 | mh | N | R | ~221 | ~159 | -0.09 | What style of guitars does Calvin own? | D16:4: Calvin: [omitted] … | Calvin: [omitted]! |
| 274 | 50 9-66 | mh | N | F | 16 | ~93 | 0.40/0.48 | Do all of Dave'[omitted]'[omitted]'[omitted]'s a pic of the car I'm r… |
| 275 | 50 9-66 | mh | N | F | 10 | ~128 | 0.44/0.48 | Do all of Dave'[omitted]'s a pic of the car I'm r… |
| 276 | 50 9-66 | mh | N | F | 7 | ~161 | 0.46/0.48 | Do all of Dave'[omitted]'s a pic of the car I'm r… |
| 277 | 26 0-14 | od | I | F | ~73 | 11 | 0.43/0.74 | [omitted]'[omitted]'t always easy, but I … | Caroline: [omitted]… |
| 278 | 26 0-42 | od | I | F | 11 | ~187 | 0.43/0.47 | [omitted]… | D10:12: Melanie: [omitted]… | Melanie: Thanks, Caroline. It'[omitted]'m doi… |
| 279 | 26 0-42 | od | I | R | ~109 | ~156 | -0.05 | [omitted]… | D10:14: Melanie: I'[omitted]'s still a work in progress, but I'm doi… |
| 280 | 26 0-50 | od | I | R | ~104 | ~112 | -0.04 | What would Caroline'[omitted]'re ya doin'[omitted]'[omitted]… |
| 281 | 26 0-59 | od | I | F | 12 | ~305 | 0.42/0.48 | Would Caroline be considered religious? | D14:19: Caroline: [omitted]… | Melanie: [omitted] … |
| 282 | 26 0-69 | od | I | R | ~65 | ~147 | -0.02 | [omitted]? | D13:16: Melanie: Wow, Caroline! That'[omitted]'ll start thinking about what we can do. |
| 283 | 26 0-69 | od | I | F | 17 | ~59 | 0.40/0.48 | [omitted]? | D7:4: Melanie: Wow, Caroline. We'[omitted]'s more to do. Your drive to help is awesome! W… | Melanie: [omitted]. |
| 284 | 41 2-8 | od | I | R | ~81 | ~258 | -0.02 | What might John'[omitted]'s definitely isn'[omitted]'t. We really need to do som… | Maria: Glad I could help, John. What's up next for you? |
| 285 | 41 2-41 | od | I | R | ~182 | ~71 | -0.06 | Does John live close to a beach or the mountains? | D22:15: John: [omitted] … | John: [omitted]… |
| 286 | 41 2-45 | od | I | R | ~157 | ~291 | -0.06 | Would John be open to moving to another country? | D24:3: John: [omitted]… | Maria: [omitted]… |
| 287 | 41 2-45 | od | I | R | ~290 | ~338 | -0.09 | Would John be open to moving to another country? | D7:2: John: Hey Maria! Wanted to let you know that I'[omitted]'s been a wild ride, b… | Maria: [omitted]… |
| 288 | 41 2-50 | od | I | R | ~397 | ~484 | -0.12 | What attributes describe John? | D26:6: John: [omitted], … | Maria: [omitted]… |
| 289 | 41 2-50 | od | I | R | ~93 | ~453 | -0.03 | What attributes describe John? | D2:14: John: [omitted]'[omitted]'s really cool seeing you ma… |
| 290 | 41 2-50 | od | I | R | ~392 | ~545 | -0.12 | What attributes describe John? | D3:5: John: [omitted]… | Maria: [omitted]… |
| 291 | 41 2-50 | od | I | R | ~481 | ~235 | -0.15 | What attributes describe John? | D4:6: John: [omitted] … | Maria: [omitted]… |
| 292 | 41 2-64 | od | I | R | ~327 | ~377 | -0.13 | What job might Maria pursue in the future? | D11:10: Maria: [omitted]… | Maria: I bet! What are your plans for the future? |
| 293 | 41 2-64 | od | I | R | ~177 | ~295 | -0.07 | What job might Maria pursue in the future? | D27:4: Maria: [omitted]… | Maria: I bet! What are your plans for the future? |
| 294 | 41 2-64 | od | I | R | ~281 | ~132 | -0.11 | What job might Maria pursue in the future? | D32:14: Maria: [omitted]… | Maria: I bet! What are your plans for the future? |
| 295 | 41 2-64 | od | I | R | ~96 | ~243 | -0.04 | What job might Maria pursue in the future? | D5:8: Maria: [omitted]… | Maria: I bet! What are your plans for the future? |
| 296 | 42 3-0 | od | I | R | ~374 | ~602 | -0.19 | [omitted]? | D1:7: Nate: [omitted]… | Nate: [omitted]… |
| 297 | 42 3-14 | od | I | R | ~184 | ~66 | -0.10 | What nickname does Nate use for Joanna? | D7:1: Nate: [omitted]! | Nate: [omitted]! |
| 298 | 42 3-66 | od | I | R | ~143 | ~118 | -0.09 | [omitted]? | D19:3: Nate: I'[omitted]'m super proud of what I accomplished.… | Nate: [omitted]… |
| 299 | 42 3-66 | od | I | R | ~444 | ~353 | -0.23 | [omitted]? | D25:19: Nate: [omitted]. [image: a… | Nate: [omitted]… |
| 300 | 42 3-66 | od | I | R | ~328 | ~475 | -0.15 | [omitted]? | D28:25: Nate: [omitted] … | Nate: [omitted]… |
| 301 | 42 3-66 | od | I | R | ~417 | ~347 | -0.20 | [omitted]? | D5:8: Nate: [omitted]… | Nate: [omitted]… |
| 302 | 42 3-68 | od | I | F | 9 | ~163 | 0.44/0.47 | How many hikes has Joanna been on? | D28:22: Joanna: [omitted]… | Joanna: [omitted]… |
| 303 | 42 3-68 | od | I | F | 28 | ~475 | 0.35/0.47 | How many hikes has Joanna been on? | D7:6: Joanna: [omitted]… | Joanna: [omitted]… |
| 304 | 42 3-87 | od | I | F | 7 | ~469 | 0.46/0.48 | What state did Nate visit? | D29:6: Nate: [omitted]… | Nate: [omitted]… |
| 305 | 43 4-3 | od | I | R | ~59 | ~106 | -0.04 | [omitted]? | D1:14: Tim: It'[omitted]'re f… | John: [omitted]… |
| 306 | 43 4-3 | od | I | R | ~307 | ~284 | -0.16 | [omitted]? | D1:16: Tim: Thanks! We'[omitted]'s awesome to see how sports can unite people. B… |
| 307 | 43 4-3 | od | I | R | ~95 | ~668 | -0.07 | [omitted]? | D1:18: Tim: [omitted]… | John: [omitted]… |
| 308 | 43 4-5 | od | I | R | ~156 | ~184 | -0.08 | Based on Tim'[omitted]'s an awesome book collection! It's cool to escap… |
| 309 | 43 4-19 | od | I | R | ~43 | ~167 | -0.02 | [omitted]… | D3:13: John: [omitted]'[omitted]'[omitted] … |
| 310 | 43 4-19 | od | I | R | ~195 | ~254 | -0.09 | [omitted]… | D3:15: John: [omitted]'[omitted]'[omitted] … |
| 311 | 43 4-34 | od | I | F | 17 | ~136 | 0.40/0.48 | What could John do after his basketball career? | D26:1: John: Hey Tim! Great to hear from you. My week'[omitted]'s awesome! I could stop by there after my season. |
| 312 | 43 4-34 | od | I | R | ~117 | ~291 | -0.09 | What could John do after his basketball career? | D27:26: John: He'[omitted]'s why he eventually becomes king. | John: [omitted]. |
| 313 | 43 4-53 | od | I | R | ~79 | ~326 | -0.06 | [omitted]? | D20:2: John: [omitted]… | John: [omitted]… |
| 314 | 43 4-67 | od | I | R | ~53 | ~294 | -0.02 | [omitted]? | D15:3: Tim: [omitted]im: [omitted] … |
| 315 | 43 4-67 | od | I | R | ~252 | ~336 | -0.13 | [omitted]? | D4:1: Tim: Hey John! How'[omitted]'m writing articles about fantasy nov… | Tim: [omitted] … |
| 316 | 43 4-67 | od | I | R | ~70 | ~213 | -0.05 | [omitted]? | D6:6: Tim: [omitted]'[omitted]'[omitted] … |
| 317 | 44 5-19 | od | I | R | ~120 | ~200 | -0.08 | [omitted]… | D10:12: Andrew: Lately I'[omitted]'t hike. I'[omitted]'t wait to see their happy face! This was my dog … |
| 318 | 44 5-19 | od | I | R | ~62 | ~343 | -0.03 | [omitted]… | D12:1: Andrew: [omitted]'[omitted]'t wait to see their happy face! This was my dog … |
| 319 | 44 5-33 | od | I | R | ~171 | ~311 | -0.10 | [omitted]… | D21:5: Andrew: Haven'[omitted]'s hard to find open spaces in… | Andrew: [omitted] … |
| 320 | 44 5-43 | od | I | R | ~305 | ~667 | -0.12 | [omitted]? | D11:9: Andrew: [omitted]'[omitted]'s t… | Audrey: [omitted]… |
| 321 | 44 5-44 | od | I | R | ~124 | ~155 | -0.06 | [omitted]… | D11:9: Andrew: [omitted]'[omitted]'s t… | Audrey: [omitted]… |
| 322 | 44 5-52 | od | I | F | 22 | ~180 | 0.37/0.48 | [omitted]… | D1:14: Andrew: I'[omitted]'s really peaceful and calming. It's nice to … |
| 323 | 44 5-52 | od | I | R | ~285 | ~262 | -0.13 | [omitted]… | D20:5: Andrew: [omitted]? | Andrew: Yeah do that! It'[omitted]'s nice to … |
| 324 | 44 5-52 | od | I | R | ~153 | ~225 | -0.08 | [omitted]… | D23:1: Andrew: Hey Audrey, it'[omitted]'s really peaceful and calming. It's nice to … |
| 325 | 44 5-53 | od | I | F | ~51 | 20 | 0.38/0.50 | [omitted]… | D2:18: Andrew: [omitted]… | Andrew: No, no pets right now. But I do love animals. |
| 326 | 44 5-53 | od | I | R | ~138 | ~60 | -0.09 | [omitted]… | D3:1: Andrew: [omitted]… | Andrew: No, no pets right now. But I do love animals. |
| 327 | 44 5-53 | od | I | R | ~34 | ~95 | -0.00 | [omitted]… | D5:7: Andrew: [omitted]… | Andrew: No, no pets right now. But I do love animals. |
| 328 | 44 5-53 | od | I | R | ~82 | ~80 | -0.05 | [omitted]… | D8:27: Andrew: Agreed! It'[omitted]'m in … | Andrew: No, no pets right now. But I do love animals. |
| 329 | 47 6-6 | od | I | R | ~77 | ~60 | -0.03 | Does James live in Connecticut? | D5:1: James: [omitted]… | James: [omitted] … |
| 330 | 47 6-19 | od | I | R | ~104 | ~223 | -0.04 | [omitted]? | D9:16: James: [omitted]. | James: [omitted] … |
| 331 | 47 6-33 | od | I | R | ~505 | ~667 | -0.18 | Did John and James study together? | D17:13: John: Your support means a lot to me. You'[omitted]'[omitted]… |
| 332 | 48 7-11 | od | I | R | ~196 | ~336 | -0.08 | Is Deborah married? | D19:11: Deborah: [omitted]'[omitted]'s wonderful! How long have you been married? |
| 333 | 48 7-23 | od | I | R | ~416 | ~173 | -0.21 | Why did Jolene sometimes put off doing yoga? | D2:30: Jolene: We are planning to play "[omitted]" next Saturday. | Jolene: [omitted]… |
| 334 | 48 7-23 | od | I | R | ~129 | ~189 | -0.08 | Why did Jolene sometimes put off doing yoga? | D3:11: Jolene: [omitted]. | Jolene: [omitted]… |
| 335 | 48 7-36 | od | I | R | ~178 | ~456 | -0.07 | How old is Jolene? | D13:5: Jolene: I'[omitted]'s been a great opportunity to test my sk… | Jolene: How old is Luna? |
| 336 | 48 7-36 | od | I | R | ~326 | ~171 | -0.15 | How old is Jolene? | D21:6: Jolene: [omitted]… | Jolene: How old is Luna? |
| 337 | 48 7-36 | od | I | R | ~255 | ~481 | -0.10 | How old is Jolene? | D21:8: Jolene: [omitted]… | Jolene: How old is Luna? |
| 338 | 48 7-36 | od | I | R | ~184 | ~318 | -0.07 | How old is Jolene? | D22:14: Jolene: [omitted]… | Jolene: How old is Luna? |
| 339 | 48 7-36 | od | I | R | ~378 | ~191 | -0.21 | How old is Jolene? | D22:6: Jolene: [omitted]… | Jolene: How old is Luna? |
| 340 | 48 7-36 | od | I | R | ~58 | ~455 | -0.02 | How old is Jolene? | D24:14: Jolene: Got a lot of finals coming up this month, so I'[omitted]'s been quite … | Jolene: How old is Luna? |
| 341 | 48 7-36 | od | I | R | ~332 | ~132 | -0.15 | How old is Jolene? | D24:2: Jolene: [omitted]… | Jolene: How old is Luna? |
| 342 | 48 7-36 | od | I | R | ~288 | ~255 | -0.11 | How old is Jolene? | D25:5: Jolene: [omitted]. | Jolene: How old is Luna? |
| 343 | 48 7-36 | od | I | R | ~478 | ~442 | -0.34 | How old is Jolene? | D26:6: Jolene: [omitted]… | Jolene: How old is Luna? |
| 344 | 48 7-36 | od | I | R | ~375 | ~101 | -0.20 | How old is Jolene? | D8:2: Jolene: [omitted]… | Jolene: How old is Luna? |
| 345 | 49 8-10 | od | I | R | ~273 | ~450 | -0.13 | [omitted]… | D19:1: Evan: Hey Sam, hope you'[omitted]'re doing great. I just got… |
| 346 | 49 8-10 | od | I | R | ~71 | ~47 | -0.04 | [omitted]… | D19:3: Evan: So excited and a bit nervous! It'[omitted]'m really l… | Evan: [omitted]… |
| 347 | 49 8-19 | od | I | R | ~324 | ~330 | -0.20 | [omitted]… | D14:12: Evan: [omitted]'[omitted]'m here for ya. Life can be tough sometimes, bu… |
| 348 | 49 8-19 | od | I | R | ~224 | ~340 | -0.12 | [omitted]… | D22:1: Sam: [omitted]… | Sam: [omitted]… |
| 349 | 49 8-19 | od | I | R | ~360 | ~392 | -0.23 | [omitted]… | D3:10: Sam: Yeah, you'[omitted]'[omitted]. | Sam: [omitted]… |
| 350 | 49 8-19 | od | I | R | ~107 | ~213 | -0.05 | [omitted]… | D3:15: Evan: Sure Sam, I'[omitted]'s get together and I'[omitted]'… | Sam: [omitted]… |
| 351 | 49 8-19 | od | I | R | ~228 | ~110 | -0.12 | [omitted]… | D8:17: Sam: [omitted]? | Sam: [omitted]… |
| 352 | 49 8-19 | od | I | R | ~309 | ~220 | -0.19 | [omitted]… | D8:22: Evan: [omitted]… | Sam: [omitted]… |
| 353 | 49 8-19 | od | I | R | ~348 | ~179 | -0.22 | [omitted]… | D9:11: Sam: I haven'[omitted]'s so tranquil… | Sam: [omitted]… |
| 354 | 49 8-20 | od | I | R | ~59 | ~40 | -0.04 | [omitted]… | D5:9: Evan: [omitted]… | Evan: [omitted]… |
| 355 | 49 8-20 | od | I | R | ~307 | ~25 | -0.17 | [omitted]… | D7:12: Sam: [omitted] … | Evan: [omitted]… |
| 356 | 49 8-20 | od | I | F | 17 | ~281 | 0.40/0.48 | [omitted]… | D8:1: Sam: Hey Evan, some big news: I'[omitted]'m determined. [… | Evan: [omitted]… |
| 357 | 49 8-20 | od | I | R | ~150 | ~307 | -0.08 | [omitted]… | D8:12: Evan: [omitted]… | Evan: [omitted]… |
| 358 | 49 8-20 | od | I | R | ~357 | ~437 | -0.20 | [omitted]… | D8:5: Sam: [omitted]… | Evan: [omitted]… |
| 359 | 49 8-20 | od | I | R | ~320 | ~111 | -0.18 | [omitted]… | D8:7: Sam: Sure, I'[omitted]'[omitted] … | Evan: [omitted]… |
| 360 | 49 8-20 | od | I | R | ~268 | ~347 | -0.15 | [omitted]… | D8:8: Evan: [omitted]! | Evan: [omitted]… |
| 361 | 49 8-20 | od | I | R | ~43 | ~47 | -0.03 | [omitted]… | D9:1: Sam: [omitted]… | Evan: [omitted]… |
| 362 | 49 8-43 | od | I | F | 5 | ~331 | 0.47/0.48 | How often does Sam get health checkups? | D2:6: Sam: [omitted]… | Evan: [omitted]… |
| 363 | 49 8-43 | od | I | R | ~116 | ~83 | -0.06 | How often does Sam get health checkups? | D7:2: Sam: [omitted].… | Evan: [omitted]… |
| 364 | 49 8-51 | od | I | R | ~35 | ~148 | -0.00 | [omitted] … | D14:1: Sam: Hey Evan! I'[omitted]'s tough w… |
| 365 | 49 8-51 | od | I | R | ~64 | ~120 | -0.04 | [omitted] … | D14:2: Evan: [omitted]… | Evan: [omitted]… |
| 366 | 49 8-51 | od | I | R | ~141 | ~475 | -0.10 | [omitted] … | D4:2: Evan: Hey Sam, sorry about that. Don'[omitted]'s work on it together. | Evan: [omitted]… |
| 367 | 49 8-51 | od | I | R | ~70 | ~260 | -0.05 | [omitted] … | D4:6: Evan: [omitted]… | Evan: [omitted]… |
| 368 | 50 9-4 | od | I | R | ~257 | ~133 | -0.12 | Which country do Calvin and Dave want to meet in? | D3:10: Dave: Sounds like an amazing plan, Cal! I can'[omitted]'ll show you around … | Calvin: [omitted] … |
| 369 | 50 9-4 | od | I | F | 27 | ~244 | 0.35/0.48 | Which country do Calvin and Dave want to meet in? | D3:9: Calvin: Thanks, Dave! I'[omitted]'re … |
| 370 | 50 9-37 | od | I | F | 17 | ~401 | 0.40/0.47 | Does Calvin love music tours? | D16:2: Calvin: [omitted]… | Dave: [omitted]… |
| 371 | 50 9-37 | od | I | F | 23 | ~366 | 0.37/0.47 | Does Calvin love music tours? | D18:7: Calvin: [omitted]'[omitted]'m sure y… |
| 372 | 50 9-37 | od | I | F | 16 | ~399 | 0.40/0.47 | Does Calvin love music tours? | D7:1: Calvin: [omitted] … | Dave: [omitted]… |
| 373 | 26 0-35 | tp | T | F | 11 | ~37 | 0.43/0.48 | When did Melanie go camping in July? | D9:1: Melanie: Hey Caroline, hope all'[omitted]'s still a work in progress, but I'm doi… |
| 374 | 30 1-38 | tp | T | F | ~51 | 1 | 0.50/0.73 | [omitted]? | D19:6: Gina: [omitted]… | Gina: [omitted]… |
| 375 | 41 2-10 | tp | T | R | ~177 | ~400 | -0.06 | When did Maria meet Jean? | D7:1: Maria: Hey John, how'[omitted]'s been happening late… | John: [omitted]… |
| 376 | 42 3-22 | tp | T | F | ~32 | 6 | 0.46/0.49 | [omitted]? | D12:13: Nate: [omitted]? | Joanna: [omitted]… |
| 377 | 42 3-22 | tp | T | R | ~172 | ~415 | -0.12 | [omitted]? | D12:14: Joanna: Yep! I chose to write about this because it'[omitted]'s about loss, identity, a… | Joanna: [omitted]… |
| 378 | 42 3-57 | tp | T | F | 6 | ~25 | 0.46/0.49 | When did Joanna plan to go over to Nate'[omitted]'ll come over tomorrow if that's fine. [image: a photo of a bo… | Nate: [omitted]… |
| 379 | 43 4-20 | tp | T | R | ~201 | ~84 | -0.10 | [omitted]? | D5:2: John: [omitted]… | John: [omitted]… |
| 380 | 43 4-24 | tp | T | F | 17 | ~46 | 0.26/0.33 | [omitted]? | D7:1: John: [omitted] … | Tim: [omitted] … |
| 381 | 43 4-45 | tp | T | F | 12 | ~83 | 0.42/0.47 | [omitted]? | D18:1: Tim: Hey John! Hope you'[omitted]'[omitted]… |
| 382 | 43 4-55 | tp | T | F | 12 | ~154 | 0.42/0.47 | When did John start playing professionally? | D1:3: John: [omitted]! | John: [omitted] … |
| 383 | 44 5-0 | tp | T | F | 19 | ~519 | 0.39/0.48 | [omitted]? | D1:7: Audrey: I'[omitted]'t live without… | Audrey: [omitted] … |
| 384 | 44 5-8 | tp | T | F | ~85 | 12 | 0.28/0.33 | Did Andrew have a pet dog during March 2023? | D2:8: Andrew: [omitted]ndrew: No, no pets right now. But I do love animals. |
| 385 | 44 5-16 | tp | T | R | ~36 | ~52 | -0.01 | When is Andrew going to go hiking with Audrey? | D11:7: Andrew: Yeah definitely ! I'[omitted]'s do it next mon… | Audrey: [omitted]… |
| 386 | 44 5-22 | tp | T | F | 4 | ~31 | 0.32/0.49 | [omitted]? | D14:1: Andrew: Hey, Audrey! I can'[omitted]'… | Andrew: [omitted]… |
| 387 | 44 5-45 | tp | T | F | 24 | ~105 | 0.36/0.47 | [omitted]? | D12:1: Andrew: [omitted] … | Andrew: [omitted]… |
| 388 | 44 5-45 | tp | T | F | 13 | ~52 | 0.42/0.47 | [omitted]? | D24:2: Andrew: [omitted]… | Andrew: [omitted]… |
| 389 | 44 5-45 | tp | T | F | 30 | ~59 | 0.34/0.47 | [omitted]? | D28:6: Andrew: [omitted]… | Andrew: [omitted]… |
| 390 | 44 5-46 | tp | T | F | 16 | ~106 | 0.27/0.33 | [omitted]? | D12:1: Andrew: [omitted] … | Andrew: [omitted]… |
| 391 | 44 5-46 | tp | T | F | 8 | ~51 | 0.30/0.33 | [omitted]? | D24:2: Andrew: [omitted]… | Andrew: [omitted]… |
| 392 | 44 5-47 | tp | T | F | 12 | ~537 | 0.42/0.46 | [omitted] | D24:2: Andrew: [omitted]'[omitted]'s been amazing hav… |
| 393 | 44 5-55 | tp | T | R | ~49 | ~369 | -0.02 | When did Andrew make his dogs a fun indoor area? | D28:12: Andrew: [omitted] … | Andrew: [omitted]… |
| 394 | 47 6-11 | tp | T | R | ~164 | ~526 | -0.06 | How was John feeling on April 10, 2022? | D6:7: John: That'[omitted]'s like the virtual world is con… | John: [omitted]… |
| 395 | 47 6-32 | tp | T | F | 26 | ~192 | 0.24/0.31 | Where was James at on July 12, 2022? | D16:9: James: [omitted]… | James: [omitted]… |
| 396 | 47 6-53 | tp | T | F | 13 | ~43 | 0.42/0.48 | [omitted]? | D27:2: James: [omitted]… | James: [omitted]… |
| 397 | 48 7-43 | tp | T | F | 8 | ~177 | 0.45/0.48 | When did Deborah go for a bicycle ride with Anna? | D12:1: Deborah: [omitted]… | Deborah: [omitted]… |
| 398 | 48 7-46 | tp | T | F | 5 | ~32 | 0.47/0.48 | [omitted]… | D12:10: Jolene: Just so you know, I'[omitted]'s been tough but also real… | Jolene: [omitted]… |
| 399 | 48 7-50 | tp | T | F | 21 | ~289 | 0.38/0.48 | Which year did Jolene start practicing yoga? | D13:17: Jolene: I'[omitted]'ve had a real positive eff… | Jolene: [omitted]… |
| 400 | 48 7-52 | tp | T | F | 11 | ~86 | 0.43/0.48 | [omitted]? | D16:2: Jolene: [omitted]'[omitted]'s happened! On Friday I had… |
| 401 | 48 7-54 | tp | T | F | 4 | ~215 | 0.48/0.77 | [omitted]? | D16:6: Jolene: [omitted]. | Jolene: [omitted] … |
| 402 | 48 7-54 | tp | T | R | ~99 | ~165 | -0.04 | [omitted]? | D2:28: Jolene: Even as a child I learned to play on my own. | Jolene: [omitted] … |
| 403 | 48 7-55 | tp | T | R | ~183 | ~192 | -0.07 | [omitted]? | D2:28: Jolene: Even as a child I learned to play on my own. | Jolene: [omitted] … |
| 404 | 48 7-78 | tp | T | R | ~328 | ~335 | -0.14 | [omitted]? | D2:1: Deborah: [omitted]'[omitted]'s me and my partner at one … |
| 405 | 49 8-22 | tp | T | F | 6 | ~379 | 0.46/0.48 | When Evan did meet his future wife? | D5:1: Evan: [omitted] -… | Sam: [omitted]? |
| 406 | 49 8-25 | tp | T | R | ~392 | ~77 | -0.24 | [omitted]? | D5:6: Sam: [omitted] … | Evan: [omitted]… |
| 407 | 49 8-25 | tp | T | F | 2 | ~208 | 0.49/0.73 | [omitted]? | D5:7: Evan: [omitted]… | Evan: [omitted]… |
| 408 | 49 8-70 | tp | T | F | 6 | ~80 | 0.46/0.48 | [omitted]? | D5:1: Evan: [omitted] -… | Evan: [omitted]… |
| 409 | 50 9-10 | tp | T | F | ~43 | 4 | 0.48/0.74 | When did Calvin's place get flooded in Tokyo? | D6:3: Calvin: [omitted]… | Dave: [omitted]… |
| 410 | 50 9-27 | tp | T | R | ~70 | ~97 | -0.04 | Which city was Calvin visiting in August 2023? | D16:6: Calvin: [omitted]… | Calvin: [omitted] … |
| 411 | 50 9-31 | tp | T | F | ~31 | ~38 | 0.33/0.33 | [omitted]? | D14:1: Dave: [omitted] … | Dave: [omitted]… |
| 412 | 50 9-31 | tp | T | R | ~91 | ~532 | -0.03 | [omitted]? | D17:1: Dave: [omitted]… | Dave: [omitted]… |
| 413 | 50 9-48 | tp | T | R | ~36 | ~64 | -0.00 | [omitted]? | D22:1: Dave: [omitted]… | Dave: [omitted]… |
| 414 | 50 9-57 | tp | T | R | ~63 | ~457 | -0.02 | Which hobby did Dave pick up in October 2023? | D27:2: Dave: Hey Calvin! That's cool that you'[omitted]'ve been getti… | Dave: [omitted]… |
| 415 | 50 9-68 | tp | T | F | 8 | ~134 | 0.45/0.50 | [omitted]? | D28:20: Dave: [omitted]… | Dave: [omitted]… |
| 416 | 26 0-19 | mh | O | F | 22 | ~135 | 0.37/0.47 | What do Melanie'[omitted]'ll do… |
| 417 | 26 0-34 | mh | O | F | ~36 | 29 | 0.34/0.48 | [omitted]? | D9:2: Caroline: [omitted]… | Melanie: Wow, Caroline. We'[omitted]'s more to do. Yo… |
| 418 | 26 0-43 | mh | O | R | ~33 | ~77 | -0.00 | What kind of art does Caroline make? | D11:8: Caroline: [omitted] … | Caroline: [omitted] … |
| 419 | 26 0-43 | mh | O | F | ~34 | 26 | 0.35/0.49 | What kind of art does Caroline make? | D9:14: Caroline: [omitted]. [image: a photography of a pain… | Caroline: [omitted] … |
| 420 | 26 0-149 | sh | O | F | ~63 | 26 | 0.35/0.48 | What do Melanie'[omitted]'[omitted]… | Melanie: [omitted]. … |
| 421 | 30 1-23 | mh | O | F | 19 | ~49 | 0.39/0.49 | How did Gina promote her clothes store? | D8:4: Gina: [omitted] … | Gina: [omitted]… |
| 422 | 30 1-45 | sh | O | F | ~98 | 5 | 0.47/0.48 | [omitted]? | D1:28: Jon: Yeah, awesome! Glad to be part of it. | Jon: [omitted]… |
| 423 | 41 2-11 | mh | O | F | ~77 | 3 | 0.48/0.49 | [omitted]? | D7:5: Maria: [omitted] … | Maria: [omitted] … |
| 424 | 41 2-30 | mh | O | F | ~31 | 4 | 0.48/0.48 | What shelters does Maria volunteer at? | D2:1: Maria: [omitted]… | Maria: [omitted]… |
| 425 | 41 2-42 | mh | O | F | ~141 | 10 | 0.44/0.48 | What area was hit by a flood? | D14:21: John: [omitted]… | John: [omitted]… |
| 426 | 41 2-84 | sh | O | F | ~29 | 1 | 0.33/0.48 | [omitted]? | D10:10: Maria: [omitted]… | Maria: [omitted]… |
| 427 | 42 3-55 | mh | O | F | ~128 | 27 | 0.35/0.47 | What is Joanna inspired by? | D26:3: Joanna: [omitted]… | Nate: [omitted]… |
| 428 | 42 3-55 | mh | O | F | ~322 | 8 | 0.45/0.47 | What is Joanna inspired by? | D26:7: Joanna: [omitted]… | Nate: [omitted]… |
| 429 | 42 3-70 | mh | O | F | 18 | ~37 | 0.39/0.47 | What activities does Nate do with his turtles? | D28:31: Nate: Sounds good. Well I'[omitted]'re rea… | Nate: [omitted]… |
| 430 | 42 3-81 | mh | O | R | ~36 | ~68 | -0.01 | What recipes has Joanna made? | D21:17: Joanna: Hey Nate! Here'[omitted]'[omitted]… | Joanna: [omitted]… |
| 431 | 43 4-4 | mh | O | F | ~62 | 13 | 0.42/0.49 | What books has Tim read? | D6:8: Tim: Thanks! "[omitte[omitted]… | Tim: [omitted]… |
| 432 | 43 4-13 | mh | O | F | 26 | ~38 | 0.35/0.48 | What does Tim do to escape reality? | D3:30: Tim: [omitted]. … | Tim: [omitted]… |
| 433 | 43 4-14 | mh | O | F | 13 | ~36 | 0.42/0.48 | What kind of writing does Tim do? | D15:3: Tim: [omitted]im: [omitted]… |
| 434 | 43 4-50 | mh | O | F | 3 | ~38 | 0.48/0.50 | [omitted]? | D19:20: John: I recently finished rereading "[omitted]" - it was really inspiring. It made me think aga… | John: [omitted]… |
| 435 | 44 5-2 | mh | O | F | 11 | ~53 | 0.43/0.48 | [omitted]? | D25:1: Andrew: [omitted] … | Andrew: [omitted]… |
| 436 | 44 5-9 | mh | O | F | 5 | ~53 | 0.47/0.48 | [omitted]… | D6:2: Audrey: [omitted]'[omitted]'m busy taking ca… |
| 437 | 44 5-12 | mh | O | F | 19 | ~103 | 0.39/0.48 | [omitted]? | D14:1: Andrew: Hey, Audrey! I can'[omitted]'… | Andrew: [omitted]… |
| 438 | 44 5-15 | mh | O | R | ~139 | ~197 | -0.07 | [omitted]? | D7:8: Andrew: I'[omitted]'[omitted]… | Audrey: [omitted]… |
| 439 | 44 5-26 | mh | O | F | 19 | ~102 | 0.39/0.48 | What is the biggest stressor in Andrew'[omitted]'re doing good! So I'[omitted]'[omitted]… |
| 440 | 44 5-40 | mh | O | F | 6 | ~261 | 0.46/0.50 | [omitted]? | D14:2: Audrey: [omitted]… | Audrey: [omitted] … |
| 441 | 44 5-42 | mh | O | F | ~56 | 12 | 0.42/0.48 | [omitted]? | D26:5: Audrey: [omitted]… | Audrey: [omitted] … |
| 442 | 44 5-49 | mh | O | F | 8 | ~65 | 0.45/0.48 | What does Audrey view her pets as? | D15:15: Audrey: Yep, pets are family. It'[omitted]'s a photo of m… | Audrey: [omitted]… |
| 443 | 44 5-54 | mh | O | R | ~56 | ~413 | -0.03 | [omitted]? | D13:8: Audrey: [omitted].… | Audrey: [omitted]… |
| 444 | 44 5-54 | mh | O | R | ~74 | ~32 | -0.06 | [omitted]? | D27:12: Audrey: [omitted]… | Audrey: [omitted]… |
| 445 | 44 5-54 | mh | O | F | 14 | ~34 | 0.41/0.48 | [omitted]? | D4:21: Audrey: [omitted]… | Audrey: [omitted]… |
| 446 | 47 6-5 | mh | O | F | ~152 | 26 | 0.35/0.46 | What are John and James' favorite games? | D3:11: John: [omitted]… | John: [omitted]? |
| 447 | 47 6-24 | mh | O | R | ~189 | ~46 | -0.13 | What kind of games has James tried to develop? | D1:4: James: [omitted]… | John: [omitted]? |
| 448 | 47 6-51 | mh | O | R | ~132 | ~71 | -0.10 | [omitted] … | D5:4: John: I'[omitted]'s kinda like getting tran… | James: [omitted] … |
| 449 | 47 6-51 | mh | O | R | ~60 | ~245 | -0.05 | [omitted] … | D8:20: John: Awesome that you have them! I'[omitted]'s cool. Are you playing any… | James: [omitted] … |
| 450 | 47 6-75 | sh | O | F | 23 | ~54 | 0.24/0.54 | [omitted]? | D4:16: James: I'[omitted]'s intense! Check… | John: [omitted]… |
| 451 | 48 7-8 | mh | O | F | ~39 | 6 | 0.46/0.49 | [omitted]? | D15:29: Deborah: [omitted]! | Deborah: [omitted]… |
| 452 | 48 7-13 | mh | O | F | 7 | ~64 | 0.46/0.49 | What places give Deborah peace? | D4:34: Deborah: That sounds great, Jolene. Nature'[omitted]'[omitted]… |
| 453 | 48 7-41 | mh | O | R | ~27 | ~168 | --0.01 | [omitted]? | D28:16: Jolene: [omitted]… | Jolene: [omitted]… |
| 454 | 48 7-82 | mh | O | F | 11 | ~109 | 0.43/0.47 | [omitted]? | D4:12: Deborah: [omitted]… | Deborah: [omitted]… |
| 455 | 48 7-82 | mh | O | F | 17 | ~127 | 0.40/0.47 | [omitted]? | D6:10: Deborah: [omitted] … | Deborah: [omitted]… |
| 456 | 48 7-84 | mh | O | F | 6 | ~43 | 0.46/0.73 | [omitted]? | D1:2: Jolene: [omitted]… | Jolene: [omitted]… |
| 457 | 48 7-85 | mh | O | F | 6 | ~34 | 0.46/0.75 | [omitted]? | D4:12: Deborah: [omitted]… | Deborah: [omitted]… |
| 458 | 48 7-107 | sh | O | F | ~37 | 1 | 0.50/0.82 | [omitted]? | D4:36: Deborah: [omitted]'[omitted]'[omitted]… |
| 459 | 48 7-140 | sh | O | F | 5 | ~74 | 0.47/0.48 | [omitted]? | D15:10: Jolene: Yeah, we love playing "[omitted]" together! It'[omitted]'s compe… | Jolene: [omitted]… |
| 460 | 48 7-158 | sh | O | F | ~72 | 8 | 0.45/0.47 | [omitted]… | D21:6: Jolene: [omitted]… | Jolene: [omitted]… |
| 461 | 48 7-179 | sh | O | R | ~467 | ~34 | -0.25 | [omitted]? | D27:12: Deborah: [omitted]! … | Jolene: [omitted]. |
| 462 | 49 8-7 | mh | O | F | 9 | ~55 | 0.44/0.49 | What new hobbies did Sam consider trying? | D2:10: Sam: Thanks, Evan. Like you said, I'[omitted]'ve been thinki… | Evan: [omitted] … |
| 463 | 49 8-11 | mh | O | F | 5 | ~97 | 0.47/0.48 | [omitted]? | D12:1: Sam: Hey Evan, hope you'[omitted]'s been bothering me la… | Evan: [omitted]… |
| 464 | 49 8-11 | mh | O | R | ~41 | ~110 | -0.02 | [omitted]? | D25:1: Sam: [omitted]'[omitted]'s tough w… |
| 465 | 49 8-42 | mh | O | F | ~113 | 1 | 0.50/0.77 | How did Evan get into painting? | D1:15: Sam: Wow, that'[omitted]'s alwa… |
| 466 | 49 8-55 | mh | O | F | ~238 | 11 | 0.43/0.47 | [omitted]? | D14:2: Evan: [omitted]… | Evan: Ready for an adventure? Where will you go? |
| 467 | 49 8-62 | mh | O | F | 23 | ~46 | 0.37/0.50 | What health scares did Sam and Evan experience? | D14:1: Sam: Hey Evan! I'[omitted]'m doing my b… |
| 468 | 49 8-90 | sh | O | F | ~186 | 3 | 0.48/0.49 | [omitted]… | D3:5: Evan: [omitted]… | Evan: [omitted]… |
| 469 | 50 9-6 | mh | O | F | 12 | ~55 | 0.42/0.50 | Which types of cars does Dave like the most? | D3:12: Dave: [omitted]… | Calvin: [omitted]… |
| 470 | 50 9-20 | mh | O | F | 20 | ~45 | 0.38/0.46 | Who inspired Dave'[omitted]'ve alway… |
| 471 | 50 9-20 | mh | O | F | 10 | ~173 | 0.44/0.46 | Who inspired Dave'[omitted]'ve alway… |
| 472 | 50 9-30 | mh | O | F | 25 | ~48 | 0.36/0.47 | What kind of music does Dave listen to? | D10:11: Dave: Nope, never been to Japan but I'[omitted]'ve heard it's full of vibes, good … | Dave: [omitted]… |
| 473 | 50 9-97 | sh | O | F | 19 | ~44 | 0.39/0.48 | [omitted]? | D13:11: Dave: Thanks, Calvin! It'[omitted]'[omitted]… |
| 474 | 50 9-118 | sh | O | F | ~34 | 6 | 0.46/0.49 | What do Calvin and Dave use to reach their goals? | D21:15: Calvin: [omitted]… | Dave: [omitted] … |
| 475 | 50 9-120 | sh | O | R | ~43 | ~45 | -0.02 | [omitted]? | D22:5: Dave: Thanks Calvin! I'[omitted]'s not just a hobby, it'[omitted]'[omitted]… |
| 476 | 50 9-124 | sh | O | F | 16 | ~72 | 0.40/0.47 | [omitted]… | D23:11: Dave: [omitted],… | Calvin: [omitted]… |
| 477 | 50 9-152 | sh | O | R | ~201 | ~100 | -0.07 | What new item did Dave buy recently? | D30:5: Dave: [omitted]… | Calvin: [omitted]… |
| 478 | 50 9-154 | sh | O | F | 7 | ~53 | 0.46/0.77 | What event did Calvin attend in Boston? | D30:2: Calvin: Hey Dave, it'[omitted]'t wait to see your pics. I went to a fancy gala … | Calvin: Yeah, for sure! I'[omitted]'m in Boston. See you… |
| 479 | 26 0-3 | mh | M | F | 9 | ~188 | 0.44/0.49 | What did Caroline research? | D2:8: Caroline: Researching adoption agencies — it'[omitted]'[omitted]… |
| 480 | 26 0-18 | mh | M | F | 17 | ~225 | 0.40/0.47 | Where has Melanie camped? | D6:16: Melanie: [omitted]elanie: [omitted] … |
| 481 | 26 0-37 | mh | M | F | 7 | ~212 | 0.46/0.47 | What did Melanie paint recently? | D8:6: Melanie: [omitted]'[omitted]'s … |
| 482 | 26 0-51 | mh | M | F | 10 | ~217 | 0.44/0.48 | What has Melanie painted? | D8:6: Melanie: [omitted]'[omitted]'s special to… |
| 483 | 26 0-60 | mh | M | F | 8 | ~208 | 0.45/0.46 | What instruments does Melanie play? | D2:5: Melanie: [omitted] … | Caroline: [omitted]? |
| 484 | 26 0-150 | sh | M | F | 19 | ~176 | 0.39/0.48 | [omitted]? | D18:13: Melanie: Thanks, Caroline. They'[omitted]'[omitted]… |
| 485 | 41 2-30 | mh | M | F | 15 | ~98 | 0.41/0.48 | What shelters does Maria volunteer at? | D17:12: Maria: John, that'[omitted]'t… |
| 486 | 41 2-37 | mh | M | F | 12 | ~144 | 0.42/0.50 | [omitted]? | D24:1: John: [omitted]'[omitted]'[omitted] … |
| 487 | 41 2-107 | sh | M | F | ~233 | ~538 | 0.28/0.32 | [omitted]? | D17:12: Maria: [omitted]… | Maria: [omitted]… |
| 488 | 42 3-27 | mh | M | F | 15 | ~170 | 0.41/0.48 | What places has Joanna submitted her work to? | D16:1: Joanna: [omitted]… | Joanna: [omitted]… |
| 489 | 42 3-55 | mh | M | F | ~43 | 18 | 0.39/0.47 | What is Joanna inspired by? | D11:11: Joanna: [omitted]… | Nate: [omitted]… |
| 490 | 42 3-58 | mh | M | R | ~314 | ~622 | -0.18 | What things has Nate reccomended to Joanna? | D19:17: Nate: [omitted]… | Joanna: [omitted] … |
| 491 | 42 3-58 | mh | M | R | ~521 | ~623 | -0.26 | What things has Nate reccomended to Joanna? | D27:23: Nate: Yep! I'[omitted]'s b… | Joanna: [omitted] … |
| 492 | 43 4-4 | mh | M | F | 24 | ~343 | 0.36/0.49 | What books has Tim read? | D26:36: Tim: I'[omitted]'s coming out called "[omitted]". It's b… | Tim: [omitted]… |
| 493 | 43 4-9 | mh | M | F | 20 | ~103 | 0.38/0.48 | Which endorsement deals has John been offered? | D25:2: John: [omitted]… | John: [omitted]… |
| 494 | 44 5-9 | mh | M | R | ~43 | ~250 | -0.01 | [omitted]… | D16:6: Audrey: [omitted]… | Audrey: [omitted]… |
| 495 | 44 5-14 | mh | M | F | 15 | ~70 | 0.41/0.75 | [omitted]? | D9:20: Andrew: Haven'[omitted]'s up? Missed chatting with ya! Check it out,… |
| 496 | 44 5-35 | mh | M | R | ~77 | ~396 | -0.05 | What are the names of Andrew'[omitted]'s my buddy and I hope him and Toby become buddies! | Andrew: Thanks, I think that'[omitted]'ll take good ca… |
| 497 | 44 5-42 | mh | M | R | ~78 | ~313 | -0.03 | [omitted]? | D6:4: Audrey: [omitted]… | Audrey: [omitted] … |
| 498 | 47 6-8 | mh | M | R | ~31 | ~433 | -0.01 | How many pets does James have? | D1:14: James: [omitted] … | James: [omitted]… |
| 499 | 47 6-9 | mh | M | F | 12 | ~365 | 0.42/0.74 | What are the names of James's dogs? | D5:1: James: [omitted]… | James: [omitted]… |
| 500 | 47 6-51 | mh | M | R | ~225 | ~254 | -0.15 | [omitted] … | D19:7: John: I'[omitted]'[omitted]… | James: [omitted] … |
| 501 | 47 6-54 | mh | M | F | 17 | ~286 | 0.40/0.48 | [omitted]? | D27:1: John: [omitted]… | James: [omitted]… |
| 502 | 48 7-1 | mh | M | F | 12 | ~429 | 0.42/0.83 | [omitted]? | D6:4: Deborah: [omitted]'[omitted]'s my old home. I go there now and then for my mom, who … |
| 503 | 48 7-84 | mh | M | F | 2 | ~192 | 0.49/0.73 | [omitted]? | D17:10: Jolene: [omitted]… | Jolene: [omitted]… |
| 504 | 49 8-26 | mh | M | R | ~85 | ~258 | -0.05 | What motivates Evan to take care of his health? | D5:13: Evan: I'[omitted]'s pretty cool! [image: a p… | Evan: [omitted]… |
| 505 | 50 9-5 | mh | M | R | ~81 | ~265 | -0.03 | What are Dave'[omitted]'[omitted]… | Calvin: [omitted]… |
| 506 | 26 0-4 | mh | P | R | ~189 | ~353 | -0.08 | What is Caroline'[omitted]'ve come so far, but there's more to do. Yo… |
| 507 | 30 1-60 | sh | P | F | 8 | ~85 | 0.45/0.48 | What does Jon'[omitted]'s been tough, but I'[omitted]'[omitted]? |
| 508 | 30 1-75 | sh | P | F | 22 | ~297 | 0.37/0.49 | [omitted]? | D15:12: Gina: I'[omitted]'[omitted]… | Gina: Can't wait for tomorrow's grand opening! |
| 509 | 42 3-122 | sh | P | F | ~93 | ~69 | 0.29/0.32 | What did Nate do for Joanna on 25 May, 2022? | D13:9: Nate: [omitted]! [image: a photo of a stuffed… | Joanna: [omitted] … |
| 510 | 42 3-135 | sh | P | R | ~147 | ~409 | -0.08 | [omitted]? | D16:11: Joanna: Awesome! I'[omitted]'t wait! | Nate: [omitted]… |
| 511 | 42 3-151 | sh | P | R | ~212 | ~381 | -0.10 | [omitted]? | D19:9: Nate: [omitted]? | Joanna: [omitted]… |
| 512 | 42 3-173 | sh | P | R | ~58 | ~228 | -0.04 | [omitted]? | D24:13: Nate: [omitted]'[omitted]'t give up, … |
| 513 | 42 3-195 | sh | P | F | ~210 | 12 | 0.42/0.47 | [omitted]'[omitted]'[omitted]… | Nate: Can't wait to see it, Joanna! I'm here to support you. |
| 514 | 48 7-121 | sh | P | F | ~167 | ~227 | 0.27/0.33 | [omitted]? | D9:14: Jolene: I'[omitted]'s a lot to manage and I'… | Jolene: [omitted]? |
| 515 | 49 8-110 | sh | P | R | ~41 | ~425 | -0.02 | What injury did Evan suffer from in August 2023? | D9:2: Evan: [omitted]'[omitted]'s up? It's been a few days since we talked. How h… |
| 516 | 50 9-79 | sh | P | F | 7 | ~489 | 0.46/0.48 | [omitted]? | D5:11: Dave: [omitted]… | Calvin: [omitted]… |
| 517 | 50 9-119 | sh | P | F | 20 | ~280 | 0.38/0.87 | What does working on cars represent for Dave? | D22:5: Dave: Thanks Calvin! I'[omitted]'s not just a hobby, it'[omitted]'m passionate about. Doing… |

## Appendix B. Method notes and caveats

- Data: `per_question.jsonl` of the three runs (logged stages: `vector` 30, `lexical` 30, `extra_legs`, `fused` 10, `pool`, `rerank_scores`, `final`, `context`), the dataset `data/locomo10.json` for turn text, sessions and adjacency (the engine's derived sessions equal the dataset sessions here; the replay reproduction confirms it).
- Reproduction checks: fused top-10 recomputed from the logged legs = logged `fused` (1,540 / 1,540, fix and base); context from hits + window = logged `retrieved_turns` (1,540 / 1,540 fix with 2/4, base with 2/2); rr context from min-max rule + floor 0.3 + window 2/2 = logged (1,540 / 1,540).
- CPU replica: Qwen3-Embedding-0.6B fp32, the arm's query instruction, documents "Speaker: text [image: caption]"; top-30 agreement with the logged bf16-GPU run 97.8%, top-1 agreement 1,492 / 1,540. BM25: k1 1.2, b 0.75, lower-cased alphanumeric tokens, OR of all query tokens; top-30 agreement 97.1%, top-1 1,493 / 1,540. Replica baseline: 1,251-1,252 fully covered vs the exact 1,251, i.e. +-3 questions of noise on any delta below about 5.
- Not tested: any option that needs an LLM at read or write time (planner, HyDE, fact mining, list cards, session summaries); the Qwen3-Embedding-4B embedder (snapshot would not load offline); the reader's reaction to larger contexts. Where a number depends on those it is marked ESTIMATE.
- Cause labels are token-overlap heuristics, not human judgement. The N class in particular mixes implicit answers, reply-turns and weak labels; a manual pass over it (action N1) would sharpen both this analysis and the benchmark numbers.
- Gold ids that do not exist in the dataset (8) are dropped; 2 questions have no usable gold. Question ids refer to the run files; open `per_question.jsonl` (fields `gold_turns[].ranks`, `stages`, `top_non_gold`, `context_text`) for the full record of any row.
