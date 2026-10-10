# Failure forensics of the baseline, LoCoMo dev cat 1-5 and OP-Bench dev (2026-10-10)

Baseline `xb-loc-dev` (commit 5f186e3, `arms/BEST_dev_2026-10-10.json`, dev conv-26/30/41/42, 757 q, 603 correct = 79.7%) and `xb-opb-dev` (331 probes, official overall 21.2).
Method: `forensics_report.py` rebuilt `per_question.jsonl` (stage ranks of every gold turn in every leg, the context, the verdict); each of the 154 wrong answers was then read against its gold, its gold-turn ranks and its context and given one stage code. CPU only, no model call, at most 6 worker processes; held-out conversations were never opened. Per-question catalogue: `evals/analysis/catalogue/locomo_dev_failures.{jsonl,md}` (text-free) and the gitignored full-text copy in `evals/runs/_analysis/`. Schema: `evals/schemas/failure_catalogue.schema.json`. All lever gains below are **capture-rate assumptions applied to measured class sizes, not measurements**; the queued screens measure them.

Reading guide. 1 question = 0.171 points on cat 1-4 (n=584) and 0.132 points on cat 1-5 (n=757). Stage codes: (a) gold never stored or quarantined, (b) in no leg's candidates (recall), (c) in a leg but cut by fusion / rerank / assembly, (d) in context but the reader answered wrongly, (e) judge error, (f) gold or dataset error.

## 1. Stage x category (154 wrong)

| Stage / sub-type | single-hop | multi-hop | temporal | open-domain | cat 5 | total |
|---|---:|---:|---:|---:|---:|---:|
| (a) write path (not stored / quarantined) | 0 | 0 | 0 | 0 | 0 | **0** |
| (b) recall: no leg returned a gold turn | 4 | 14 | 0 | 8 | - | **26** |
| (c) fusion cut (in a leg, outside fused top-k) | 3 | 8 | 0 | 2 | - | 13 |
| (c) rerank cut | 0 | 0 | 0 | 1 | - | 1 |
| (c) assembly / budget cut | 0 | 0 | 0 | 0 | - | 0 |
| **(c) total** | 3 | 8 | 0 | 3 | - | **14** |
| (d) wrong detail | 9 | 2 | 0 | 1 | - | 12 |
| (d) distractor line | 1 | 0 | 1 | 0 | - | 2 |
| (d) incomplete list | 1 | 4 | 0 | 0 | - | 5 |
| (d) wrong count | 0 | 5 | 0 | 0 | - | 5 |
| (d) date arithmetic / wrong absolute date | 0 | 0 | 6 | 0 | - | 6 |
| (d) refusal or premise denial | 2 | 1 | 4 | 1 | - | 8 |
| (d) inference refused or wrong | 0 | 0 | 0 | 6 | - | 6 |
| **(d) total, cat 1-4** | 13 | 12 | 11 | 8 | - | **44** |
| (d) cat 5: named person is the wrong speaker / owner | - | - | - | - | 39 | 39 |
| (d) cat 5: near-match memory of the named person used | - | - | - | - | 10 | 10 |
| (d) cat 5: absent name matched to nearest entity | - | - | - | - | 1 | 1 |
| **(d) total, cat 5** | - | - | - | - | 50 | **50** |
| (e) judge error by the calibration conventions | 5 | 5 | 0 | 0 | 0 | **10** |
| (f) gold / dataset error (errata, counted) | 4 | 1 | 2 | 0 | 3 (answerable) | **10** (7 + 3 new) |
| **wrong** | **29** | **40** | **13** | **19** | **53** | **154** |
| n | 311 | 111 | 130 | 32 | 173 | 757 |

By conversation (wrong / n): conv-26 38/199 (80.9), conv-30 18/105 (82.9), conv-41 32/193 (83.4), **conv-42 66/260 (74.6)**. conv-42 is the outlier because it holds the hardest multi-hop set (37 questions, 3.6 gold turns each, 19 wrong; across all dev cat 1-4 questions accuracy falls from 90% with one gold turn to 68% with two, 47% with 3-4 and 46% with 5+), 9 of 11 open-domain wrong, and two parallel personas (Joanna / Nate: both write, game, bake dairy-free, keep pets) that produce 18 of 61 cat-5 swaps.

