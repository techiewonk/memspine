#!/usr/bin/env bash
# A16 rule: confirm candidates on the other two dev conversations (conv-41, conv-42) before any held-out run.
cd "$(dirname "$0")" || exit 1
while ! grep -q "FIX7 DONE" runs/_logs/fix2_screen.txt 2>/dev/null; do sleep 60; done
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check --item-ids conv-41,conv-42"
for pair in "cd-ref:qs-eq06-rq4b4-fix" "cd-list:qs-eq06-rq4b4-fix-list" "cd-bal-list:qs-eq06-rq4b4-fix-bal-list"; do
  id=${pair%%:*}; arm=${pair#*:}
  bash run.sh --arm $arm --run-id $id --mode qa --topk 10 --questions 351 --forensics --batch-turns 32 --flags "$J" --force && echo "$id done" >> runs/_logs/confirm_dev.txt || echo "$id FAILED" >> runs/_logs/confirm_dev.txt
done
echo CONFIRM DONE >> runs/_logs/confirm_dev.txt
