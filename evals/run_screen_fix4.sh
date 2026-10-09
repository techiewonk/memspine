#!/usr/bin/env bash
# grounded_v3 on the fixed reranker, after the balanced re-screen.
cd "$(dirname "$0")" || exit 1
while ! grep -q "FIX3 DONE" runs/_logs/fix2_screen.txt 2>/dev/null; do sleep 60; done
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
J="--retry-refusal --judge-guards --judge-date-check"
bash run.sh --arm qs-eq06-rq4b4-fix --run-id f4-v3-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "--qa-prompt grounded_v3 $J" --force
bash run.sh --arm qs-eq06-rq4b4-fix --run-id f4-v3-i2 --mode qa --topk 10 --items 2 --questions 233 --forensics --batch-turns 32 --flags "--qa-prompt grounded_v3 $J" --force && echo "f4-v3 done" >> runs/_logs/fix2_screen.txt || echo "f4-v3 FAILED" >> runs/_logs/fix2_screen.txt
echo FIX4 DONE >> runs/_logs/fix2_screen.txt
