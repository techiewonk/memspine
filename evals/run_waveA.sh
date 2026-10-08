#!/usr/bin/env bash
# Wave A screens (gaps plan 2026-10-08) on a second frozen engine (_wt_screens_a), so
# chain 3 keeps its own. The baseline is the new `base` read path (temporal leg on).
# Usage: bash run_waveA.sh ARM [ARM ...]   (each ARM = arms/<ARM>.json, LoCoMo cats 1-4)
# Local embedder, retrieval-only, --max-model-calls 0: $0.
cd "D:/mem/memory research/memspine/evals"
source <(sed -n '/^FROZEN=/,/^}/p' run_v32_chain.sh)
FROZEN="D:/mem/memory research/_wt_screens_a/src"
L="locomo data/locomo10.json"
for arm in "$@"; do
  run "wA-$arm" $L "$arm" 4096
done
echo WAVE-A DONE
