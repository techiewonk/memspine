#!/usr/bin/env bash
# Stage 2b, prioritised: (1) Qwen3.5-9B think off vs on QA, (2) Jina reranker/embedder retrieval arms,
# (3) the bge-base arms. Starts once both 4B-reranker grid arms are finished.
cd "$(dirname "$0")" || exit 1
while [ "$(grep -c 'rq4b4' runs/_logs/grid_times.txt 2>/dev/null)" -lt 2 ]; do sleep 30; done
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
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
unset MEMSPINE_EVAL_THINK
ollama stop qwen3.5:9b >/dev/null 2>&1
echo QA DONE >> runs/_logs/stage2_times.txt
export BT=32
( for a in qs-ejina-rjina qs-ebge-rjina qs-ejina-rq06 qs-ebgeb-roff; do bash run_qwen_stack.sh $a >> runs/_logs/stage2_times.txt; done ) &
( for a in qs-ejina-roff qs-eq06-rjina qs-ejina-rq4b4 qs-ebgeb-rjina; do bash run_qwen_stack.sh $a >> runs/_logs/stage2_times.txt; done ) &
wait
echo RETRIEVAL DONE >> runs/_logs/stage2_times.txt
echo STAGE2 DONE >> runs/_logs/stage2_times.txt
