#!/usr/bin/env bash
# Round 5: forensics levers. Owner/entity check (I59/I60), user header + re-injection penalty (I63/I64),
# count-verify + date-repair post-steps (I56/I57). Judge conventions (I58) are scored offline (rescore_conventions.py).
# Same slices; reference = r3c-ref (equivalent defaults). Via pinned_run.sh.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/r3.txt; J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
OPB="--dataset op_bench --data data/opbench_src --questions 331"
OPBF="--item-ids conv-26:Caroline,conv-30:Jon,conv-41:John,conv-42:Joanna"
echo "r5 start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
screen() {
  local id=$1 arm=$2 lf=$3 of=$4 skipopb=$5
  bash run.sh --arm $arm --run-id $id-t2 --mode qa --topk 10 --items 1 --max-queries 2 --categories 1,2,3,4,5 --forensics --batch-turns 32 --flags "$lf" --force || { echo "$id CHECK FAILED" >> $L; return; }
  bash run.sh --arm $arm --run-id $id-loc --mode qa --topk 10 --items 2 --categories 1,2,3,4,5 --questions 304 --forensics --batch-turns 32 --flags "$lf" --force && echo "$id-loc done $(date +%T)" >> $L || echo "$id-loc FAILED" >> $L
  [ -n "$skipopb" ] && return
  bash run.sh --arm $arm --run-id $id-opb --mode qa --topk 10 $OPB --batch-turns 32 --flags "$OPBF $of" --force && echo "$id-opb done $(date +%T)" >> $L || echo "$id-opb FAILED" >> $L
}
screen r5-owner   scr-owner   "$J" ""
screen r5-userhdr scr-userhdr "$J" ""
screen r5-post    BEST_dev_2026-10-10 "$J --count-verify single --date-repair" "" skip
echo R5 DONE >> $L
