#!/usr/bin/env bash
# H7: run evals from a pinned git worktree so edits in the main tree never leak into a GPU run.
# usage: bash pinned_run.sh <commit-ish> <script-in-evals> [args...]
# The worktree lives at ../../memspine-run (detached at the commit); runs/ there is a junction to this
# tree's evals/runs, so outputs land in one place. PYTHONPATH points `memspine` at the worktree's src/.
#
# P0-02: each run gets the environment of its own commit. The venv is built from the pinned commit's uv.lock
# (`uv sync --frozen`) and cached at ../memspine-venvs/<sha256(uv.lock + pyproject.toml + extras + python)[:16]>/,
# a sibling of the repo, so a stray sync in the shared .venv can no longer break a queued run (2026-10-11).
# Environment variables (all optional):
#   PINNED_SHARED_VENV=1   keep the old behaviour: use the repo's shared .venv, build nothing
#   PINNED_DRY_RUN=1       print venv path, lock hash and whether it would build, then exit 0 (touches nothing)
#   PINNED_EXTRAS          uv sync extras flags (default "--all-extras", what the shared venv was built with)
#   PINNED_PYTHON          python request for the venv, e.g. 3.13 (default: .python-version at the commit, else "default")
#   PINNED_VENV_ROOT       cache root (default: ../memspine-venvs)
#   PINNED_LOCK_WAIT       seconds to wait for another build of the same venv (default 3600)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"; repo="$(cd "$here/.." && pwd)"; wt="$(cd "$repo/.." && pwd)/memspine-run"
rev="$(git -C "$repo" rev-parse "${1:?commit}")"; shift
script="${1:?script}"; shift

extras="${PINNED_EXTRAS---all-extras}"
pyver="${PINNED_PYTHON:-}"
[ -n "$pyver" ] || pyver="$(git -C "$repo" show "$rev:.python-version" 2>/dev/null | head -n1 | tr -d '[:space:]' || true)"
[ -n "$pyver" ] || pyver="default"
venv_root="${PINNED_VENV_ROOT:-$(cd "$repo/.." && pwd)/memspine-venvs}"
# GPU torch: uv.lock resolves PyPI torch, which is CPU-only on Windows. PINNED_TORCH installs a CUDA build after the
# sync (default: the exact torch build of the shared venv, e.g. "torch==2.11.0+cu128", from the matching PyTorch index),
# so screens run on the GPU with the same torch as the reference runs. PINNED_TORCH=none keeps the lock's torch.
torch_spec="${PINNED_TORCH:-}"
if [ -z "$torch_spec" ]; then
  sp="$repo/.venv/Scripts/python.exe"; [ -x "$sp" ] || sp="$repo/.venv/bin/python"
  # a worktree has no .venv of its own: fall back to the main checkout's shared venv (../memspine/.venv)
  [ -x "$sp" ] || sp="$(cd "$repo/.." && pwd)/memspine/.venv/Scripts/python.exe"
  tv="$("$sp" -c 'import torch;print(torch.__version__)' 2>/dev/null || true)"
  [ -n "$tv" ] && torch_spec="torch==$tv" || torch_spec="none"
fi
torch_index="${PINNED_TORCH_INDEX:-}"
if [ -z "$torch_index" ] && [[ "$torch_spec" == *+cu* ]]; then
  torch_index="https://download.pytorch.org/whl/${torch_spec##*+}"
fi

# Lock hash: read from the commit itself (no worktree needed, so a dry run touches nothing).
lock_hash="$({ printf 'uv.lock\n'; git -C "$repo" show "$rev:uv.lock"
               printf '\npyproject.toml\n'; git -C "$repo" show "$rev:pyproject.toml"
               printf '\nextras=%s\npython=%s\ntorch=%s\n' "$extras" "$pyver" "$torch_spec"; } | sha256sum | cut -c1-16)"
venv="$venv_root/$lock_hash"
if [ -n "${PINNED_SHARED_VENV:-}" ]; then
  venv="$repo/.venv"; action="shared"
elif [ -f "$venv/.ready" ]; then action="reuse"
else action="build"
fi

