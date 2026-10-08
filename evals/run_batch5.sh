#!/usr/bin/env bash
# Screens batch 5 on frozen engine _wt_screens_d: N02 depth stacked on the new base
# (TODO 1.2) and the N35 context-budget sweep of the new base (TODO 1.4: 2K/8K/16K;
# 4K is the batch-4 baseline). Starts after batch 4.
cd "D:/mem/memory research/memspine/evals"
until grep -q "written:" runs/_logs/v32f-wD-local-n62-temporal-weight.log 2>/dev/null \
   || grep -q "Traceback" runs/_logs/v32f-wD-local-n62-temporal-weight.log 2>/dev/null; do sleep 300; done
source <(sed -n '/^FROZEN=/,/^}/p' run_v32_chain.sh)
FROZEN="D:/mem/memory research/_wt_screens_d/src"
L="locomo data/locomo10.json"
run "wD-local-temporal-n02" $L local-temporal-n02 4096
for b in 2048 8192 16384; do
  run "wD-budget-$b" $L local-temporal-leg $b
done
echo BATCH5 DONE
