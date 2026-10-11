#!/usr/bin/env bash
# W1 adaptive-window write, rollout step 1 (facts section only): LoCoMo focus screen of
# scr-window-facts-jina = scr-pool-protect-jina + write.window_extract: facts (one structured
# window_extract call per adaptive window, grounded facts with the cited turns as parents) fed to the
# view_tags retrieval leg (the G-C2 machinery), LOCAL reader and judge (qwen3.5:9b, thinking off) and the
# LOCAL engine role window_extract (ollama/qwen3.5:9b at 127.0.0.1:11434, bound in the arm).
# Score: python eval_focus.py f-win-i2 --ref f-jref-i2. Compare with f-gc2 (the same facts mined once per
# session at sleep) to read the window-size effect; the confound to keep in mind is that the sleep cycle's
# consolidate stage also writes one extractive summary per session into the pool, as in the GC arms.
#
# NOT launched by the author. The coordinator queues it after the clean re-baseline and the GC screens: the
# arm adds an ingest-time LLM pass on the same Ollama server. Expected ingest cost for the full 10
# conversations: see evals/analysis/WINDOW_STATS_2026-10-11.json (about 0.2 calls per turn).
# CPU rehearsal with fake LLMs: python evals/smoke_gc.py --arm scr-window-facts-jina
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8 PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
L=runs/_logs/window.txt; S=runs/_logs/window_scores.txt
J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
B="--memspine-build-sleep --max-model-calls 4000"   # build: flush_windows, then the sleep cycle
py="${PYTHON:-}"
if [ -z "$py" ]; then
  for c in "../.venv/Scripts/python.exe" "../.venv/bin/python" "../../memspine/.venv/Scripts/python.exe"; do
    [ -x "$c" ] && { py="$c"; break; }
  done
fi
[ -n "$py" ] || py="$(command -v python3 || command -v python)"
[ -f runs/f-jref-i2--memspine/results.jsonl ] || { echo "f-jref-i2 missing: run run_screens_fast_jina.sh first" >&2; exit 2; }
echo "window start $(date +%T) commit ${MEMSPINE_PINNED_COMMIT:-?}" >> $L
screen() {
  local id=$1 arm=$2 lf=$3
  bash run.sh --arm $arm --run-id $id-t2 --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$lf" --force || { echo "$id CHECK FAILED" >> $L; return; }
  bash run.sh --arm $arm --run-id $id-i2 --mode qa --topk 10 --query-ids @analysis/focus_slice_r7protect.json --questions 437 --forensics --batch-turns 32 --flags "$lf" --force && echo "$id-i2 done $(date +%T)" >> $L || { echo "$id-i2 FAILED" >> $L; return; }
  { echo "== $id-i2 vs f-jref-i2 $(date +%T)"; "$py" eval_focus.py $id-i2 --ref f-jref-i2; } >> $S 2>&1
}
screen f-win scr-window-facts-jina "$J $B"
echo "WINDOW DONE $(date +%T)" >> $L
