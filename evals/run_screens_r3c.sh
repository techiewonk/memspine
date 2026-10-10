#!/usr/bin/env bash
# Round 3c on the final merged commit: fresh reference (I22 changed is_refusal default), routed_generic prompt,
# sensitivity gate, store-calibrated relevance gate (+ no-memory prompt). LoCoMo conv-26/30 cat 1-5 (304 q) + OP-Bench dev.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/r3.txt; J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
OPB="--dataset op_bench --data data/opbench_src --questions 331"
OPBF="--item-ids conv-26:Caroline,conv-30:Jon,conv-41:John,conv-42:Joanna"
echo "r3c start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
screen() {
  local id=$1 arm=$2 lf=$3 of=$4
  bash run.sh --arm $arm --run-id $id-t2 --mode qa --topk 10 --items 1 --max-queries 2 --categories 1,2,3,4,5 --forensics --batch-turns 32 --flags "$lf" --force || { echo "$id CHECK FAILED" >> $L; return; }
  bash run.sh --arm $arm --run-id $id-loc --mode qa --topk 10 --items 2 --categories 1,2,3,4,5 --questions 304 --forensics --batch-turns 32 --flags "$lf" --force && echo "$id-loc done $(date +%T)" >> $L || echo "$id-loc FAILED" >> $L
  bash run.sh --arm $arm --run-id $id-opb --mode qa --topk 10 $OPB --batch-turns 32 --flags "$OPBF $of" --force && echo "$id-opb done $(date +%T)" >> $L || echo "$id-opb FAILED" >> $L
}
screen r3c-ref     BEST_dev_2026-10-10 "$J" ""
screen r3c-routed  best-generic-prompt "--qa-prompt routed_generic --retry-refusal --judge-guards --judge-date-check" ""
screen r3c-sens    scr-sens            "$J" ""
screen r3c-storecal scr-storecal       "$J --no-memory-prompt" "--no-memory-prompt"
echo R3C DONE >> $L
