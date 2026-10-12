# ADR-067: Restructure memspine in place; no compatibility shims; migration-time upcaster (G-23)

- **Status:** accepted
- **Date:** 2026-10-13 (owner decision 2026-10-12)
- **Decision ids:** G-23 (amended), G-16 (public API shape), ER-33 (event versioning), ER-27 (retention and rebuild limits).
- **Sources:** `docs/v2/RESTRUCTURE.md` (target tree, steps, risks); `docs/v2/PLAN.md` (work packages X-00 to X-04, F-05 onwards); `docs/v2/ARCHITECTURE.md` sections 7 and 8; `docs/v2/DECISION_AMENDMENTS_2026-10-13.md` and `docs/v2/DECISION_AMENDMENTS_2026-10-13b.md`.

## Context

The restructure plan in `docs/v2/` reshapes the engine: the large `engine.py` facade is split, modules move to a
layered tree, and the public surface becomes a small loop-verb core on a scoped handle (G-16 D). Earlier drafts of the
plan treated "v2" as a new package beside v1 and promised `memspine.compat.v1` aliases, deprecation warnings and a
dual-read period for one minor release (G-23 C as first recorded). That would keep two code paths alive, double the
test surface and leave dead code in the tree that nobody owns.

The event log is the truth (ADR-001). Derived layers can be rebuilt from it. So the only thing that must survive a
restructure is the persisted data, not the old Python names.

## Decision

1. **In place.** memspine is restructured inside `src/memspine/`, on the same repository and principles. There is no
   separate v2 package or fork. "v2" is only the name of the plan.
2. **No compatibility shims.** No `memspine.compat.v1`, no deprecated aliases, no re-export modules at old import paths,
   no deprecation period. A removed or renamed config flag fails at load and names its replacement.
3. **Delete with the replacement.** Replaced code is deleted in the same PR that introduces its replacement.
4. **Codemod.** A codemod rewrites imports in `src/`, `tests/` and `evals/` in the same commit as each move. No old
   import path survives a move PR.
5. **Pure moves** are guarded by the CPU golden (identical twice, and byte-identical against the recorded hit order or
   context hash where the step says so) and both test suites. A move PR changes no behaviour.
6. **Behaviour changes** are built flag-off, are byte-identical when off, and graduate to default only through the
   paired adoption gate (G-01).
7. **G-23, amended.** Old persisted events are handled by a **migration-time upcaster**: events are upcast when read
   during migration or replay, by versioned upcasters, one chain per event kind (ER-33: durable event and public API
   versions are strict; internal step types are typed but carry no public promise). Projections are rebuilt by replay
   into a fresh store; the old store is never written. There is **no runtime compatibility layer** for old import paths
   or old APIs. Stores with a rolling log advertise limited rebuild and fail rather than rebuild silently (ER-27).
8. **Reproducibility of the past** is by commit: `pinned_run.sh` and a per-run venv reproduce the pre-restructure
   reference (clean-jina-full) from a worktree. Its interface, `evals/runs`, `data/locomo10.json` and
   `evals/data/opbench_src` are not changed by the restructure.

## Consequences

- One code path, one test surface. Smaller tree; no dead aliases to maintain or remove later.
- Every move PR is large and mechanical, so the golden and both suites are a hard gate on each one.
- Outside callers on old import paths or old `Engine` verbs break at upgrade. The upgrade notes list the new paths;
  the codemod can be run on their code.
- Old stores need an explicit migration run (replay with upcasters) before use. Re-extraction costs model calls only
  when a role is bound; with none bound, only deterministic projections are rebuilt.
- Docs, work packages and tests must not promise aliases or a dual-read period (X-04 aligned the active docs).

## Alternatives rejected

- **Separate `memspine-v2` package.** Two packages drift, the shared parts need a third home, and the gate and
  evals would run against two surfaces. Rejected by the owner (2026-10-12).
- **Shims with a deprecation period** (`memspine.compat.v1`, dual reads for one minor release). Keeps the old surface
  alive and tested, delays deletion, and leaves the question of who removes it. Rejected: the event log makes a
  data-level migration sufficient.
- **Runtime upcasting on every read, forever.** Keeps old event shapes in the hot path. Rejected in favour of upcasting
  once, at migration or replay.

## Links

- `docs/v2/RESTRUCTURE.md` (layout, steps, owned files, risks)
- `docs/v2/PLAN.md` (work packages)
- `docs/v2/ARCHITECTURE.md` sections 7 and 8
- ADR-001 (event-sourced core), ADR-011 (event-log retention), ADR-039 (erasure reaches summaries)
