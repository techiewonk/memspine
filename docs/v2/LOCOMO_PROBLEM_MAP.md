# LoCoMo problem map: class x stage x category, solutions, screens (LOCOMO-ATLAS)

Generated 2026-10-13 by `evals/build_locomo_problem_atlas.py` from existing run files only (CPU, no model call). **Text-free**: ids, codes, counts, numbers. The per-question explorer (`evals/runs/_analysis/locomo_problem_atlas.html`) and the per-question table carry dataset text and stay local and gitignored.

- **Run used: `clean-jina-full`** (latest clean full reference, jina reranker v3.5, Qwen3.5-9B reader and judge). It has leg ranks (`clean-jina-full--forensics/forensics.jsonl`) but no `--trace` folder, so the stage codes are computed here from the logged ranks and the classifier below, not read from the hand labels of `full-persp-loc`. Hand-label agreement is checked on `full-persp-loc` (section 3).
- Scope: all 10 conversations, categories 1-4 (1540 questions, 287 wrong = 81.4% correct). The run has no category 5 (the manifest says `full+cat1/2/3/4`), so cat 5 is not in this map.
- Per-question labels (text-free): `evals/analysis/locomo_problem_labels_clean_jina_full.txt`; per-session counts: `evals/analysis/locomo_session_problem_counts_clean_jina_full.csv`. Solution map (data): `evals/analysis/locomo_solution_map.json` (generator `evals/build_locomo_solution_map.py`).
- All recoverable numbers are **hypotheses** (capture-rate assumptions on measured class sizes), not measurements.

## 1. Class x stage x category (wrong answers)

`stage` is the weakest link (a write, b recall, cf fusion/pool cut, cr rerank_keep cut, d reader sub-type, e judge/metric, f gold error). `H` marks a heuristic class (the rule reads wording or token overlap; the rule text is in the JSON map); the rest are direct tests of logged ranks, tags or flags.

| class | H | stage(s) | single-hop | multi-hop | temporal | open-domain | total | % of wrong |
|---|---|---|---:|---:|---:|---:|---:|---:|
| `wrong_detail` | H | d:det 46 | 30 | 14 | 2 | 0 | 46 | 16.0 |
| `list_aggregation_generic` | H | b 25, cf 12 | 1 | 36 | 0 | 0 | 37 | 12.9 |
| `open_domain_inference_retrieval` |  | b 24, cf 3, cr 1 | 0 | 0 | 0 | 28 | 28 | 9.8 |
| `inference_open_domain` |  | d:inf 26 | 0 | 0 | 0 | 26 | 26 | 9.1 |
| `gold_error` |  | f 24 | 13 | 2 | 9 | 0 | 24 | 8.4 |
| `list_incomplete` |  | d:lst 16 | 4 | 12 | 0 | 0 | 16 | 5.6 |
| `refusal_premise_denial` |  | d:ref 14 | 2 | 2 | 10 | 0 | 14 | 4.9 |
| `temporal_evidence_missing` |  | cf 8, b 5, cr 1 | 0 | 0 | 14 | 0 | 14 | 4.9 |
| `fusion_loss_lexical_only` |  | cf 12 | 8 | 4 | 0 | 0 | 12 | 4.2 |
| `date_duration_arithmetic` | H | d:dar 10 | 1 | 0 | 9 | 0 | 10 | 3.5 |
| `count_wrong` | H | d:cnt 9 | 0 | 9 | 0 | 0 | 9 | 3.1 |
| `date_wrong_event` |  | d:dar 8 | 0 | 0 | 8 | 0 | 8 | 2.8 |
| `judge_false_negative` | H | e 6 | 4 | 1 | 1 | 0 | 6 | 2.1 |
| `fusion_loss_vector_only` |  | cf 6 | 3 | 3 | 0 | 0 | 6 | 2.1 |
| `image_caption_misuse` | H | d:det 6 | 6 | 0 | 0 | 0 | 6 | 2.1 |
| `multi_evidence_recall` |  | b 5 | 0 | 5 | 0 | 0 | 5 | 1.7 |
| `implicit_or_paraphrased_gold` | H | b 3, cf 1 | 3 | 1 | 0 | 0 | 4 | 1.4 |
| `vocabulary_mismatch_recall` | H | b 4 | 2 | 2 | 0 | 0 | 4 | 1.4 |
| `distractor_line` | H | d:dis 3 | 3 | 0 | 0 | 0 | 3 | 1.0 |
| `rerank_keep_cut` |  | cr 2 | 1 | 1 | 0 | 0 | 2 | 0.7 |
| `lexical_outranked_recall` | H | b 2 | 1 | 1 | 0 | 0 | 2 | 0.7 |
| `date_offset_small` |  | d:dar 1 | 0 | 0 | 1 | 0 | 1 | 0.3 |
| `relative_date_gold_vs_judge` | H | e 1 | 1 | 0 | 0 | 0 | 1 | 0.3 |
| `no_gold_resolvable` |  | x 1 | 0 | 0 | 0 | 1 | 1 | 0.3 |
| `speaker_confusion` | H | d:spk 1 | 0 | 1 | 0 | 0 | 1 | 0.3 |
| `fusion_loss_two_leg_crowded` |  | cf 1 | 1 | 0 | 0 | 0 | 1 | 0.3 |
| **all wrong** | | | **84** | **94** | **54** | **55** | **287** | 100 |

| stage | single-hop | multi-hop | temporal | open-domain | total |
|---|---:|---:|---:|---:|---:|
| b (b) recall: gold in no leg | 6 | 33 | 5 | 24 | 68 |
| cf (c) cut at fusion / pool | 13 | 19 | 8 | 3 | 43 |
| cr (c) cut at rerank_keep | 1 | 1 | 1 | 1 | 4 |
| d:det (d) reader: wrong detail | 36 | 14 | 2 | 0 | 52 |
| d:lst (d) reader: incomplete list | 4 | 12 | 0 | 0 | 16 |
| d:cnt (d) reader: wrong count | 0 | 9 | 0 | 0 | 9 |
| d:dar (d) reader: date / duration | 1 | 0 | 18 | 0 | 19 |
| d:ref (d) reader: refusal | 2 | 2 | 10 | 0 | 14 |
| d:inf (d) reader: inference | 0 | 0 | 0 | 26 | 26 |
| d:dis (d) reader: distractor | 3 | 0 | 0 | 0 | 3 |
| d:spk (d) reader: wrong speaker | 0 | 1 | 0 | 0 | 1 |
| e (e) judge / metric | 5 | 1 | 1 | 0 | 7 |
| f (f) gold error | 13 | 2 | 9 | 0 | 24 |
| x unresolved | 0 | 0 | 0 | 1 | 1 |

Non-exclusive tags on wrong questions: `3plus_gold_turns` 55, `spans_3plus_sessions` 43, `errata` 32, `also_fusion_lost` 23, `leg_top10_cut` 13, `answer_contains_gold` 4, `convention` 2, `relative_year_gold` 1.

Dev (conv-26/30/41/42) against held-out wrong counts: 96 of 584 dev questions, 191 of 956 held-out.

## 2. Per conversation

| conversation | questions | wrong | accuracy % |
|---|---:|---:|---:|
| conv-26 | 152 | 19 | 87.5 |
| conv-30 | 81 | 13 | 84.0 |
| conv-41 | 152 | 17 | 88.8 |
| conv-42 | 199 | 47 | 76.4 |
| conv-43 | 178 | 42 | 76.4 |
| conv-44 | 123 | 26 | 78.9 |
| conv-47 | 150 | 29 | 80.7 |
| conv-48 | 191 | 34 | 82.2 |
| conv-49 | 156 | 29 | 81.4 |
| conv-50 | 158 | 31 | 80.4 |

Dev (conv-26/30/41/42) against held-out (the other six) per 100 questions, classes with at least 5 wrong questions:

