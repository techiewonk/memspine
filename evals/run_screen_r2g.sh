#!/usr/bin/env bash
# R2-1b gated bridge hop, after the R2 engine screen.
cd "$(dirname "$0")" || exit 1
while ! grep -q "R2E DONE" runs/_logs/r2.txt 2>/dev/null; do sleep 60; done
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
bash run.sh --arm best-bridge-gated --run-id r2-gated-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$J" --force || echo "r2-gated CHECK FAILED" >> runs/_logs/r2.txt
bash run.sh --arm best-bridge-gated --run-id r2-gated-i2 --mode qa --topk 10 --items 2 --questions 233 --forensics --batch-turns 32 --flags "$J" --force && echo "r2-gated done" >> runs/_logs/r2.txt || echo "r2-gated FAILED" >> runs/_logs/r2.txt
echo R2G DONE >> runs/_logs/r2.txt
