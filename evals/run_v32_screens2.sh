#!/usr/bin/env bash
# Plan v3.2 free screens, batch 2 (local embedder, retrieval-only, $0).
cd "D:/mem/memory research/memspine/evals"
run() { # name config budget [extra flags]
  env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN HF_HUB_OFFLINE=1 \
    ../.venv/Scripts/python.exe -m memspine_evals c0-1 --dataset locomo --path data/locomo10.json \
    --categories all --with-memspine --only-systems memspine --memspine-read-mode replay \
    --memspine-config "$(cat arms/$2.json)" --retrieval-only --max-model-calls 0 \
    --budget $3 --top-k 10 $4 --run-id "v32-$1" > "runs/_logs/v32-$1.log" 2>&1
  echo "done $1 $?"
}
SLEEP="--memspine-build-sleep"
( run local-session-cap2 local-session-cap2 4096; run local-prf local-prf 4096; run local-rulecards local-rulecards 4096 "$SLEEP"; run local-rulecards-floor-verbatim local-rulecards-floor-verbatim 4096 "$SLEEP" ) &
( run local-subject-leg local-subject-leg 4096; run local-rulecards-floor local-rulecards-floor 4096 "$SLEEP"; run local-rulecards-weak local-rulecards-weak 4096 "$SLEEP" ) &
wait
echo ALL DONE 2