| class | dev wrong | dev per 100 | held-out wrong | held-out per 100 |
|---|---:|---:|---:|---:|
| `wrong_detail` | 12 | 2.1 | 34 | 3.6 |
| `list_aggregation_generic` | 14 | 2.4 | 23 | 2.4 |
| `open_domain_inference_retrieval` | 8 | 1.4 | 20 | 2.1 |
| `inference_open_domain` | 9 | 1.5 | 17 | 1.8 |
| `gold_error` | 8 | 1.4 | 16 | 1.7 |
| `list_incomplete` | 4 | 0.7 | 12 | 1.3 |
| `refusal_premise_denial` | 6 | 1.0 | 8 | 0.8 |
| `temporal_evidence_missing` | 2 | 0.3 | 12 | 1.3 |
| `fusion_loss_lexical_only` | 8 | 1.4 | 4 | 0.4 |
| `date_duration_arithmetic` | 0 | 0.0 | 10 | 1.0 |
| `count_wrong` | 4 | 0.7 | 5 | 0.5 |
| `date_wrong_event` | 6 | 1.0 | 2 | 0.2 |
| `judge_false_negative` | 3 | 0.5 | 3 | 0.3 |
| `fusion_loss_vector_only` | 2 | 0.3 | 4 | 0.4 |
| `image_caption_misuse` | 0 | 0.0 | 6 | 0.6 |
| `multi_evidence_recall` | 2 | 0.3 | 3 | 0.3 |

## 3. Classifier check against the hand labels

`full-persp-loc` has one hand-read stage code per wrong answer (`full_persp_loc_stage_labels.txt`, 302 rows). The same classifier run on that run's logged ranks agrees on 279 of 302 codes exactly (92.4%) and on 296 (98.0%) at the group level (b / cf / cr / d / e / f). Largest disagreements (hand > classifier): `d:lst>d:det` 8, `d:det>d:lst` 2, `d:det>d:inf` 2, `e>d:det` 2, `d:dar>d:det` 2, `d:dis>d:det` 1, `d:ref>d:inf` 1, `cr>b` 1, `d:inf>cr` 1, `d:spk>d:ref` 1, `d:det>e` 1, `d:inf>x` 1.

The reader sub-types and the e/f split are where rules and a human reader differ most; those classes are marked H. Retrieval stages (b, cf, cr) are direct tests of the logged ranks and agree by construction except where a question mixes lost turns.

## 4. Solution map: class to solution to setting to screen

Status: **ref** = on in the reference config; **off** = built in the engine, off by default; **harness** = eval-harness flag or reader prompt variant; **GAP** = proposed in a forensic doc, not built. `no screen` = built but no screen id is queued for it. Doc sections for every mapping are in `locomo_solution_map.json` (`doc_refs`).

