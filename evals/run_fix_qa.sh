#!/usr/bin/env bash
# Full LoCoMo QA with the gap fixes, run from this worktree's code: run_fix_qa.sh <arm> [run-suffix]
# Env: MEMSPINE_EVAL_MAX_QUERIES / ITEMS for small tests; MEMSPINE_FORENSICS_DIR for the stage log.
cd "$(dirname "$0")" || exit 1
arm=$1; sfx=${2:-}
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN MEMSPINE_EVAL_THINK
export PYTHONPATH="$(cd .. && pwd)/src"
FLAGS=${FLAGS-"--qa-prompt grounded --retry-refusal --judge-guards"}  # FLAGS="" = harness defaults
s=$(date +%s)
../../memspine/.venv/Scripts/python.exe _launch.py c0-1 --dataset locomo --path ../../memspine/data/locomo10.json \
  --categories 1,2,3,4 --mode qa ${ITEMS:+--items $ITEMS} --with-memspine --only-systems memspine --memspine-read-mode replay \
  --memspine-config "$(cat arms/$arm.json)" --memspine-batch-turns 32 --top-k "${TOPK:-20}" \
  $FLAGS \
  --reader-model qwen3.5:9b --judge-model qwen3.5:9b --base-url http://127.0.0.1:11434/v1 \
  --max-model-calls 6000 --run-id qa-full-$arm$sfx 2>&1 | cat > runs/_logs/qa-full-$arm$sfx.log
echo "qa-full-$arm$sfx rc=${PIPESTATUS[0]} secs=$(( $(date +%s)-s ))" >> runs/_logs/qa_fix_times.txt
