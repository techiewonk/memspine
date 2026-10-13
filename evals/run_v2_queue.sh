#!/usr/bin/env bash
# v2 screen queue (docs/v2/SCREENS.md). Runs the rows of v2_queue.tsv in order through pinned_run.sh, so every run
# uses a per-commit venv and a pinned worktree. One GPU start runs the whole queue.
#
#   MODE=sample|focus|final bash evals/run_v2_queue.sh [--dry-run] [--only S01,S05|B1] [--commit REV] [--rerun] [--reexpose "reason"]
#
# SAMPLE-FIRST (owner decision 2026-10-13: on GPU, test with samples, keep recording, iterate, one final run):
#   MODE=sample (DEFAULT)  every arm and its reference run on a small stratified seeded sample slice (evals/analysis/
#                          sample_slice_*.json: ~80 LoCoMo, ~60 OP-Bench questions; 60 questions over 3 conversations for
#                          engine-LLM rows, which are ingest-bound). The reference run is cached per reference arm +
#                          commit + slice and reused by every arm. NEVER the blind sets, never a PROMOTE verdict: the
#                          output is descriptive (wins/losses/ties, net, delta + bootstrap interval, per-category,
#                          cost, latency) and one row per run goes to the ledger runs/_logs/v2_results.tsv.
#                          Re-running is the point: no skip-if-finished. Read: python v2_ledger.py summary
#   MODE=focus             today's focus-slice paired screens (LoCoMo 437 q, OP-Bench dev 331 probes), with the verdicts.
#   MODE=final             the full LoCoMo set with the paired gate (+ OP-Bench dev for rows that have it), then the sealed
#                          blind sets via eval_blind (one exposure per arm), ONLY for screens marked
#                          `python v2_ledger.py mark <id> promote_candidate`. Others print "not a promote_candidate".
#
#   --dry-run / PINNED_DRY_RUN=1   print every command (and pinned_run's own dry-run header), run nothing, write no state
#   --only LIST                    screen ids and/or batch ids (B1,B2,B3), comma separated
#   --commit REV                   commit the whole queue is pinned to (default: HEAD). Never change it mid-queue.
#   --rerun                        run screens again even if the state file says they finished
#   --reexpose REASON              pass through to eval_blind.py; WITHOUT it a sealed blind set is never scored twice
#   V2_SLICE_MAP=path|''           FAIL-SLICE: per-screen targeted slices (default evals/v2_slice_map.tsv; empty = off, random sample slices only)
#   REF_OVERRIDE=f-jref3-i2        replace the default LoCoMo focus reference f-jref2-i2 (use after INCOMPARABLE)
#   QUEUE_SKIP_BLIND=1             stop every screen before its blind set
#
# Per screen (focus/final): behavioural check (CPU) -> sets in order (f focus, o OP-Bench dev, F full, b blind). Rules:
#   PROMOTE : the first set and the full set must give PROMOTE to continue; the other sets must not be REJECT
#   NONINF  : continue unless REJECT           PARITY: expected NO CHANGE (anything else is flagged)
#   REPORT  : run and score, no adoption verdict   REF: reference run only
# Stops: INCOMPARABLE or a behavioural SAFETY_REJECT stops the rest of the BATCH; INCOMPLETE/REJECT stop the screen.
# Progress: evals/runs/_logs/v2_queue.log (human) and v2_queue.state (id<TAB>status<TAB>per-set verdicts); the sample and
# final modes use v2_queue.sample.state / v2_queue.final.state so one mode never marks another mode's work as done.
# The queue never adopts anything: CANDIDATE means the sets passed; adoption is suite/gate.py plus the owner.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; cd "$here" || exit 2
dry="${PINNED_DRY_RUN:-}"; only=""; commit=""; rerun=0; reexpose=""; MODE="${MODE:-sample}"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) dry=1; shift ;;
    --only) only=${2:?}; shift 2 ;;
    --mode) MODE=${2:?}; shift 2 ;;
    --commit) commit=${2:?}; shift 2 ;;
    --rerun) rerun=1; shift ;;
    --reexpose) reexpose=${2:?}; shift 2 ;;
    -h|--help) sed -n 2,40p "$0"; exit 0 ;;
    *) echo "run_v2_queue.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$MODE" in sample|focus|final) ;; *) echo "run_v2_queue.sh: MODE must be sample, focus or final (got '$MODE')" >&2; exit 2 ;; esac
