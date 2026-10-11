#!/usr/bin/env bash
# H7: run evals from a pinned git worktree so edits in the main tree never leak into a GPU run.
# usage: bash pinned_run.sh <commit-ish> <script-in-evals> [args...]
# The worktree lives at ../../memspine-run (detached at the commit); runs/ there is a junction to this
# tree's evals/runs, so outputs land in one place. PYTHONPATH points `memspine` at the worktree's src/.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; repo="$(cd "$here/.." && pwd)"; wt="$(cd "$repo/.." && pwd)/memspine-run"
rev="$(git -C "$repo" rev-parse "${1:?commit}")"; shift
script="${1:?script}"; shift
if [ -d "$wt/.git" ] || [ -f "$wt/.git" ]; then
  git -C "$wt" checkout -q --detach "$rev"
else
  git -C "$repo" worktree add -q --detach "$wt" "$rev"
fi
[ -n "$(git -C "$wt" status --porcelain --untracked-files=no)" ] && { echo "pinned_run: worktree is dirty" >&2; exit 1; }
mkdir -p "$wt/data" "$wt/evals/data"
cp -u "$repo/data/locomo10.json" "$wt/data/"
[ -d "$wt/evals/data/opbench_src" ] || cp -r "$repo/evals/data/opbench_src" "$wt/evals/data/"
if [ ! -e "$wt/evals/runs" ]; then
  cmd //c mklink //J "$(cygpath -w "$wt/evals/runs")" "$(cygpath -w "$here/runs")" >/dev/null
fi
export PYTHONPATH="$(cygpath -w "$wt/src")"
export MEMSPINE_PINNED_COMMIT="$rev"
# G-24: record the exact environment of this run (pinning the code does not pin the shared venv; a stray sync broke
# every queued screen on 2026-10-11). Best effort: a failure here never stops the run.
py="$repo/.venv/Scripts/python.exe"; [ -x "$py" ] || py="$repo/.venv/bin/python"
envdir="$here/runs/_logs/env"; mkdir -p "$envdir" 2>/dev/null || true
stamp="$(date +%Y%m%d-%H%M%S)"; freeze="$envdir/${rev:0:7}_$stamp.txt"
if { uv pip freeze --python "$py" 2>/dev/null || "$py" -m pip freeze --all 2>/dev/null; } > "$freeze" && [ -s "$freeze" ]; then
  MEMSPINE_ENV_HASH="$(sha256sum "$freeze" | cut -c1-16)"; export MEMSPINE_ENV_HASH
  echo "$stamp commit=$rev env=$MEMSPINE_ENV_HASH script=$script freeze=env/$(basename "$freeze")" >> "$here/runs/_logs/pinned_runs.log" || true
else
  echo "pinned_run: could not freeze the environment (continuing)" >&2
fi
cd "$wt/evals"
echo "pinned_run: commit $rev in $wt"
exec bash "$script" "$@"