if [ -n "${PINNED_DRY_RUN:-}" ]; then
  echo "pinned_run: dry-run"
  echo "  commit=$rev"
  echo "  lock_hash=$lock_hash"
  echo "  python=$pyver"
  echo "  extras=$extras"
  echo "  torch=$torch_spec ${torch_index:+(index $torch_index)}"
  echo "  venv=$venv"
  echo "  action=$action"
  exit 0
fi

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

# Build (or reuse) the per-run venv. Two runs that need the same venv serialise on a mkdir lock; the second one
# finds `.ready` after the wait and reuses it. A failed build removes the half-made directory.
venv_bin() { if [ -d "$1/Scripts" ]; then echo "$1/Scripts"; else echo "$1/bin"; fi; }
if [ "$action" = "build" ]; then
  mkdir -p "$venv_root"
  lockdir="$venv_root/$lock_hash.lock"; waited=0; max_wait="${PINNED_LOCK_WAIT:-3600}"
  until mkdir "$lockdir" 2>/dev/null; do
    [ -f "$venv/.ready" ] && break
    [ "$waited" -ge "$max_wait" ] && { echo "pinned_run: timed out waiting for $lockdir (remove it if no build is running)" >&2; exit 1; }
    sleep 5; waited=$((waited + 5))
  done
  if [ ! -f "$venv/.ready" ]; then
    trap 'rmdir "$lockdir" 2>/dev/null || true' EXIT
    echo "pinned_run: building venv $venv from $rev uv.lock (extras: $extras)"
    rm -rf "$venv"
    pyargs=(); [ "$pyver" = "default" ] || pyargs=(--python "$pyver")
    # shellcheck disable=SC2086  # $extras is a flag list on purpose
    if UV_PROJECT_ENVIRONMENT="$(cygpath -w "$venv" 2>/dev/null || echo "$venv")" \
         uv sync --frozen --project "$(cygpath -w "$wt" 2>/dev/null || echo "$wt")" "${pyargs[@]}" $extras \
       && { [ "$torch_spec" = "none" ] || { echo "pinned_run: installing $torch_spec ${torch_index:+from $torch_index}"; \
              uv pip install --python "$(venv_bin "$venv")/python.exe" --reinstall-package torch --no-deps \
                ${torch_index:+--index-url "$torch_index"} "$torch_spec"; }; }; then
      printf '%s\n' "$rev" > "$venv/.ready"
    else
      rm -rf "$venv"; echo "pinned_run: venv build failed" >&2; exit 1
    fi
    rmdir "$lockdir" 2>/dev/null || true; trap - EXIT
  fi
fi
vbin="$(venv_bin "$venv")"
py="$vbin/python.exe"; [ -x "$py" ] || py="$vbin/python"
[ -x "$py" ] || { echo "pinned_run: no python in $venv" >&2; exit 1; }
VIRTUAL_ENV="$(cygpath -w "$venv" 2>/dev/null || echo "$venv")"; export VIRTUAL_ENV
PATH="$(cygpath -u "$vbin" 2>/dev/null || echo "$vbin"):$PATH"; export PATH
export PYTHON="$py"   # run.sh and friends honour $PYTHON before looking for a .venv
unset PYTHONHOME

export PYTHONPATH="$(cygpath -w "$wt/src")"
export MEMSPINE_PINNED_COMMIT="$rev"
export MEMSPINE_VENV_HASH="$lock_hash"
# G-24: record the exact environment of this run (now the per-run venv's own packages). Best effort: a failure
# here never stops the run.
envdir="$here/runs/_logs/env"; mkdir -p "$envdir" 2>/dev/null || true
stamp="$(date +%Y%m%d-%H%M%S)"; freeze="$envdir/${rev:0:7}_$stamp.txt"
if { uv pip freeze --python "$py" 2>/dev/null || "$py" -m pip freeze --all 2>/dev/null; } > "$freeze" && [ -s "$freeze" ]; then
  MEMSPINE_ENV_HASH="$(sha256sum "$freeze" | cut -c1-16)"; export MEMSPINE_ENV_HASH
  echo "$stamp commit=$rev env=$MEMSPINE_ENV_HASH venv=$lock_hash script=$script freeze=env/$(basename "$freeze")" >> "$here/runs/_logs/pinned_runs.log" || true
else
  echo "pinned_run: could not freeze the environment (continuing)" >&2
fi
cd "$wt/evals"
echo "pinned_run: commit $rev in $wt (venv $venv, $action)"
exec bash "$script" "$@"
