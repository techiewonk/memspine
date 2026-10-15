#!/usr/bin/env bash
# Round 2 engine fixes on the best config (fresh reference first), after the R2-4 screen.
cd "$(dirname "$0")" || exit 1
while ! grep -q "R2H DONE" runs/_logs/r2.txt 2>/dev/null; do sleep 60; done
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
for pair in "r2-best:BEST_dev_2026-10-10" "r2-wide:best-wide" "r2-bridge:best-bridge" "r2-wide-bridge:best-wide-bridge"; do
  id=${pair%%:*}; arm=${pair#*:}
  bash run.sh --arm $arm --run-id $id-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$J" --force || echo "$id CHECK FAILED" >> runs/_logs/r2.txt
  bash run.sh --arm $arm --run-id $id-i2 --mode qa --topk 10 --items 2 --questions 233 --forensics --batch-turns 32 --flags "$J" --force && echo "$id done" >> runs/_logs/r2.txt || echo "$id FAILED" >> runs/_logs/r2.txt
done
echo R2E DONE >> runs/_logs/r2.txt
