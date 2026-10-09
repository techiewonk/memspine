#!/usr/bin/env bash
# Full LoCoMo QA (categories 1-4 = 1,540 questions), Qwen3.5-9B Q4_K_M reader + judge, thinking off.
# Waits for the retrieval stage to finish so Ollama has GPU memory. Times -> runs/_logs/qa_full_times.txt
cd "$(dirname "$0")" || exit 1
while ! grep -q "STAGE2 DONE" runs/_logs/stage2_times.txt 2>/dev/null; do sleep 60; done
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN MEMSPINE_EVAL_THINK
for arm in ${QA_ARMS:-qs-eq06-roff qs-ejina-rjina qs-ebge-roff}; do
  s=$(date +%s)
  ../.venv/Scripts/python.exe _launch.py c0-1 --dataset locomo --path ../data/locomo10.json --categories 1,2,3,4 \
    --mode qa --with-memspine --only-systems memspine --memspine-read-mode replay \
    --memspine-config "$(cat arms/$arm.json)" --memspine-batch-turns 32 \
    --reader-model qwen3.5:9b --judge-model qwen3.5:9b --base-url http://localhost:11434/v1 \
    --max-model-calls 4000 --run-id qa-full-$arm 2>&1 | cat > runs/_logs/qa-full-$arm.log
  echo "qa-full-$arm secs=$(( $(date +%s)-s ))" >> runs/_logs/qa_full_times.txt
done
echo QA FULL DONE >> runs/_logs/qa_full_times.txt
