#!/usr/bin/env bash
# Cross-benchmark baseline of the best config (neutral refusal retry, I1): LoCoMo dev cat 1-5 + OP-Bench dev.
# Launch through pinned_run.sh (H7). Checks first; full slices only if the checks pass.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/xb.txt; ARM=BEST_dev_2026-10-10
J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
echo "start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
bash run.sh --arm $ARM --run-id xb-loc-t2 --mode qa --topk 10 --items 1 --max-queries 2 --categories 1,2,3,4,5 --forensics --batch-turns 32 --flags "$J" --force || { echo "xb-loc CHECK FAILED" >> $L; exit 1; }
bash run.sh --arm $ARM --run-id xb-opb-t8 --mode qa --topk 10 --dataset op_bench --data data/opbench_src --items 1 --questions 8 --batch-turns 32 --flags "--opbench-per-task 2" --force || { echo "xb-opb CHECK FAILED" >> $L; exit 1; }
echo "checks ok $(date +%T)" >> $L
bash run.sh --arm $ARM --run-id xb-loc-dev --mode qa --topk 10 --items 4 --categories 1,2,3,4,5 --questions 757 --forensics --batch-turns 32 --flags "$J" --force && echo "xb-loc-dev done $(date +%T)" >> $L || echo "xb-loc-dev FAILED" >> $L
bash run.sh --arm $ARM --run-id xb-opb-dev --mode qa --topk 10 --dataset op_bench --data data/opbench_src --questions 331 --batch-turns 32 --flags "--item-ids conv-26:Caroline,conv-30:Jon,conv-41:John,conv-42:Joanna" --force && echo "xb-opb-dev done $(date +%T)" >> $L || echo "xb-opb-dev FAILED" >> $L
echo XB DONE >> $L
