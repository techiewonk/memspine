#!/usr/bin/env bash
# Reader fixes C1/C2/C5 on the fixed-reranker config: 2-question check, then 2 dev conversations (paired).
cd "$(dirname "$0")" || exit 1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
BASE="--qa-prompt grounded --retry-refusal --judge-guards"
C12="--qa-prompt grounded_detail --retry-refusal --judge-guards --memspine-mark-hits star"
run() { # id arm flags
  bash run.sh --arm "$2" --run-id "$1-t2" --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$3" --force || echo "CHECK FAILED $1" >> runs/_logs/reader_screen.txt
  bash run.sh --arm "$2" --run-id "$1-i2" --mode qa --topk 10 --items 2 --questions 233 --forensics --batch-turns 32 --flags "$3" --force && echo "$1 done" >> runs/_logs/reader_screen.txt || echo "$1 FAILED" >> runs/_logs/reader_screen.txt
}
run rs-r0-ref   qs-eq06-rq4b4-fix     "$BASE"
run rs-r1-c12   qs-eq06-rq4b4-fix     "$C12"
run rs-r2-c5    qs-eq06-rq4b4-fix-dur "$BASE"
run rs-r3-all   qs-eq06-rq4b4-fix-dur "$C12"
echo READER SCREEN DONE >> runs/_logs/reader_screen.txt
