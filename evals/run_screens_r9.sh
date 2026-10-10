#!/usr/bin/env bash
# Round 9: wave-2 families on top of the protected pool (scr-pool-protect), ALL LoCoMo convs cat 1-4 (ref r7-protect-full)
# + OP-Bench dev (ref r7-protect-opb): evidence table (A04/A06, with I56+E06), slot-driven loop (E02), fact chain (E01),
# assertion check (P03, OP-Bench target; LoCoMo = no-harm check).
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/r9.txt; J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
OPB="--dataset op_bench --data data/opbench_src --questions 331"
OPBF="--item-ids conv-26:Caroline,conv-30:Jon,conv-41:John,conv-42:Joanna"
echo "r9 start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
screen() {
  local id=$1 arm=$2 lf=$3 of=$4
  bash run.sh --arm $arm --run-id $id-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$lf" --force || { echo "$id CHECK FAILED" >> $L; return; }
  bash run.sh --arm $arm --run-id $id-full --mode qa --topk 10 --forensics --batch-turns 32 --flags "$lf" --force && echo "$id-full done $(date +%T)" >> $L || echo "$id-full FAILED" >> $L
  bash run.sh --arm $arm --run-id $id-opb --mode qa --topk 10 $OPB --batch-turns 32 --flags "$OPBF $of" --force && echo "$id-opb done $(date +%T)" >> $L || echo "$id-opb FAILED" >> $L
}
screen r9-table  scr-pool-protect "$J --count-verify two_call --verify-slots --evidence-table" ""
screen r9-slot   scr-agentic-slot "$J" ""
screen r9-chain  scr-factchain    "$J" ""
screen r9-assert scr-pool-protect "$J --assertion-check rules" "--assertion-check rules"
echo R9 DONE >> $L
