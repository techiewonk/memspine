#!/usr/bin/env bash
# Plan v3.2 free screens, batch 5: second corpora for U5 (local embedder, retrieval-only, $0).
cd "D:/mem/memory research/memspine/evals"
run() { # name dataset path config [extra]
  PYTHONPATH="D:/mem/memory research/_wt_screens/src" env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN HF_HUB_OFFLINE=1 \
    ../.venv/Scripts/python.exe -m memspine_evals c0-1 --dataset $2 --path $3 \
    --with-memspine --only-systems memspine --memspine-read-mode replay \
    --memspine-config "$(cat arms/$4.json)" --retrieval-only --max-model-calls 0 \
    --budget 4096 --top-k 10 $5 --run-id "v32-$1" > "runs/_logs/v32-$1.log" 2>&1
  echo "done $1 $?"
}
( run lme-combo-A longmemeval data/longmemeval_s_cleaned.json local-combo-A "--variant s"; run lme-depth-n02 longmemeval data/longmemeval_s_cleaned.json local-depth-n02 "--variant s" ) &
( run lme-temporal-leg longmemeval data/longmemeval_s_cleaned.json local-temporal-leg "--variant s"; run convomem-combo-A convomem data/convomem local-combo-A ""; run convomem-temporal-leg convomem data/convomem local-temporal-leg "" ) &
wait
echo ALL DONE 5