| class (wrong q) | relation | solution | status | setting keys | screens |
|---|---|---|---|---|---|
| `wrong_detail` (46) | primary | Present the retrieved hits first, neighbours separate; per-shape window | off | `read.present_order`; `read.replay_window_before/after` | no screen |
|  | primary | Reader prompt variants (world-knowledge clause, inference route, answer-style, caption line) | harness | `readers.py: dated_world, dated_infer, grounded_detail, routed_generic` | no screen |
|  | secondary | Neighbour turns v2, exact token budget, complete evidence bundles | off | `read.neighbours_v2`; `read.exact_budget`; `read.evidence_bundles` | S19, S52, S53, S54, S63 |
|  | secondary | Larger reader (27-32B) or thinking mode for inference and detail failures | GAP | `(proposed) llm.roles.reader` | - |
|  | secondary | Answer verification and one bounded repair (verify-answer, verify-slots diagnosed) | harness | `--verify-answer`; `--verify-slots diagnosed` | no screen |
| `list_aggregation_generic` (37) | primary | Bounded second-pass retrieval for missing evidence (agentic query/slot, second_round, fact chain) | off | `read.agentic_mode=query|slot`; `read.second_round`; `read.fact_chain` | no screen |
|  | primary | List mode: speaker-vote leg and wider pool for list-shaped questions | ref | `read.list_mode (on in clean-jina-full)` | no screen |
|  | primary | LLM planner / query rewrites / HyDE-style answer hypothesis | off | `read.planner=llm`; `read.planner_version`; `read.compose_rewrites` | no screen |
|  | primary | Larger candidate pool with a tiered window | off | `read.candidate_pool=3 (reference: 2)` | no screen |
|  | primary | Derived list memory: standing answers, patterns, communities | off | `offline.standing_v2`; `offline.patterns_v2`; `offline.communities_v2` | S34, S35, S40, S64 |
|  | secondary | Typed query contract (list / when / count) shapes evidence and answer format | off | `read.query_contract=heuristic`; `read.contract_rules` | S06, S11, S60 |
|  | secondary | Graph extraction in sleep plus one-hop graph / entity legs | off | `read.graph_leg_v2`; `read.entity_leg_v2`; `graph.edge_compiler_v2` | S02, S13, S28, S30, S32, S39, S61 |
|  | secondary | Evidence obligations for joins, durations, lists, counts | off | `read.obligations`; `read.deep_v2` | S56, S63 |
|  | secondary | Rerank a wider search pool (rerank_v2 + rerank_v2_search_pool) | off | `read.rerank_v2`; `read.rerank_v2_search_pool=30`; `read.rerank_keep` | S16, S67, S60, S61 |
|  | secondary | Write-time fact extraction (window_extract, write-v2 stack incl. temporal stage W2-14) | off | `write.window_extract=facts`; `write.envelope_v2`; `write.temporal_v2` | S03, S04, S43, S44, S45, S48, S62 |
| `open_domain_inference_retrieval` (28) | secondary | Bounded second-pass retrieval for missing evidence (agentic query/slot, second_round, fact chain) | off | `read.agentic_mode=query|slot`; `read.second_round`; `read.fact_chain` | no screen |
|  | secondary | Deep read: one sufficiency check and at most one targeted re-search | off | `read.deep_v2`; `llm.roles.sufficiency` | S55, S63 |
|  | secondary | LLM planner / query rewrites / HyDE-style answer hypothesis | off | `read.planner=llm`; `read.planner_version`; `read.compose_rewrites` | no screen |
| `inference_open_domain` (26) | primary | Larger reader (27-32B) or thinking mode for inference and detail failures | GAP | `(proposed) llm.roles.reader` | - |
|  | primary | Reader prompt variants (world-knowledge clause, inference route, answer-style, caption line) | harness | `readers.py: dated_world, dated_infer, grounded_detail, routed_generic` | no screen |
|  | secondary | Deep read: one sufficiency check and at most one targeted re-search | off | `read.deep_v2`; `llm.roles.sufficiency` | S55, S63 |
| `gold_error` (24) | primary | Errata file: report accuracy with and without gold/dataset errors | ref | `analysis/locomo_errata.json` | no screen |
| `list_incomplete` (16) | primary | Typed query contract (list / when / count) shapes evidence and answer format | off | `read.query_contract=heuristic`; `read.contract_rules` | S06, S11, S60 |
|  | primary | Enumerate-then-count and evidence table (count-verify, verify-slots) | harness | `--count-verify`; `--evidence-table`; `--verify-slots strict|soft|diagnosed` | no screen |
|  | primary | Evidence obligations for joins, durations, lists, counts | off | `read.obligations`; `read.deep_v2` | S56, S63 |
|  | secondary | Neighbour turns v2, exact token budget, complete evidence bundles | off | `read.neighbours_v2`; `read.exact_budget`; `read.evidence_bundles` | S19, S52, S53, S54, S63 |
|  | secondary | List mode: speaker-vote leg and wider pool for list-shaped questions | ref | `read.list_mode (on in clean-jina-full)` | no screen |
|  | secondary | Reader prompt variants (world-knowledge clause, inference route, answer-style, caption line) | harness | `readers.py: dated_world, dated_infer, grounded_detail, routed_generic` | no screen |
|  | secondary | Derived list memory: standing answers, patterns, communities | off | `offline.standing_v2`; `offline.patterns_v2`; `offline.communities_v2` | S34, S35, S40, S64 |
| `refusal_premise_denial` (14) | primary | Reader prompt variants (world-knowledge clause, inference route, answer-style, caption line) | harness | `readers.py: dated_world, dated_infer, grounded_detail, routed_generic` | no screen |
|  | secondary | Deep read: one sufficiency check and at most one targeted re-search | off | `read.deep_v2`; `llm.roles.sufficiency` | S55, S63 |
|  | secondary | Larger reader (27-32B) or thinking mode for inference and detail failures | GAP | `(proposed) llm.roles.reader` | - |
|  | secondary | Answer verification and one bounded repair (verify-answer, verify-slots diagnosed) | harness | `--verify-answer`; `--verify-slots diagnosed` | no screen |
| `temporal_evidence_missing` (14) | primary | Temporal leg on relative / partial dates and event intervals | off | `read.temporal_relative`; `read.temporal_infer_year`; `read.temporal_leg_event_dates` | S24, S62, S63 |
|  | secondary | Session summaries / episodes lifted to turns (L2) | off | `offline.session_close_v2`; `read.episode_leg_v2`; `sleep summary arm (gc9)` | S01, S31, S33, S61, S64 |
|  | secondary | v2 leg registry, rule router, depth tiers | off | `read.legs_v2`; `read.router_rules`; `read.tiers_v2` | S10, S12, S36, S37, S38, S60 |
|  | secondary | Lexical index options (english analyzer, date words in BM25) | off | `read.lexical_analyzer=english`; `read.lexical_dates` | no screen |
|  | secondary | Write-time fact extraction (window_extract, write-v2 stack incl. temporal stage W2-14) | off | `write.window_extract=facts`; `write.envelope_v2`; `write.temporal_v2` | S03, S04, S43, S44, S45, S48, S62 |
| `fusion_loss_lexical_only` (12) | primary | Family fusion (dense / lexical / structured / derived families vote separately) | off | `read.family_fusion` | S14, S51, S60, S61 |
|  | primary | Reserve pool slots for the top of every leg (leg-protected pool) | ref | `read.pool_protect_per_leg (=3 in clean-jina-full)`; `read.pool_protect_mode: per_leg|source_family` | S05, S09 |
|  | secondary | Perspective policy (subject-weight) and perspective-leg weight | ref | `read.perspective_mode=subject_weight (on)`; `read.perspective_leg`; `read.perspective_weight` | no screen |
| `date_duration_arithmetic` (10) | primary | Duration / interval / milestone arithmetic by code | harness | `--duration-solve (I76)`; `--milestones (E05)`; `--date-repair (I57)` | no screen |
|  | secondary | Typed query contract (list / when / count) shapes evidence and answer format | off | `read.query_contract=heuristic`; `read.contract_rules` | S06, S11, S60 |
|  | secondary | Evidence obligations for joins, durations, lists, counts | off | `read.obligations`; `read.deep_v2` | S56, S63 |
| `count_wrong` (9) | primary | Typed query contract (list / when / count) shapes evidence and answer format | off | `read.query_contract=heuristic`; `read.contract_rules` | S06, S11, S60 |
|  | primary | Enumerate-then-count and evidence table (count-verify, verify-slots) | harness | `--count-verify`; `--evidence-table`; `--verify-slots strict|soft|diagnosed` | no screen |
|  | primary | Evidence obligations for joins, durations, lists, counts | off | `read.obligations`; `read.deep_v2` | S56, S63 |
|  | secondary | Derived list memory: standing answers, patterns, communities | off | `offline.standing_v2`; `offline.patterns_v2`; `offline.communities_v2` | S34, S35, S40, S64 |
| `date_wrong_event` (8) | secondary | Duration / interval / milestone arithmetic by code | harness | `--duration-solve (I76)`; `--milestones (E05)`; `--date-repair (I57)` | no screen |
|  | secondary | Resolve relative phrases against the session date (annotation) and repair the gaps | ref | `read.resolve_relative_dates (on)`; `read.relative_dates_anchored (on)`; `I79 annotator (bare weekday, 'the weekend', 'N units now')` | S62 |
|  | secondary | Answer verification and one bounded repair (verify-answer, verify-slots diagnosed) | harness | `--verify-answer`; `--verify-slots diagnosed` | no screen |
| `judge_false_negative` (6) | primary | Deterministic judge pre-checks: date guard and judge conventions (I58) | ref | `judge date guard (date_equivalent)`; `judge_conventions v1 (list_superset, typo, numeral)` | no screen |
|  | primary | Judge conventions beyond list/typo: containment, date-range and relative-year equivalence | GAP | `(proposed) conventions v3` | - |
| `fusion_loss_vector_only` (6) | primary | Family fusion (dense / lexical / structured / derived families vote separately) | off | `read.family_fusion` | S14, S51, S60, S61 |
|  | primary | Reserve pool slots for the top of every leg (leg-protected pool) | ref | `read.pool_protect_per_leg (=3 in clean-jina-full)`; `read.pool_protect_mode: per_leg|source_family` | S05, S09 |
|  | secondary | Larger candidate pool with a tiered window | off | `read.candidate_pool=3 (reference: 2)` | no screen |
|  | secondary | Rerank a wider search pool (rerank_v2 + rerank_v2_search_pool) | off | `read.rerank_v2`; `read.rerank_v2_search_pool=30`; `read.rerank_keep` | S16, S67, S60, S61 |
| `image_caption_misuse` (6) | primary | Reader prompt variants (world-knowledge clause, inference route, answer-style, caption line) | harness | `readers.py: dated_world, dated_infer, grounded_detail, routed_generic` | no screen |
|  | secondary | Resolve image evidence from the original asset | off | `ingest.assets`; `read.asset_evidence=cached|fetch` | no screen |
| `multi_evidence_recall` (5) | primary | Bounded second-pass retrieval for missing evidence (agentic query/slot, second_round, fact chain) | off | `read.agentic_mode=query|slot`; `read.second_round`; `read.fact_chain` | no screen |
|  | primary | Deep read: one sufficiency check and at most one targeted re-search | off | `read.deep_v2`; `llm.roles.sufficiency` | S55, S63 |
|  | primary | Graph extraction in sleep plus one-hop graph / entity legs | off | `read.graph_leg_v2`; `read.entity_leg_v2`; `graph.edge_compiler_v2` | S02, S13, S28, S30, S32, S39, S61 |
|  | primary | LLM planner / query rewrites / HyDE-style answer hypothesis | off | `read.planner=llm`; `read.planner_version`; `read.compose_rewrites` | no screen |
|  | secondary | Neighbour turns v2, exact token budget, complete evidence bundles | off | `read.neighbours_v2`; `read.exact_budget`; `read.evidence_bundles` | S19, S52, S53, S54, S63 |
|  | secondary | Session summaries / episodes lifted to turns (L2) | off | `offline.session_close_v2`; `read.episode_leg_v2`; `sleep summary arm (gc9)` | S01, S31, S33, S61, S64 |
|  | secondary | Derived list memory: standing answers, patterns, communities | off | `offline.standing_v2`; `offline.patterns_v2`; `offline.communities_v2` | S34, S35, S40, S64 |
| `implicit_or_paraphrased_gold` (4) | primary | Session summaries / episodes lifted to turns (L2) | off | `offline.session_close_v2`; `read.episode_leg_v2`; `sleep summary arm (gc9)` | S01, S31, S33, S61, S64 |
|  | primary | Answer-hypothesis (HyDE) leg for implicit / paraphrased gold | GAP | `(proposed) read.hyde_leg` | - |
|  | secondary | Graph extraction in sleep plus one-hop graph / entity legs | off | `read.graph_leg_v2`; `read.entity_leg_v2`; `graph.edge_compiler_v2` | S02, S13, S28, S30, S32, S39, S61 |
|  | secondary | Lift derived hits to source turns before rerank | off | `read.lift_before_rerank` | S15, S60, S61 |
|  | secondary | LLM planner / query rewrites / HyDE-style answer hypothesis | off | `read.planner=llm`; `read.planner_version`; `read.compose_rewrites` | no screen |
|  | secondary | Larger candidate pool with a tiered window | off | `read.candidate_pool=3 (reference: 2)` | no screen |
|  | secondary | Write-time fact extraction (window_extract, write-v2 stack incl. temporal stage W2-14) | off | `write.window_extract=facts`; `write.envelope_v2`; `write.temporal_v2` | S03, S04, S43, S44, S45, S48, S62 |
| `vocabulary_mismatch_recall` (4) | primary | LLM planner / query rewrites / HyDE-style answer hypothesis | off | `read.planner=llm`; `read.planner_version`; `read.compose_rewrites` | no screen |
|  | secondary | Session summaries / episodes lifted to turns (L2) | off | `offline.session_close_v2`; `read.episode_leg_v2`; `sleep summary arm (gc9)` | S01, S31, S33, S61, S64 |
|  | secondary | Answer-hypothesis (HyDE) leg for implicit / paraphrased gold | GAP | `(proposed) read.hyde_leg` | - |
|  | secondary | Graph extraction in sleep plus one-hop graph / entity legs | off | `read.graph_leg_v2`; `read.entity_leg_v2`; `graph.edge_compiler_v2` | S02, S13, S28, S30, S32, S39, S61 |
| `distractor_line` (3) | primary | Owner / event check for wrong-line and wrong-speaker answers | GAP | `(proposed) answer provenance check` | - |
|  | primary | Present the retrieved hits first, neighbours separate; per-shape window | off | `read.present_order`; `read.replay_window_before/after` | no screen |
|  | primary | Answer verification and one bounded repair (verify-answer, verify-slots diagnosed) | harness | `--verify-answer`; `--verify-slots diagnosed` | no screen |
|  | secondary | Neighbour turns v2, exact token budget, complete evidence bundles | off | `read.neighbours_v2`; `read.exact_budget`; `read.evidence_bundles` | S19, S52, S53, S54, S63 |
|  | secondary | Larger reader (27-32B) or thinking mode for inference and detail failures | GAP | `(proposed) llm.roles.reader` | - |
| `rerank_keep_cut` (2) | primary | Raise rerank_keep (10 -> 15) | off | `read.rerank_keep=15` | no screen |
|  | primary | Rerank a wider search pool (rerank_v2 + rerank_v2_search_pool) | off | `read.rerank_v2`; `read.rerank_v2_search_pool=30`; `read.rerank_keep` | S16, S67, S60, S61 |
|  | secondary | Lift derived hits to source turns before rerank | off | `read.lift_before_rerank` | S15, S60, S61 |
| `lexical_outranked_recall` (2) | primary | Lexical index options (english analyzer, date words in BM25) | off | `read.lexical_analyzer=english`; `read.lexical_dates` | no screen |
|  | secondary | Family fusion (dense / lexical / structured / derived families vote separately) | off | `read.family_fusion` | S14, S51, S60, S61 |
|  | secondary | v2 leg registry, rule router, depth tiers | off | `read.legs_v2`; `read.router_rules`; `read.tiers_v2` | S10, S12, S36, S37, S38, S60 |
|  | secondary | Rerank a wider search pool (rerank_v2 + rerank_v2_search_pool) | off | `read.rerank_v2`; `read.rerank_v2_search_pool=30`; `read.rerank_keep` | S16, S67, S60, S61 |
| `date_offset_small` (1) | primary | Duration / interval / milestone arithmetic by code | harness | `--duration-solve (I76)`; `--milestones (E05)`; `--date-repair (I57)` | no screen |
|  | primary | Resolve relative phrases against the session date (annotation) and repair the gaps | ref | `read.resolve_relative_dates (on)`; `read.relative_dates_anchored (on)`; `I79 annotator (bare weekday, 'the weekend', 'N units now')` | S62 |
|  | secondary | Write-time fact extraction (window_extract, write-v2 stack incl. temporal stage W2-14) | off | `write.window_extract=facts`; `write.envelope_v2`; `write.temporal_v2` | S03, S04, S43, S44, S45, S48, S62 |
| `relative_date_gold_vs_judge` (1) | primary | Deterministic judge pre-checks: date guard and judge conventions (I58) | ref | `judge date guard (date_equivalent)`; `judge_conventions v1 (list_superset, typo, numeral)` | no screen |
|  | primary | Judge conventions beyond list/typo: containment, date-range and relative-year equivalence | GAP | `(proposed) conventions v3` | - |
|  | secondary | Resolve relative phrases against the session date (annotation) and repair the gaps | ref | `read.resolve_relative_dates (on)`; `read.relative_dates_anchored (on)`; `I79 annotator (bare weekday, 'the weekend', 'N units now')` | S62 |
| `no_gold_resolvable` (1) | | malformed evidence id (locomo_errata bad_evidence_id): a dataset fix, not an engine one | | | |
| `speaker_confusion` (1) | primary | Owner / event check for wrong-line and wrong-speaker answers | GAP | `(proposed) answer provenance check` | - |
|  | primary | Perspective policy (subject-weight) and perspective-leg weight | ref | `read.perspective_mode=subject_weight (on)`; `read.perspective_leg`; `read.perspective_weight` | no screen |
|  | secondary | Present the retrieved hits first, neighbours separate; per-shape window | off | `read.present_order`; `read.replay_window_before/after` | no screen |
| `fusion_loss_two_leg_crowded` (1) | primary | Family fusion (dense / lexical / structured / derived families vote separately) | off | `read.family_fusion` | S14, S51, S60, S61 |
|  | primary | Reserve pool slots for the top of every leg (leg-protected pool) | ref | `read.pool_protect_per_leg (=3 in clean-jina-full)`; `read.pool_protect_mode: per_leg|source_family` | S05, S09 |
|  | secondary | Larger candidate pool with a tiered window | off | `read.candidate_pool=3 (reference: 2)` | no screen |
|  | secondary | Rerank a wider search pool (rerank_v2 + rerank_v2_search_pool) | off | `read.rerank_v2`; `read.rerank_v2_search_pool=30`; `read.rerank_keep` | S16, S67, S60, S61 |

