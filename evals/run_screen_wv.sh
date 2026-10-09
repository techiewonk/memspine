#!/usr/bin/env bash
# 152-question screen (conv-26, GPU): fixed reference, word-vector variants, fixed Qwen reranker.
cd "$(dirname "$0")" || exit 1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True ITEMS=1 TOPK=10
for arm in qs-eq06-fix qs-eq06-fix-wv-nobm25 qs-eq06-fix-wv-plusbm25 qs-eq06-rq4b4-fix; do
  rm -rf runs/qa-full-$arm-s152*
  MEMSPINE_FORENSICS_DIR=runs/qa-full-$arm-s152--forensics bash run_fix_qa.sh $arm -s152
done
echo SCREEN DONE >> runs/_logs/qa_fix_times.txt
