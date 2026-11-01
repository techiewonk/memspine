# ADR-087: Revision-bound verification evidence and the local baseline gate

**Status:** Accepted
**Date:** 2026-11-01
**Decider:** Repository owner (decision rounds 1-8, 2026-11-01)
**Register:** D-111

## Context

Verification records under `docs/provider_verification/` state test counts but not the
revision they ran on (DS-20). Tracker rows were closed on evidence that could not be tied
to code, and intermittent failures (RM-121, write-p95 timing) were handled ad hoc.

## Decision

1. **Revision-bound records.** `scripts/record_verification_run.py` writes a `revision`
   block (commit SHA, tree hash, branch, `dirty`) and an `env` block (Python, `uv.lock`
   SHA-256, extras). It refuses to write a record from a dirty tree. Tracker rows cite the
   commit SHA.
2. **Local baseline gate (phase C).** ruff + `mypy --strict` + `tests/unit`,
   `tests/integration`, `tests/combinations` (run in chunks) + `evals/tests`, all on one
   recorded clean commit. Live gates (Temporal server, strict native Docker, RM-16) are
   separate rows recorded as `local-service` evidence.
3. **Flakes.** Any failure blocks the gate unless the test is on an explicit quarantine
   list (`tests/quarantine.txt`) that names a tracker row. The record lists every
   quarantined test that ran. Timing assertions move behind a `perf` marker that is not in
   the gate.
4. **Freeze point.** The phase C run happens once, after the 2026-11-01 decision waves land.
   It is repeated at release (RM-30).
5. **Closing rule.** A tracker row closes only when it cites code (file and symbol) and a
   test name, or a revision-bound record.

## Consequences

Evidence becomes reproducible. A dirty-tree run is diagnostic only. Quarantine entries are
visible debt, not silent retries.