## 5. Recoverable-question estimate

For each screen: the **upper bound** is the number of wrong questions in the classes the screen's solutions are aimed at (primary relation); the 30% and 60% columns apply a capture assumption to that bound (hypotheses, not measurements: the sample screens in section 6 are the only evidence). `+secondary` adds the classes the screen may touch (bound only, no capture). Bundles (S60-S64) are the union of their components, so their bound is not the sum of the parts. Ranked by upper bound among screens not yet final (queued, deferred, iterate).

### 5.1 Ranked open screens

| rank | screen | status | setting | bundle | upper bound | 30% | 60% | +secondary | sample result | est min | primary classes |
|---:|---|---|---|---|---:|---:|---:|---:|---|---:|---|
| 1 | S60 Bundle: read-v2-core | queued | `profile read-v2-core` | yes | 46 | 13.8 | 27.6 | 67 | not sampled | 160 | fusion_loss_vector_only, fusion_loss_lexical_only, fusion_loss_two_leg_crowded, rerank_keep_cut, list_incomplete, count_wrong |
| 2 | S63 Bundle: read-v2-safe | queued | `profile read-v2-safe` | yes | 44 | 13.2 | 26.4 | 164 | not sampled | 200 | multi_evidence_recall, list_incomplete, count_wrong, temporal_evidence_missing |
| 3 | S64 Bundle: offline-v2 | queued | `profile offline-v2` | yes | 41 | 12.3 | 24.6 | 48 | not sampled | 230 | implicit_or_paraphrased_gold, list_aggregation_generic |
| 4 | S34 Standing answers | queued | `offline.standing_v2 + read.standing_v2` |  | 37 | 11.1 | 22.2 | 30 | not sampled | 150 | list_aggregation_generic |
| 5 | S35 Patterns | queued | `offline.patterns_v2` |  | 37 | 11.1 | 22.2 | 30 | not sampled | 150 | list_aggregation_generic |
| 6 | S40 Communities | queued | `offline.communities_v2` |  | 37 | 11.1 | 22.2 | 30 | not sampled | 170 | list_aggregation_generic |
| 7 | S61 Bundle: read-v2-graph | queued | `profile read-v2-graph` | yes | 30 | 9.0 | 18.0 | 57 | not sampled | 190 | fusion_loss_vector_only, fusion_loss_lexical_only, fusion_loss_two_leg_crowded, rerank_keep_cut, multi_evidence_recall, implicit_or_paraphrased_gold |
| 8 | S06 Answer contract | iterate | `read.query_contract=heuristic` |  | 25 | 7.5 | 15.0 | 47 | 1/-0 (net +1; 0 in target) | 108 | list_incomplete, count_wrong |
| 9 | S11 Contract rules v2 | iterate | `read.contract_rules` |  | 25 | 7.5 | 15.0 | 47 | 1/-1 (net +0; 0 in target) | 128 | list_incomplete, count_wrong |
| 10 | S56 Obligations (bound) | queued | `read.obligations` |  | 25 | 7.5 | 15.0 | 47 | not sampled | 45 | list_incomplete, count_wrong |
| 11 | S14 Family fusion | iterate | `read.family_fusion` |  | 19 | 5.7 | 11.4 | 2 | 4/-1 (net +3; 0 in target) | 20 | fusion_loss_vector_only, fusion_loss_lexical_only, fusion_loss_two_leg_crowded |
| 12 | S51 Family fusion focus | queued | `read.family_fusion (437 q)` |  | 19 | 5.7 | 11.4 | 2 | not sampled | 20 | fusion_loss_vector_only, fusion_loss_lexical_only, fusion_loss_two_leg_crowded |
| 13 | S62 Bundle: write-v2 | queued | `profile write-v2` | yes | 15 | 4.5 | 9.0 | 50 | not sampled | 230 | temporal_evidence_missing, date_offset_small |
| 14 | S55 Deep read (bound) | queued | `read.deep_v2 + sufficiency role` |  | 5 | 1.5 | 3.0 | 68 | not sampled | 45 | multi_evidence_recall |
| 15 | S02 Graph extraction in sleep | iterate | `graph arm (gc3)` |  | 5 | 1.5 | 3.0 | 45 | 5/-1 (net +4; 0 in target) | 150 | multi_evidence_recall |
| 16 | S13 Entity leg (J) | deferred | `read.entity_leg_v2` |  | 5 | 1.5 | 3.0 | 45 | 0/-0 (net +0; 0 in target) | 128 | multi_evidence_recall |
| 17 | S28 Graph key v2 | iterate | `graph.key_v2` |  | 5 | 1.5 | 3.0 | 45 | 4/-5 (net -1; 0 in target) | 150 | multi_evidence_recall |
| 18 | S30 Graph search leg | iterate | `read.graph_leg_v2 + edge compiler` |  | 5 | 1.5 | 3.0 | 45 | 5/-5 (net +0; 0 in target) | 150 | multi_evidence_recall |
| 19 | S32 Entity search leg | iterate | `read.entity_leg_v2` |  | 5 | 1.5 | 3.0 | 45 | 2/-5 (net -3; 0 in target) | 150 | multi_evidence_recall |
| 20 | S39 Edge compiler alone | deferred | `graph.edge_compiler_v2` |  | 5 | 1.5 | 3.0 | 45 | not sampled | 150 | multi_evidence_recall |
| 21 | S01 Session summaries in sleep | iterate | `sleep summary arm (gc9)` |  | 4 | 1.2 | 2.4 | 23 | 3/-1 (net +2; 0 in target) | 45 | implicit_or_paraphrased_gold |
| 22 | S31 Episode search leg | iterate | `read.episode_leg_v2` |  | 4 | 1.2 | 2.4 | 23 | 4/-6 (net -2; 0 in target) | 160 | implicit_or_paraphrased_gold |
| 23 | S33 Session close | queued | `offline.session_close_v2` |  | 4 | 1.2 | 2.4 | 23 | 1/-3 (net -2; 0 in target) | 160 | implicit_or_paraphrased_gold |
| 24 | S16 Reranker v2 | iterate | `read.rerank_v2` |  | 2 | 0.6 | 1.2 | 46 | 0/-0 (net +0; 0 in target) | 63 | rerank_keep_cut |
| 25 | S67 Reranker with wider pool | queued | `read.rerank_v2_search_pool=30` |  | 2 | 0.6 | 1.2 | 46 | not sampled | 63 | rerank_keep_cut |
| 26 | S19 Neighbour turns v2 | iterate | `read.neighbours_v2` |  | 0 | 0.0 | 0.0 | 70 | 0/-0 (net +0; 0 in target) | 20 | (secondary only) |
| 27 | S52 Tight-budget reference | queued | `reply_reserve 3072` |  | 0 | 0.0 | 0.0 | 70 | not sampled | 20 | (secondary only) |
| 28 | S53 Exact budget (bound) | queued | `read.exact_budget` |  | 0 | 0.0 | 0.0 | 70 | not sampled | 20 | (secondary only) |
| 29 | S54 Evidence bundles | queued | `read.evidence_bundles` |  | 0 | 0.0 | 0.0 | 70 | not sampled | 170 | (secondary only) |
| 30 | S43 Window-facts reference | deferred | `reference only` |  | 0 | 0.0 | 0.0 | 56 | not sampled | 150 | (secondary only) |
| 31 | S44 Envelope v2 | deferred | `write.envelope_v2` |  | 0 | 0.0 | 0.0 | 56 | not sampled | 170 | (secondary only) |
| 32 | S45 Segmentation | deferred | `write.segmentation_v2` |  | 0 | 0.0 | 0.0 | 56 | not sampled | 170 | (secondary only) |
| 33 | S48 Stamping | deferred | `write.stamping_v2` |  | 0 | 0.0 | 0.0 | 56 | not sampled | 150 | (secondary only) |
| 34 | S10 Memory tiers in search | deferred | `read.tiers_v2` |  | 0 | 0.0 | 0.0 | 16 | 0/-0 (net +0; 0 in target) | 20 | (secondary only) |
| 35 | S12 Router rules | deferred | `read.router_rules` |  | 0 | 0.0 | 0.0 | 16 | 0/-0 (net +0; 0 in target) | 20 | (secondary only) |
| 36 | S36 Tiers (sleep run) | deferred | `read.tiers_v2` |  | 0 | 0.0 | 0.0 | 16 | not sampled | 150 | (secondary only) |
| 37 | S37 Router (sleep run) | deferred | `read.router_rules` |  | 0 | 0.0 | 0.0 | 16 | not sampled | 150 | (secondary only) |
| 38 | S38 v2 legs alone | queued | `read.legs_v2` |  | 0 | 0.0 | 0.0 | 16 | not sampled | 150 | (secondary only) |
| 39 | S15 Lift before rerank | iterate | `read.lift_before_rerank` |  | 0 | 0.0 | 0.0 | 6 | 0/-0 (net +0; 0 in target) | 20 | (secondary only) |

