#!/usr/bin/env bash
# R2-1b out-of-sample check: conv-41/42 (351 q), fresh reference on today's code then the gated bridge hop.
cd "$(dirname "$0")" || exit 1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check --item-ids conv-41,conv-42"
for pair in "cd-best:BEST_dev_2026-10-10" "cd-gated:best-bridge-gated"; do
  id=${pair%%:*}; arm=${pair#*:}
  bash run.sh --arm $arm --run-id $id --mode qa --topk 10 --questions 351 --forensics --batch-turns 32 --flags "$J" --force && echo "$id done" >> runs/_logs/r2.txt || echo "$id FAILED" >> runs/_logs/r2.txt
done
echo R2C DONE >> runs/_logs/r2.txt