commit="${commit:-$(git -C "$here/.." rev-parse HEAD)}"
REF="${REF_OVERRIDE:-f-jref2-i2}"
logdir="$here/runs/_logs"; LOG="$logdir/v2_queue.log"; STATE="$logdir/v2_queue.state"
[ "$MODE" = focus ] || STATE="$logdir/v2_queue.$MODE.state"
DECISIONS="${V2_LEDGER_DIR:-$logdir}/v2_decisions.tsv"
SLICE_LOC="${SAMPLE_SLICE_LOC:-analysis/sample_slice_loc_80_s20261013.json}"
SLICE_LOC_GC="${SAMPLE_SLICE_LOC_GC:-analysis/sample_slice_loc_60_s20261013_c3.json}"
SLICE_OPB="${SAMPLE_SLICE_OPB:-analysis/sample_slice_opb_60_s20261013.json}"
if [ -z "$dry" ]; then mkdir -p "$logdir"; touch "$STATE"; fi
J="--qa-prompt grounded --retry-refusal --judge-guards --judge-date-check"
B="--memspine-build-sleep --max-model-calls 4000"
OPBD="--dataset op_bench --data data/opbench_src"
OPB="$OPBD --questions 331"
OPBF="--item-ids conv-26:Caroline,conv-30:Jon,conv-41:John,conv-42:Joanna"
BLIND_REF="scr-pool-protect-jina"
MAB="data/mab/Conflict_Resolution.parquet"

exec 3>&1  # fd 3: dry-run command lines stay visible even where a call's stdout is redirected to a file
say() { local m; m="$(date +%H:%M:%S) $*"; if [ -n "$dry" ]; then echo "$m"; else echo "$m" | tee -a "$LOG"; fi; }
pr() {  # pinned run: pr <script-in-evals> args...
  if [ -n "$dry" ]; then { printf '  [dry] bash pinned_run.sh %s' "$commit"; printf ' %q' "$@"; printf '
'; } >&3; return 0; fi
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
say "queue start mode=$MODE commit=$commit ref=$REF only=${only:-all}"
STAMP="$(date +%m%d-%H%M)"; [ -n "$dry" ] && STAMP="<MMDD-HHMM>"
LED="--ledger-mode $MODE --ledger-commit ${commit:0:7}"

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
      pr v2_py.sh eval_focus.py "$rid" --ref "$ref" --ledger-screen "$id" --ledger-arm "$arm" $LED > "$out" 2>&1 || true ;;
    o)
      of="$OPBF"; [ "$kind" = GC ] && of="$OPBF $B"
      pr run.sh --arm "$arm" --run-id "v2-$id-opb" --mode qa --topk 10 $OPB --batch-turns 32 --flags "$of" --force || { VERDICT=RUN_FAILED; return; }
      pr v2_py.sh eval_focus.py "v2-$id-opb" --opb > "$out" 2>&1 || true ;;
    F)
      pr run.sh --arm "$arm" --run-id "v2-$id-t2" --mode qa --topk 10 --items 1 --max-queries 2 --forensics --batch-turns 32 --flags "$fl" --force || { VERDICT=CHECK_FAILED; return; }
      pr run.sh --arm "$arm" --run-id "v2-$id-full" --mode qa --topk 10 --forensics --batch-turns 32 --flags "$fl" --force || { VERDICT=RUN_FAILED; return; }
      pr v2_py.sh eval_paired.py "v2-$id-full" --ref clean-jina-full --ledger-screen "$id" --ledger-arm "$arm" $LED > "$out" 2>&1 || true ;;
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
  if [ -n "$dry" ]; then
    case "$code" in o|b) pr v2_py.sh v2_ledger.py record --mode "$MODE" --screen "$id" --arm "$arm" --reference "$ref" --commit "${commit:0:7}" --slice-id "set-$code" --verdict "<verdict>" ;; esac
    VERDICT=DRY; return
  fi
  cat "$out" >> "$LOG"; VERDICT="$(verdict_of "$out")"; [ -n "$VERDICT" ] || VERDICT=NO_VERDICT
  case "$code" in o|b)  # no numbers for these sets (blind: aggregates only); the verdict is the record
    pr v2_py.sh v2_ledger.py record --mode "$MODE" --screen "$id" --arm "$arm" --reference "$ref" --commit "${commit:0:7}" --slice-id "set-$code" --verdict "$VERDICT" >/dev/null 2>&1 || true ;;
  esac
}