Open screens that target no wrong LoCoMo class (parity, runtime or other-benchmark screens): S17 (read.exact_budget), S41 (offline.reconcile_v2), S42 (offline.histories_v2), S47 (offline.decay_v2), S49 (write.role_policy_v2), S50 (write.intake_v2), S57 (read.trust_rendering), S65 (profile trust-v2), S66 (profile run-v2).

### 5.2 Other screens (final status or skipped), for reference

| screen | status | upper bound | 30% | 60% | measured (SCREEN_ATLAS) |
|---|---|---:|---:|---:|---|
| S05 Pool protection (reproduction) | pass | 19 | 5.7 | 11.4 | 0/0 |
| S09 Same-commit reference | pass | 19 | 5.7 | 11.4 | — |
| S24 Time view (as-of) | skipped | 14 | 4.2 | 8.4 | not run (no as-of questions) |

### 5.3 Per solution

| solution | status | upper bound | 30% | 60% | screens |
|---|---|---:|---:|---:|---|
| SOL-READERPROMPT Reader prompt variants (world-knowledge clause, inference route, answer-style, caption line) | harness | 92 | 27.6 | 55.2 | no screen |
| SOL-PRESENT Present the retrieved hits first, neighbours separate; per-shape window | off | 49 | 14.7 | 29.4 | no screen |
| SOL-PLANNER LLM planner / query rewrites / HyDE-style answer hypothesis | off | 46 | 13.8 | 27.6 | no screen |
| SOL-AGENTIC Bounded second-pass retrieval for missing evidence (agentic query/slot, second_round, fact chain) | off | 42 | 12.6 | 25.2 | no screen |
| SOL-POOL3 Larger candidate pool with a tiered window | off | 37 | 11.1 | 22.2 | no screen |
| SOL-LISTMODE List mode: speaker-vote leg and wider pool for list-shaped questions | ref | 37 | 11.1 | 22.2 | no screen |
| SOL-STANDING Derived list memory: standing answers, patterns, communities | off | 37 | 11.1 | 22.2 | S34, S35, S40, S64 |
| SOL-GAP-BIGREADER Larger reader (27-32B) or thinking mode for inference and detail failures | GAP | 26 | 7.8 | 15.6 | - |
| SOL-CONTRACT Typed query contract (list / when / count) shapes evidence and answer format | off | 25 | 7.5 | 15.0 | S06, S11, S60 |
| SOL-OBLIG Evidence obligations for joins, durations, lists, counts | off | 25 | 7.5 | 15.0 | S56, S63 |
| SOL-COUNT Enumerate-then-count and evidence table (count-verify, verify-slots) | harness | 25 | 7.5 | 15.0 | no screen |
| SOL-ERRATA Errata file: report accuracy with and without gold/dataset errors | ref | 24 | 7.2 | 14.4 | no screen |
| SOL-POOLPROT Reserve pool slots for the top of every leg (leg-protected pool) | ref | 19 | 5.7 | 11.4 | S05, S09 |
| SOL-FAMFUS Family fusion (dense / lexical / structured / derived families vote separately) | off | 19 | 5.7 | 11.4 | S14, S51, S60, S61 |
| SOL-TEMPLEG Temporal leg on relative / partial dates and event intervals | off | 14 | 4.2 | 8.4 | S24, S62, S63 |
| SOL-DURATION Duration / interval / milestone arithmetic by code | harness | 11 | 3.3 | 6.6 | no screen |
| SOL-DATEGUARD Deterministic judge pre-checks: date guard and judge conventions (I58) | ref | 7 | 2.1 | 4.2 | no screen |
| SOL-JUDGE-I77 Judge conventions beyond list/typo: containment, date-range and relative-year equivalence | GAP | 7 | 2.1 | 4.2 | - |
| SOL-GRAPH Graph extraction in sleep plus one-hop graph / entity legs | off | 5 | 1.5 | 3.0 | S02, S13, S28, S30, S32, S39, S61 |
| SOL-DEEPREAD Deep read: one sufficiency check and at most one targeted re-search | off | 5 | 1.5 | 3.0 | S55, S63 |
| SOL-EPISODE Session summaries / episodes lifted to turns (L2) | off | 4 | 1.2 | 2.4 | S01, S31, S33, S61, S64 |
| SOL-GAP-HYDE Answer-hypothesis (HyDE) leg for implicit / paraphrased gold | GAP | 4 | 1.2 | 2.4 | - |
| SOL-GAP-DISTRACTOR Owner / event check for wrong-line and wrong-speaker answers | GAP | 4 | 1.2 | 2.4 | - |
| SOL-VERIFY Answer verification and one bounded repair (verify-answer, verify-slots diagnosed) | harness | 3 | 0.9 | 1.8 | no screen |
| SOL-RRPOOL Rerank a wider search pool (rerank_v2 + rerank_v2_search_pool) | off | 2 | 0.6 | 1.2 | S16, S67, S60, S61 |
| SOL-RERANKKEEP Raise rerank_keep (10 -> 15) | off | 2 | 0.6 | 1.2 | no screen |
| SOL-LEXICAL Lexical index options (english analyzer, date words in BM25) | off | 2 | 0.6 | 1.2 | no screen |
| SOL-RELANCHOR Resolve relative phrases against the session date (annotation) and repair the gaps | ref | 1 | 0.3 | 0.6 | S62 |
| SOL-PERSPECTIVE Perspective policy (subject-weight) and perspective-leg weight | ref | 1 | 0.3 | 0.6 | no screen |
| SOL-LIFT Lift derived hits to source turns before rerank | off | 0 | 0.0 | 0.0 | S15, S60, S61 |
| SOL-LEGS-V2 v2 leg registry, rule router, depth tiers | off | 0 | 0.0 | 0.0 | S10, S12, S36, S37, S38, S60 |
| SOL-WINDOWFACTS Write-time fact extraction (window_extract, write-v2 stack incl. temporal stage W2-14) | off | 0 | 0.0 | 0.0 | S03, S04, S43, S44, S45, S48, S62 |
| SOL-BUNDLES Neighbour turns v2, exact token budget, complete evidence bundles | off | 0 | 0.0 | 0.0 | S19, S52, S53, S54, S63 |
| SOL-ASSET Resolve image evidence from the original asset | off | 0 | 0.0 | 0.0 | no screen |

