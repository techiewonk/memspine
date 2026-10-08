#!/usr/bin/env bash
# Plan v3.2 free screens, one chain against the FROZEN engine worktree (no mid-edit imports).
# Local embedder, retrieval-only, --max-model-calls 0, AWS vars unset: $0.
cd "D:/mem/memory research/memspine/evals"
FROZEN="D:/mem/memory research/_wt_screens/src"
run() { # name dataset path config budget [extra...]
  local name=$1 ds=$2 path=$3 cfg=$4 budget=$5; shift 5
  local extra=()
  [ "$ds" = locomo ] && extra+=(--categories all)
  PYTHONPATH="$FROZEN" FASTEMBED_CACHE_PATH="D:/hf-cache/fastembed" env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN HF_HUB_OFFLINE=1 \
    ../.venv/Scripts/python.exe -m memspine_evals c0-1 --dataset "$ds" --path "$path" "${extra[@]}" \
    --with-memspine --only-systems memspine --memspine-read-mode replay \
    --memspine-config "$(cat arms/$cfg.json)" --retrieval-only --max-model-calls 0 \
    --budget "$budget" --top-k 10 "$@" --run-id "v32f-$name" > "runs/_logs/v32f-$name.log" 2>&1
  echo "done $name $?"
}
L="locomo data/locomo10.json"
S="--memspine-build-sleep"
(
  run combo-A $L local-combo-A 4096
  run session-cap2 $L local-session-cap2 4096
  run prf $L local-prf 4096
  run rulecards $L local-rulecards 4096 $S
  run rulecards-floor-verbatim $L local-rulecards-floor-verbatim 4096 $S
  run sentence-leg $L local-sentence-leg 4096
  run second-round $L local-second-round 4096
  run sw-standing $L local-sw-standing 4096
  run sw-metadata-leg $L local-sw-metadata-leg 4096
  run lme-combo-A longmemeval data/longmemeval_s_cleaned.json local-combo-A 4096 --variant s
  run lme-depth-n02 longmemeval data/longmemeval_s_cleaned.json local-depth-n02 4096 --variant s
) &
(
  run depth-n02 $L local-depth-n02 4096
  run subject-leg $L local-subject-leg 4096
  run rulecards-floor $L local-rulecards-floor 4096 $S
  run rulecards-weak $L local-rulecards-weak 4096 $S
  run multi-intent $L local-multi-intent 4096
  run cluster-expand $L local-cluster-expand 4096
  run sw-current-state $L local-sw-current-state 4096
  run sw-auto-mode $L local-sw-auto-mode 4096
  run w1-protected $L local-w1-protected 4096
  run lme-temporal-leg longmemeval data/longmemeval_s_cleaned.json local-temporal-leg 4096 --variant s
  run convomem-combo-A convomem data/convomem local-combo-A 4096
  run convomem-temporal-leg convomem data/convomem local-temporal-leg 4096
) &
wait
echo CHAIN DONE
