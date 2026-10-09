#!/usr/bin/env bash
# After the fix2 screen: rerank_balanced alone again, now that the GR-15 no-op is fixed.
cd "$(dirname "$0")" || exit 1
while ! grep -q "FIX2 DONE" runs/_logs/fix2_screen.txt 2>/dev/null; do sleep 60; done
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
J="--retry-refusal --judge-guards --judge-date-check"
bash run.sh --arm qs-eq06-rq4b4-fix-bal --run-id f3-bal-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "--qa-prompt grounded $J" --force
bash run.sh --arm qs-eq06-rq4b4-fix-bal --run-id f3-bal-i2 --mode qa --topk 10 --items 2 --questions 233 --forensics --batch-turns 32 --flags "--qa-prompt grounded $J" --force && echo "f3-bal done" >> runs/_logs/fix2_screen.txt || echo "f3-bal FAILED" >> runs/_logs/fix2_screen.txt
echo FIX3 DONE >> runs/_logs/fix2_screen.txt
