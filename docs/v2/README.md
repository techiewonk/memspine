# MemSpine restructure plan ("v2")

## Latest owner decisions and Claude handoff (2026-10-13)

- [Self-contained Claude handoff and all 40 selection reviews](CLAUDE_ENHANCEMENT_HANDOFF_2026-10-13.md).
- [Confirmed amendments: ER-06, ER-24, ER-31](DECISION_AMENDMENTS_2026-10-13.md).
- [Current reconciled selection data](data/decisions_reconciled_2026-10-13.json).

These three amendments override the original export and historical review only for those IDs.
ER-07 through ER-15 and the in-flight erasure/derived-visibility follow-ups remain open.
No implementation or evaluation completion is implied.

The plan of record for restructuring MemSpine as a whole, combining what the 15-engine survey, the gap plan, the
independent review and the Codex reviews taught us. "v2" is only the name of this plan: there is no separate v2
package or product. The current code ("v1" below) stays the measured reference until each restructured part passes
the same gates.

## How the restructure relates to today's code (owner decisions, 2026-10-11 and 2026-10-12)

- **Same project, same principles.** v2 is the next architecture of `memspine` itself, not a new package or a
  fork: the event log stays the truth, derived layers stay opt-in, no model call by default, every change
  graduates through the gate.
- **Plan only here.** Everything about v2 lives in `docs/v2/` on the main branch. v1 documents
  (`docs/GAP_TO_SOLUTION_PLAN.md`, `evals/analysis/GAP_REGISTER.md`, the ADRs) point here instead of copying it.
- **Code:** memspine's structure changes altogether, step by step (`RESTRUCTURE.md`, `ROADMAP.md`). Code the new
  design replaces is deleted or cleaned, not kept behind compatibility aliases. Pure moves keep both test suites
  green; a step that changes behaviour graduates through the paired gate before it becomes the default.
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
| `PLAN.md` | The work packages with layers, operators, dependencies, agent lanes, gates and status |
| `EXECUTION.md` | How the work is executed: branching, review, agents, Python, GPU policy |
| `pipelines/` | Insert, offline and retrieve specs with typed step schemas, scenarios and owner decisions |
| `ROADMAP.md` | Build order in phases with entry and exit gates |
| `data/` | The question set as published (`questions.json`), the answers (`decisions_answered.json`, `owner_followup_2026-10-12.json`), per-area verification (`verify_<area>.json`), the briefs |

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
- 2026-10-12 (later): `docs/v2_decisions/` merged here (questions.json moved to `data/`, its brief was identical to `data/question_brief.md`); stale owner markers resolved; model chapter's server graph corrected to Neo4j (Q4).
- 2026-10-12 (owner): not a separate v2; memspine is restructured as a whole along this plan; replaced code is deleted, no compatibility shims.
