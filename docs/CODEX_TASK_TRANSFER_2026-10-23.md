# Task transfer to Codex (2026-10-23)

This note suggests what Codex picks up next after the 2026-10-23 Claude session, in what
order, and how to verify it. It holds no status:

- **Status:** every status, its evidence and the authoritative "next" text are in the matching
  row of [PROVIDER_TODO.md](PROVIDER_TODO.md), so read that row before starting.
- **Environment, rules and pitfalls:** see
  [CODEX_SESSION_TRANSFER_2026-10-23.md](CODEX_SESSION_TRANSFER_2026-10-23.md).
- **Updating:** when you finish or change a task, update its register row (status, evidence,
  next) and refresh the tracker. Do not track it here.

## A. Ready now: no owner input needed

| # | Register ID | Task | Scope and files | Done when |
|---|---|---|---|---|
| T-01 | RM-15, PA-039 | Recorded chunked full unit run on the committed revision | `scripts/record_verification_run.py`. Its prerequisite (RM-02 uv upgrade) is done, so RM-15 is no longer blocked; set its status when you start. | The run is recorded with revision, commands, counts and skips. The 7 known `cashews` integration failures are noted, or the `[cache]` extra is installed. |
| T-02 | RM-121 | Flaky `test_rm110_graph_seeds::test_engine_search_identical_with_cache_and_scan` | `tests/unit/wiring/test_rm110_graph_seeds.py`, `memories/associative/entity_cache.py`, the read graph leg. It fails about 1 run in 3 when run alone, both before and after RM-96. | The failing diff is captured. The tie or timing source is fixed, by making the order deterministic or the comparison order-insensitive where order is not part of the contract. 20 consecutive runs pass. |
| T-03 | RM-122 | LadybugDB `Engine.search` with `graph_filter` is at 50.3 ms p95 against a 50 ms budget | `services/graph/ladybug.py` (`_frontier_rows`, `frontier_edges`), `read/pipeline/legs.py` (seed frontier fetch), `scripts/benchmark_graph_filter.py`, `docs/provider_verification/rm96-single-query-walks-2026-10-23.json` | Re-measured on a quiet host with repetitions. If still over budget, cache or batch the seed frontier, or overlap it with the other legs. The equivalence tests (`test_rm96_pushdown_walk.py`) stay green. |
| T-04 | RM-113 | The rest of the connector, adapter and factory layering | (1) 6 kept lines in `tests/unit/baselines/connection_constructors.txt`: `store/leases.py` and `store/side_tables.py` path mode (they need a lent engine plus a rewrite of about 30 wiring tests), and the `observability/tap_report.py` / `tap_sinks.py` sqlite sink (register it from the composition layer with a lent connector, or get a reviewed contract change). (2) Item 6: share the taskiq Redis client across Engines through the ADR-079 registry. (3) Load the eval asset pack (`evals/precompute_assets.py`, `assets.db` + `documents/`) into each eval item's store. | The baseline lines are removed in the same commits as their fixes; `ci_importlinter.py` stays 18/0; the standard doc section 2a is updated. |
| T-05 | RM-111 | Close out the storage-slot implementation | The row's next column still lists the WP4/WP7 remainder. The asset-registry move is done (RM-113 item 18), and the mirror gate is gone (RM-120). Check each WP in `docs/STORAGE_SLOTS_MIGRATION_PLAN.md` against the code. | The row is set to DONE with evidence, or the true remainder is listed precisely. |
| T-06 | RM-118 (c) | GLiNER read decider and non-GLiNER route deciders, if wanted | `providers/decider.py`, `services/decider/*`. Both are rejected at start today. | Only if the owner selects it: ask before starting. |
| T-07 | RM-02, PA-113 | Hugging Face Hub 2.x | An agent was working on this at hand-off, on branch `worktree-agent-ae9c7e9f093e6056b`. Raise or remove the `[decider]` `huggingface-hub<2` cap, then run `uv lock`, check the offline/cache behaviour of `clients/weights/*` and run the regression. | `audit_dependencies.py --check` passes and `PYTHON313_DEPENDENCIES.md` is updated. If a dependency blocks the upgrade, its name and version are documented. |

