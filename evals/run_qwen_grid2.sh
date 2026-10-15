#!/usr/bin/env bash
# Resume: the six arms of the full grid not finished in the first pass.
cd "$(dirname "$0")" || exit 1
( for a in qs-ebge-rq4b4 qs-ebgeb-roff qs-ebgeb-rbge; do bash run_qwen_stack.sh $a >> runs/_logs/grid_times.txt; done ) &
( for a in qs-eq06-rq4b4 qs-ebgeb-rq4b4 qs-ebgeb-rq06; do bash run_qwen_stack.sh $a >> runs/_logs/grid_times.txt; done ) &
wait
echo GRID DONE >> runs/_logs/grid_times.txt
