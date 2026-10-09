#!/usr/bin/env bash
# Config-only fixes on the fixed reranker (B4 temporal, B5/B6 BM25 analyzer), after grounded_v3.
cd "$(dirname "$0")" || exit 1
while ! grep -q "FIX4 DONE" runs/_logs/fix2_screen.txt 2>/dev/null; do sleep 60; done
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
for v in ldates trel english; do
  bash run.sh --arm qs-eq06-rq4b4-fix-$v --run-id f5-$v-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$J" --force || echo "f5-$v CHECK FAILED" >> runs/_logs/fix2_screen.txt
  bash run.sh --arm qs-eq06-rq4b4-fix-$v --run-id f5-$v-i2 --mode qa --topk 10 --items 2 --questions 233 --forensics --batch-turns 32 --flags "$J" --force && echo "f5-$v done" >> runs/_logs/fix2_screen.txt || echo "f5-$v FAILED" >> runs/_logs/fix2_screen.txt
done
echo FIX5 DONE >> runs/_logs/fix2_screen.txt
