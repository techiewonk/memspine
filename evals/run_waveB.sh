#!/usr/bin/env bash
# Screens batch 4 (wave B) on frozen engine _wt_screens_d: N59, N63, N40, N33, N62, with
# its own baseline. Starts after batch 3.
cd "D:/mem/memory research/memspine/evals"
until grep -q "written:" runs/_logs/v32f-wC-local-n41n42-blend05-ctx1.log 2>/dev/null \
   || grep -q "Traceback" runs/_logs/v32f-wC-local-n41n42-blend05-ctx1.log 2>/dev/null; do sleep 300; done
source <(sed -n '/^FROZEN=/,/^}/p' run_v32_chain.sh)
FROZEN="D:/mem/memory research/_wt_screens_d/src"
L="locomo data/locomo10.json"
for arm in local-temporal-leg local-n59-entity-leg local-n63-statement-probe local-n40-speaker-probe local-n33-short-bm25 local-n62-temporal-weight; do
  run "wD-$arm" $L "$arm" 4096
done
echo WAVE-B DONE
