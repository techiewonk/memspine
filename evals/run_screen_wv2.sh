#!/usr/bin/env bash
# BM25 + word vectors both on: 2-question check then 152-question screen per variant, after the full reranker run.
cd "$(dirname "$0")" || exit 1
while ! grep -q "^qa-full-qs-eq06-rq4b4-fix rc" runs/_logs/qa_fix_times.txt; do sleep 60; done
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True ITEMS=1 TOPK=10
for arm in qs-eq06-fix-wv-w05 qs-eq06-rq4b4-fix-wv qs-eq06-rq4b4-fix-wv-shape; do
  rm -rf runs/qa-full-$arm-t2* runs/qa-full-$arm-s152*
  MEMSPINE_EVAL_MAX_QUERIES=2 MEMSPINE_FORENSICS_DIR=runs/qa-full-$arm-t2--forensics bash run_fix_qa.sh $arm -t2
  MEMSPINE_FORENSICS_DIR=runs/qa-full-$arm-s152--forensics bash run_fix_qa.sh $arm -s152
done
echo SCREEN2 DONE >> runs/_logs/qa_fix_times.txt