## B. Needs an owner decision first (ask; do not assume)

| Register ID | Question for the owner |
|---|---|
| PA-107 (+ RM-19, RM-20, RM-21) | Resume the eval migration? 181 flat eval arms (`storage: {path: ...}`, old model keys) cause most of the 187 eval failures. The ADR-081 and ADR-082 codemods (`scripts/migrate_config_keys_adr081.py`, `..._adr082.py`) do most of it; immutable historical results must be preserved. The register asks to resume early, starting with `v2-PICK3-j.json`. |
| RM-107 | Do the built-in vocabulary packs get a top domain? |
| RM-62, RM-63 | Confirm the proposed predicate-family names, then move them into the default pack. |
| RM-66 | The owner runs the learned.alias audit on each multi-namespace SQLite log. |
| PA-051, PA-053 | Choose a controlled timing host, plus the foreground p95 and derivation-lag budgets. |
| RM-16, RM-25, RM-26, RM-27 | Select scope: live Temporal gate, HF cache repair and batching rerun, CPU model envelope, deployment drills. |
| PA-095, PA-096 | Branch review and PR packet, and the release or push workflow. Today pushes go only to `origin/restructure`, on request. |

## C. Semantic and background-ingestion programme (large; follow the plans, in order)

The owner asked to **leave the blocked items until the BI build**. These are RM-52
enforcement, RM-55 merge edges and lifecycle from the envelope.

| Order | Register IDs | Read first |
|---|---|---|
| 1 | BI-01, RM-22, RM-23, RM-24, RS-08 (contracts, registry, F1..F5 fixtures, Q-S1-B cutover) | `docs/BACKGROUND_INGESTION_PLAN.md`, `docs/ENRICHMENT_SCHEMA_CONTRACTS.md`, ADR-077/078 |
| 2 | BI-02 .. BI-04 (raw acceptance, durable discovery, window formation), DS-05, DS-06, DS-07, DS-09 | same |
| 3 | BI-05, BI-06 (grounded mentions and facts), DS-03, DS-04, DS-08, DS-10, DS-11, DS-13, DS-18, DS-19, RM-34, RM-42, RM-45, RM-47, RM-48 | ADR-072/076/078 |
| 4 | BI-07 .. BI-09 (fenced commit, mention graph projection, readiness), RM-41 (qualifiers in BI-08), RM-43, RM-46, RM-52, RM-55, RM-57, DS-16, DS-17 | ADR-080 |
| 5 | BI-10 .. BI-12 (compression, summaries, integration), RM-44, RM-49, RM-50, RM-51, RM-54, RM-59, RM-60, DS-12, DS-14, DS-15, DS-20, RS-05, RS-06, RS-07, RS-09 | — |
| any time | RM-36 (reply inheriting the parent session) | the row's selected answer |

## D. Needs a real deployment (cannot be done locally)

PA-056 .. PA-073, PA-082, RM-27, RM-30 (release time) and RM-31. Start only when the owner
names the deployment (PA-056).

## E. Deferred by the owner

PA-088 (no paid evaluation budget), RM-79 (shared local model server), PA-109 (llama-cpp-python
Windows toolchain).

## Verification before you report a task done

1. The focused tests for the change, then the chunked regression (session transfer, section 3).
2. For evals: compare failure sets against `d9a87db6`, with no new failures.
3. `python scripts/ci_importlinter.py` shows 18 kept / 0 broken. ruff and `mypy --strict` are
   clean on the touched files.
4. Generated files are regenerated with their scripts. The tracker row is updated, then
   `scripts/refresh_tracker.py` and `--check`.
5. Commit with no trailer lines. Push only when the owner asks.