### 5.4 Built solutions with no queued screen (testing gap), by upper bound

| solution | status | upper bound | 30% | 60% | primary classes |
|---|---|---:|---:|---:|---|
| SOL-READERPROMPT Reader prompt variants (world-knowledge clause, inference route, answer-style, caption line) | harness | 92 | 27.6 | 55.2 | inference_open_domain, wrong_detail, image_caption_misuse, refusal_premise_denial |
| SOL-PRESENT Present the retrieved hits first, neighbours separate; per-shape window | off | 49 | 14.7 | 29.4 | distractor_line, wrong_detail |
| SOL-PLANNER LLM planner / query rewrites / HyDE-style answer hypothesis | off | 46 | 13.8 | 27.6 | list_aggregation_generic, multi_evidence_recall, vocabulary_mismatch_recall |
| SOL-AGENTIC Bounded second-pass retrieval for missing evidence (agentic query/slot, second_round, fact chain) | off | 42 | 12.6 | 25.2 | multi_evidence_recall, list_aggregation_generic |
| SOL-POOL3 Larger candidate pool with a tiered window | off | 37 | 11.1 | 22.2 | list_aggregation_generic |
| SOL-COUNT Enumerate-then-count and evidence table (count-verify, verify-slots) | harness | 25 | 7.5 | 15.0 | count_wrong, list_incomplete |
| SOL-DURATION Duration / interval / milestone arithmetic by code | harness | 11 | 3.3 | 6.6 | date_duration_arithmetic, date_offset_small |
| SOL-VERIFY Answer verification and one bounded repair (verify-answer, verify-slots diagnosed) | harness | 3 | 0.9 | 1.8 | distractor_line |
| SOL-RERANKKEEP Raise rerank_keep (10 -> 15) | off | 2 | 0.6 | 1.2 | rerank_keep_cut |
| SOL-LEXICAL Lexical index options (english analyzer, date words in BM25) | off | 2 | 0.6 | 1.2 | lexical_outranked_recall |

## 6. Measured flips on the sample screens (cross-check)

Each `smp-S*-loc-*` run is compared question by question with its same-slice reference rerun (`smp-ref-scr-pool-protect-jina-c0a8dd8b-sample-loc-80-s20261013` for the 80-question slice, `smp-ref-scr-pool-protect-jina-c0a8dd8b-sample-loc-60-s20261013-c3` for the 60-question 3-conversation slice used by sleep screens). Both slices were drawn from `clean-jina-full`. Rerun noise: two reruns of the same reference (different commits) on the 80-slice differ on 0 of 80 questions, and the 80-slice reference differs from `clean-jina-full` on 0 of 80, so at an identical configuration the pipeline is deterministic and a flip is caused by the screen's change (it can still be reader churn on a changed context rather than a fix of the targeted class). The class of a flipped question is its class in the full run.

