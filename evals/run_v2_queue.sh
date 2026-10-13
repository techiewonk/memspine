#!/usr/bin/env bash
# v2 screen queue (docs/v2/SCREENS.md). Runs the rows of v2_queue.tsv in order through pinned_run.sh, so every run
# uses a per-commit venv and a pinned worktree. One GPU start runs the whole queue.
#
#   bash evals/run_v2_queue.sh [--dry-run] [--only S01,S05|B1] [--commit REV] [--rerun] [--reexpose "reason"]
#
#   --dry-run / PINNED_DRY_RUN=1   print every command (and pinned_run's own dry-run header), run nothing, write no state
#   --only LIST                    screen ids and/or batch ids (B1,B2,B3), comma separated
#   --commit REV                   commit the whole queue is pinned to (default: HEAD). Never change it mid-queue.
#   --rerun                        run screens again even if the state file says they finished
#   --reexpose REASON              pass through to eval_blind.py; WITHOUT it a sealed blind set is never scored twice
#   REF_OVERRIDE=f-jref3-i2        replace the default LoCoMo focus reference f-jref2-i2 (use after INCOMPARABLE)
#   QUEUE_SKIP_BLIND=1             stop every screen before its blind set
#
# Per screen: behavioural check (CPU) -> sets in order (f focus, o OP-Bench dev, F full, b blind). Rules:
#   PROMOTE : the first set and the full set must give PROMOTE to continue; the other sets must not be REJECT
#   NONINF  : continue unless REJECT           PARITY: expected NO CHANGE (anything else is flagged)
#   REPORT  : run and score, no adoption verdict   REF: reference run only
# Stops: INCOMPARABLE or a behavioural SAFETY_REJECT stops the rest of the BATCH; INCOMPLETE/REJECT stop the screen.
# Progress: evals/runs/_logs/v2_queue.log (human) and v2_queue.state (id<TAB>status<TAB>per-set verdicts).
# The queue never adopts anything: CANDIDATE means the sets passed; adoption is suite/gate.py plus the owner.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; cd "$here" || exit 2
dry="${PINNED_DRY_RUN:-}"; only=""; commit=""; rerun=0; reexpose=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry=1; shift ;;
    --only) only=${2:?}; shift 2 ;;
    --commit) commit=${2:?}; shift 2 ;;
    --rerun) rerun=1; shift ;;
    --reexpose) reexpose=${2:?}; shift 2 ;;
    -h|--help) sed -n 2,24p "$0"; exit 0 ;;
    *) echo "run_v2_queue.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done
commit="${commit:-$(git -C "$here/.." rev-parse HEAD)}"
REF="${REF_OVERRIDE:-f-jref2-i2}"
logdir="$here/runs/_logs"; LOG="$logdir/v2_queue.log"; STATE="$logdir/v2_queue.state"
if [ -z "$dry" ]; then mkdir -p "$logdir"; touch "$STATE"; fi
J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
B="--memspine-build-sleep --max-model-calls 4000"
OPB="--dataset op_bench --data data/opbench_src --questions 331"
OPBF="--item-ids conv-26:Caroline,conv-30:Jon,conv-41:John,conv-42:Joanna"
BLIND_REF="scr-pool-protect-jina"
MAB="data/mab/Conflict_Resolution.parquet"

say() { local m; m="$(date +%H:%M:%S) $*"; if [ -n "$dry" ]; then echo "$m"; else echo "$m" | tee -a "$LOG"; fi; }
pr() {  # pinned run: pr <script-in-evals> args...
  if [ -n "$dry" ]; then printf '  [dry] bash pinned_run.sh %s' "$commit"; printf ' %q' "$@"; printf '
'; return 0; fi
  bash pinned_run.sh "$commit" "$@"
}
state_get() { [ -f "$STATE" ] && awk -F'\t' -v id="$1" '$1==id{s=$2} END{print s}' "$STATE"; }
state_set() { [ -n "$dry" ] || printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$STATE"; }
selected() {  # selected <id> <batch>
  [ -z "$only" ] && return 0
  case ",$only," in *",$1,"*|*",$2,"*) return 0 ;; esac
  return 1
}
finished() { case "$1" in CANDIDATE|NOT_ADOPTED|DONE) return 0 ;; *) return 1 ;; esac; }
verdict_of() { grep -E '^verdict:' "$1" | tail -1 | sed -E 's/^verdict: *//; s/ *\(.*//; s/[[:space:]]+$//'; }

