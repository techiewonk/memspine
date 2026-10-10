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
cd "$wt/evals"
echo "pinned_run: commit $rev in $wt"
exec bash "$script" "$@"
