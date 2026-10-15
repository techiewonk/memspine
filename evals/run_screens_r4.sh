#!/usr/bin/env bash
# Round 4 screens. NOT LAUNCHED when written; queue it through pinned_run.sh after r3/r3b finish:
#   bash pinned_run.sh <commit> run_screens_r4.sh
#
# Part 1 - A7: attribution of the "fix run" jump (74.5% -> 80.1% on 1,540 q), which changed five things at
#   once: reader prompt, refusal retry, judge rubric, replay window (+ date anchoring, date-mention leg), and
#   the server context. A 2x2 on the same 304 q (conv-26/30, cat 1-5), engine factor x reader+judge factor:
#       engine OLD = arms/a7-engine-old.json  (= qs-eq06-roff: window default, no anchoring, no mention leg)
#       engine NEW = arms/a7-engine-new.json  (= qs-eq06-fix : window 2/4, relative_dates_anchored, temporal_leg_mentions)
#       reader+judge OLD = no flags (default prompt, no retry, no judge guards, no date check)
#       reader+judge NEW = "$J" (grounded prompt, --retry-refusal, --judge-guards, --judge-date-check)
#   Both engine arms keep rerank off, so the reranker is not a factor. The server context (Ollama num_ctx 8192,
#   flash attention, q8 KV cache) is held at today's setting in all four cells; the (OLD, OLD) cell therefore also
#   shows the server effect against the recorded 74.5% on the same questions. Headline = the judge-controlled
#   number: engine effect at fixed reader+judge (cells C-A, D-B), reader+judge effect at fixed engine (B-A, D-C),
#   paired per question (McNemar), interaction = (D-C)-(B-A). LoCoMo only (no OP-Bench cell: A7 is about LoCoMo).
# Part 2 - B10: `rerank_context` (the reranker scores a candidate with its 1-2 neighbouring turns, so a reply is
#   judged with its question) on top of BEST_dev_2026-10-10 (pool 2, keep 10), one change each:
#   scr-rctx1 / scr-rctx2. Same slices and reference as r3 (xb-loc-dev on LoCoMo conv-26/30 cat 1-5 304 q,
#   xb-opb-dev on OP-Bench dev 331 probes). Adoption per the r3 rule (net gain beyond the +-5 question band on
#   both slices, no category loss).
# Via pinned_run.sh. GPU: one run at a time (the reader and judge share the Ollama server).
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/r4.txt; J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
OPB="--dataset op_bench --data data/opbench_src --questions 331"
OPBF="--item-ids conv-26:Caroline,conv-30:Jon,conv-41:John,conv-42:Joanna"
echo "r4 start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
loc() {  # id arm locomo-flags : 2-question check, then the 304-question LoCoMo slice
  local id=$1 arm=$2 lf=$3
  bash run.sh --arm $arm --run-id $id-t2 --mode qa --topk 10 --items 1 --max-queries 2 --categories 1,2,3,4,5 --forensics --batch-turns 32 --flags "$lf" --force || { echo "$id CHECK FAILED" >> $L; return 1; }
  bash run.sh --arm $arm --run-id $id-loc --mode qa --topk 10 --items 2 --categories 1,2,3,4,5 --questions 304 --forensics --batch-turns 32 --flags "$lf" --force && echo "$id-loc done $(date +%T)" >> $L || echo "$id-loc FAILED" >> $L
}
screen() {  # id arm locomo-flags opbench-extra-flags : the r3 slice pair
  local id=$1 arm=$2 lf=$3 of=$4
  loc $id $arm "$lf" || return
  bash run.sh --arm $arm --run-id $id-opb --mode qa --topk 10 $OPB --batch-turns 32 --flags "$OPBF $of" --force && echo "$id-opb done $(date +%T)" >> $L || echo "$id-opb FAILED" >> $L
}
# A7: 2x2 (cell = engine x reader+judge)
loc r4-a7-A a7-engine-old ""      # old engine, old reader+judge
loc r4-a7-B a7-engine-old "$J"    # old engine, new reader+judge
loc r4-a7-C a7-engine-new ""      # new engine, old reader+judge
loc r4-a7-D a7-engine-new "$J"    # new engine, new reader+judge
# B10: rerank_context 1 and 2
screen r4-rctx1 scr-rctx1 "$J" ""
screen r4-rctx2 scr-rctx2 "$J" ""
echo R4 DONE >> $L