Facts behind the table:
- (a) is empty: 2,080/2,080 dev turns written, text identical, none quarantined; the only unwritten gold reference is a malformed evidence id (`"D"`, 3-88), a dataset error.
- (b) 47 gold turns are in no leg (vector top-60, BM25, extra legs); 26 questions. Most have 2-7 gold turns (set questions: "what recommendations", "what recipes", "what do both have in common").
- (c) 29 gold turns were lost at fusion: their best vector rank was 5-82 (median ~19), so a pool above 2 would have kept most of them; 9 had no vector rank (lexical only). 2 lost at rerank.
- (d) in 44 cat-1-4 and 50 cat-5 failures every gold or answer-bearing turn was in the context.
- Cat-5: in 52 of 53 failures the turn the answer was built from was in the context; the reader adopted it for the wrong person (39) or built an answer from a neighbour (10).
- (e) Ten rows are right under `judge_calibration_dev.jsonl` conventions (a list answer that contains every gold item plus true extras is CORRECT; a one-letter typo and a synonym are CORRECT). Hidden in the 154, so they are 1.7 points of cat 1-4 that no system change can buy.
- Errata: 7 known (0-5, 1-9, 1-57, 2-69, 3-24 gold; 3-91, 3-92 need image) plus 6 new candidates (section 6).

## 2. Ceiling and ranked levers

Ceiling if a class were fixed completely, and the realistic capture assumed (stated so it can be challenged). Points are cumulative additions to 603 correct.

| # | Lever | class (q) | ceiling cat 1-4 / cat 1-5 | assumed capture | expected q | expected points cat 1-4 / cat 1-5 |
|---|---|---:|---|---:|---:|---|
| 1 | Subject / owner check at read (perspective resolver) | cat-5 swap 39 | 0 / +5.2 | 50% | 20 | 0 / **+2.6** |
| 2 | Judge conventions (deterministic pre-judge + second judge on disputes) | (e) 10 | +1.7 / +1.3 | 80% | 8 | **+1.4 / +1.1** (measurement) |
| 3 | Recall for set questions (intent list trigger, subject legs, word-vector leg) | (b) 26 | +4.5 / +3.4 | 35% | 9 | **+1.5 / +1.2** |
| 4 | Inference route and neutral retry (open-domain refusals and "would/likely") | d-ref 8 + d-inf 6 | +2.4 / +1.8 | 45% | 6 | **+1.0 / +0.8** |
| 5 | Wider candidate pool, chunked rerank, rerank_context | (c) 14 | +2.4 / +1.8 | 45% | 6 | **+1.0 / +0.8** |
| 6 | Near-match gate for cat 5 (no-record hint, relevance gate, abstain_on_raw) | cat-5 near 10 + entity 1 | 0 / +1.5 | 40% | 4 | 0 / **+0.5** |
| 7 | Date repair by code (weekday/date consistency, "last <weekday>" re-resolution) | d-date 6 | +1.0 / +0.8 | 65% | 4 | **+0.7 / +0.5** |
| 8 | Enumerate-then-count with event identity; dedupe; list prompt | d-count 5 + d-list 5 | +1.7 / +1.3 | 40% | 4 | **+0.7 / +0.5** |
| 9 | Top-hit-first block, rerank_context, token window | d-detail 12 + d-distr 2 | +2.4 / +1.8 | 25% | 3.5 | **+0.6 / +0.5** |
| 10 | Errata (report with / without) | (f) 7 + 6 candidates | +1.2 relative | 100% | - | reporting only |

Sum of expected: cat 1-4 about +40.5 q: 483 -> 523.5 / 584 = **89.6%** (90.5% excluding the 8 errata rows); cat 1-5 about +64.5 q: 603 -> 667.5 / 757 = **88.2%** (89.6% excluding errata). Honest reading: **90 on cat 1-4 is reachable only if levers 2-9 all land at or above the assumed capture; 90 on cat 1-5 needs lever 1 at 60% or better (24 q) and a clean lever 6, and it stays at the edge of the +-2.2 point question-sampling CI (A10).** The 9B reader (C11) caps what levers 4 and 9 can give; a larger local reader is the unlisted upside. Levers 1 and 6 do not move cat 1-4; levers 3, 5, 8 and 9 move only cat 1-4 and cat-5 only through side effects, which the guard slice (cat 5) must show.

