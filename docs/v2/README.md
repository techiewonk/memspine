# MemSpine v2 (plan)

MemSpine v2 is a planned redesign that combines what the 15-engine survey, the gap plan, the independent review
and the Codex reviews taught us. This folder is the plan of record for v2. **It changes no code.** v1 keeps running
and keeps being measured until v2 is built and has passed the same gates.

## How v2 relates to v1 (owner decision, 2026-10-11)

- **Same project, same principles.** v2 is the next architecture of `memspine` itself, not a new package or a
  fork: the event log stays the truth, derived layers stay opt-in, no model call by default, every change
  graduates through the gate.
- **Plan only here.** Everything about v2 lives in `docs/v2/` on the main branch. v1 documents
  (`docs/GAP_TO_SOLUTION_PLAN.md`, `evals/analysis/GAP_REGISTER.md`, the ADRs) point here instead of copying it.
- **Code, when it starts:** v2 components land in `src/memspine/` behind flags and profiles, phase by phase
  (`ROADMAP.md`); a component becomes default only after its gate passes. API changes keep
  `memspine.compat.v1` aliases for one minor release.
- **Evaluation:** the hardened gate (G-01, G-E1): paired against the frozen v1 reference (clean-jina-full,
  81.4% on 1,540 LoCoMo cat 1-4, reproducible by commit through `pinned_run.sh`), all blind sets,
  per-conversation uncertainty.

## What is here

| File | What it holds |
|---|---|
| `DECISIONS.md` | The 155 answers: chosen option, recommendation, owner notes |
| `areas/*.md` | One chapter per area: the design as decided, each decision verified, what will happen, where in code, migration, gates |
| `VERIFY.md` | Cross-area check: conflicts, combined choices, what needs the owner, what must be measured first |
| `ARCHITECTURE.md` | The v2 architecture end to end: insert, offline, retrieve, layers, models, operations |
| `ROADMAP.md` | Build order in phases with entry and exit gates |
| `data/` | Machine-readable answers (`decisions_answered.json`), per-area verification (`verify_<area>.json`), the briefs |

## Sources

- Decision page (owner's answers): https://claude.ai/artifact/2KXuPiBf2EgU1Ww8nx5bjj
- Gap review doc (all gaps with code, flows, research, Codex review): https://claude.ai/artifact/MvR65yFBPaeGRVU4nUkezr
- Gap plan page: https://claude.ai/artifact/KKgn55rTgdaqcxThX5gnAK
- Engine survey records: `docs/engine_survey/` (15 engines, 14 phases, prompt matrix, gap plan with change log)
- Independent review: `docs/engine_survey/independent_review_2026-10-11/` (RV01-RV22)

## Change log

- 2026-10-11: v2 plan started; 155 decisions answered (149 took the recommendation, 6 chose
  differently, 24 with combining notes); area chapters, cross-area verification, architecture and roadmap.
- 2026-10-11 (later): owner decided v2 stays inside `memspine` (Q9); the plan moved into `docs/v2/` on the main
  branch and the separate `v2/plan` branch was retired.
