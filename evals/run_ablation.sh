#!/usr/bin/env bash
# One-conversation ablation (conv-26, 152 questions, CPU embeddings) separating reader fixes from retrieval fixes.
cd "$(dirname "$0")" || exit 1
export ITEMS=1
TOPK=10 bash run_fix_qa.sh _test-roff-cpu -abl-reader                          # A1 reader fixes only
TOPK=20 FLAGS="" bash run_fix_qa.sh _test-fix-cpu -abl-retrieval               # A2 retrieval fixes only
TOPK=20 bash run_fix_qa.sh _test-fix-cpu -abl-both                             # A3 both
TOPK=10 FLAGS="" bash run_fix_qa.sh _test-fix-cpu -abl-window-only             # A4 window 2/4 + dates, top_k 10
echo ABLATION DONE >> runs/_logs/qa_fix_times.txt
