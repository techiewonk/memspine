#!/usr/bin/env bash
# Round 3b: perspective layer screens (P1 subject_weight, P3 + modality/polarity/scope axes). Same slices as r3. Via pinned_run.sh.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/r3.txt; J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
OPB="--dataset op_bench --data data/opbench_src --questions 331"
OPBF="--item-ids conv-26:Caroline,conv-30:Jon,conv-41:John,conv-42:Joanna"
echo "r3b start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
for pair in "r3-persp:scr-persp-w" "r3-paxes:scr-persp-axes"; do
  id=${pair%%:*}; arm=${pair#*:}
  bash run.sh --arm $arm --run-id $id-t2 --mode qa --topk 10 --items 1 --max-queries 2 --categories 1,2,3,4,5 --forensics --batch-turns 32 --flags "$J" --force || { echo "$id CHECK FAILED" >> $L; continue; }
  bash run.sh --arm $arm --run-id $id-loc --mode qa --topk 10 --items 2 --categories 1,2,3,4,5 --questions 304 --forensics --batch-turns 32 --flags "$J" --force && echo "$id-loc done $(date +%T)" >> $L || echo "$id-loc FAILED" >> $L
  bash run.sh --arm $arm --run-id $id-opb --mode qa --topk 10 $OPB --batch-turns 32 --flags "$OPBF" --force && echo "$id-opb done $(date +%T)" >> $L || echo "$id-opb FAILED" >> $L
done
echo R3B DONE >> $L
