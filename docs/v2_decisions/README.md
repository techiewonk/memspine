# MemSpine v2 decision set (2026-10-11)

155 architecture decisions for MemSpine v2, built from the 15-engine survey (`docs/engine_survey/`), the gap plan
(41 gaps), the independent review (RV01-RV22) and the Codex reviews. Six areas: write path (W-01..W-26), memory
layers and data model (M-01..M-26), offline memory (O-01..O-26), retrieval and composition (R-01..R-26), models,
prompts, cost and speed (P-01..P-25), evaluation, safety, API and operations (G-01..G-26).

Each question carries today's behaviour (file:line), how named engines do it, 3-5 options with impact on quality,
latency, cost, simplicity and safety, a recommendation with evidence, dependencies, gap ids, and the paper mapping
(memory type, loop operator). `owner_note` marks standing owner decisions (Jina v3.5 reranker; no LongMemEval).

- Answer page (saves each decision): https://claude.ai/artifact/2KXuPiBf2EgU1Ww8nx5bjj
- `questions.json`: the set as published; `BRIEF.md`: the brief the area agents worked from.
- Answers are exported here as `answers.json` once decided; the plan of record then moves into
  `docs/GAP_TO_SOLUTION_PLAN.md` and ADRs.

## Change log
- 2026-10-11: set created (155 questions, six areas); owner notes on R-14, P-18 (reranker) and G-02 (benchmarks);
  one prompt example in the "today" text scrubbed (it was a benchmark gold answer, see register 21:30).
