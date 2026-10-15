#!/usr/bin/env bash
# Stage 2 (after the first grid): Jina arms with batched writes, then Qwen3.5-9B QA think off vs on.
cd "$(dirname "$0")" || exit 1
while ! grep -q "GRID DONE" runs/_logs/grid_times.txt 2>/dev/null; do sleep 60; done
export BT=32
( for a in qs-ejina-roff qs-ejina-rq06 qs-ejina-rq4b4; do bash run_qwen_stack.sh $a >> runs/_logs/stage2_times.txt; done ) &
( for a in qs-ejina-rjina qs-eq06-rjina qs-ebge-rjina; do bash run_qwen_stack.sh $a >> runs/_logs/stage2_times.txt; done ) &
wait
echo RETRIEVAL DONE >> runs/_logs/stage2_times.txt
unset BT AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
for think in off on; do
  s=$(date +%s)
  if [ $think = on ]; then export MEMSPINE_EVAL_THINK=on; else unset MEMSPINE_EVAL_THINK; fi
  ../.venv/Scripts/python.exe _launch.py c0-1 --dataset locomo --path ../data/locomo10.json --categories 1,2,3,4 \
    --mode qa --items 1 --with-memspine --only-systems memspine --memspine-read-mode replay \
    --memspine-config "$(cat arms/qs-eq06-roff.json)" --memspine-batch-turns 32 \
    --reader-model qwen3.5:9b --judge-model qwen3.5:9b --base-url http://127.0.0.1:11434/v1 \
    --max-model-calls 1200 --run-id qa-q35-think-$think 2>&1 | cat > runs/_logs/qa-q35-think-$think.log
  echo "qa-q35-think-$think secs=$(( $(date +%s)-s ))" >> runs/_logs/stage2_times.txt
done
echo STAGE2 DONE >> runs/_logs/stage2_times.txt
