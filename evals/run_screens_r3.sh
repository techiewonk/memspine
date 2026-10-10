#!/usr/bin/env bash
# Round 3 screens (one change each vs BEST) on LoCoMo conv-26/30 cat 1-5 (304 q) + OP-Bench dev (331 probes).
# Reference = the cross-benchmark baseline (xb-loc-dev on the same questions, xb-opb-dev). Via pinned_run.sh.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/r3.txt; J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
OPB="--dataset op_bench --data data/opbench_src --questions 331"
OPBF="--item-ids conv-26:Caroline,conv-30:Jon,conv-41:John,conv-42:Joanna"
echo "start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
screen() {  # id arm locomo-flags opbench-extra-flags
  local id=$1 arm=$2 lf=$3 of=$4
  bash run.sh --arm $arm --run-id $id-t2 --mode qa --topk 10 --items 1 --max-queries 2 --categories 1,2,3,4,5 --forensics --batch-turns 32 --flags "$lf" --force || { echo "$id CHECK FAILED" >> $L; return; }
  bash run.sh --arm $arm --run-id $id-loc --mode qa --topk 10 --items 2 --categories 1,2,3,4,5 --questions 304 --forensics --batch-turns 32 --flags "$lf" --force && echo "$id-loc done $(date +%T)" >> $L || echo "$id-loc FAILED" >> $L
  bash run.sh --arm $arm --run-id $id-opb --mode qa --topk 10 $OPB --batch-turns 32 --flags "$OPBF $of" --force && echo "$id-opb done $(date +%T)" >> $L || echo "$id-opb FAILED" >> $L
}
screen r3-relgate  scr-relgate         "$J" ""
screen r3-generic  best-generic-prompt "--qa-prompt grounded_generic --retry-refusal --judge-guards --judge-date-check" ""
screen r3-intent   scr-intent          "$J" ""
screen r3-dedupe   scr-dedupe          "$J" ""
screen r3-latest   scr-latest          "$J" ""
screen r3-norecord BEST_dev_2026-10-10 "$J --no-record-hint" "--no-record-hint"
echo R3 DONE >> $L
