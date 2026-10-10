#!/usr/bin/env bash
# One wrapper for a memspine LoCoMo run (D6 / PROC-4 / HAR-7). Replaces the core of the
# ad hoc run_*.sh scripts: validated arguments, credentials cleared, an explicit model-call
# cap, the resolved command line saved with the log, a STATUS file, and a post-run row check.
#
#   evals/run.sh --arm <arm> --run-id <id> --mode qa|retrieval --topk N \
#       [--items N] [--max-queries N] [--questions N] [--flags "..."] [--forensics] \
#       [--engine-src PATH] [--data PATH] [--dataset NAME] [--categories 1,2,3,4] \
#       [--batch-turns N] [--force]
#
# MemoryAgentBench / ConvoMem (V03 blind validation): --dataset memoryagentbench --data
# data/mab/Conflict_Resolution.parquet | --dataset convomem --data data/convomem, with a required
# --questions N and --flags "--item-ids @analysis/blind_split_mab.json" (see run_blind_validation.sh).
#
# OP-Bench (local only, no licence): --dataset op_bench --data data/opbench_src --questions N
# (exact probe count of the slice; --flags "--item-ids ... --opbench-per-task N ...").
#
# Output: runs/<run-id>--memspine/{results.jsonl,summary.json}, runs/_logs/<run-id>.log,
#         runs/<run-id>.STATUS ("ok" or "failed rc=N" / "failed <reason>"), and with
#         --forensics runs/<run-id>--forensics/. Exit code is non-zero on any failure.
# Env: PYTHON (interpreter), READER_MODEL / JUDGE_MODEL / BASE_URL (qa mode).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

die() { echo "run.sh: $*" >&2; exit 2; }

arm="" run_id="" mode="" topk="" items="" max_queries="" questions="" flags="" forensics=0
engine_src="" data="" dataset="locomo" categories="1,2,3,4" batch_turns=32 force=0 think=off system=memspine
while [ $# -gt 0 ]; do
  case "$1" in
    --arm) arm=${2:?}; shift 2 ;;
    --run-id) run_id=${2:?}; shift 2 ;;
    --mode) mode=${2:?}; shift 2 ;;
    --topk) topk=${2:?}; shift 2 ;;
    --items) items=${2:?}; shift 2 ;;
    --max-queries) max_queries=${2:?}; shift 2 ;;
    --questions) questions=${2:?}; shift 2 ;;
    --flags) flags=${2-}; shift 2 ;;
    --forensics) forensics=1; shift ;;
    --engine-src) engine_src=${2:?}; shift 2 ;;
    --data) data=${2:?}; shift 2 ;;
    --dataset) dataset=${2:?}; shift 2 ;;
    --categories) categories=${2:?}; shift 2 ;;
    --batch-turns) batch_turns=${2:?}; shift 2 ;;
    --force) force=1; shift ;;
    --think) think=${2-}; shift 2 ;;
    --system) system=${2:?}; shift 2 ;;  # memspine (default) | no-memory | naive-rag-* (I34)
    -h|--help) sed -n 2,14p "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[ -n "$arm" ] || die "--arm is required"