if [ -n "$dry" ]; then
  say "DRY RUN: commit $commit, reference $REF, nothing is executed"
  PINNED_DRY_RUN=1 bash pinned_run.sh "$commit" run.sh 2>&1 | sed 's/^/  /'
fi
say "queue start commit=$commit ref=$REF only=${only:-all}"

VERDICT=""
run_set() {  # run_set <id> <arm> <ref> <kind> <setcode>   -> sets VERDICT
  local id=$1 arm=$2 ref=$3 kind=$4 code=$5 fl="$J" out rid of ledger
  [ "$kind" = GC ] && fl="$J $B"
  out="$(mktemp)"; VERDICT=""
  case "$code" in
    f)
      rid="v2-$id-i2"; [ "$id" = S09 ] && rid="f-jref3-i2"
      pr run.sh --arm "$arm" --run-id "${rid%-i2}-t2" --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$fl" --force || { VERDICT=CHECK_FAILED; return; }
      pr run.sh --arm "$arm" --run-id "$rid" --mode qa --topk 10 --query-ids @analysis/focus_slice_r7protect.json --questions 437 --forensics --batch-turns 32 --flags "$fl" --force || { VERDICT=RUN_FAILED; return; }
      [ "$ref" = "-" ] && { VERDICT=REF_DONE; return; }
      pr v2_py.sh eval_focus.py "$rid" --ref "$ref" > "$out" 2>&1 || true ;;
    o)
      of="$OPBF"; [ "$kind" = GC ] && of="$OPBF $B"
      pr run.sh --arm "$arm" --run-id "v2-$id-opb" --mode qa --topk 10 $OPB --batch-turns 32 --flags "$of" --force || { VERDICT=RUN_FAILED; return; }
      pr v2_py.sh eval_focus.py "v2-$id-opb" --opb > "$out" 2>&1 || true ;;
    F)
      pr run.sh --arm "$arm" --run-id "v2-$id-t2" --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$fl" --force || { VERDICT=CHECK_FAILED; return; }
      pr run.sh --arm "$arm" --run-id "v2-$id-full" --mode qa --topk 10 --forensics --batch-turns 32 --flags "$fl" --force || { VERDICT=RUN_FAILED; return; }
      pr v2_py.sh eval_paired.py "v2-$id-full" --ref clean-jina-full > "$out" 2>&1 || true ;;
    b)
      [ -n "${QUEUE_SKIP_BLIND:-}" ] && { VERDICT=SKIPPED; return; }
      ledger="$logdir/blind_exposure.jsonl"
      if [ -z "$dry" ] && [ -z "$reexpose" ] && [ -f "$ledger" ] && grep -q "\"$arm\"" "$ledger"; then
        say "$id: blind sets already exposed for $arm (ledger); not run again. Pass --reexpose REASON to score again."
        VERDICT=SEALED; return
      fi
      if [ -n "$dry" ] || [ ! -f "runs/bv-$BLIND_REF-mab--memspine/results.jsonl" ] || [ ! -f "runs/bv-$BLIND_REF-cm--memspine/results.jsonl" ]; then
        say "$id: blind reference runs for $BLIND_REF are built once (lazy, about 65 min) if missing"
        pr run_blind_validation.sh "$BLIND_REF" --topk 10 || { VERDICT=RUN_FAILED; return; }
      fi
      pr run_blind_validation.sh "$arm" --topk 10 --force || { VERDICT=RUN_FAILED; return; }
      if [ -n "$reexpose" ]; then
        pr v2_py.sh eval_blind.py "$arm" --ref "$BLIND_REF" --mab-data "$MAB" --reexpose "$reexpose" > "$out" 2>&1 || true
      else
        pr v2_py.sh eval_blind.py "$arm" --ref "$BLIND_REF" --mab-data "$MAB" > "$out" 2>&1 || true
      fi
      if grep -q '^REFUSED' "$out" 2>/dev/null; then VERDICT=SEALED; return; fi ;;
  esac
  [ -n "$dry" ] && { VERDICT=DRY; return; }
  cat "$out" >> "$LOG"; VERDICT="$(verdict_of "$out")"; [ -n "$VERDICT" ] || VERDICT=NO_VERDICT
}