# ---- MODE=sample ---------------------------------------------------------------------------------------------------
declare -A REFSEEN=()
slice_field() { sed -n "s/^ *\"$2\": *\"\{0,1\}\([^\",]*\).*/\1/p" "$here/$1" | head -1; }
ref_spec() {  # ref_spec <queue reference run id> -> REFARM REFKIND (the arm whose sample run is the reference)
  REFARM=""; REFKIND=J
  case "$1" in
    v2-S*-i2) local rid=${1#v2-}; rid=${rid%-i2}
      REFARM="$(awk -F'\t' -v i="$rid" '$1==i{print $4}' "$here/v2_queue.tsv")"
      REFKIND="$(awk -F'\t' -v i="$rid" '$1==i{print $9}' "$here/v2_queue.tsv")" ;;
  esac
  [ -n "$REFARM" ] || { REFARM="$BLIND_REF"; REFKIND=J; }
}
sample_run() {  # sample_run <arm> <run-id> <loc|opb> <slice> <n> <kind> [categories]
  local arm=$1 rid=$2 set=$3 sl=$4 n=$5 kind=$6 cats=${7:-} fl="$J" of="$OPBF" cx=()
  [ "$kind" = GC ] && { fl="$J $B"; of="$OPBF $B"; }
  [ -n "$cats" ] && cx=(--categories "$cats")  # slice-map column 6 'categories=5': LoCoMo category-5 (adversarial) slices; absent = run.sh default 1,2,3,4
  # category 5: the right answer IS a refusal, so the refusal retry (re-ask after a refusal) is dropped for both arm and reference
  [ "$cats" = 5 ] && fl="${fl//--retry-refusal/}"
  if [ "$set" = loc ]; then
    pr run.sh --arm "$arm" --run-id "$rid" --mode qa --topk 10 "${cx[@]}" --query-ids "@$sl" --questions "$n" --forensics --batch-turns 32 --flags "$fl" --force
  else
    pr run.sh --arm "$arm" --run-id "$rid" --mode qa --topk 10 $OPBD --questions "$n" --query-ids "@$sl" --batch-turns 32 --flags "$of" --force
  fi
}
ensure_ref() {  # ensure_ref <set> <slice> [categories]: REFARM/REFKIND already set -> REFRUN (built once per ref arm + commit + slice [+ categories])
  local set=$1 sl=$2 cats=${3:-} sid sn
  sid="$(slice_field "$sl" slice_id)"; sn="$(slice_field "$sl" n)"
  REFRUN="smp-ref-${REFARM}-${SMP_REF_COMMIT:-${commit:0:8}}-${sid}${cats:+-cat${cats//,/}}"  # distinct cached reference per category set
  if [ -n "${SMP_REF_COMMIT:-}" ]; then  # cross-commit parity (S29): the reference must already exist, never rebuilt here
    if [ -f "runs/${REFRUN}--memspine/results.jsonl" ] && [ "$(cat "runs/${REFRUN}.STATUS" 2>/dev/null)" = ok ]; then
      say "reference $REFRUN (pre-built at SMP_REF_COMMIT) reused"; return 0
    fi
    [ -n "$dry" ] && { say "[dry] reference $REFRUN must be pre-built at SMP_REF_COMMIT=$SMP_REF_COMMIT"; return 0; }
    say "reference $REFRUN missing: build it first with --commit $SMP_REF_COMMIT (see docs/v2/SCREENS.md, S29)"; return 1
  fi
  if [ -f "runs/${REFRUN}--memspine/results.jsonl" ] && [ "$(cat "runs/${REFRUN}.STATUS" 2>/dev/null)" = ok ]; then
    say "reference $REFRUN cached (reused)"; return 0
  fi
  if [ -n "${REFSEEN[$REFRUN]:-}" ]; then say "reference $REFRUN cached (built earlier in this queue, reused)"; return 0; fi
  REFSEEN[$REFRUN]=1
  say "building reference $REFRUN once (reference arm $REFARM, commit ${commit:0:8}, slice $sid)"
  sample_run "$REFARM" "$REFRUN" "$set" "$sl" "$sn" "$REFKIND" "$cats" || { say "reference $REFRUN FAILED"; unset 'REFSEEN[$REFRUN]'; return 1; }
}
slices_for() {  # slices_for <screen> <set> <kind> <default slice>: one "slice path[TAB]categories" per line (FAIL-SLICE: evals/v2_slice_map.tsv; categories from column 6 'categories=5', empty when absent)
  local f="${V2_SLICE_MAP-$here/v2_slice_map.tsv}" out=""
  if [ -n "$f" ] && [ -f "$f" ]; then
    out="$(awk -F'	' -v id="$1" -v s="$2" -v k="$3" '{gsub(/
/,"")} /^#/||NF<3{next} $1==id&&$2==s{p=$3; if(k=="GC"&&$4!="")p=$4; c=""; if($6 ~ /^categories=/)c=substr($6,12); print p "	" c}' "$f")"
  fi
  if [ -n "$out" ]; then printf '%s
