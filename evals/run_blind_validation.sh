#!/usr/bin/env bash
# V03: run ONE arm on both BLIND validation slices (MAB-CR 400 q + ConvoMem 200 q).
#
#   bash evals/run_blind_validation.sh <arm> [--topk N] [--force] [--check-only]
#   python evals/eval_blind.py <arm> --ref <reference-arm> --mab-data data/mab/Conflict_Resolution.parquet
#
# BLIND - validation only, never inspect per-question failures for design. Nothing is tuned on
# these slices; read only the aggregate output of eval_blind.py. (Do not open the run files.)
#
# Order: 2-question checks first (run ids bv-<arm>-{mab,cm}-chk), then the full slices
# (bv-<arm>-mab, bv-<arm>-cm). A failed check stops the queue before any full run. Needs the
# Ollama reader/judge (qa mode) like any run.sh QA run; never start it while another GPU run is live.
# Estimate at ~1 s answer + ~1 s judge + ~4 s retrieval per question: MAB-CR 400 q about 40 min,
# ConvoMem 200 q about 20 min, plus ingestion (6k/32k contexts: 455-2,310 facts; ConvoMem ~56 turns).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

arm="${1:?usage: run_blind_validation.sh <arm> [--topk N] [--force] [--check-only]}"; shift
topk=10 force="" check_only=0
while [ $# -gt 0 ]; do
  case "$1" in
    --topk) topk=${2:?}; shift 2 ;;
    --force) force="--force"; shift ;;
    --check-only) check_only=1; shift ;;
    *) echo "run_blind_validation.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done

mab_data="data/mab/Conflict_Resolution.parquet"
cm_data="data/convomem"
mab_split="analysis/blind_split_mab.json"
cm_split="analysis/blind_split_convomem.json"
for f in "$mab_split" "$cm_split"; do [ -f "$f" ] || { echo "missing frozen slice: evals/$f" >&2; exit 2; }; done

run() {  # run <run-id> <dataset> <data> <questions> <extra flags> [max-queries-per-item]
  local id=$1 ds=$2 data=$3 q=$4 flags=$5 mq=${6:+--max-queries $6}
  bash run.sh $mq --arm "$arm" --run-id "$id" --mode qa --topk "$topk" --dataset "$ds" \
    --data "$data" --questions "$q" --flags "$flags" $force
}

# 2-question checks: the first two ids of each slice (item filter by id, 1 question per item).
mab_ids="$("${PYTHON:-python}" -c "import json;print(','.join(json.load(open('$mab_split'))['blind_items'][:2]))")"
cm_ids="$("${PYTHON:-python}" -c "import json;print(','.join(json.load(open('$cm_split'))['blind_items'][:2]))")"
run "bv-$arm-mab-chk" memoryagentbench "$mab_data" 2 "--item-ids $mab_ids" 1
run "bv-$arm-cm-chk" convomem "$cm_data" 2 "--item-ids $cm_ids" 1
[ "$check_only" -eq 0 ] || { echo "checks ok; stopping (--check-only)"; exit 0; }

run "bv-$arm-mab" memoryagentbench "$mab_data" 400 "--item-ids @$mab_split"
run "bv-$arm-cm" convomem "$cm_data" 200 "--item-ids @$cm_split"
echo "done: python evals/eval_blind.py $arm --ref <reference> --mab-data evals/$mab_data"
