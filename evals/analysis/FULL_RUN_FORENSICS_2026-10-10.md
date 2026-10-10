# Full-run forensics: `full-persp-loc`, all 10 LoCoMo conversations, 1,540 questions (2026-10-10)

Run `full-persp-loc` (memspine commit 22b97b9, arm `scr-persp-w` = `BEST_dev_2026-10-10` + perspective policy `heuristic` + `read.perspective_mode: subject_weight`; Qwen3.5-9B reader with neutral refusal retry, same model as guarded judge; categories 1-4, all ten conversations): **1,238 of 1,540 correct = 80.4%** (single-hop 88.7, multi-hop 66.3, temporal 81.0, open-domain 46.9; dev 82.7, held-out 79.0). 302 answers are wrong. Reference full runs on the same 1,540 questions: `qa-full-qs-eq06-fix` (80.1, no reranker, no perspective), `qa-full-qs-eq06-roff-fx` (74.5); dev reference `xb-loc-dev` (BEST without perspective, 4 dev conversations, 82.7).

CPU only, no model call, at most 6 workers; the full-text working files stay under the gitignored `evals/runs/_analysis/`. This document holds counts, question ids and codes, no question, answer or turn text. All lever gains are **capture-rate assumptions on measured class sizes, not measurements**.

Reproduce: `python evals/forensics_report.py --run evals/runs/<run>--memspine --forensics evals/runs/<run>--forensics --data data/locomo10.json --out evals/runs/_analysis/fx_<run>` for `full-persp-loc`, `qa-full-qs-eq06-fix` and `xb-loc-dev` (stage ranks per gold turn), then `python evals/full_run_forensics.py` (all tables below come from its `tables.md`); the hand labels are `evals/analysis/full_persp_loc_stage_labels.txt` (one code per wrong answer). Explorer: section 7.

## 0. Headlines

