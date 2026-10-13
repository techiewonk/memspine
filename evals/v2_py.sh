#!/usr/bin/env bash
# Run a python script with the interpreter pinned_run.sh exported ($PYTHON), from the pinned worktree evals/ dir.
exec "${PYTHON:-python}" "$@"
