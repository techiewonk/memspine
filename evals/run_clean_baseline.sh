#!/usr/bin/env bash
# Clean re-baseline after the I80 prompt-contamination fix (8cebf97): current best candidate (scr-pool-protect) on ALL
# LoCoMo convs cat 1-4 (1,540 q) with the CLEAN default grounded prompt. New absolute reference; earlier absolute numbers
# are contaminated (relative comparisons stand). Via pinned_run.sh.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/clean.txt; J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
echo "clean start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
bash run.sh --arm scr-pool-protect --run-id clean-protect-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$J" --force || { echo "clean CHECK FAILED" >> $L; exit 1; }
bash run.sh --arm scr-pool-protect --run-id clean-protect-full --mode qa --topk 10 --forensics --batch-turns 32 --flags "$J" --force && echo "clean-protect-full done $(date +%T)" >> $L || echo "clean-protect-full FAILED" >> $L
echo CLEAN DONE >> $L