declare -A BSTOP=()
while IFS=$'\t' read -r id batch pkg arm ref rule sets deps kind est flag; do
  case "$id" in ''|\#*) continue ;; esac
  selected "$id" "$batch" || continue
  [ "$ref" = "f-jref2-i2" ] && ref="$REF"
  if [ -z "$dry" ] && [ "$rerun" = 0 ] && finished "$(state_get "$id")"; then
    say "$id ($pkg) already $(state_get "$id"); skipping (use --rerun)"; continue
  fi
  if [ -n "${BSTOP[$batch]:-}" ]; then
    say "$id ($pkg) SKIPPED: batch $batch was stopped"; state_set "$id" SKIPPED_BATCH_STOPPED ""; continue
  fi
  blocked=""
  if [ "$deps" != "-" ] && [ -z "$dry" ]; then
    for d in ${deps//,/ }; do finished "$(state_get "$d")" || blocked="$d"; done
  fi
  if [ -n "$blocked" ]; then say "$id ($pkg) BLOCKED: needs $blocked"; state_set "$id" BLOCKED "needs $blocked"; continue; fi
  say "== $id [$batch] $pkg arm=$arm ref=$ref rule=$rule sets=$sets (est ${est} min)"

  summary=""; status=CANDIDATE
  # behavioural gate first: model-free, CPU only; a safety failure stops the batch before any GPU time is spent
  if [ "$rule" != REF ]; then
    bout="$(mktemp)"
    if [ -n "$dry" ]; then
      pr v2_py.sh v2_behavioural.py "$arm" --json-out "runs/_logs/v2_behavioural/$arm.json"
    else
      pr v2_py.sh v2_behavioural.py "$arm" --json-out "runs/_logs/v2_behavioural/$arm.json" > "$bout" 2>&1 || true
      cat "$bout" >> "$LOG"
    fi
    bl="$(grep -E '^behavioural:' "$bout" 2>/dev/null | tail -1)"; summary="beh=${bl#behavioural: }"
    case "$bl" in
      *SAFETY_REJECT*) say "$id: $bl -> REJECT, batch $batch stopped"; BSTOP[$batch]=1; state_set "$id" REJECT "$summary"; continue ;;
      *REGRESSION*) status=NOT_ADOPTED; say "$id: $bl (blocks adoption; sets still run)" ;;
      *ERROR*) say "$id: $bl (flag not wired in this commit?) -> screen skipped"; state_set "$id" BLOCKED "$summary"; continue ;;
    esac
  fi
  n=${#sets}; i=0; last=$((n-1))
  while [ "$i" -lt "$n" ]; do
    code="${sets:$i:1}"
    run_set "$id" "$arm" "$ref" "$kind" "$code"
    v="$VERDICT"; summary="$summary $code=$v"; say "$id set $code verdict: $v"
    if [ -n "$dry" ]; then i=$((i+1)); continue; fi
    case "$v" in
      INCOMPARABLE) say "$id: INCOMPARABLE -> batch $batch stopped (fix the manifest difference or set REF_OVERRIDE)"
                    BSTOP[$batch]=1; status=INCOMPARABLE; break ;;
      INCOMPLETE|NO_VERDICT|CHECK_FAILED|RUN_FAILED|"NO DATA") status=FAILED; break ;;
      REJECT*) status=NOT_ADOPTED; break ;;
      SEALED|SKIPPED|REF_DONE) break ;;
    esac
    case "$rule" in
      PROMOTE)
        if [ "$v" != PROMOTE ] && { [ "$i" -eq 0 ] || [ "$code" = F ]; }; then status=NOT_ADOPTED; break; fi
        if [ "$code" = b ] && [ "$v" != CONFIRMS ]; then status=NOT_ADOPTED; fi ;;
      PARITY)
        [ "$v" != "NO CHANGE" ] && status=NOT_ADOPTED ;;
    esac
    i=$((i+1))
  done
  [ "$rule" = REPORT ] && [ "$status" = CANDIDATE ] && status=DONE
  [ "$rule" = REF ] && status=DONE
  say "$id -> $status ($summary)"; state_set "$id" "$status" "$summary"
done < "$here/v2_queue.tsv"
say "queue end"
