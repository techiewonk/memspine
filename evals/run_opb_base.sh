#!/usr/bin/env bash
# I34: OP-Bench dev with NO memory (same reader + judge) = our own BASE for the paper's drop-vs-BASE metric. Via pinned_run.sh.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/xb.txt
bash run.sh --arm BEST_dev_2026-10-10 --system no-memory --run-id opb-base-t8 --mode qa --topk 10 --dataset op_bench --data data/opbench_src --items 1 --questions 8 --flags "--opbench-per-task 2" --force || { echo "opb-base CHECK FAILED" >> $L; exit 1; }
bash run.sh --arm BEST_dev_2026-10-10 --system no-memory --run-id opb-base-dev --mode qa --topk 10 --dataset op_bench --data data/opbench_src --questions 331 --flags "--item-ids conv-26:Caroline,conv-30:Jon,conv-41:John,conv-42:Joanna" --force && echo "opb-base-dev done $(date +%T)" >> $L || echo "opb-base-dev FAILED" >> $L
echo OPBBASE DONE >> $L
