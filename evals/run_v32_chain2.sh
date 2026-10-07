#!/usr/bin/env bash
# Re-run the two rule-cards arms that the sleep estimate refused (fixed in 3441e74).
cd "D:/mem/memory research/memspine/evals"
source <(sed -n '/^FROZEN=/,/^}/p' run_v32_chain.sh)
L="locomo data/locomo10.json"
run rulecards-floor $L local-rulecards-floor 4096 --memspine-build-sleep
run rulecards-weak $L local-rulecards-weak 4096 --memspine-build-sleep
echo CHAIN2 DONE
