#!/usr/bin/env bash
# FAST screens, LoCoMo ONLY (user 2026-10-11: focus on LoCoMo first, OP-Bench parked).
# Focus slice: 287 FAIL + 150 CONTROL (all 10 convs, cat 1-4). Score with: python eval_focus.py <id>-i2
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/fast.txt; J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
echo "fast-loc start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
screen() {
  local id=$1 arm=$2 lf=$3
  bash run.sh --arm $arm --run-id $id-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$lf" --force || { echo "$id CHECK FAILED" >> $L; return; }
  bash run.sh --arm $arm --run-id $id-i2 --mode qa --topk 10 --query-ids @analysis/focus_slice_r7protect.json --questions 437 --forensics --batch-turns 32 --flags "$lf" --force && echo "$id-i2 done $(date +%T)" >> $L || echo "$id-i2 FAILED" >> $L
}
screen f-family    scr-pool-family     "$J"
screen f-milestone scr-pool-protect    "$J --milestones nearest"
screen f-table     scr-pool-protect    "$J --count-verify two_call --verify-slots --evidence-table"
screen f-slot      scr-agentic-slot    "$J"
screen f-chain     scr-factchain       "$J"
screen f-contract  scr-contract        "$J"
echo FAST DONE >> $L
