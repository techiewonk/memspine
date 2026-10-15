#!/usr/bin/env bash
# Held-out run (pre-registered in prereg/2026-10-10_heldout_reader_fixes.md): R0 primary, R3 secondary.
cd "$(dirname "$0")" || exit 1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
H="--item-ids conv-43,conv-44,conv-47,conv-48,conv-49,conv-50"
bash run.sh --arm qs-eq06-rq4b4-fix --run-id ho-r0 --mode qa --topk 10 --questions 956 --forensics --batch-turns 32 \
  --flags "--qa-prompt grounded --retry-refusal --judge-guards $H" && echo "ho-r0 done" >> runs/_logs/heldout.txt || echo "ho-r0 FAILED" >> runs/_logs/heldout.txt
bash run.sh --arm qs-eq06-rq4b4-fix-dur --run-id ho-r3 --mode qa --topk 10 --questions 956 --forensics --batch-turns 32 \
  --flags "--qa-prompt grounded_detail --memspine-mark-hits star --retry-refusal --judge-guards $H" && echo "ho-r3 done" >> runs/_logs/heldout.txt || echo "ho-r3 FAILED" >> runs/_logs/heldout.txt
echo HELDOUT DONE >> runs/_logs/heldout.txt
