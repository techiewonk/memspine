#!/usr/bin/env bash
# Local Qwen stack: retrieval-only LoCoMo screens (zero model-API calls, $0).
#   run_qwen_stack.sh <arm> [items]      e.g. run_qwen_stack.sh qs-eq06-rq4b4 3
# Writes runs/qs-<arm>[-n<items>]/ and runs/_logs/ ; prints wall-clock seconds.
cd "$(dirname "$0")" || exit 1
arm=$1; items=${2:-}
id="${arm}${items:+-n$items}${BT:+-bt$BT}"
mkdir -p runs/_logs
start=$(date +%s)
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
../.venv/Scripts/python.exe ../evals/_launch.py c0-1 --dataset locomo --path ../data/locomo10.json \
  --categories all --with-memspine --only-systems memspine --memspine-read-mode replay \
  --memspine-config "$(cat arms/$arm.json)" --retrieval-only --max-model-calls 0 \
  --budget 4096 --top-k 10 ${items:+--items $items} ${BT:+--memspine-batch-turns $BT} --run-id "$id" 2>&1 | cat > "runs/_logs/$id.log"
rc=${PIPESTATUS[0]}
echo "$id rc=$rc secs=$(( $(date +%s) - start ))"
