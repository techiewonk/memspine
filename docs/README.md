# memspine — design docs

The blueprint and evidence base for the engine. **The structure plan is authoritative;** if any doc disagrees with it, the plan wins.

## Using memspine

| Doc | What it is |
|-----|------------|
| [`FEATURES.md`](./FEATURES.md) | **Feature catalog** — the 9 memory types (purpose · enable key · verbs · ADR), the Memory Firewall, and the E1–E9 optimization program (on-by-default vs opt-in). |
| [`USAGE.md`](./USAGE.md) | **How-to guide** — install & extras, constructing an `Engine`, a worked example per memory type, the CLI, and the REST protocol (with the no-authn caveat). |

## Ecosystem comparison

| Doc | What it is |
|-----|------------|
| [`ECOSYSTEM_COMPARISON.md`](./ECOSYSTEM_COMPARISON.md) | **Multi-dimensional comparison** — memspine vs peers; **§3.15** deep memory-type compare (memspine × cognee × EverMemOS). *(Pass #4 + type deep-dive: 2026-07-10; Pass #5 sync/links: 2026-07-10.)* |
| [`ARCHITECTURE_FLOWS.md`](./ARCHITECTURE_FLOWS.md) | **Code-traced flows** — write/read/sleep mermaid diagrams, **§8–§9** stage technology + prompt flows. *(Pass #4; deep algo/prompt → Pass #5 methodology/prompts.)* |
| [`ECOSYSTEM_METHODOLOGY.md`](./ECOSYSTEM_METHODOLOGY.md) | **Pass #5 research survey** — per-repo methodology, algorithms, flows, implications for memspine. |
| [`ECOSYSTEM_MEMORY_TAXONOMY.md`](./ECOSYSTEM_MEMORY_TAXONOMY.md) | **Pass #5 memory-category taxonomy** — all in-scope engines → memspine 9-type crosswalk. |
| [`ECOSYSTEM_PROMPTS.md`](./ECOSYSTEM_PROMPTS.md) | **Pass #5 full prompt catalog** — verbatim prompts + wiring (see also staging). |
| [`exports/ECOSYSTEM_PEER_TRACE.csv`](./exports/ECOSYSTEM_PEER_TRACE.csv) | **Spreadsheet export** — one row per engine: write/read I/O, packages, prompts, vector/embedder/worker defaults. |
| [`exports/ECOSYSTEM_MEMORY_TYPES.csv`](./exports/ECOSYSTEM_MEMORY_TYPES.csv) | **Full memory-type rows** — all Pass #5 engines (supersedes MS×CG×EO-only for multi-engine work). |
| [`exports/ECOSYSTEM_MEMORY_TYPES_MS_CG_EO.csv`](./exports/ECOSYSTEM_MEMORY_TYPES_MS_CG_EO.csv) | **Memory-type crosswalk** — memspine × cognee × EverMemOS (narrow prior export). |
| [`exports/ECOSYSTEM_REPO_SYNC.csv`](./exports/ECOSYSTEM_REPO_SYNC.csv) | **One-shot git sync ledger** — SHA/branch/pull status (Pass #5 freeze). |
| [`exports/ECOSYSTEM_PROMPTS.csv`](./exports/ECOSYSTEM_PROMPTS.csv) | **Prompt metadata** — id/purpose/hot_path/path → text in ECOSYSTEM_PROMPTS.md / staging. |
| [`exports/ECOSYSTEM_ALGORITHMS.csv`](./exports/ECOSYSTEM_ALGORITHMS.csv) | **Algorithm index** — family × repo × citation. |
| [`exports/ECOSYSTEM_PACKAGE_GAPS.csv`](./exports/ECOSYSTEM_PACKAGE_GAPS.csv) | **Package gaps** vs memspine core/extras (compact). |
| [`exports/ECOSYSTEM_PACKAGE_ADOPTION.csv`](./exports/ECOSYSTEM_PACKAGE_ADOPTION.csv) | **Package adoption** — category/package/adopters + `memspine_slot` / `gap_note` (Pass #5). |

## Design & rationale

| Doc | What it is |
|-----|------------|
| [`memspine-structure-plan.md`](./memspine-structure-plan.md) | **The buildable blueprint** — repository tree, extras matrix, locked decision register (D-01…D-54), enhancement program (E1–E9, Parts B–E), phase plan (P0–P7). Start here. |
| [`UNIMEM_V2_REWORK_PROPOSAL.md`](./UNIMEM_V2_REWORK_PROPOSAL.md) | Architecture rationale — from storage facade to cognitive engine — with the code-level evidence base. |
| [`DEPENDENCY_ANALYSIS.md`](./DEPENDENCY_ANALYSIS.md) | Code-level scan of the memory-engine ecosystem: why each dependency was chosen, adoption signal, D-26…D-42 reasoning. *(Manifest scan 2026-07-07; pass #3 stage/package cross-ref 2026-07-10 — §3.12 + ARCHITECTURE_FLOWS §8.)* |
| [`PACKAGE_CATALOG.md`](./PACKAGE_CATALOG.md) | Every candidate package (564 scanned), grouped by function, with "does what". **Cross-ref:** package adoption rationale in [`ECOSYSTEM_COMPARISON.md`](./ECOSYSTEM_COMPARISON.md) §3.12. |
| [`adr/`](./adr/) | Architecture Decision Records — one file per decision (ADR-001 … ADR-021). |

## Reading order

1. `memspine-structure-plan.md` §0–§2 (positioning, decision register, extras matrix)
2. `UNIMEM_V2_REWORK_PROPOSAL.md` §0–§4 (why + architecture)
3. `ECOSYSTEM_COMPARISON.md` §1 + §3.10–§3.15 + §4 (positioning, stages, packages, **memory-type deep dive**, novelty) when comparing to peers  
3b. **Pass #5:** `ECOSYSTEM_METHODOLOGY.md` + `ECOSYSTEM_MEMORY_TAXONOMY.md` + `ECOSYSTEM_PROMPTS.md` (staging for full prompt bodies)  
4. `ARCHITECTURE_FLOWS.md` §2 (memspine reference) + §3.0 (peer quick trace) + **§3.18** (MS×CG×EO types) + §4 + §8–§9 for flow-level diffs  
5. `memspine-structure-plan.md` §1 + §5 (tree + phase map) when implementing

## Status reconciliation (authoritative = structure plan)

`DEPENDENCY_ANALYSIS.md` was written during the first adoption pass and still lists two candidates that were **later decided against** — the structure plan reflects the final state:

- **File-native / Markdown profile** (its A6 row): **skipped** — decision **D-30**. Not a v0.1 goal.
- **CJK lexical / jieba** (its A6/§1 rows): **dropped** — decision **D-34 reversed**. No `[cjk]` extra.
- **LanceDB behind `[lance]` extra** (P1 v1.4): **superseded** — **ADR-021** moved LanceDB to core; SQLite vector fallback removed.

Second-pass decisions **D-36…D-43** (SQLAlchemy+Alembic, xxhash/fastuuid, orjson, local models, graspologic, fakeredis, MemOS patterns, prompt subsystem) live in the structure plan's Parts C–E.
