#!/usr/bin/env bash
# Combined-engine ideas (GAP_REGISTER section L, 2026-10-11): LoCoMo-only focus screens of G-C2 (write-time atomic
# facts as a retrieval leg), G-C3 (entity/fact-edge graph leg + entity summaries) and G-C9 (session summaries +
# profile insights packed into the context), each on top of scr-pool-protect-jina, LOCAL reader and judge
# (qwen3.5:9b, thinking off) and LOCAL engine LLM roles (ollama/qwen3.5:9b at 127.0.0.1:11434 bound in the arm).
# Score: python eval_focus.py <id>-i2 --ref f-jref-i2 (pairs by question id; the Jina reference run f-jref-i2 must
# exist: it comes from run_screens_fast_jina.sh). eval_focus now prints "F1 (official LoCoMo)" beside the verdict.
#
# NOT launched by the author. The coordinator queues it AFTER the clean re-baseline: the arms add an ingest-time
# LLM pass (the sleep cycle, --memspine-build-sleep) on the same Ollama server, so nothing else may use it.
# Ingest cost per arm, full 10 conversations / 272 sessions (estimates, see GAP_REGISTER L): gc2 ~270 extract calls
# (~40-70 min), gc3 ~550-650 extract_edges calls + a few entity-summary batches (~1.5-2 h), gc9 ~270 summarize +
# ~270 reflect calls (~15-25 min). The 2-question check (t2) builds ONE conversation and exercises the whole sleep.
# The model-call cap run.sh derives (3 x questions + 200) does not count ingest calls, so it is raised here.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/gc.txt; S=runs/_logs/gc_scores.txt
J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
B="--memspine-build-sleep --max-model-calls 4000"   # sleep after ingest; later --max-model-calls overrides run.sh's
py="${PYTHON:-}"
if [ -z "$py" ]; then
  for c in "../.venv/Scripts/python.exe" "../.venv/bin/python" "../../memspine/.venv/Scripts/python.exe"; do
    [ -x "$c" ] && { py="$c"; break; }
  done
fi
[ -n "$py" ] || py="$(command -v python3 || command -v python)"
[ -f runs/f-jref-i2--memspine/results.jsonl ] || { echo "f-jref-i2 missing: run run_screens_fast_jina.sh first" >&2; exit 2; }
echo "gc start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
screen() {
  local id=$1 arm=$2 lf=$3
  bash run.sh --arm $arm --run-id $id-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$lf" --force || { echo "$id CHECK FAILED" >> $L; return; }
  bash run.sh --arm $arm --run-id $id-i2 --mode qa --topk 10 --query-ids @analysis/focus_slice_r7protect.json --questions 437 --forensics --batch-turns 32 --flags "$lf" --force && echo "$id-i2 done $(date +%T)" >> $L || { echo "$id-i2 FAILED" >> $L; return; }
  { echo "== $id-i2 vs f-jref-i2 $(date +%T)"; "$py" eval_focus.py $id-i2 --ref f-jref-i2; } >> $S 2>&1
}
screen f-gc2 scr-gc2-facts-jina   "$J $B"
screen f-gc3 scr-gc3-graph-jina   "$J $B"
screen f-gc9 scr-gc9-summary-jina "$J $B"
echo "GC DONE $(date +%T)" >> $L
