# ADR-096: Release workflow, reference timing host and local service stack

**Status:** Accepted
**Date:** 2026-11-01
**Decider:** Repository owner (decision rounds 1-8, 2026-11-01)
**Register:** D-120

## Decision

- **Push.** Standing authorization: push to `origin/restructure` after each committed wave
  whose focused tests pass. Never push to main, never force-push.
- **Release (PA-095/PA-096).** When the phase C baseline is recorded, open one PR from
  restructure to main with a review packet: diff summary, verification record SHA and
  deployment prerequisites. The owner merges. A later release is tagged `v0.x.y-alpha`;
  GitHub Actions builds the wheel and runs the RM-30 smoke. PyPI publishing needs manual
  owner approval.
- **Reference timing host (PA-051).** The owner's Windows development machine, under a
  controlled protocol: quiet host, fixed power plan, N repetitions with variance, and host
  specs and load stored in each benchmark record. Deployment hosts re-measure.
- **Concurrency budgets (PA-053).** Measure first, at 1, 10 and 50 msgs/s with parallel
  reads: p50/p95/p99 and derivation lag. Budgets are then proposed with about 30% headroom
  for owner approval.
- **Local service stack.** A docker compose file (Temporal dev server, Postgres,
  Redis/Valkey, MongoDB, Neo4j) runs the gates that need only these services (RM-16,
  RM-31, parts of PA-056 to PA-073) as `local-service` evidence. Real cluster, failover and
  canary checks stay DEPLOYMENT_CHECK.
- **Evals (phase D).** A one-time rewrite of the repository's own arm files and scripts to
  the current config (RM-19, RM-20, RM-21, PA-107). Historical result files stay untouched.
  This edits repository files and is not a user-facing codemod, so it is consistent with
  ADR-086.
