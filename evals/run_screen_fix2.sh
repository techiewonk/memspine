#!/usr/bin/env bash
# Targeted fixes from the dev reasoning (2026-10-10): balanced rerank pool, grounded_v2 prompt.
# 2-question check then 2 dev conversations; all with --judge-date-check (R0 rescored offline).
cd "$(dirname "$0")" || exit 1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
J="--retry-refusal --judge-guards --judge-date-check"
run() {
  bash run.sh --arm "$2" --run-id "$1-t2" --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$3 $J" --force || echo "CHECK FAILED $1" >> runs/_logs/fix2_screen.txt
  bash run.sh --arm "$2" --run-id "$1-i2" --mode qa --topk 10 --items 2 --questions 233 --forensics --batch-turns 32 --flags "$3 $J" --force && echo "$1 done" >> runs/_logs/fix2_screen.txt || echo "$1 FAILED" >> runs/_logs/fix2_screen.txt
}
run f2-bal  qs-eq06-rq4b4-fix-bal "--qa-prompt grounded"
run f2-v2   qs-eq06-rq4b4-fix     "--qa-prompt grounded_v2"
run f2-both qs-eq06-rq4b4-fix-bal "--qa-prompt grounded_v2"
echo FIX2 DONE >> runs/_logs/fix2_screen.txt
