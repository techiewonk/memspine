#!/usr/bin/env bash
# Wave A batch 2 on frozen engine _wt_screens_b (N58, N45, N61, N44 + its own baseline).
# Waits for batch 1 (run_waveA.sh) to finish so at most one wave-A stream runs at a time.
cd "D:/mem/memory research/memspine/evals"
until grep -q "written:" runs/_logs/v32f-wA-local-n64-query-instruction.log 2>/dev/null \
   || grep -q "Traceback" runs/_logs/v32f-wA-local-n64-query-instruction.log 2>/dev/null; do sleep 120; done
source <(sed -n '/^FROZEN=/,/^}/p' run_v32_chain.sh)
FROZEN="D:/mem/memory research/_wt_screens_b/src"
L="locomo data/locomo10.json"
for arm in local-temporal-leg local-n58-english-bm25 local-n45-infer-year local-n61-overlap-rank local-n44-soft; do
  run "wB-$arm" $L "$arm" 4096
done
echo WAVE-A2 DONE
