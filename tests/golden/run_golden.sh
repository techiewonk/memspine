#!/usr/bin/env bash
# CPU golden entry point for CI (X-02) and for move PRs. No GPU, no network, ~10 s.
# Usage: tests/golden/run_golden.sh [extra pytest args]
# Exit code is pytest's. Run from anywhere; the script cds to the repo root.
# Set PYTHON=/path/to/venv/python to pick the interpreter (default: python on PATH).
set -euo pipefail
cd "$(dirname "$0")/../.."
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${PYTHON:-python}" -m pytest tests/golden -m golden -o addopts="" -q "$@"
