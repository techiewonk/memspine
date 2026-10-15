# Deep review request: MemSpine restructure plan (2026-10-13)

You are asked for an independent deep review of the plan to restructure MemSpine. Read the files below in order, check
claims against the code in `src/memspine/` (branch `feat/local-qwen-stack`), and disagree where the evidence says so.
Do not change code. Never quote benchmark dataset text; use invented names.

## Read in this order

| # | File | What it is |
|---|---|---|
| 1 | `docs/v2/README.md` | What the plan is; no separate v2, memspine restructured in place, no compatibility shims |
| 2 | `docs/v2/VERIFY.md` (section 0) | The owner's answers Q1-Q9 and the 2026-10-13 amendment (graph layer on by default) |
| 3 | `docs/v2/ARCHITECTURE.md` | The end-to-end design, including section 8 (runtime and modular structure) |
| 4 | `docs/v2/pipelines/README.md`, `insert.md`, `offline.md`, `retrieve.md` | The three pipelines step by step with typed inputs and outputs, triggers, scenarios and owner decisions |
| 5 | `docs/v2/RESTRUCTURE.md` | Target package tree, import contract, steps S0-S16, owned files per package |
| 6 | `docs/v2/PLAN.md` (generated from `docs/v2/data/packages.py`) | 109 work packages: layers, operators, dependencies, kind, agent lane, gate, status |
| 7 | `docs/v2/EXECUTION.md`, `docs/v2/briefs/TEMPLATE.md` | How Sonnet agents execute packages (integration branch, review gates, up to 8 agents) |
| 8 | `docs/v2/DECISIONS.md`, `docs/v2/areas/*.md`, `docs/v2/data/*.json` | All 155 decisions, per-area verification, owner follow-ups |
| - | `docs/v2/tracker.html` | Offline copy of the live tracker (open in a browser; notes stay in that browser) |

## Questions for the review

1. **Order and collisions.** Is the order right (pure-move split S1 first, foundations S2 after, then write, read,
   offline)? Can any two packages that may run in parallel still edit the same file? Are serial lanes enough?
2. **Layered principle.** Event log as truth; L0 always; L1 and L3 (graph) on when a worker model is configured; L2, L4,
   L5 opt-in; no model call by default; every derived write through one door with lineage, trust cap and fingerprint.
   Where does the plan or a pipeline spec break this?
3. **Pipelines.** For insert, offline and retrieve: missing steps, wrong step order (for example lift before rerank,
   gate positions), scenarios not covered, schemas that do not carry what a later step needs, failure or idempotency gaps.
4. **Runtime.** Outbox as truth with taskiq as transport (in-memory broker in library mode), async core with dedicated
   executors, four priority classes, coalescing backpressure, non-blocking `sleep()` handles with one follow-up per
   namespace, triggers (N deposits AND M idle) OR context pressure C, gap K for both. Races, starvation, deadlocks
   (note the 2026-10-12 hang: a model call inside a namespace lock), restart semantics.
5. **Modularity.** dishka scopes (app, job, request), typed registries with entry points, import-linter contract,
   versioned Pydantic schemas exported as JSON Schema. Will this hold or decay?
6. **Sizing.** Is every package one Sonnet agent's worth of work (about one PR)? Which to split or merge?
7. **Evidence and gates.** Are the gates sufficient against overfitting to LoCoMo (paired gate, blind sets, behavioural
   suite)? Which behaviour packages lack a measurable gate?
8. **What is missing** entirely.

## Output wanted

A markdown report: verdict per question (agree / disagree / modify) with file:line evidence, concrete edits to
`docs/v2/data/packages.py` and the specs, a list of wrong claims, and your top five risks.
