#!/usr/bin/env bash
# Screens batch 3 on frozen engine _wt_screens_c: N30 mentions, rerank base, N41 blend,
# N41+N42 (blend + session neighbours), with its own baseline. Starts after batch 2.
cd "D:/mem/memory research/memspine/evals"
until grep -q "written:" runs/_logs/v32f-wB-local-n44-soft.log 2>/dev/null \
   || grep -q "Traceback" runs/_logs/v32f-wB-local-n44-soft.log 2>/dev/null; do sleep 180; done
source <(sed -n '/^FROZEN=/,/^}/p' run_v32_chain.sh)
FROZEN="D:/mem/memory research/_wt_screens_c/src"
L="locomo data/locomo10.json"
for arm in local-temporal-leg local-n30-mentions local-rerank-base local-n41-blend05 local-n41n42-blend05-ctx1; do
  run "wC-$arm" $L "$arm" 4096
done
echo WAVE-A3 DONE
