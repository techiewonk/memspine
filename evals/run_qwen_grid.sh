#!/usr/bin/env bash
# Full LoCoMo retrieval-only grid, two parallel streams. Results: runs/qs-*/ , timings: runs/_logs/grid_times.txt
cd "$(dirname "$0")" || exit 1
( for a in qs-ebge-roff qs-ebge-rbge qs-ebge-rq06 qs-ebge-rq4b4 qs-ebgeb-roff qs-ebgeb-rq4b4; do bash run_qwen_stack.sh $a >> runs/_logs/grid_times.txt; done ) &
( for a in qs-eq06-roff qs-eq06-rbge qs-eq06-rq06 qs-eq06-rq4b4 qs-ebgeb-rbge qs-ebgeb-rq06; do bash run_qwen_stack.sh $a >> runs/_logs/grid_times.txt; done ) &
wait
echo GRID DONE >> runs/_logs/grid_times.txt
