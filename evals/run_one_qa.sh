#!/usr/bin/env bash
# One full-LoCoMo QA run (categories 1-4) on a given arm: run_one_qa.sh <arm>
cd "$(dirname "$0")" || exit 1
arm=$1
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN MEMSPINE_EVAL_THINK
s=$(date +%s)
../.venv/Scripts/python.exe _launch.py c0-1 --dataset locomo --path ../data/locomo10.json --categories 1,2,3,4 \
  --mode qa --with-memspine --only-systems memspine --memspine-read-mode replay \
  --memspine-config "$(cat arms/$arm.json)" --memspine-batch-turns 32 \
  --reader-model qwen3.5:9b --judge-model qwen3.5:9b --base-url http://localhost:11434/v1 \
  --max-model-calls 4000 --run-id qa-full-$arm 2>&1 | cat > runs/_logs/qa-full-$arm.log
echo "qa-full-$arm secs=$(( $(date +%s)-s ))" >> runs/_logs/qa_single_times.txt