1. **Retrieval is 43% of the loss, the reader 46%, errata and judge 11%.** Of 302 wrong answers: recall 69 (23%), cuts 61 (20%: 56 at the fused pool, 5 at `rerank_keep`), reader 138 (46%), judge 11 (4%), gold error 23 (8%), write path 0. The write path is clean: 5,882 of 5,882 turns written, none quarantined, one instruction-flagged, none anomalous.
2. **Perfect retrieval alone gives about 87%, not 90.** When every gold turn is in the context (1,301 questions) the accuracy is 87.1% (88.4% without the errata rows, 89.5% if the 10 judge errors were credited too); the 168 failures with complete evidence are 136 reader, 22 errata and 10 judge. With incomplete evidence (235 questions) the accuracy is 43.4%. Reaching 90% needs retrieval to complete the evidence of most of the 235 **and** the reader/judge ceiling to rise from 87.1% to about 90%.
3. **The perspective trade-off is not the "other speaker's turn was down-weighted" mechanism.** The subject factor reaches a gold turn in 45 of 2,356 cases (1.9%) and in 1 of the 36 single-hop and 0 of the 11 open-domain losses. The cost comes from the **perspective leg**, an extra RRF voter that replays the vector order restricted to the subject's turns: it crowds out gold that only BM25 finds (dev control: such gold turns in the pool 11 -> 3 of 20). Half of the dev single-hop deficit is reader churn on a changed context (section 3).
4. **Temporal at full scale is 81.0 because the held-out conversations are 76.4 against 87.7 on dev** (xb-loc-dev: 90.0 on dev). The question mix is not the cause (with dev's per-type accuracy held-out would score 88.3): the gap is 8.6 more duration/interval failures, 9.5 more fusion cuts and 4.1 more gold errors than dev rates predict. 21 of the 61 temporal failures could be repaired by code.
5. **At the assumed capture the seven counted levers sum to +75 questions: 85.3% (86.6% excluding errata, 87.1% excluding errata and with the judge-conventions column).** 90% of 1,540 is 1,386 correct, 148 more than today (128 more on the 1,517 questions that remain after the 23 errata rows). That needs about twice the assumed capture, or about 1.5x plus a larger reader (C11, blocked by GPU memory).

## 1. Method

- Inputs: `full-persp-loc--memspine/results.jsonl`, `full-persp-loc--trace/` (`forensics.jsonl.gz` with `trace_full`: query analysis, resolved perspective with factors, cuts with reasons, replay window, records with tags, assembled block; `reads.jsonl.gz`: exact reader prompts, raw replies, retry, judge calls, verdict meta; `write_trace.jsonl`: firewall signals and tags of every turn), `full-persp-loc--forensics/` (stage ranks), the errata file and the judge-convention code.
- Every one of the 302 wrong answers was read once, by one reviewer, against its question, gold, answer, the rank of each gold turn in every leg, and (for the 138 reader cases and all 61 temporal failures) the dated lines of the exact reader prompt. The stage is the weakest link: (a) write, (b) recall (a gold turn in no leg), (c) cut (in a leg, then lost at the fused pool or at `rerank_keep`), (d) reader with sub-type, (e) judge (the answer states the gold fact), (f) gold error (the 22 wrong answers that carry a default-excluded errata tag, `gold_error` or `needs_image`, plus 41/2-68, a `premise_error` row: 23).
- Consistency checks run by the script: every `b` label has a gold turn in no leg, every `cf` label a gold turn lost at fusion, every `cr` a gold turn lost at rerank, every `d` label has all its gold in the context; 102 of the 138 reader cases also have every gold turn text inside the exact reader prompt (the other 36 rest on neighbour lines that arrived through the replay window or on evidence labels that point elsewhere).
- Exact cut reasons. `trace_full.cuts` holds 12,860 entries over 1,540 questions and **every one has the reason `rerank_keep`** (keep 10); there is no budget, floor, perspective, dedupe or abstain cut in this run (`final_not_in_context` empty, `assembled.abstained` false, `perspective.dropped` empty in all questions, one `context_truncated` row). The 56 fusion cuts have no cut record at all: the pool is the top 20 (30 for list-mode questions) of the fused list and the engine does not log what falls below it (gap I78).
- The refusal retry fired on 22 questions and was accepted on none (the second reply was again a refusal); 85 verdicts came from a deterministic guard (`date_equivalent`), not the judge.

## 2. Stage tables

### 2.1 Stage x category (302 wrong)

| stage | single-hop | multi-hop | temporal | open-domain | total |
|---|---:|---:|---:|---:|---:|
| (a) write path | 0 | 0 | 0 | 0 | 0 |
| (b) recall: gold in no leg | 9 | 34 | 4 | 22 | 69 |
| (c) cut at fusion / pool (fused top-20) | 22 | 20 | 12 | 2 | 56 |
| (c) cut at rerank_keep (top-10 of the pool) | 2 | 1 | 2 | 0 | 5 |
| (c) cut at assembly / budget / floor | 0 | 0 | 0 | 0 | 0 |
| (d) reader: wrong detail | 38 | 6 | 0 | 3 | 47 |
| (d) reader: incomplete list | 2 | 19 | 1 | 0 | 22 |
| (d) reader: wrong count | 0 | 8 | 0 | 0 | 8 |
| (d) reader: date / duration arithmetic, wrong date | 1 | 1 | 23 | 0 | 25 |
| (d) reader: refusal or premise denial | 1 | 1 | 8 | 1 | 11 |
| (d) reader: inference refused or wrong | 0 | 0 | 0 | 23 | 23 |
| (d) reader: distractor line | 1 | 0 | 0 | 0 | 1 |
| (d) reader: wrong speaker | 1 | 0 | 0 | 0 | 1 |
| (e) judge (answer states the gold fact) | 6 | 3 | 2 | 0 | 11 |
| (f) gold / dataset error (errata) | 12 | 2 | 9 | 0 | 23 |
| **wrong** | 95 | 95 | 61 | 51 | 302 |
| n | 841 | 282 | 321 | 96 | 1540 |
| accuracy % | 88.7 | 66.3 | 81.0 | 46.9 | 80.4 |

| group | single-hop | multi-hop | temporal | open-domain | total |
|---|---:|---:|---:|---:|---:|
| write | 0 | 0 | 0 | 0 | 0 |
| recall | 9 | 34 | 4 | 22 | 69 |
| cut | 24 | 21 | 14 | 2 | 61 |
| reader | 44 | 35 | 32 | 27 | 138 |
| judge | 6 | 3 | 2 | 0 | 11 |
| gold error | 12 | 2 | 9 | 0 | 23 |

Reading it: single-hop loses to the reader (44) and to cuts (24), multi-hop to recall (34) and lists/counts (27), temporal to cuts (14) and date arithmetic (24), open-domain to inference (23) and recall (22). Accuracy by number of gold turns: one gold turn 85.8% (n = 1,126), two 72.6% (226), three or four 61.0% (141), five or more 44.2% (43).

### 2.2 Stage x conversation, dev vs held-out

| stage | 26 | 30 | 41 | 42 | 43 | 44 | 47 | 48 | 49 | 50 | dev | held-out |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| (a) write path | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| (b) recall: gold in no leg | 2 | 3 | 9 | 13 | 5 | 13 | 7 | 6 | 6 | 5 | 27 | 42 |
| (c) cut at fusion / pool (fused top-20) | 1 | 2 | 3 | 9 | 7 | 8 | 4 | 10 | 7 | 5 | 15 | 41 |
| (c) cut at rerank_keep (top-10 of the pool) | 0 | 0 | 0 | 1 | 1 | 0 | 0 | 1 | 1 | 1 | 1 | 4 |
| (c) cut at assembly / budget / floor | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| (d) reader: wrong detail | 4 | 5 | 2 | 5 | 5 | 2 | 2 | 7 | 8 | 7 | 16 | 31 |
| (d) reader: incomplete list | 1 | 0 | 2 | 2 | 1 | 3 | 4 | 1 | 5 | 3 | 5 | 17 |
| (d) reader: wrong count | 0 | 0 | 0 | 4 | 1 | 1 | 0 | 0 | 1 | 1 | 4 | 4 |
| (d) reader: date / duration arithmetic, wrong date | 0 | 1 | 1 | 5 | 3 | 6 | 2 | 2 | 2 | 3 | 7 | 18 |
| (d) reader: refusal or premise denial | 2 | 1 | 1 | 2 | 1 | 0 | 2 | 1 | 1 | 0 | 6 | 5 |
| (d) reader: inference refused or wrong | 3 | 0 | 1 | 2 | 6 | 0 | 7 | 1 | 0 | 3 | 6 | 17 |
| (d) reader: distractor line | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 |
| (d) reader: wrong speaker | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 |
| (e) judge (answer states the gold fact) | 1 | 0 | 2 | 2 | 1 | 1 | 1 | 2 | 0 | 1 | 5 | 6 |
| (f) gold / dataset error (errata) | 1 | 2 | 2 | 3 | 7 | 1 | 1 | 1 | 4 | 1 | 8 | 15 |
| **wrong** | 15 | 14 | 24 | 48 | 39 | 35 | 30 | 32 | 35 | 30 | 101 | 201 |
| n | 152 | 81 | 152 | 199 | 178 | 123 | 150 | 191 | 156 | 158 | 584 | 956 |
| accuracy % | 90.1 | 82.7 | 84.2 | 75.9 | 78.1 | 71.5 | 80.0 | 83.2 | 77.6 | 81.0 | 82.7 | 79.0 |

| stage | 26 | 30 | 41 | 42 | 43 | 44 | 47 | 48 | 49 | 50 | dev | held-out |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| write | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| recall | 2 | 3 | 9 | 13 | 5 | 13 | 7 | 6 | 6 | 5 | 27 | 42 |
| cut | 1 | 2 | 3 | 10 | 8 | 8 | 4 | 11 | 8 | 6 | 16 | 45 |
| reader | 10 | 7 | 8 | 20 | 18 | 12 | 17 | 12 | 17 | 17 | 45 | 93 |
| judge | 1 | 0 | 2 | 2 | 1 | 1 | 1 | 2 | 0 | 1 | 5 | 6 |
| gold error | 1 | 2 | 2 | 3 | 7 | 1 | 1 | 1 | 4 | 1 | 8 | 15 |

| category | n dev | dev % | n held-out | held-out % |
|---|---:|---:|---:|---:|
| single-hop | 311 | 87.8 | 530 | 89.2 |
| multi-hop | 111 | 72.1 | 171 | 62.6 |
| temporal | 130 | 87.7 | 191 | 76.4 |
| open-domain | 32 | 50.0 | 64 | 45.3 |
| all | 584 | 82.7 | 956 | 79.0 |

Held-out is 3.7 points below dev (79.0 against 82.7): multi-hop 62.6 against 72.1 and temporal 76.4 against 87.7; single-hop is even (89.2 against 87.8) and open-domain is 45.3 against 50.0. Per 100 questions the held-out conversations lose more to cuts (4.7 against 2.7) and to reader date/inference failures, and carry 15 of the 23 errata rows. conv-44 (71.5) and conv-42 (75.9, the two-persona conversation) are the weakest; conv-26 (90.1) is the best.

### 2.3 What each stage looks like (measured facts)

- **(a) write, 0.** Nothing to fix; the tags of every stored turn are in `write_trace.jsonl`.
- **(b) recall, 69** (single 9, multi 34, temporal 4, open 22). Gold turns in no leg per question: one in 45 questions, two in 17, three in 3, four or more in 4; every gold turn missed in 26; the query analysis flags a set/list/aggregation shape in 27; median 3 gold turns per question, and 18 are questions that have only one gold turn. These are multi-turn evidence sets (accuracy falls from 85.8% with one gold turn to 72.6% with two, 61.0% with three or four and 44.2% with five or more).
- **(c) cut, 61.** 66 gold turns were lost at fusion in 56 questions (vector rank median 42, only 10 within the top 30; 20 turns found by BM25 only, 9 by an extra leg only). Of the 56 fusion cuts, **23 have a lost gold turn inside the top 10 of one leg (16 within the top 3, 15 of them single-hop)**: the turn was found and then not kept. A further 33 sit at leg rank 11-60. All 5 `rerank_keep` cuts have the gold turn at position 13 to 15 of the reranker order, 3 to 5 places below the keep line (one of them, 3-88, is the malformed-evidence row); `rerank_keep` 15 would have kept all five. 26 gold turns in all were cut by `rerank_keep` (16 in wrong answers, 10 in correct ones); 9 gold turns that never reached the context sit at positions 11-15 of the reranker order.
- **(d) reader, 138.** Wrong detail 47 (single-hop 38), incomplete list 22, wrong count 8, date/duration 25, refusal or premise denial 11, inference 23 (all open-domain), distractor 1, wrong speaker 1. 136 of the 138 have every gold turn in the context; the reader is the single largest stage.
- **(e) judge, 11.** 4 are credited by the built conventions (I58: `list_superset` x3, `typo` x1: 0-70, 2-30, 3-183, 4-30). The other 7 are a single gold phrase contained in a longer answer (2-120, 9-127), a relative-year gold ("N years ago" form) against an absolute year (7-101), an answer day inside a coarse gold range (5-37, 6-59), a near-synonym (3-148) and a list that holds both gold items (7-145): gap I77.
- **(f) gold error, 23.** All 22 default-tag rows are wrong answers (an unfixable 1.4 points); 41/2-68 (premise error) is the 23rd label. One new errata candidate was found while reading: 42/3-35 (the gold date is the anchor date of the session, the annotated line resolves to a different day and the reader followed the annotation); it is not yet in `locomo_errata.json` (I62).

## 3. The perspective trade-off

### 3.1 The numbers

| category | n | eq06-fix % | full-persp % | gained | lost | net |
|---|---:|---:|---:|---:|---:|---:|
| single-hop | 841 | 90.8 | 88.7 | 18 | 36 | -18 |
| multi-hop | 282 | 62.8 | 66.3 | 32 | 22 | 10 |
| temporal | 321 | 76.6 | 81.0 | 30 | 16 | 14 |
| open-domain | 96 | 49.0 | 46.9 | 9 | 11 | -2 |
| all | 1540 | 80.1 | 80.4 | 89 | 85 | 4 |

| conv | n | eq06-fix % | full-persp % | gained | lost | net |
|---|---:|---:|---:|---:|---:|---:|
| conv-26 | 152 | 83.6 | 90.1 | 14 | 4 | 10 |
| conv-30 | 81 | 85.2 | 82.7 | 4 | 6 | -2 |
| conv-41 | 152 | 88.2 | 84.2 | 4 | 10 | -6 |
| conv-42 | 199 | 75.4 | 75.9 | 14 | 13 | 1 |
| conv-43 | 178 | 76.4 | 78.1 | 9 | 6 | 3 |
| conv-44 | 123 | 74.8 | 71.5 | 7 | 11 | -4 |
| conv-47 | 150 | 82.0 | 80.0 | 5 | 8 | -3 |
| conv-48 | 191 | 82.2 | 83.2 | 11 | 9 | 2 |
| conv-49 | 156 | 78.8 | 77.6 | 8 | 10 | -2 |
| conv-50 | 158 | 77.8 | 81.0 | 13 | 8 | 5 |

This comparison is a **stack** comparison: `qa-full-qs-eq06-fix` has no reranker, no list mode and no perspective, so the +4 net (89 gained, 85 lost, p = 0.82) mixes the reranker, list mode and pool-2 effects with the perspective layer. The pure perspective effect is the dev control with `xb-loc-dev`:

| category | n | xb-loc-dev % | full-persp % | gained | lost | net |
|---|---:|---:|---:|---:|---:|---:|
| single-hop | 311 | 90.7 | 87.8 | 4 | 13 | -9 |
| multi-hop | 111 | 64.0 | 72.1 | 13 | 4 | 9 |
| temporal | 130 | 90.0 | 87.7 | 2 | 5 | -3 |
| open-domain | 32 | 40.6 | 50.0 | 4 | 1 | 3 |
| all | 584 | 82.7 | 82.7 | 23 | 23 | 0 |

Pure perspective is net 0 on dev (23 gained, 23 lost) but redistributes: multi-hop +9 (13 against 4, p = 0.049), open-domain +3 (4 against 1), temporal -3 (2 against 5), single-hop -9 (4 against 13, p = 0.049). Against `qa-full-qs-eq06-fix` on all ten conversations single-hop is -18 net (18 against 36, p = 0.02), multi-hop +10, temporal +14 (p = 0.054), open-domain -2.

### 3.2 Hypothesis: single-hop and open-domain losses come from down-weighting the other speaker's turns that hold the answer

**Not supported.** The factor that down-weights a turn is applied to 1,400 pool candidates over 1,540 questions (under one per question): 934 are the target's own turns about someone else (x0.84), 340 are turns about another person (x0.6), 126 an unresolved third party (x0.8). It reaches the answer rarely:

| gold turn class | gold turns | in pool | in final top-10 | in context | in the perspective leg |
|---|---:|---:|---:|---:|---:|
| factor 1.0 (about the target) | 2030 | 1669 | 1645 | 1821 | 1684 |
| factor 0.84 (target speaks about another person) | 34 | 34 | 34 | 34 | 0 |
| factor 0.8 (unresolved third party) | 3 | 3 | 3 | 3 | 0 |
| factor 0.6 (turn about someone else) | 8 | 8 | 6 | 7 | 0 |
| not retrieved by any leg | 281 | 0 | 0 | 95 | 0 |

Questions with at least one gold turn at factor<1 ('other speaker' = the `spk:` tag of the gold record is not a resolved target of the question).

| group | n | gold at factor<1 | % | of which other speaker | % | gold at 0.6 | % |
|---|---:|---:|---:|---:|---:|---:|---:|
| lost vs eq06-fix | 85 | 5 | 5.9 | 1 | 1.2 | 1 | 1.2 |
| gained vs eq06-fix | 89 | 3 | 3.4 | 1 | 1.1 | 1 | 1.1 |
| both correct | 1149 | 28 | 2.4 | 5 | 0.4 | 4 | 0.3 |
| both wrong | 217 | 7 | 3.2 | 2 | 0.9 | 2 | 0.9 |
| lost vs xb-loc-dev (dev) | 23 | 2 | 8.7 | 0 | 0.0 | 0 | 0.0 |
| gained vs xb-loc-dev (dev) | 23 | 1 | 4.3 | 0 | 0.0 | 0 | 0.0 |
| all questions | 1540 | 43 | 2.8 | 9 | 0.6 | 8 | 0.5 |

| category | set vs eq06-fix | questions | gold at factor<1 | of which other speaker |
|---|---:|---:|---:|---:|
| single-hop | lost | 36 | 4 | 1 |
| single-hop | gained | 18 | 0 | 0 |
| single-hop | all questions | 841 | 25 | 6 |
| multi-hop | lost | 22 | 1 | 0 |
| multi-hop | gained | 32 | 2 | 1 |
| multi-hop | all questions | 282 | 13 | 1 |
| temporal | lost | 16 | 0 | 0 |
| temporal | gained | 30 | 0 | 0 |
| temporal | all questions | 321 | 4 | 2 |
| open-domain | lost | 11 | 0 | 0 |
| open-domain | gained | 9 | 1 | 0 |
| open-domain | all questions | 96 | 1 | 0 |

- Of 2,356 gold turns, **45 carry a factor below 1 (1.9%); all 45 are in the pool, 43 in the final top 10, 44 in the context**. The factor is not what loses them.
- Among the 85 questions lost against eq06-fix, 5 have a down-weighted gold turn (1 from the other speaker); among the 36 single-hop losses 4 (1 from the other speaker) and among the 11 open-domain losses 0. The questions gained are no different (3 of 89), and the base rate over all questions is 2.8%.

Retrieval change behind the flips, against `qa-full-qs-eq06-fix` and against the pure-perspective control:

| flip set | n | more gold in context (persp) | fewer | same count | persp has ALL gold, ref not | ref has ALL gold, persp not | identical context turn set |
|---|---:|---:|---:|---:|---:|---:|---:|
| lost vs eq06-fix | 85 | 8 | 24 | 52 | 6 | 21 | 0 |
| gained vs eq06-fix | 89 | 38 | 1 | 50 | 25 | 0 | 0 |
| lost vs xb-loc-dev | 23 | 2 | 6 | 15 | 2 | 6 | 0 |
| gained vs xb-loc-dev | 23 | 7 | 1 | 15 | 4 | 1 | 0 |

### 3.3 What actually moves: the perspective leg crowds the pool

`perspective_leg` takes the top 30 records of the vector order whose subject is the question's target and adds them as one more RRF leg (all scores 1.0). Records the vector leg already ranks high and that are about the target now collect two votes; a gold turn that **only BM25 finds** (not in the vector top 60, so it cannot be in the leg that is built from the vector order) keeps one vote and drops below the pool line of 20.

| gold turn class | gold turns | pool: without | pool: with | final-10: without | final-10: with | context: without | context: with |
|---|---:|---:|---:|---:|---:|---:|---:|
| factor 1.0 (about the target) | 706 | 579 | 600 | 570 | 592 | 642 | 652 |
| factor 0.84 (target speaks about another person) | 19 | 19 | 19 | 19 | 19 | 19 | 19 |
| factor 0.8 (unresolved third party) | 2 | 2 | 2 | 2 | 2 | 2 | 2 |
| factor 0.6 (turn about someone else) | 3 | 3 | 3 | 3 | 3 | 3 | 3 |
| lexical-only gold (BM25 top-10, not in vector top-60) | 20 | 11 | 3 | 8 | 2 | 14 | 11 |
| vector top-30 but NOT in the perspective leg | 47 | 39 | 37 | 39 | 37 | 47 | 46 |
| in the perspective leg | 593 | 534 | 576 | 529 | 569 | 563 | 586 |

| category | gold turns | pool: without | pool: with | final-10: without | final-10: with | context: without | context: with |
|---|---:|---:|---:|---:|---:|---:|---:|
| single-hop | 323 | 266 | 264 | 261 | 261 | 311 | 306 |
| multi-hop | 316 | 189 | 207 | 187 | 204 | 225 | 238 |
| temporal | 136 | 128 | 127 | 128 | 127 | 135 | 131 |
| open-domain | 55 | 20 | 26 | 18 | 24 | 29 | 32 |

- Gold turns in the perspective leg: pool 534 -> 576, final 529 -> 569, context 563 -> 586 (this is the multi-hop and open-domain gain: gold turns in the pool +18 and +6).
- **Gold turns that BM25 finds and the vector does not (20 in dev): pool 11 -> 3, final top 10 8 -> 2, context 14 -> 11.** Over all ten conversations 26 such gold turns (BM25 rank <= 3, no vector rank): 3 in the pool, 15 in the context via the replay window, against 24 in the `qa-full-qs-eq06-fix` context.
- Single-hop and open-domain losses against eq06-fix: 16 gold turns were in the eq06-fix context and not in the persp context; **15 of the 16 are outside the pool (fusion)**, 10 had BM25 rank <= 3, 7 are BM25-only, 1 carries a factor, 1 is a `rerank_keep` cut.
- In the 13 single-hop dev losses against `xb-loc-dev`, 4 lose gold turns from the context and **9 have the same gold coverage** (the other lines changed): reader churn. Over the dev conversations 490 questions have complete evidence in both runs and 23 of them (4.7%) still flip; against eq06-fix it is 80 of 1,212 (6.6%). The 50 dev questions whose context turn set is identical in the two runs do not flip (0 of 50), so every flip is caused by a different context, not by sampling. The dev single-hop deficit (-9) is therefore about half crowding (4 gold turns lost) and half sensitivity of the 9B reader to a changed neighbourhood; at the 4.7% churn rate 311 single-hop questions produce about 15 flips in either direction.

Verdict: keep the subject factor (it touches under one candidate per question and 1.9% of gold turns, and costs nothing measurable), **change the leg** (I75): make the perspective leg a multiplier or a half-weight voter, and give the top of every leg a guaranteed pool slot. The leg is what pays for multi-hop (+9) and what costs the BM25-only single-hop answers.

## 4. Temporal

Temporal is 81.0% on all conversations: 87.7% on dev (90.0% for `xb-loc-dev`, which is the "about 90 on dev") and 76.4% on the six held-out conversations; conv-44 is 50.0% (12 of 24).

| conv | n temporal | full-persp % | eq06-fix % | wrong |
|---|---:|---:|---:|---:|
| conv-26 | 37 | 91.9 | 83.8 | 3 |
| conv-30 | 26 | 92.3 | 88.5 | 2 |
| conv-41 | 27 | 88.9 | 92.6 | 3 |
| conv-42 | 40 | 80.0 | 70.0 | 8 |
| conv-43 | 26 | 73.1 | 76.9 | 7 |
| conv-44 | 24 | 50.0 | 62.5 | 12 |
| conv-47 | 34 | 73.5 | 73.5 | 9 |
| conv-48 | 42 | 92.9 | 76.2 | 3 |
| conv-49 | 33 | 75.8 | 75.8 | 8 |
| conv-50 | 32 | 81.2 | 68.8 | 6 |
| dev | 130 | 87.7 | 82.3 | 16 |
| held-out | 191 | 76.4 | 72.8 | 45 |

### 4.1 Failures by cause

The reader causes were assigned from the exact prompt (the dated line of each gold turn, with its `[= date]` annotation), the raw answer and the gold.

| cause | total | dev | held-out |
|---|---:|---:|---:|
| reader: duration / interval arithmetic, or a start date from 'N units ago' (code could do it) | 16 | 3 | 13 |
| gold turn missing (fusion cut, not in the pool) | 12 | 1 | 11 |
| gold error (errata) | 9 | 2 | 7 |
| reader: wrong event matched (distractor line, or the prompt's own date example copied) | 5 | 3 | 2 |
| reader: refusal / premise denial although the annotated evidence is in the prompt | 4 | 2 | 2 |
| gold turn missing (recall) | 4 | 2 | 2 |
| reader: session or anchor date given as the event date (or a day invented inside a coarse range) | 3 | 1 | 2 |
| reader: bare weekday / weekend phrase without a resolved [= date] annotation | 2 | 1 | 1 |
| gold turn missing (rerank_keep cut) | 2 | 0 | 2 |
| judge (range / day-level equivalence) | 2 | 0 | 2 |
| reader: annotation and answer agree, the gold uses the anchor date (gold-format mismatch) | 1 | 1 | 0 |
| reader: incomplete list | 1 | 0 | 1 |
| **temporal wrong** | 61 | 16 | 45 |

| cause | 26 | 30 | 41 | 42 | 43 | 44 | 47 | 48 | 49 | 50 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gold error (errata) | 1 | 0 | 0 | 1 | 2 | 1 | 1 | 0 | 3 | 0 |
| reader: refusal / premise denial although the annotated evidence is in the prompt | 2 | 0 | 0 | 0 | 1 | 0 | 0 | 1 | 0 | 0 |
| reader: wrong event matched (distractor line, or the prompt's own date example copied) | 0 | 1 | 0 | 2 | 0 | 0 | 1 | 0 | 0 | 1 |
| gold turn missing (fusion cut, not in the pool) | 0 | 1 | 0 | 0 | 1 | 4 | 2 | 0 | 1 | 3 |
| gold turn missing (recall) | 0 | 0 | 1 | 1 | 0 | 0 | 1 | 1 | 0 | 0 |
| reader: duration / interval arithmetic, or a start date from 'N units ago' (code could do it) | 0 | 0 | 2 | 1 | 2 | 4 | 3 | 0 | 2 | 2 |
| reader: bare weekday / weekend phrase without a resolved [= date] annotation | 0 | 0 | 0 | 1 | 0 | 1 | 0 | 0 | 0 | 0 |
| reader: annotation and answer agree, the gold uses the anchor date (gold-format mismatch) | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
| reader: session or anchor date given as the event date (or a day invented inside a coarse range) | 0 | 0 | 0 | 1 | 0 | 1 | 0 | 1 | 0 | 0 |
| gold turn missing (rerank_keep cut) | 0 | 0 | 0 | 0 | 1 | 0 | 0 | 0 | 1 | 0 |
| judge (range / day-level equivalence) | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0 | 0 | 0 |
| reader: incomplete list | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 |
| **wrong / n** | 3/37 | 2/26 | 3/27 | 8/40 | 7/26 | 12/24 | 9/34 | 3/42 | 8/33 | 6/32 |

Plain words for the reader causes (examples are ids, not text):

- **Duration and interval arithmetic, 16** (13 held-out): the endpoints are in the prompt, annotated, and the 9B reader subtracts wrongly (months between two dated events: 5-34, 5-47, 5-59, 8-39; weeks: 2-63, 4-16; a start date from "for N years" or "N months now": 2-31, 4-56, 6-4, 8-25), refuses (3-43, 6-53, 6-64) or gives a wrong figure for the length of a project or event (5-7, 9-61, 9-62). Most wrong figures are off by one or two units.
- **Bare weekday or weekend phrase without an annotation, 2** (3-31: the line names a weekday with no annotation and the answer returned the session date; 5-22: a bare "weekend" is never mapped to the weekend of a named month).
- **Session or anchor date given as the event date, 3** (3-44, 5-57, 7-71).
- **Wrong event matched, 5** (1-20, 3-41, 6-41, 9-67, and 3-54, where the answer is **the date example printed in the reader prompt itself**; I79).
- **Refusal although the annotated evidence is in the prompt, 4** (0-26, 0-49, 4-40, 7-44: the link needs an image caption, a "last year" reference or one world-knowledge step; 7-44 denies a premise while stating the gold date).
- **Gold-format mismatch, 1** (3-35: annotation and answer agree, the gold uses the anchor day).

Annotation coverage is good: 186 of the 189 temporal gold turns that contain a relative phrase carry a resolved `[= date]` in the prompt. The leftover failures are not unresolved phrases but arithmetic on resolved ones.

### 4.2 Why the held-out conversations are harder

| question type | n dev | dev % | n held-out | held-out % |
|---|---:|---:|---:|---:|
| duration / interval length | 6 | 66.7 | 14 | 14.3 |
| other dated (what/where/who on a date) | 9 | 100.0 | 40 | 65.0 |
| when (event date) | 105 | 88.6 | 123 | 86.2 |
| when + ordinal / ordering | 10 | 80.0 | 8 | 100.0 |
| which year | 0 | - | 6 | 66.7 |

| gold form | n dev | dev % | n held-out | held-out % |
|---|---:|---:|---:|---:|
| exact date gold | 54 | 88.9 | 79 | 87.3 |
| other gold form | 23 | 78.3 | 66 | 57.6 |
| relative / range gold | 53 | 90.6 | 46 | 84.8 |

Mix against rate: with dev's accuracy for each question type, held-out temporal would score 88.3% (the six "which year" questions, which dev does not have, counted at their actual score) against 76.4% observed. The question mix does not explain the gap; the failure rate inside the types does.

- **Duration questions**: 14 in the held-out conversations at 14.3% against 6 on dev at 66.7%.
- **Fusion cuts**: 11 held-out temporal questions lose their gold turn at the pool (4 of them in conv-44, 3 in conv-50) against 1 on dev; every gold turn is in the context for 85.9% of held-out temporal questions against 96.9% on dev.
- **Gold errors**: 7 held-out against 2 on dev (conv-49 alone has 3).
- With dev's rate per cause the held-out set would lose about 4 questions to duration, 1.5 to fusion and 3 to gold errors; it loses 13, 11 and 7. Those three causes explain the whole 11-point gap; the other causes are at dev rates.

Code could repair 21 of the 61 temporal failures (16 duration/start-date, 2 bare weekday, 3 session-as-event); 9 are errata, 18 are missing gold turns, 2 judge, and 11 are reader failures that need a better reader (wrong event, refusal, format).

## 5. Ranked levers to 90 on the full 1,540

Target arithmetic: 1,386 correct needed (148 more); 1,366 on the 1,517 questions that remain after the 23 errata rows (128 more). Capture rates are assumptions, stated so they can be challenged; the levers overlap (a fixed recall can leave a reader failure behind), so the sum is an upper bound of the independent view.

| # | Lever | Built or NEW | Class (q) | Capture | Expected q | Points |
|---|---|---|---|---:|---:|---:|
| 1 | Reserve pool slots for the top of every leg and make the perspective leg a multiplier / half-weight voter | **NEW I75** (pool and leg weights exist: `candidate_pool`, per-leg weights) | fusion cuts with the lost gold inside a leg's top 10: 23 (15 single-hop) | 60% | 14 | +0.9 |
| 2 | Duration / interval solver by code, date repair, bare-weekday and "for N units" annotation | **NEW I76, I79**; built `--date-repair` (I57, "when" questions only) | duration/start-date 17, bare weekday 2, session-as-event 3 = 22 | 55% | 12 | +0.8 |
| 3 | Agentic multi-step read, intent list trigger, decomposition planner for recall | built I67 (screen pending), I4 (neutral/safe), B1 H3 planner | recall 69 (multi-hop 34, open-domain 22, 27 set-shaped) | 20% | 14 | +0.9 |
| 4 | Wider pool (3), chunked rerank, `rerank_context`, `rerank_keep` 15 | built B10, I9, `candidate_pool`; screens pending | fusion cuts at leg rank 11-60: 33, `rerank_keep` cuts at order 13-15: 5 | 30% | 11 | +0.7 |
| 5 | Inference route and premise-tolerant answering | built `routed_generic`, I1 neutral retry (on, accepted 0 of 22); I61 open | inference 23, non-duration refusals 7 | 30% | 9 | +0.6 |
| 6 | List and count steps | built I56 `--count-verify`, `routed_generic` list arm, C6 | incomplete list 22, wrong count 8 | 27% | 8 | +0.5 |
| 7 | Detail: top-hit-first block, `rerank_context`, token window, owner check | built B10, I6, I59 (all opt-in, no screen on this run) | wrong detail 47, distractor 1, wrong speaker 1 | 15% | 7 | +0.5 |
| | **Counted sum** | | | | **75** | **+4.9** |
| 8 | Judge conventions (second column): I58 built, 4 credits today; I77 NEW adds containment and range equivalence | built I58 / **NEW I77** | judge 11 | 80% | 9 on the conventions column only | +0.6 (measurement) |
| 9 | Larger reader (27-32B), thinking mode | C11 (blocked by GPU memory), A9 | reader failures with complete evidence 136 (plus 10 judge, 22 errata) | unmeasured | not counted (10 to 30 plausible) | |
| 10 | Errata: report with and without, add 1 candidate | I62 | 23 (+1) | 100% | denominator only | 81.4% headline excluding errata |

Scenarios: **assumed capture 1,313 / 1,540 = 85.3%** (86.6% without errata; 87.1% without errata and with the conventions column). **1.5x capture plus 20 questions from a larger reader: 89.0%** (90.3% without errata). **90.0% on the headline needs 148 questions, 2.0x the counted sum.** Perfect retrieval alone (the 235 incomplete-evidence questions answered at the 87.1% evidence-complete accuracy) gives 87.1%; the last three points are the reader and judge. Rank by points per effort: 1 and 2 are small, deterministic and cheap (no extra model call), 4 and 7 are built and need only screens, 3 is the only lever on the largest class and also the least certain, 5 and 6 are bound by the 9B reader.

## 6. New gaps (register section I, ids I75-I79)

I75 leg-protected pool and a cheaper perspective leg; I76 duration / interval solver by code; I77 judge conventions beyond list/typo/numeral (containment, date-range and relative-year equivalence, near-synonym on a disputed row only); I78 the engine logs no fusion cut and no per-leg floors (cuts hold only `rerank_keep`); I79 relative-date annotator gaps (bare weekday, "the weekend", "N units now" start dates) and the literal date example in the reader prompt that was copied once. Each is written with evidence, solution options, status and the non-overfitting check in `GAP_REGISTER.md` section I.

## 7. Explorer

`evals/build_forensics_explorer.py --all-convs --no-summary` rebuilds `evals/runs/_analysis/forensics_explorer.html` (gitignored, local) with all ten conversations and the full traces: baseline `full-persp-loc` read from its `--trace` folder, comparison runs `xb-loc-dev`, `qa-full-qs-eq06-fix`, `qa-full-qs-eq06-roff-fx` and `full-persp-opb` (OP-Bench, still running at build time, shown as "running" with its finished probes: re-run the command when `full-persp-opb--memspine/results.jsonl` has its summary row). Verified in headless Edge on real trace files: the page loads and every tab renders (overview, failure matrix, gates, questions, memory, pipeline coverage); on 11 questions across conversations 26, 30, 41, 43, 44, 47, 50 the question detail shows, for `full-persp-loc`, the logged query analysis (shape flags and split intents), the resolved perspective with each down-weighted candidate named by turn, all legs with gold ranks, the reranker order, the exact cut table (reason `rerank_keep`, keep 10), the assembled block, the replay-window anchors with their neighbours, the exact reader prompt and raw replies (both calls of a refusal retry), the judge prompt and raw reply, and, for the 85 guard verdicts, a statement that no judge call was made; no read step is marked "not logged" (steps 2 and 8 are now "logged": no decider or relevance gate in this config, the memory lines are inside the logged prompt); on the write side W1-W8 and W10 are logged from `write_trace.jsonl` for conversations 26, 43 and 50 (tags, firewall signals with the outlier and MINJA scores, record id), W9 is honestly "not logged" because `observability.write_timers` was off. Fixes made to the generator while doing this: `log_dir` now prefers the `--trace` folder (the plain `--forensics` folder of the same run has no `trace_full`), legacy stage logs without `query_id` are joined by question text, the held-out guard is lifted only by `--all-convs`, the plain dev build skips `full-*` runs, the unused duplicate `context_text` is no longer embedded (the page is 75.6 MB, loads in a headless browser in under 8 s), and guard verdicts, the logged query analysis and a readable perspective factor list are displayed.

## 8. Limits

One run, one reader, one judge (A1 deferred); the stage labels were assigned by one reviewer, once, and the retrieval labels use the weakest link, so a question with a missing turn that was not needed for the answer can be over-counted as retrieval (the labels `b`/`cf` need a gold turn that did not reach the context; 235 questions are in that state, 133 wrong). The stack comparison against `qa-full-qs-eq06-fix` confounds reranker, list mode and perspective; the pure perspective numbers exist for the four dev conversations only. The sign tests are two-sided exact on the flip counts and ignore multiplicity. Class sizes are accurate to about +-3 questions at the boundaries; capture rates are assumptions.