## 3. Mapping to built features and queued screens

| Lever | Built, opt-in | Queued screen that tests it | NEW gap (no existing one covers it) |
|---|---|---|---|
| 1 subject / owner check | I39 perspective layer: `read.speaker_vote_mode=subject` (retrieval weighting), perspective axes spk/sub/ask, I47, I54 | r3b `r3-persp` (subject_weight), `r3-paxes` (+ axes) test the weighting only | **I59**: no read-side rule that refuses to attribute a fact whose speaker/subject is not the asked-about person |
| 2 judge | `--judge-guards`, `--judge-date-check` (A2, dates only); A1 second judge deferred by the user | none (harness change; validated against `judge_calibration_dev.jsonl` and the 2x2 of r4 `r4-a7-*`) | **I58**: no deterministic superset-list / typo / synonym rule |
| 3 recall of sets | I4 `read.list_trigger=intent`, B15 word-vector leg, R2-2 two-person speaker vote, `speaker_vote_mode=subject` | r3 `r3-intent`; r3b `r3-persp` | covered by B1/B7/B8/I4 |
| 4 inference / refusal | `grounded_generic_infer`, `routed_generic` (I3/C6/B3/C4), neutral retry (I1) | r3 `r3-generic`; r3c `r3c-routed` | **I61**: premise-tolerant answering (question date or entity mismatches the store) |
| 5 cuts | `candidate_pool`, chunked rerank wrapper (I9), `rerank_context` (B10) | r4 `r4-rctx1`, `r4-rctx2` (chunked rerank I9 has no screen) | covered by B7/B10/I9 (screen gap for I9) |
| 6 near-match | I32 `--no-record-hint`, I29 relevance gate, I30 `abstain_on_raw`, store-calibrated gate | r3 `r3-norecord`, `r3-relgate`; r3c `r3c-storecal`, `r3c-sens` | **I60**: no entity-existence check (Oscar for Oliver) |
| 7 dates | `relative_dates_anchored`, `temporal_resolve`, `--judge-date-check` (grades, does not repair) | none | **I57**: no weekday/date consistency repair |
| 8 count / list | I31 dedupe, I4, `routed_generic` list branch | r3 `r3-dedupe`, `r3-intent`; r3c `r3c-routed` | **I56**: no enumerate-then-count with event identity |
| 9 detail | I6 token-aware window, `rerank_context`, R2-4 (rejected as top-hit-first) | r4 `r4-rctx1/2`; I6 has no screen in r3-r4 | covered by C1/C2/B10 |
| - latest-wins (I17) | `read.latest_wins` | r3 `r3-latest` | no dev failure in the 154 is a stale-value case: the screen is expected neutral on LoCoMo (it targets knowledge-update data); do not count it toward 90 |
| - decider (I28) | OpenDecider port | used by `r3-relgate` (decider mode) | - |
| - A7 2x2 | engine old/new x reader+judge old/new | r4 `r4-a7-A..D` | attributes the part of the +/-8 delta that belongs to the judge, which bears on lever 2 |

Not in any screen: lever 2, 7, the I9 chunked rerank, the I6 token window; lever 1's read-side gate (I59) is not built.

## 4. Examples per class (dev corpus, short)

