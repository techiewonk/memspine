#!/usr/bin/env bash
# Launch a base screen batch in the background with the coordinator's environment (per-run venv without llama-cpp).
# usage: bash evals/run_screen_batch.sh <only: B1|S01,S02|...> [mode: sample|focus|final]
set -u
here="$(cd "$(dirname "$0")" && pwd)"
only="${1:?only}"; mode="${2:-sample}"
export PINNED_EXTRAS="--extra dev --extra graph --extra cache --extra ingest --extra ner --extra decider --extra community --extra compress --extra opensearch --extra temporal --extra taskiq --extra rest --extra rerank --extra st --extra static --extra aws --extra promptopt --extra postgres --extra weaviate --extra neo4j --extra encrypt"
export MODE="$mode"
log="$here/runs/_logs/screen_${only//,/_}_${mode}_$(date +%m%d-%H%M).log"
cd "$here"
nohup bash run_screen_queue.sh --only "$only" --mode "$mode" > "$log" 2>&1 &
echo "started pid $! log $log"
