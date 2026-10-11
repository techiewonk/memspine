#!/usr/bin/env bash
# FAST LoCoMo-only focus screens with jina-reranker-v3.5 (user decision 2026-10-11). First the Jina REFERENCE on the
# focus slice (f-jref = scr-pool-protect-jina), then one change per arm; score: python eval_focus.py <id>-i2 --ref f-jref-i2
# (pairs by question id). Then the clean re-baseline with Jina on all 1,540.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/fast.txt; J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
echo "fast-jina start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
screen() {
  local id=$1 arm=$2 lf=$3
  bash run.sh --arm $arm --run-id $id-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$lf" --force || { echo "$id CHECK FAILED" >> $L; return; }
  bash run.sh --arm $arm --run-id $id-i2 --mode qa --topk 10 --query-ids @analysis/focus_slice_r7protect.json --questions 437 --forensics --batch-turns 32 --flags "$lf" --force && echo "$id-i2 done $(date +%T)" >> $L || echo "$id-i2 FAILED" >> $L
}
screen f-jref       scr-pool-protect-jina "$J"
screen f-family     scr-pool-family-jina  "$J"
screen f-milestone  scr-pool-protect-jina "$J --milestones nearest"
screen f-table      scr-pool-protect-jina "$J --count-verify two_call --verify-slots --evidence-table"
screen f-slot       scr-agentic-slot-jina "$J"
screen f-chain      scr-factchain-jina    "$J"
screen f-contract   scr-contract-jina     "$J"
echo FAST DONE >> $L
bash run.sh --arm scr-pool-protect-jina --run-id clean-jina-full --mode qa --topk 10 --forensics --batch-turns 32 --flags "$J" --force && echo "clean-jina-full done $(date +%T)" >> $L || echo "clean-jina-full FAILED" >> $L
echo CLEAN DONE >> $L