[ -n "$run_id" ] || die "--run-id is required"
[ -n "$topk" ] || die "--topk is required"
case "$mode" in qa|retrieval) ;; *) die "--mode must be qa or retrieval" ;; esac
case "$run_id" in */*|*..*|"") die "--run-id must be a plain name" ;; esac
arm_json="arms/$arm.json"
[ -f "$arm_json" ] || die "arm config not found: evals/$arm_json"

# Interpreter, engine source and data path (defaults mirror the repo layout).
repo="$(cd .. && pwd)"
py="${PYTHON:-}"
if [ -z "$py" ]; then
  for c in "../.venv/Scripts/python.exe" "../.venv/bin/python" "../../memspine/.venv/Scripts/python.exe"; do
    [ -x "$c" ] && { py="$c"; break; }
  done
fi
[ -n "$py" ] || py="$(command -v python3 || command -v python)" || die "no python found; set PYTHON"
engine_src="${engine_src:-$repo/src}"
[ -d "$engine_src" ] || die "--engine-src not a directory: $engine_src"
if [ "$dataset" = op_bench ]; then
  # OP-Bench: --data is the local checkout (a directory; its data/ folder works too).
  [ -n "$data" ] || data="data/opbench_src"
  [ -e "$data" ] || die "OP-Bench checkout not found: $data (evals/data/opbench_src; gitignored, local only)"
  [ -z "$max_queries" ] || die "--max-queries is LoCoMo-only; use --flags \"--opbench-per-task N\" with --items"
  [ -n "$questions" ] || die "op_bench needs --questions N (the exact probe count of the slice; evals/analysis/OPBENCH_PROTOCOL.md section 7)"
else
  if [ -z "$data" ]; then
    for c in "../data/locomo10.json" "../../memspine/data/locomo10.json" "data/locomo10.json"; do
      [ -f "$c" ] && { data="$c"; break; }
    done
  fi
  [ -e "${data:-/nonexistent}" ] || die "dataset file not found; pass --data"
fi
# Blind-validation datasets (V03): a directory is fine for ConvoMem; the judge and the sampling
# are fixed unless --flags names them. memoryagentbench: the deterministic alias judge (scores
# supersession by answer match; the supersession-order metric is computed by eval_blind.py).
# convomem: the default rubric judge routes abstention items to the abstention judge;
# --per-stratum 1000 keeps every question so item ids are stable (the default 20 would not).
case "$dataset" in
  memoryagentbench)
    case " $flags " in *" --judge-prompt "*) ;; *) flags="$flags --judge-prompt alias" ;; esac ;;
  convomem)
    case " $flags " in *" --per-stratum "*) ;; *) flags="$flags --per-stratum 1000" ;; esac ;;
esac

# Never overwrite a run silently.
mkdir -p runs/_logs
found=()
for p in runs/"$run_id" runs/"$run_id"--* runs/"$run_id".STATUS; do
  [ -e "$p" ] && found+=("$p")
done
if [ ${#found[@]} -gt 0 ]; then
  if [ "$force" -eq 1 ]; then
    rm -rf "${found[@]}"
  else
    die "refusing to overwrite: ${found[*]} (use --force)"
  fi
fi

# Expected question count (the harness has a per-item cap via MEMSPINE_EVAL_MAX_QUERIES).
if [ -z "$questions" ]; then
  if [ "$dataset" = locomo ] && [ "$categories" = "1,2,3,4" ]; then
    if [ -z "$items" ]; then questions=1540          # LoCoMo categories 1-4, 10 conversations
    elif [ "$items" = 1 ]; then questions=152        # conv-26
    fi
  fi
  if [ -n "$max_queries" ]; then questions=$(( max_queries * ${items:-10} )); fi
fi
[ -n "$questions" ] || die "cannot derive the expected question count; pass --questions N"
exact=1; [ -n "$max_queries" ] && exact=0   # a per-item cap is an upper bound

if [ "$mode" = qa ]; then
  max_calls=$(( questions * 3 + 200 ))
else
  max_calls=0
fi

# Credentials: unset (env -u silently does nothing under Git Bash on Windows).
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE MEMSPINE_EVAL_THINK
# --think on: the reader (not the judge) may reason; explicit flag, recorded in the log (A9).
case "$think" in
  on) export MEMSPINE_EVAL_THINK=on ;;
  off) ;;
  *) echo "run.sh: --think must be on or off" >&2; exit 2 ;;
esac
export PYTHONPATH="$engine_src"
export PYTORCH_CUDA_ALLOC_CONF="${PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True}"
[ -n "$max_queries" ] && export MEMSPINE_EVAL_MAX_QUERIES="$max_queries"
if [ "$forensics" -eq 1 ]; then
  export MEMSPINE_FORENSICS_DIR="runs/$run_id--forensics"
else
  unset MEMSPINE_FORENSICS_DIR
fi

cmd=("$py" _launch.py c0-1 --dataset "$dataset" --path "$data")
# LoCoMo categories mean nothing to other datasets (op_bench probes are not categorised).
[ "$dataset" = op_bench ] || cmd+=(--categories "$categories")
cmd+=(--mode "$mode" --with-memspine --only-systems "$system" --memspine-read-mode replay
     --memspine-config "$(cat "$arm_json")" --memspine-batch-turns "$batch_turns" --top-k "$topk")
[ -n "$items" ] && cmd+=(--items "$items")
if [ "$mode" = qa ]; then
  cmd+=(--reader-model "${READER_MODEL:-qwen3.5:9b}" --judge-model "${JUDGE_MODEL:-qwen3.5:9b}"
        --base-url "${BASE_URL:-http://127.0.0.1:11434/v1}" --max-model-calls "$max_calls")
else
  cmd+=(--retrieval-only --max-model-calls 0)
fi
# shellcheck disable=SC2206  # --flags is a deliberately word-split option string
[ -n "$flags" ] && cmd+=($flags)
cmd+=(--run-id "$run_id")

log="runs/_logs/$run_id.log"
status="runs/$run_id.STATUS"
{
  echo "# run.sh $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# PYTHONPATH=$PYTHONPATH MEMSPINE_FORENSICS_DIR=${MEMSPINE_FORENSICS_DIR:-} MEMSPINE_EVAL_MAX_QUERIES=${MEMSPINE_EVAL_MAX_QUERIES:-} MEMSPINE_EVAL_THINK=${MEMSPINE_EVAL_THINK:-off}"
  echo "# expected questions=$questions exact=$exact max-model-calls=$max_calls"
  printf '# command:'; printf ' %q' "${cmd[@]}"; echo
} | tee "$log"

start=$(date +%s)
rc=0
"${cmd[@]}" >> "$log" 2>&1 || rc=$?
secs=$(( $(date +%s) - start ))

fail() { echo "failed $1" > "$status"; echo "run.sh: failed $1 (see evals/$log)" >&2; exit "${2:-1}"; }
[ "$rc" -eq 0 ] || fail "rc=$rc" "$rc"

results="runs/$run_id--memspine/results.jsonl"
if [ "$system" != memspine ]; then results=$(ls runs/"$run_id"--*/results.jsonl 2>/dev/null | grep -v -- --forensics | head -1); fi
[ -n "$results" ] || results="runs/$run_id--$system/results.jsonl"
[ -s "$results" ] || fail "no results.jsonl at evals/$results"
rows=$(grep -c '"kind": *"result"' "$results" || true)
if [ "$exact" -eq 1 ] && [ "$rows" -ne "$questions" ]; then
  fail "row count $rows != expected $questions"
fi
if [ "$exact" -eq 0 ] && { [ "$rows" -lt 1 ] || [ "$rows" -gt "$questions" ]; }; then
  fail "row count $rows outside 1..$questions"
fi
echo "ok" > "$status"
echo "run.sh: ok run=$run_id rows=$rows secs=$secs" | tee -a "$log"
