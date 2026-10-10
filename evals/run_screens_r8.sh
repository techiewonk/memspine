#!/usr/bin/env bash
# Round 8: (1) BLIND validation of the reference (scr-persp-w) and the candidate (scr-pool-protect) on MAB-CR + ConvoMem;
# (2) screens on ALL LoCoMo convs cat 1-4 (1,540; ref full-persp-loc / r7-protect-full) + OP-Bench dev (ref r3-persp-opb):
#     source-family pool (R02), milestones post-step (E05), query contract header (A03), relevance gate clean on top of protect (P01/I74).
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/r8.txt; J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
OPB="--dataset op_bench --data data/opbench_src --questions 331"
OPBF="--item-ids conv-26:Caroline,conv-30:Jon,conv-41:John,conv-42:Joanna"
echo "r8 start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
for arm in scr-persp-w scr-pool-protect; do
  bash run_blind_validation.sh $arm --topk 10 --force && echo "blind $arm done $(date +%T)" >> $L || echo "blind $arm FAILED" >> $L
done
screen() {
  local id=$1 arm=$2 lf=$3
  bash run.sh --arm $arm --run-id $id-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$lf" --force || { echo "$id CHECK FAILED" >> $L; return; }
  bash run.sh --arm $arm --run-id $id-full --mode qa --topk 10 --forensics --batch-turns 32 --flags "$lf" --force && echo "$id-full done $(date +%T)" >> $L || echo "$id-full FAILED" >> $L
  bash run.sh --arm $arm --run-id $id-opb --mode qa --topk 10 $OPB --batch-turns 32 --flags "$OPBF" --force && echo "$id-opb done $(date +%T)" >> $L || echo "$id-opb FAILED" >> $L
}
screen r8-relprot   scr-protect-relgate "$J"
screen r8-family    scr-pool-family     "$J"
screen r8-milestone scr-pool-protect    "$J --milestones nearest"
screen r8-contract  scr-contract        "$J"
echo R8 DONE >> $L
