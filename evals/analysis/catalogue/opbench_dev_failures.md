# OP-Bench dev failure catalogue: aggregate counts only (xb-opb-dev, 288 of 331 probes below 0.5)

OP-Bench has no licence (OPBENCH_PROTOCOL.md): the per-probe catalogue is written to the gitignored `evals/runs/_analysis/opbench_dev_failures_catalogue.jsonl` and is NOT committed. Counts below come from cheap automated signals (retrieved turns, persona share, distinctive-token leak, reference cues, question/context overlap, affirmation regex); a 30-probe manual check is in `FAILURE_FORENSICS_BASELINE_2026-10-10.md`.

| type | probes below 0.5 | class | n |
|---|---:|---|---:|
| diversity | 167 | low score without a detected cause | 4 |
| diversity | 167 | persona-theme repetition (stored facts re-used in an unrelated reply) | 148 |
| diversity | 167 | template repetition (shared opener and phrasing, little persona content) | 15 |
| irrelevance_easy/fully_irrelevant | 52 | memory injected for an unrelated request | 52 |
| irrelevance_hard/behavioral_attribution | 5 | subject confusion (behaviour attributed to the persona) | 5 |
| irrelevance_hard/contextual_misalignment | 4 | near-match memory treated as relevant (context bait) | 4 |
| irrelevance_hard/interest_confusion | 6 | near-match memory treated as relevant (interest bait) | 6 |
| irrelevance_hard/subject_confusion | 5 | subject confusion (persona facts given to another subject) | 5 |
| sycophancy/coarse-grained | 18 | confirmation with no supporting memory | 11 |
| sycophancy/coarse-grained | 18 | low score without affirmation | 2 |
| sycophancy/coarse-grained | 18 | near-match memory treated as confirmation | 4 |
| sycophancy/coarse-grained | 18 | user premise contested with other stored memories (over-personalised correction) | 1 |
| sycophancy/fact | 4 | low score without a detected cause | 2 |
| sycophancy/fact | 4 | memory cited in a fact/opinion reply (over-personalised pushback or agreement) | 2 |
| sycophancy/fine-grained | 20 | confirmation with no supporting memory | 17 |
| sycophancy/fine-grained | 20 | near-match memory treated as confirmation | 3 |
| sycophancy/value | 7 | low score without a detected cause | 1 |
| sycophancy/value | 7 | memory cited in a fact/opinion reply (over-personalised pushback or agreement) | 6 |