- (b) recall, 3-74 "What recommendations has Nate received from Joanna?" (7 gold turns, all in no leg; answer: Joanna "offered to get new book recommendations ... the specific titles ... are not listed"). 2-40 "names of John's children": one of two gold turns recalled; answer names only Kyle.
- (c) fusion, 2-35 "What states has Maria vacationed at?" gold Oregon, Florida; the Oregon turn had vector rank 9 and no lexical hit and was cut at the fused top-10; answer "Florida and California". 0-3 "What did Caroline research?" gold "Adoption agencies": vector rank 9, cut; answer "career options, counseling".
- (d) refusal, 3-43 "How long did it take Joanna to finish her book?" gold four months; both gold turns in context; answer "The memories do not state the specific duration". 0-71: "does not mention a specific book" although D7:11 and D17:10 are in context.
- (d) inference, 0-22 Dr. Seuss: gold "Yes, since she collects classic children's books"; answer "No, ... she does not mention Dr. Seuss". 3-12: gold asthma; answer picks "lactose intolerant".
- (d) count, 3-80 gold seven tournaments, answer "at least five"; 3-19 gold twice, answer "at least three" (one event listed under two dates).
- (d) date, 3-31: gold "The Friday before 10 July, 2022"; answer "Friday, 2022-07-10" (10 July 2022 is a Sunday). 3-41 gold 5 October, answer "Friday, 2022-09-09".
- (d) detail, 1-49 gold "cozy and comfortable", answer "a cool oasis"; 3-60 gold Nintendo Switch (inferred from Xenoblade 2), answer "an Xbox".
- (e) judge, 3-183 gold "Xenoblade Chronicles", answer "Xeonoblade Chronicles" (typo); 2-120 gold "Flood", answer "A flood affected John's old area ..."; 0-24 gold "Running, pottery", answer lists both plus other true items.
- (f) 0-5 "Sunday before 25 May" contradicts the transcript; 3-24 gold date ambiguous.
- Cat 5, wrong owner (39): 0-181 "What did Melanie make for a local church?" -> "Melanie made a stained glass window" (the turn is Caroline's: "I made this stained glass window ..."); 2-174 "What does John say she feels when doing upside-down yoga poses?" -> "She feels free and light" (Maria's line). Retrieval worked; the reader attributed.
- Cat 5, near-match (10): 2-185 "What happened to Maria's job in August 2023?" -> "Nothing happened ... she was still working" (John lost his job); 3-216 "filling Nate used in May 2022" -> "mixed berry" (Joanna's cake has strawberry filling).
- Cat 5, entity (1): 0-179 "Where did Oscar hide his bone once?" -> "in Melanie's slipper" (the dog is Oliver).

Cat 5 why-classes: for 49 of 53 the asked event belongs to someone else or to a neighbouring event (39 the other speaker's fact, 10 another event of the named person), the name is absent from the store in 1, and 3 are answerable by the transcript (dataset noise: 0-184 guitar, 0-186 "Brave", 3-244 Tilly).

## 5. OP-Bench dev (331 probes; 288 below 0.5)

Per-probe results stay in the gitignored `evals/runs/_analysis/`; OP-Bench has no licence, so this section holds counts and own-answer fragments only. Cheap signals: retrieved turn ids resolved to the dialogue, persona share, distinctive context tokens re-used in the answer (leak), second-person reference cues, question/context overlap, an affirmation regex, addressee check. Manual check of 30 random failed probes: classes agreed on 25 (83%); all 5 disagreements are repetition probes where a generic reply was labelled persona-theme (the detector over-assigns), so the true share of template repetition is about a third of the 167, not 9%.

| Type (baseline mean, n probes) | below 0.5 | Class (automated) | n |
|---|---:|---|---:|
| fully irrelevant (0.135, 60) | 52 | memory injected for an unrelated request | 52 |
| baiting (0.03, 20) | 20 | subject confusion (5 persona facts to another subject, 5 behaviour attributed to the persona); near-match memory treated as relevant (6 interest bait, 4 context bait) | 20 |
| sycophancy fact (0.785, 20) | 4 | memory cited in the reply 2; undetected 2 | 4 |
| sycophancy value (0.667, 20) | 7 | memory cited in an opinion reply 6; undetected 1 | 7 |
| sycophancy memory, fine-grained (0.11, 20) | 20 | confirmation with no supporting memory 17; near-match memory treated as confirmation 3 | 20 |
| sycophancy memory, coarse-grained (0.223, 20) | 18 | confirmation with no supporting memory 11; near-match memory treated as confirmation 4; premise contested with other stored memories 1; undetected 2 | 18 |
| repetition (0.236, 171) | 167 | persona-theme re-use 148 (over-assigned, see above); template repetition 15; undetected 4 | 167 |

Findings that matter for the levers:
1. **No probe had an empty context.** Context is 2.0-2.3k tokens for every type, and the persona's own turns are about 50% of it (the other half is the partner's turns), including for "what habits lead to better health". The reader is told to personalise when relevant, so it personalises: "Since you've been so focused on your dance studio and your business lately ..." on a health-habit question. This is I29/I30 and the whole of fully-irrelevant and baiting (72 of 80 irrelevance probes below 0.5, 0 of 80 empty contexts).
2. **Role inversion** (new): in 23% of the failed irrelevance and repetition answers the reply greets the *partner* by name ("Hey Mel!" to a Caroline memory) or speaks as the persona ("my own journey", "I joined a mentorship program"). The memory is rendered as a two-speaker transcript and the reader does not know which speaker is the user. Perspective gap, same family as cat-5 owner swaps (I39) but at the reader prompt: **I63**.
3. **Memory sycophancy**: 28 of the 38 failures confirm an event that has no matching turn (best question-to-context word overlap below 0.3); 7 confirm after a topical near-match; 1 contests the premise with other memories; 2 are unclassified. The support check of I32 (`--no-record-hint`) is the right lever; it is off by default. BASE itself is only 33.9, RAG 25.8 (Qwen3-8B), so the target is about 30-40.
4. **Repetition**: 148 of the 167 below-0.5 answers re-use stored facts from the same persona pool; every probe retrieves the same ~6 sessions of the same person. No existing gap covers usage-aware retrieval (a record injected in the last replies should be penalised): **I64**. Our repetition embedder (bge-small) and judge differ from the paper's, so 22.0 is not comparable with 38.12 / 66.73 (I34/I35).
5. Fact 78.5 and value 66.8 are *good* (RAG 56.9 / 60.6, Mem0 48.4 / 59.3, MemU 40.8 / 56.2, MemOS 44.2 / 55.4; BASE 91.6 / 91.8 on Qwen3-8B) because those probes assert a world fact or an opinion that no stored turn bears on; the reader answers from general knowledge and the persona is mostly ignored. They fail only when the context carries a topically near memory (11 of 40 probes below 0.5).

Lever map for OP-Bench (expected, to be measured): relevance gate that can return an empty context (I29/I30/I37, screens `r3-relgate`, `r3c-storecal`): fully irrelevant 15 -> 40-65, baiting 3 -> 10-25 (bait needs the topic-level gate, not only the persona-level one); no-record hint (I32, `r3-norecord`): memory sycophancy 16.6 -> 25-35; subject/owner check (I59) for baiting subject confusion (10 of 20); role labelling (I63, new) for the 23%; usage penalty (I64, new) for repetition. Overall 21.2 -> about 35-45 if the gate lands.

## 6. New errata candidates (not yet in `locomo_errata.json`; report-only until reviewed)

1-63 (question date May 23 vs transcript May 27, gold is the internship), 2-68 (question year December 2023 vs transcript December 2022), 3-88 (malformed evidence id `"D"`), and three cat-5 questions the transcript answers: 0-184, 0-186, 3-244 (**I62**). Adding them changes the cat 1-4 headline by about +0.4 and cat 5 by +1.2 points when excluded; they are not excluded in any number above.

## 7. New gaps found (register section I, ids I56-I64)

I56 count / aggregation, I57 date repair, I58 judge superset/typo/synonym, I59 subject-owner read gate, I60 entity-existence gate, I61 premise-tolerant answering, I62 new errata classes, I63 role labelling of memories for the reader, I64 usage-aware repetition control. Each is written with evidence, a generic solution, the decision mechanism and the non-overfitting check in `GAP_REGISTER.md` section I; the per-subscore view is section J.

## 8. Limits

One run, one reader, one judge (A1 deferred): class sizes are +-3 q around the boundary (A16 noise floor +-5 net). The (d) sub-types were assigned by reading, once, by one reviewer; (e) uses the calibration conventions, not a second judge. Ceilings assume the classes are independent, which they are not (a fixed recall can leave a reader failure behind).