| screen | run | n | ref | screen | +gain | -loss | net | gain in target class | gain in secondary | gain elsewhere | gain was correct in full (noise) | loss on full-correct | wrong-in-full in slice (target) | fixes its class? |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| S01 | S01-loc-1013-0932 | 60 | 47 | 49 | 3 | 1 | +2 | 0 | 1 | 2 | 0 | 1 | 13 (0) | untestable here (no wrong target-class question in the slice), flips are churn elsewhere |
| S02 | S02-loc-1013-0932 | 60 | 47 | 51 | 5 | 1 | +4 | 0 | 0 | 5 | 0 | 1 | 13 (0) | untestable here (no wrong target-class question in the slice), flips are churn elsewhere |
| S03 | S03-loc-1013-0932 | 60 | 47 | 44 | 3 | 6 | -3 | 0 | 1 | 2 | 0 | 6 | 13 (0) | secondary classes only (gain seen) |
| S04 | S04-loc-1013-0932 | 60 | 47 | 41 | 1 | 7 | -6 | 0 | 0 | 1 | 0 | 7 | 13 (0) | secondary classes only |
| S05 | S05-loc-1013-0932 | 80 | 65 | 65 | 0 | 0 | +0 | 0 | 0 | 0 | 0 | 0 | 15 (0) | untestable here (no wrong target-class question in the slice), inert |
| S06 | S06-loc-1013-0932 | 80 | 65 | 66 | 1 | 0 | +1 | 0 | 0 | 1 | 0 | 0 | 15 (1) | no |
| S10 | S10-loc-1013-1228 | 80 | 65 | 65 | 0 | 0 | +0 | 0 | 0 | 0 | 0 | 0 | 15 (0) | secondary classes only |
| S11 | S11-loc-1013-1228 | 80 | 65 | 65 | 1 | 1 | +0 | 0 | 0 | 1 | 0 | 1 | 15 (1) | no |
| S12 | S12-loc-1013-1228 | 80 | 65 | 65 | 0 | 0 | +0 | 0 | 0 | 0 | 0 | 0 | 15 (0) | secondary classes only |
| S13 | S13-loc-1013-1228 | 80 | 65 | 65 | 0 | 0 | +0 | 0 | 0 | 0 | 0 | 0 | 15 (0) | untestable here (no wrong target-class question in the slice), inert |
| S14 | S14-loc-1013-1228 | 80 | 65 | 68 | 4 | 1 | +3 | 0 | 0 | 4 | 0 | 1 | 15 (0) | untestable here (no wrong target-class question in the slice), flips are churn elsewhere |
| S15 | S15-loc-1013-1228 | 80 | 65 | 65 | 0 | 0 | +0 | 0 | 0 | 0 | 0 | 0 | 15 (0) | secondary classes only |
| S16 | S16-loc-1013-1929 | 80 | 65 | 65 | 0 | 0 | +0 | 0 | 0 | 0 | 0 | 0 | 15 (0) | untestable here (no wrong target-class question in the slice), inert |
| S17 | S17-loc-1013-1929 | 80 | 65 | 65 | 0 | 0 | +0 | 0 | 0 | 0 | 0 | 0 | 15 (0) | no LoCoMo class targeted |
| S19 | S19-loc-1013-2006 | 80 | 65 | 65 | 0 | 0 | +0 | 0 | 0 | 0 | 0 | 0 | 15 (0) | secondary classes only |
| S20 | S20-loc-1013-2006 | 80 | 65 | 65 | 0 | 0 | +0 | 0 | 0 | 0 | 0 | 0 | 15 (0) | no LoCoMo class targeted |
| S25 | S25-loc-1013-2006 | 80 | 65 | 65 | 0 | 0 | +0 | 0 | 0 | 0 | 0 | 0 | 15 (0) | no LoCoMo class targeted |
| S26 | S26-loc-1013-2006 | 80 | 65 | 65 | 0 | 0 | +0 | 0 | 0 | 0 | 0 | 0 | 15 (0) | no LoCoMo class targeted |
| S28 | S28-loc-1013-2006 | 60 | 47 | 46 | 4 | 5 | -1 | 0 | 0 | 4 | 0 | 5 | 13 (0) | untestable here (no wrong target-class question in the slice), flips are churn elsewhere |
| S29 | S29-loc-1013-2006 | 80 | 65 | 65 | 0 | 0 | +0 | 0 | 0 | 0 | 0 | 0 | 15 (0) | no LoCoMo class targeted |
| S30 | S30-loc-1013-2006 | 60 | 47 | 47 | 5 | 5 | +0 | 0 | 0 | 5 | 0 | 5 | 13 (0) | untestable here (no wrong target-class question in the slice), flips are churn elsewhere |
| S31 | S31-loc-1013-2006 | 60 | 47 | 45 | 4 | 6 | -2 | 0 | 1 | 3 | 0 | 6 | 13 (0) | untestable here (no wrong target-class question in the slice), flips are churn elsewhere |
| S32 | S32-loc-1013-2006 | 60 | 47 | 44 | 2 | 5 | -3 | 0 | 0 | 2 | 0 | 5 | 13 (0) | untestable here (no wrong target-class question in the slice), flips are churn elsewhere |
| S33 | S33-loc-1013-2006 | 20 | 15 | 13 | 1 | 3 | -2 | 0 | 0 | 1 | 0 | 3 | 5 (0) | untestable here (no wrong target-class question in the slice), flips are churn elsewhere |

Screens whose declared reference is the S02 graph arm (S28, S30-S33): S28 vs S02 0/-5 (net -5); S30 vs S02 2/-6 (net -4); S31 vs S02 1/-7 (net -6); S32 vs S02 1/-8 (net -7); S33 vs S02 0/-2 (net -2).

