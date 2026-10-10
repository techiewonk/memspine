#!/usr/bin/env bash
# Round 6: agentic multi-step read (I67). BEST_dev_2026-10-10 + read.agentic (trigger multi_hop, max 2 steps; the
# action role is the local Qwen via ollama, structured retry on). NOT LAUNCHED by the author: queue it behind r5.
# Same slices as r5 (LoCoMo 304 q, OP-Bench dev, 4 personas); reference = r3c-ref (equivalent defaults).
# Via pinned_run.sh. Cost: +1 LLM call on a fired question that answers ready, +2 on one that searches twice (see I67).
# Read the result per category (multi-hop is the target, cat 5 and latency are the guards) and the per-question
# agentic_steps in runs/<id>--forensics/forensics.jsonl (action, query, why, new_ids, llm_s).
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/r3.txt; J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
OPB="--dataset op_bench --data data/opbench_src --questions 331"
OPBF="--item-ids conv-26:Caroline,conv-30:Jon,conv-41:John,conv-42:Joanna"
echo "r6 start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
screen() {
  local id=$1 arm=$2 lf=$3 of=$4 skipopb=$5
  bash run.sh --arm $arm --run-id $id-t2 --mode qa --topk 10 --items 1 --max-queries 2 --categories 1,2,3,4,5 --forensics --batch-turns 32 --flags "$lf" --force || { echo "$id CHECK FAILED" >> $L; return; }
  bash run.sh --arm $arm --run-id $id-loc --mode qa --topk 10 --items 2 --categories 1,2,3,4,5 --questions 304 --forensics --batch-turns 32 --flags "$lf" --force && echo "$id-loc done $(date +%T)" >> $L || echo "$id-loc FAILED" >> $L
  [ -n "$skipopb" ] && return
  bash run.sh --arm $arm --run-id $id-opb --mode qa --topk 10 $OPB --batch-turns 32 --flags "$OPBF $of" --force && echo "$id-opb done $(date +%T)" >> $L || echo "$id-opb FAILED" >> $L
}
screen r6-agentic scr-agentic "$J" ""
echo R6 DONE >> $L
