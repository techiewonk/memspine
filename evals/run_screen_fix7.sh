#!/usr/bin/env bash
# B1 list mode alone and with the fixed rerank_balanced, after the B5/B8 screens.
cd "$(dirname "$0")" || exit 1
while ! grep -q "FIX6 DONE" runs/_logs/fix2_screen.txt 2>/dev/null; do sleep 60; done
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
for v in list bal-list; do
  bash run.sh --arm qs-eq06-rq4b4-fix-$v --run-id f7-$v-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$J" --force || echo "f7-$v CHECK FAILED" >> runs/_logs/fix2_screen.txt
  bash run.sh --arm qs-eq06-rq4b4-fix-$v --run-id f7-$v-i2 --mode qa --topk 10 --items 2 --questions 233 --forensics --batch-turns 32 --flags "$J" --force && echo "f7-$v done" >> runs/_logs/fix2_screen.txt || echo "f7-$v FAILED" >> runs/_logs/fix2_screen.txt
done
echo FIX7 DONE >> runs/_logs/fix2_screen.txt
