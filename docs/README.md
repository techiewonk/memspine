# memspine — design docs

The blueprint and evidence base for the engine. **The structure plan is authoritative;** if any doc disagrees with it, the plan wins.

| Doc | What it is |
|-----|------------|
| [`memspine-structure-plan.md`](./memspine-structure-plan.md) | **The buildable blueprint** — repository tree, extras matrix, locked decision register (D-01…D-43), enhancement program (E1–E9, Parts B–E), phase plan (P0–P7). Start here. |
| [`UNIMEM_V2_REWORK_PROPOSAL.md`](./UNIMEM_V2_REWORK_PROPOSAL.md) | Architecture rationale — from storage facade to cognitive engine — with the code-level evidence base. |
| [`DEPENDENCY_ANALYSIS.md`](./DEPENDENCY_ANALYSIS.md) | Code-level scan of the memory-engine ecosystem: why each dependency was chosen, adoption signal, D-26…D-42 reasoning. |
| [`PACKAGE_CATALOG.md`](./PACKAGE_CATALOG.md) | Every candidate package (564 scanned), grouped by function, with "does what". |
| [`adr/`](./adr/) | Architecture Decision Records — one file per decision (ADR-001…). |

## Reading order

1. `memspine-structure-plan.md` §0–§2 (positioning, decision register, extras matrix)
2. `UNIMEM_V2_REWORK_PROPOSAL.md` §0–§4 (why + architecture)
3. `memspine-structure-plan.md` §1 + §5 (tree + phase map) when implementing

## Status reconciliation (authoritative = structure plan)

`DEPENDENCY_ANALYSIS.md` was written during the first adoption pass and still lists two candidates that were **later decided against** — the structure plan reflects the final state:

- **File-native / Markdown profile** (its A6 row): **skipped** — decision **D-30**. Not a v0.1 goal.
- **CJK lexical / jieba** (its A6/§1 rows): **dropped** — decision **D-34 reversed**. No `[cjk]` extra.

Second-pass decisions **D-36…D-43** (SQLAlchemy+Alembic, xxhash/fastuuid, orjson, local models, graspologic, fakeredis, MemOS patterns, prompt subsystem) live in the structure plan's Parts C–E.