Flipped questions by class (gains in the run's own target classes are what a screen is for):

| screen | gained question ids (class) | lost question ids (class) |
|---|---|---|
| S01 | 42/3-5 (list_incomplete), 48/7-13 (wrong_detail), 48/7-54 (temporal_evidence_missing) | 50/9-40 (ok) |
| S02 | 42/3-5 (list_incomplete), 48/7-13 (wrong_detail), 48/7-54 (temporal_evidence_missing), 48/7-101 (relative_date_gold_vs_judge), 50/9-27 (temporal_evidence_missing) | 42/3-31 (ok) |
| S03 | 42/3-5 (list_incomplete), 48/7-13 (wrong_detail), 48/7-54 (temporal_evidence_missing) | 42/3-72 (ok), 42/3-83 (ok), 48/7-80 (ok), 50/9-19 (ok), 50/9-38 (ok), 50/9-42 (ok) |
| S04 | 48/7-13 (wrong_detail) | 42/3-31 (ok), 42/3-72 (ok), 42/3-150 (ok), 50/9-12 (ok), 50/9-19 (ok), 50/9-40 (ok), 50/9-42 (ok) |
| S06 | 48/7-13 (wrong_detail) | - |
| S11 | 48/7-13 (wrong_detail) | 41/2-50 (ok) |
| S14 | 30/1-56 (list_aggregation_generic), 43/4-169 (wrong_detail), 48/7-13 (wrong_detail), 50/9-67 (wrong_detail) | 41/2-50 (ok) |
| S28 | 42/3-5 (list_incomplete), 48/7-13 (wrong_detail), 48/7-54 (temporal_evidence_missing), 48/7-101 (relative_date_gold_vs_judge) | 42/3-31 (ok), 42/3-75 (ok), 42/3-83 (ok), 50/9-12 (ok), 50/9-42 (ok) |
| S30 | 42/3-5 (list_incomplete), 42/3-85 (inference_open_domain), 48/7-13 (wrong_detail), 48/7-54 (temporal_evidence_missing), 48/7-101 (relative_date_gold_vs_judge) | 42/3-72 (ok), 42/3-75 (ok), 42/3-83 (ok), 50/9-12 (ok), 50/9-19 (ok) |
| S31 | 42/3-5 (list_incomplete), 42/3-85 (inference_open_domain), 48/7-13 (wrong_detail), 48/7-54 (temporal_evidence_missing) | 42/3-31 (ok), 42/3-72 (ok), 42/3-75 (ok), 50/9-11 (ok), 50/9-19 (ok), 50/9-42 (ok) |
| S32 | 42/3-14 (open_domain_inference_retrieval), 48/7-54 (temporal_evidence_missing) | 42/3-31 (ok), 42/3-72 (ok), 42/3-75 (ok), 42/3-83 (ok), 50/9-11 (ok) |
| S33 | 42/3-5 (list_incomplete) | 42/3-31 (ok), 42/3-75 (ok), 42/3-83 (ok) |

## 7. Biggest gaps: no solution, no built solution, or no screen

| class | wrong q | gap | what exists |
|---|---:|---|---|
| `wrong_detail` | 46 | no screen for built solution: built primary solutions exist but none has a v2 screen id | SOL-READERPROMPT (harness, primary); SOL-VERIFY (harness, secondary); SOL-PRESENT (off, primary); SOL-BUNDLES (off, secondary); SOL-GAP-BIGREADER (GAP, secondary) |
| `open_domain_inference_retrieval` | 28 | no built primary solution: only proposed (not built) solutions are aimed at this class | SOL-PLANNER (off, secondary); SOL-AGENTIC (off, secondary); SOL-DEEPREAD (off, secondary) |
| `inference_open_domain` | 26 | no screen for built solution: built primary solutions exist but none has a v2 screen id | SOL-DEEPREAD (off, secondary); SOL-READERPROMPT (harness, primary); SOL-GAP-BIGREADER (GAP, primary) |
| `refusal_premise_denial` | 14 | no screen for built solution: built primary solutions exist but none has a v2 screen id | SOL-DEEPREAD (off, secondary); SOL-READERPROMPT (harness, primary); SOL-VERIFY (harness, secondary); SOL-GAP-BIGREADER (GAP, secondary) |
| `date_duration_arithmetic` | 10 | no screen for built solution: built primary solutions exist but none has a v2 screen id | SOL-CONTRACT (off, secondary); SOL-OBLIG (off, secondary); SOL-DURATION (harness, primary) |
| `date_wrong_event` | 8 | no built primary solution: only proposed (not built) solutions are aimed at this class | SOL-RELANCHOR (ref, secondary); SOL-DURATION (harness, secondary); SOL-VERIFY (harness, secondary) |
| `image_caption_misuse` | 6 | no screen for built solution: built primary solutions exist but none has a v2 screen id | SOL-READERPROMPT (harness, primary); SOL-ASSET (off, secondary) |
| `vocabulary_mismatch_recall` | 4 | no screen for built solution: built primary solutions exist but none has a v2 screen id | SOL-GRAPH (off, secondary); SOL-EPISODE (off, secondary); SOL-PLANNER (off, primary); SOL-GAP-HYDE (GAP, secondary) |
| `distractor_line` | 3 | no screen for built solution: built primary solutions exist but none has a v2 screen id | SOL-VERIFY (harness, primary); SOL-PRESENT (off, primary); SOL-BUNDLES (off, secondary); SOL-GAP-BIGREADER (GAP, secondary); SOL-GAP-DISTRACTOR (GAP, primary) |
| `lexical_outranked_recall` | 2 | no screen for built solution: built primary solutions exist but none has a v2 screen id | SOL-FAMFUS (off, secondary); SOL-RRPOOL (off, secondary); SOL-LEGS-V2 (off, secondary); SOL-LEXICAL (off, primary) |

Classes with zero wrong questions in this run: `write_path_loss`.

## 8. Method and limits

- Stage codes come from the logged ranks per gold turn (`forensics_report.py`): `b` no leg ranked a gold turn, `cf` found in a leg then lost at the fused pool, `cr` lost at `rerank_keep`; a question's stage is its weakest link. A reader-stage question has every gold turn in the context.
- Rule order in `classify_row` (first match): errata, judge conventions / date guard / answer contains gold, write path, then retrieval classes by category and question shape, then reader classes by `failure_buckets` bucket and wording. Heuristic classes (H) read wording, not only ranks.
- `judge_false_negative` and `relative_date_gold_vs_judge` are candidates found by deterministic checks (conventions v1, `date_equivalent`, gold-words-in-answer); none was re-judged.
- Class sizes depend on one run (one seed, 9B reader) and on the heuristics; capture rates and the 30/60% columns are assumptions. A solution that is already on in the reference (status ref) cannot recover its own class again; the residual it leaves is the class count shown.
- Overlap: a question counts once in its class but can sit in the bound of several screens; screen bounds are therefore not additive.


## 9. R14 additions (2026-10-14)

Generic problems clustered from the R14 round (`docs/v2/research/R14_MASTER.md`). `R14-confirmed` rows extend an existing class or gap; `R14` rows are new. The solution map (`evals/analysis/locomo_solution_map.json`) carries the same items under the key `r14_additions` without altering the generated entries.

| Master id | Tag | Layer | Problem | Existing class / gap | Targeted q | Slice | Proposed screen |
|---|---|---|---|---|---|---|---|
| R14-M-3 | R14-confirmed | retrieval | Fusion and rerank cut loss: ranked gold discarded when legs are fused into a small pool | B7, B9, B10, I75, I78, R02 | 68 | fail_fusion (+95 lucky/barely cases as guard) | offline CPU re-fusion from forensics first (no run); S82 protect8 + convex fusion; S83 budget keep; S67 queued |
| R14-M-7 | R14-confirmed | retrieval | Explicit-date questions: the date is treated as ordinary words, no session-date prior at fusion | B4, I8 | 13 | fail_temporal (13 fail + matched controls from the 180) | S87 date_prior (CPU re-fusion first); S24/S62/S63 queued |
| R14-M-1 | R14-confirmed | retrieval | Multi-evidence recall: fixed depth, no coverage objective, weak aggregation trigger | B1, I4, I28, I56, R01 | 55 | fail_list (+ 60 matched n_gold>=3 correct as guard) | S79 trigger + coverage (CPU first, offline over forensics); S80 second-retrieval bounded |
| R14-M-17 | R14-confirmed | judge | Judge false negatives after the date guard: superset lists, annotation equivalence, containment | A2, I58, I77 | 23 | 23 FN ids + 34 FP ids frozen as a 57-row calibration extension; judge_calibration_dev.jsonl | S96 judge conventions v3 (re-score stored answers, no retrieval) |
| R14-M-18 | R14-confirmed | judge | Judge false positives and the honest headline: partial lists credited, 81.4 is not 81.4 | A1, A3, A10, A13 | 34 | 34 FP + 23 FN ids as a frozen calibration extension; whole-run re-score | S97 triple-column re-score of stored answers (reference run plus last two full runs) |
| R14-M-8 | R14-confirmed | reader | Date and duration arithmetic done by the 9B reader; event dates lack precision and status | C5, I57, I76, I79, E05 | 19 | fail_temporal date_* classes (19) + 38 unit questions | S88 duration_solve + milestones (built-off, needs a screen only) |
| R14-M-9 | R14-confirmed | reader | Over-refusal and premise denial: no typed commit route for judgement questions | C3, C4, C10, I1, I61 | 40 | fail_reader (40 + 25 refusal rows); guards cat5 and OP-Bench irrelevance | S73 prompt, S74 probes (OBS-GAPS); S89 vote; S20 cat5 guard |
| R14-M-10 | R14-confirmed | reader | Wrong detail and distractor use; one window size for every question shape | C1, C2, C9, I6, R2-4 | 59 | fail_reader (46 + 59 ids); 2x2 narrow/wide window by shape on the dev slice | S90 quote-then-answer; S91 window_by_shape; S92 verify_binding; S52-S54 queued |
| R14-M-11 | R14-confirmed | reader | List and count completeness: partial lists, over-counted events, 'at least N' | C6, I56 | 25 | fail_list (25 + list/count-shaped controls) | S93 evidence_table / two_call; S06, S11, S56 queued |
| R14-M-14 | R14-confirmed | reader | Verbosity against a short gold: answer length follows difficulty, the lever is an output contract | READER_GAPS_FORENSIC 0.4, SOL-CONTRACT | 49 | ratio>8 ids from fail_reader | S06, S11 (queued); add ratio>8 and list-recall columns |
| R14-M-19 | R14-confirmed | eval | Gold errata, sibling evidence, id hygiene and retrieval metrics that overstate success | A5, A12, I24, I62, A16 | 21 | the 10 + 8 + 3 + 59 ids; guard: correct questions with context-dependent gold | S98 support-recall report (CPU, no run); unit tests for the parser and errata |
| R14-M-2 | R14-confirmed | retrieval | Cross-session spread: turn-level, session-agnostic retrieval; summaries and episodes as legs | B8, SOL-EPISODE, SOL-GRAPH | 43 | fail_retrieval (b+cf) and fail_mix_120; metric distinct gold sessions represented | S81 session_cover; S31/S33/S61 stay as queued |
| R14-M-5 | R14-confirmed | ingest | Elliptical and context-dependent turns: decontextualisation and an index view | A07, I6, B12 | 32 | fail_retrieval + fail_context (OBS-GAPS) | S85 exchange_records (CPU simulation first); S69/S70/S75 per OBS-GAPS |
| R14-M-12 | R14 | reader | Multi-turn join and multi-entity decomposition at read | new class | 54 | fail_list + new fail_multi (43, OBS-GAPS) | S94 reader-side join (after S77); S56 queued |
| R14-M-13 | R14-confirmed | reader | Ungrounded answers: new content words appear nowhere in the context | SOL-VERIFY, defect_diagnosis | 37 | fail_reader (37 ids); false-defect rate on the 188 correct | S95 slot_verify diagnosed |
| R14-M-4 | R14-confirmed | retrieval | Lexical-zero and paraphrase gold: surface form is the strongest predictor of a miss | B2, B5, B6, B15 | 81 | fail_retrieval (81 ids, 21 paraphrase) | S84 doc expansion; S76 paraphrase probes (OBS-GAPS); word_vector_leg on the 21 ids |
| R14-M-6 | R14-confirmed | retrieval | Indirect gold for judgement questions: evidence describes behaviour, never the question's words | B3, C3, C4 | 28 | fail_retrieval (28) + all 96 cat3; oracle-gold reader-only run first to size the ceiling | S86 evidence_kind_probes; shares S74 probes (OBS-06) |
| R14-M-20 | R14-confirmed | ingest | Profile and attribute state with history; knowledge update; destructive edits | G-W4, S34 S42 S46 | 33 | fail_mix_120 attribute subset (33) + MAB-CR dev for the history half | S99 known_keys (after S34/S42); S71 per OBS-GAPS |
| R14-M-21 | R14-confirmed | ingest | Entity aliasing and link tables: seed-to-neighbour expansion without per-session LLM graph builds | I54 (kin resolution), SOL-GRAPH | 9 | fail_retrieval + fail_mix_120; CPU prototype over stored embeddings of one conversation first | S100 link_table; S68 per OBS-GAPS |
| R14-M-15 | R14-confirmed | reader | Image captions: misattributed or photo-only evidence | C7, E04 | 6 | fail_reader (6) + 28 caption-only | S72 (OBS-GAPS) |
| R14-M-16 | R14 | eval | Speaker attribution: wrong-speaker answers are rare, but swapped-speaker golds penalise correct attribution | new class | 39 | perspective screens with the 23-row stratum split out | no new screen; apply to any perspective screen |
