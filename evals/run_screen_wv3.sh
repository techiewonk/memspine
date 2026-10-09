#!/usr/bin/env bash
# BM25 + word vectors both on, 4 conversations (paired against two references on the same items).
cd "$(dirname "$0")" || exit 1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True TOPK=10
for arm in qs-eq06-fix-wv-w05 qs-eq06-rq4b4-fix-wv qs-eq06-rq4b4-fix-wv-shape; do
  rm -rf runs/qa-full-$arm-t2*
  ITEMS=1 MEMSPINE_EVAL_MAX_QUERIES=2 MEMSPINE_FORENSICS_DIR=runs/qa-full-$arm-t2--forensics bash run_fix_qa.sh $arm -t2
done
echo CHECK2 DONE >> runs/_logs/qa_fix_times.txt
for arm in qs-eq06-fix qs-eq06-rq4b4-fix qs-eq06-fix-wv-w05 qs-eq06-rq4b4-fix-wv qs-eq06-rq4b4-fix-wv-shape; do
  rm -rf runs/qa-full-$arm-i4*
  ITEMS=4 MEMSPINE_FORENSICS_DIR=runs/qa-full-$arm-i4--forensics bash run_fix_qa.sh $arm -i4
done
echo SCREEN3 DONE >> runs/_logs/qa_fix_times.txt
