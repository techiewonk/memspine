#!/usr/bin/env bash
# NOTE: superseded by evals/run.sh (ENV-3: `env -u` is a silent no-op under Git Bash on Windows; credentials are now cleared with `unset`).
# Plan v3.2 free screens, batch 3 (local embedder, retrieval-only, $0).
cd "D:/mem/memory research/memspine/evals"
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
run() { # name config budget [extra flags]
  PYTHONPATH="D:/mem/memory research/_wt_screens/src" HF_HUB_OFFLINE=1 \
    ../.venv/Scripts/python.exe -m memspine_evals c0-1 --dataset locomo --path data/locomo10.json \
    --categories all --with-memspine --only-systems memspine --memspine-read-mode replay \
    --memspine-config "$(cat arms/$2.json)" --retrieval-only --max-model-calls 0 \
    --budget $3 --top-k 10 $4 --run-id "v32-$1" > "runs/_logs/v32-$1.log" 2>&1
  echo "done $1 $?"
}
( run local-depth-n02-head local-depth-n02 4096; run local-sentence-leg local-sentence-leg 4096; run local-multi-intent local-multi-intent 4096 ) &
( run local-second-round local-second-round 4096; run local-cluster-expand local-cluster-expand 4096 ) &
( run local-rulecards-floor local-rulecards-floor 4096 "--memspine-build-sleep"; run local-rulecards-weak local-rulecards-weak 4096 "--memspine-build-sleep" ) &
wait
echo ALL DONE 3
