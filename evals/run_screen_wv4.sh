#!/usr/bin/env bash
# BM25 + word vectors both on: 2 conversations, paired against two references on the same items.
cd "$(dirname "$0")" || exit 1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True TOPK=10 ITEMS=2
for arm in qs-eq06-fix qs-eq06-rq4b4-fix qs-eq06-fix-wv-w05 qs-eq06-rq4b4-fix-wv qs-eq06-rq4b4-fix-wv-shape; do
  rm -rf runs/qa-full-$arm-i2*
  MEMSPINE_FORENSICS_DIR=runs/qa-full-$arm-i2--forensics bash run_fix_qa.sh $arm -i2
done
echo SCREEN4 DONE >> runs/_logs/qa_fix_times.txt
