#!/usr/bin/env bash
# B5 name stripping and B8 session leg on the fixed reranker, after the config screens.
cd "$(dirname "$0")" || exit 1
while ! grep -q "FIX5 DONE" runs/_logs/fix2_screen.txt 2>/dev/null; do sleep 60; done
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
for v in strip session; do
  bash run.sh --arm qs-eq06-rq4b4-fix-$v --run-id f6-$v-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$J" --force || echo "f6-$v CHECK FAILED" >> runs/_logs/fix2_screen.txt
  bash run.sh --arm qs-eq06-rq4b4-fix-$v --run-id f6-$v-i2 --mode qa --topk 10 --items 2 --questions 233 --forensics --batch-turns 32 --flags "$J" --force && echo "f6-$v done" >> runs/_logs/fix2_screen.txt || echo "f6-$v FAILED" >> runs/_logs/fix2_screen.txt
done
echo FIX6 DONE >> runs/_logs/fix2_screen.txt
