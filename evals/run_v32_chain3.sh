#!/usr/bin/env bash
# Chain 3 (frozen engine): LongMemEval-S on a seeded stratified sample (12 per type, 72 Q)
# instead of all 500 (CPU: ~4.5 min / question / arm), plus the ConvoMem arms and the
# re-runs of chain 2. Local embedder, retrieval-only, --max-model-calls 0: $0.
cd "D:/mem/memory research/memspine/evals"
source <(sed -n '/^FROZEN=/,/^}/p' run_v32_chain.sh)
L="locomo data/locomo10.json"
LME="longmemeval data/longmemeval_s_cleaned.json"
IDS="$(cat arms/lme_sample72.txt)"
(
  run lme72-combo-A $LME local-combo-A 4096 --variant s --item-ids "$IDS"
  run lme72-temporal-leg $LME local-temporal-leg 4096 --variant s --item-ids "$IDS"
  run lme72-depth-n02 $LME local-depth-n02 4096 --variant s --item-ids "$IDS"
) &
(
  run convomem-combo-A convomem data/convomem local-combo-A 4096
  run convomem-temporal-leg convomem data/convomem local-temporal-leg 4096
  run rulecards-floor $L local-rulecards-floor 4096 --memspine-build-sleep
  run rulecards-weak $L local-rulecards-weak 4096 --memspine-build-sleep
  run sw-auto-mode-valid $L local-combo-A 4096 --memspine-read-mode auto
) &
wait
echo CHAIN3 DONE
