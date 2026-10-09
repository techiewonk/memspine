# Pre-registration: held-out LoCoMo run, 2026-10-10

Written before any held-out result is seen (gap A6 / EVAL-4).

- Data: LoCoMo categories 1-4, held-out conversations from `analysis/locomo_split.json`: conv-43, conv-44, conv-47,
  conv-48, conv-49, conv-50 (956 questions). Development conversations (conv-26/30/41/42) were used for all tuning.
- Primary configuration (the headline): R0 = arm `qs-eq06-rq4b4-fix` (Qwen3-Embedding-0.6B + BM25, fixed Qwen3-Reranker-4B
  4-bit: pool 20, keep 10, rerank_floor skip; window 2/4; relative_dates_anchored; temporal_leg_mentions) with
  `--qa-prompt grounded --retry-refusal --judge-guards`, top_k 10, batch_turns 32, sampler defaults (presence_penalty 0).
- Secondary configuration: R3 = arm `qs-eq06-rq4b4-fix-dur` (+ resolve_durations) with
  `--qa-prompt grounded_detail --memspine-mark-hits star --retry-refusal --judge-guards`.
- Reader and judge: Qwen3.5-9B Q4_K_M via Ollama (8192 window), thinking off. Same judge for both, so the paired
  comparison R3 vs R0 is judge-controlled; the absolute numbers are NOT comparable with published LoCoMo scores (gap A4).
- Reported: accuracy with conversation-cluster bootstrap CI, per category, paired gains/losses R3 vs R0, and accuracy
  excluding the errata in `analysis/locomo_errata.json` (held-out entries only).
- Decision rule: adopt R3 as the default reader set only if paired net >= +10 questions (about +1 point) on held-out;
  otherwise keep R0. No further tuning on held-out conversations after this run.