' "$out"; else printf '%s
' "$4"; fi
}
sample_row() {  # sample_row <id> <arm> <ref> <kind> <sets>: descriptive only, never a verdict, never a blind set
  local id=$1 arm=$2 ref=$3 kind=$4 sets=$5 set sl sn sid rid out dflt tag
  ref_spec "$ref"
  for set in loc opb; do
    case "$set:$sets" in loc:*[fF]*|opb:*o*) ;; *) continue ;; esac
    if [ "$set" = loc ]; then dflt="$SLICE_LOC"; [ "$kind" = GC ] && dflt="$SLICE_LOC_GC"; else dflt="$SLICE_OPB"; fi
    # a per-row targeted slice (evals/v2_slice_map.tsv) replaces the random sample slice of the LoCoMo set; no row = old behaviour
    local slist; slist="$(slices_for "$id" "$set" "$kind" "$dflt")"
    while IFS=$'	' read -r sl cats; do
      [ -n "$sl" ] || continue
      sn="$(slice_field "$sl" n)"; sid="$(slice_field "$sl" slice_id)"; tag=""
      rid="smp-$id-$set-$STAMP"
      if [ "$sl" != "$dflt" ]; then tag="$(slice_field "$sl" family)"; rid="smp-$id-tgt-$tag-$STAMP"; fi
      ensure_ref "$set" "$sl" "$cats" || { SUMMARY="$SUMMARY $set=REF_FAILED"; continue; }
      say "$id sample $set: $arm on $sid (n=$sn${tag:+, targeted $tag}${cats:+, categories $cats}) vs $REFRUN"
      if ! sample_run "$arm" "$rid" "$set" "$sl" "$sn" "$kind" "$cats"; then
        SUMMARY="$SUMMARY $set=RUN_FAILED"
        pr v2_py.sh v2_ledger.py record --mode sample --screen "$id" --arm "$arm" --reference "$REFRUN" --commit "${commit:0:7}" --slice-id "$sid" --verdict RUN_FAILED
        continue
      fi
      out="$(mktemp)"
      pr v2_py.sh eval_sample.py "$rid" --ref "$REFRUN" --set "$set" --slice "$sl" --screen "$id" --arm "$arm" --arm-file "arms/$arm.json" --commit "${commit:0:7}" --mode sample > "$out" 2>&1 || true
      if [ -z "$dry" ]; then tee -a "$LOG" < "$out"; fi
      SUMMARY="$SUMMARY $set${tag:+:$tag}=DESCRIPTIVE"
    done <<< "$slist"
  done
}
decision_of() { [ -f "$DECISIONS" ] && awk -F'\t' -v id="$1" 'NR>1 && $2==id{d=$3} END{print d}' "$DECISIONS"; }

