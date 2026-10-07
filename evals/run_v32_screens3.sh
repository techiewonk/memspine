#!/usr/bin/env bash
# Plan v3.2 free screens, batch 3 (local embedder, retrieval-only, $0).
cd "D:/mem/memory research/memspine/evals"
run() { # name config budget [extra flags]
  env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN HF_HUB_OFFLINE=1 \
    ../.venv/Scripts/python.exe -m memspine_evals c0-1 --dataset locomo --path data/locomo10.json \
    --categories all --with-memspine --only-systems memspine --memspine-read-mode replay \
    --memspine-config "$(cat arms/$2.json)" --retrieval-only --max-model-calls 0 \
    --budget $3 --top-k 10 $4 --run-id "v32-$1" > "runs/_logs/v32-$1.log" 2>&1
  echo "done $1 $?"
}
( run local-sentence-leg local-sentence-leg 4096; run local-multi-intent local-multi-intent 4096 ) &
( run local-second-round local-second-round 4096; run local-cluster-expand local-cluster-expand 4096 ) &
wait
echo ALL DONE 3
