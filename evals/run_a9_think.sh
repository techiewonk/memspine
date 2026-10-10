#!/usr/bin/env bash
# A9: thinking off vs on, best dev config, conv-26 (152 q), Ollama window 16384 (the old 4096 window truncated think-on).
cd "$(dirname "$0")" || exit 1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check --server-ctx 16384"
unset MEMSPINE_EVAL_THINK
bash run.sh --arm BEST_dev_2026-10-10 --run-id a9-off --mode qa --topk 10 --items 1 --forensics --batch-turns 32 --flags "$J" --force && echo "a9-off done" >> runs/_logs/a9.txt || echo "a9-off FAILED" >> runs/_logs/a9.txt
MEMSPINE_EVAL_THINK=on bash run.sh --arm BEST_dev_2026-10-10 --run-id a9-on --mode qa --topk 10 --items 1 --forensics --batch-turns 32 --flags "$J" --force && echo "a9-on done" >> runs/_logs/a9.txt || echo "a9-on FAILED" >> runs/_logs/a9.txt
echo A9 DONE >> runs/_logs/a9.txt