declare -A BSTOP=()
while IFS=$'\t' read -r id batch pkg arm ref rule sets deps kind est flag; do
  case "$id" in ''|\#*) continue ;; esac
  selected "$id" "$batch" || continue
  [ "$ref" = "f-jref2-i2" ] && ref="$REF"
  if [ "$MODE" = sample ] && [ "$rule" = REF ]; then
    say "$id ($pkg) sample mode: reference runs are built automatically per slice + commit; nothing to do"; continue
  fi
  if [ "$MODE" = final ]; then
    [ "$rule" = REF ] && continue
    dec="$(decision_of "$id")"
    if [ "$dec" != promote_candidate ] && ! { [ -n "$dry" ] && [ ! -f "$DECISIONS" ]; }; then
      say "$id ($pkg) final: not a promote_candidate (decision: ${dec:-none}); full and blind sets not run"; continue
    fi
    [ -n "$dry" ] && [ ! -f "$DECISIONS" ] && say "$id: (dry run: no decisions file, assuming promote_candidate)"
    nsets=F; case "$sets" in *o*) nsets=${nsets}o ;; esac
    case "$rule" in PROMOTE|NONINF) nsets=${nsets}b ;; esac
    sets=$nsets
  fi
  if [ "$MODE" = focus ]; then
    sets="$(printf '%s' "$sets" | tr -cd 'fo')"
    if [ -z "$sets" ]; then say "$id ($pkg) focus: no focus-slice set in this row; skipped"; continue; fi
  fi
  if [ "$MODE" != sample ] && [ -z "$dry" ] && [ "$rerun" = 0 ] && finished "$(state_get "$id")"; then
    say "$id ($pkg) already $(state_get "$id"); skipping (use --rerun)"; continue
  fi
  if [ -n "${BSTOP[$batch]:-}" ]; then
    say "$id ($pkg) SKIPPED: batch $batch was stopped"; state_set "$id" SKIPPED_BATCH_STOPPED ""; continue
  fi
  blocked=""
  if [ "$deps" != "-" ] && [ -z "$dry" ] && [ "$MODE" = focus ]; then
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
      # The behavioural check builds a MODEL-FREE engine; arms that need an LLM role or a model-backed memory type cannot
      # load there (ConfigError). That is "not applicable", not a reason to skip the GPU screen. Unknown flags still block.
      *ConfigError*needs\ an\ LLM\ role*|*ConfigError*not\ enabled*) say "$id: $bl (model-free check not applicable; sets still run)"; summary="beh=N/A(model-free)" ;;
      *ERROR*) say "$id: $bl (flag not wired in this commit?) -> screen skipped"; state_set "$id" BLOCKED "$summary"; continue ;;
    esac
  fi
  if [ "$MODE" = sample ]; then
    SUMMARY="$summary"; sample_row "$id" "$arm" "$ref" "$kind" "$sets"
    say "$id -> SAMPLED ($SUMMARY)"; state_set "$id" SAMPLED "$SUMMARY"; continue
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
if [ "$MODE" = sample ]; then
  say "sample queue done: read the results with  python evals/v2_ledger.py summary --mode sample  (history <screen>, mark <screen> promote_candidate|drop|iterate)"
  pr v2_py.sh v2_ledger.py summary --mode sample
fi
say "queue end"
