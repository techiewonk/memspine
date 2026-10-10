#!/usr/bin/env bash
# B15 follow-up: word vectors IN PLACE OF BM25 on today's best config, 2-question check then the 2-conversation screen
# (conv-26/30, cat 1-5, 304 q). Reference = xb-loc-dev restricted to the same questions (same commit). Via pinned_run.sh.
cd "$(dirname "$0")" || exit 1
while ! grep -q "XB DONE" runs/_logs/xb.txt 2>/dev/null; do sleep 60; done
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/xb.txt; J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
bash run.sh --arm best-wv-nobm25 --run-id wvr-t2 --mode qa --topk 10 --items 1 --max-queries 2 --categories 1,2,3,4,5 --forensics --batch-turns 32 --flags "$J" --force || { echo "wvr CHECK FAILED" >> $L; exit 1; }
bash run.sh --arm best-wv-nobm25 --run-id wvr-i2 --mode qa --topk 10 --items 2 --categories 1,2,3,4,5 --questions 304 --forensics --batch-turns 32 --flags "$J" --force && echo "wvr-i2 done $(date +%T)" >> $L || echo "wvr-i2 FAILED" >> $L
echo WVR DONE >> $L
