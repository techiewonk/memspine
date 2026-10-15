#!/usr/bin/env bash
# FAST screens (user 2026-10-11): FOCUS SLICE on all 10 conversations = the 287 questions r7-protect-full gets WRONG + a seeded
# control of 150 it gets right (437 q, analysis/focus_slice_r7protect.json), + OP-Bench focus slice (331) only where the change can
# affect OP-Bench. Score: python eval_focus.py <id>-i2 ; python eval_focus.py <id>-opb  (fixed/broken/est. net/verdict).
# Winners are promoted to the full 1,540 + blind validation (MAB-CR + ConvoMem) afterwards.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/fast.txt; J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
OPB="--dataset op_bench --data data/opbench_src --query-ids @analysis/focus_slice_opb.json --questions 331"
OPBF="--item-ids conv-26:Caroline,conv-30:Jon,conv-41:John,conv-42:Joanna"
echo "fast start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
screen() {  # id arm locomo-flags opb(yes|no) opb-extra-flags
  local id=$1 arm=$2 lf=$3 opb=$4 of=$5
  bash run.sh --arm $arm --run-id $id-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$lf" --force || { echo "$id CHECK FAILED" >> $L; return; }
  bash run.sh --arm $arm --run-id $id-i2 --mode qa --topk 10 --query-ids @analysis/focus_slice_r7protect.json --questions 437 --forensics --batch-turns 32 --flags "$lf" --force && echo "$id-i2 done $(date +%T)" >> $L || echo "$id-i2 FAILED" >> $L
  [ "$opb" = yes ] || return
  bash run.sh --arm $arm --run-id $id-opb --mode qa --topk 10 $OPB --batch-turns 32 --flags "$OPBF $of" --force && echo "$id-opb done $(date +%T)" >> $L || echo "$id-opb FAILED" >> $L
}
screen f-owner     scr-persp-owner     "$J" yes ""
screen f-relprot   scr-protect-relgate "$J" yes ""
screen f-family    scr-pool-family     "$J" no  ""
screen f-milestone scr-pool-protect    "$J --milestones nearest" no ""
screen f-contract  scr-contract        "$J" yes ""
screen f-table     scr-pool-protect    "$J --count-verify two_call --verify-slots --evidence-table" no ""
screen f-slot      scr-agentic-slot    "$J" yes ""
screen f-chain     scr-factchain       "$J" yes ""
screen f-assert    scr-pool-protect    "$J --assertion-check rules" yes "--assertion-check rules"
echo FAST DONE >> $L
