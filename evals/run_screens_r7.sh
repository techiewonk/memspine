#!/usr/bin/env bash
# Round 7 (overnight): one change each on top of the perspective config (scr-persp-w), scored on ALL LoCoMo convs
# cat 1-4 (1,540 q; reference = full-persp-loc 80.4%) + OP-Bench dev (331; reference xb-opb-dev / r3-persp-opb). Via pinned_run.sh.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/r7.txt; J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
OPB="--dataset op_bench --data data/opbench_src --questions 331"
OPBF="--item-ids conv-26:Caroline,conv-30:Jon,conv-41:John,conv-42:Joanna"
echo "r7 start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
screen() {
  local id=$1 arm=$2 lf=$3
  bash run.sh --arm $arm --run-id $id-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$lf" --force || { echo "$id CHECK FAILED" >> $L; return; }
  bash run.sh --arm $arm --run-id $id-full --mode qa --topk 10 --forensics --batch-turns 32 --flags "$lf" --force && echo "$id-full done $(date +%T)" >> $L || echo "$id-full FAILED" >> $L
  bash run.sh --arm $arm --run-id $id-opb --mode qa --topk 10 $OPB --batch-turns 32 --flags "$OPBF" --force && echo "$id-opb done $(date +%T)" >> $L || echo "$id-opb FAILED" >> $L
}
screen r7-protect   scr-pool-protect          "$J"
screen r7-multprot  scr-persp-mult-protect    "$J"
screen r7-post      scr-persp-w               "$J --count-verify single --date-repair --duration-solve rewrite"
screen r7-owner     scr-persp-owner           "$J"
echo R7 DONE >> $L
