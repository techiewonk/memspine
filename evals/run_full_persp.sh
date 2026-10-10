#!/usr/bin/env bash
# FULL logged run of the perspective config (BEST_dev_2026-10-10 + perspective subject_weight = arms/scr-persp-w.json):
# LoCoMo ALL 10 conversations, categories 1-4 (1,540 q) + OP-Bench ALL 10 first-speaker personas (859 probes),
# with --trace-full (every write and read step). Held-out conversations used by user decision (2026-10-10).
# Launch via pinned_run.sh. Checks first; full runs only if they pass.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/full.txt; ARM=scr-persp-w
J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check --trace-full"
echo "start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
bash run.sh --arm $ARM --run-id full-persp-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$J" --force || { echo "LOC CHECK FAILED" >> $L; exit 1; }
bash run.sh --arm $ARM --run-id full-persp-opb-t8 --mode qa --topk 10 --dataset op_bench --data data/opbench_src --items 1 --questions 8 --batch-turns 32 --flags "--opbench-per-task 2 --trace-full" --force || { echo "OPB CHECK FAILED" >> $L; exit 1; }
echo "checks ok $(date +%T)" >> $L
bash run.sh --arm $ARM --run-id full-persp-loc --mode qa --topk 10 --forensics --batch-turns 32 --flags "$J" --force && echo "full-persp-loc done $(date +%T)" >> $L || echo "full-persp-loc FAILED" >> $L
bash run.sh --arm $ARM --run-id full-persp-opb --mode qa --topk 10 --dataset op_bench --data data/opbench_src --questions 859 --batch-turns 32 --flags "--trace-full" --force && echo "full-persp-opb done $(date +%T)" >> $L || echo "full-persp-opb FAILED" >> $L
echo FULL DONE >> $L
