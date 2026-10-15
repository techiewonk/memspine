#!/usr/bin/env bash
# SUPERSEDED (2026-10-10): referenced the removed memspine-fixes worktree; use evals/run.sh.
# A6 fixed config on GPU, then the Qwen-4B reranker baseline (main checkout code), both with stage logs.
cd "$(dirname "$0")" || exit 1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
TOPK=10 MEMSPINE_FORENSICS_DIR=runs/qa-full-qs-eq06-fix--forensics bash run_fix_qa.sh qs-eq06-fix
cd ../../memspine/evals && rm -rf runs/qa-full-qs-eq06-rq4b4-fx* && \
  MEMSPINE_FORENSICS_DIR=runs/qa-full-qs-eq06-rq4b4-fx--forensics SUFFIX=-fx bash run_one_qa.sh qs-eq06-rq4b4
echo NEXT DONE >> ../../memspine-fixes/evals/runs/_logs/qa_fix_times.txt
