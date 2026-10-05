# ADR-046 — Structured read filters: date bounds and the persons / time leg

- **Status:** proposed
- **Date:** 2026-10-06
- **Decision id:** master absorb list #36 (SM-9), #37 (GR-20)
- **Phase:** v0.3 read path · **Tier:** QW

## Context

Temporal questions ("what did Melanie do in May 2023?") are answered from the
semantic ranking alone, which has no notion of a period: a record from the right
month that is not among the most similar texts never reaches the context. Two
absorbed items address this: caller-supplied date filters on `search()` / `read()`
(GR-20, Graphiti-style search filters) and a planner-derived leg that names the
people and the period a question is about (SM-9). The vector and lexical ports
(`VectorStore.query`, `LexicalStore.search`) take no filter, and changing the storage
adapters (SQLite, LanceDB, Postgres) plus Tantivy / OpenSearch for this would touch
files other work streams own.

## Decision

1. **Date filters (#37).** `Engine.search()` and `Engine.read()` take keyword-only
   bounds `valid_from_after/_before`, `valid_to_after/_before`,
   `recorded_after/_before` and `date_filter_mode: and|or`
   (`memspine.core.read_filters.DateFilter`). `*_after` is inclusive, `*_before`
   exclusive; an open `valid_to` is later than any date. The filter is a
   **prefilter at the leg level**: one relational listing of the namespace gives the
   matching ids, every leg (vector, BM25, C3', graph, planner probes) keeps only those
   ids **before** RRF fusion, and the leg windows are widened to the namespace size so
   no matching record is cut by a window. It is never a post-hoc truncation of an
   unfiltered top-k. A cue record passes the leg filter as a key; its resolved target
   is judged after the gates. In `read()` the filter also governs the header searches,
   a `full` read's listing and replayed neighbours; the pinned persona and the lead
   section are not dated evidence. The active filter rides a `ContextVar`
   (`date_filter_scope`), like the read purpose (`read_scope`), so no private read
   helper changes signature. `POST /search` exposes the same fields.
2. **Persons / time leg (#36).** `ReadPlan` gains `persons` and `time_expr`;
   `read.planner_version: v3` selects `plan@v3` (v2 plus those two keys, still one
   call). `time_expr` becomes a `[start, end)` span by the existing deterministic
   rules (`query_interval` for absolute dates, else the H1 resolver anchored on the
   namespace's newest record, with `read.relative_week`). A structured leg ranks the
   live records in the span and/or about a person (`person:` tags when the record has
   any, else its `entity`), both first, and is fused by RRF into the lookup or compose
   read (`read.person_time_leg_k`, default 10). Every search gate still applies.

Both are off unless asked for: no bound passed, or `planner_version` other than `v3`,
gives byte-identical `read()` / `search()` output (golden
`tests/unit/golden/wave3_read_off.json`).

## Consequences

- Positive: a filter never costs recall; temporal questions get a structured signal
  at no model cost; the API matches Graphiti's search filters.
- Negative / cost: a filtered search lists the namespace once and queries each leg
  over the whole namespace (O(N) per filtered search, the same order as the existing
  C3' legs). Very large namespaces would want the filter pushed into the stores.
- Follow-up: a `where` clause on `VectorStore.query` / `LexicalStore.search`
  (LanceDB and Postgres can filter natively) would make the filter a true store-level
  prefilter; the engine API would not change.

## Alternatives rejected

- **Filter after the cut** — loses every matching record below the unfiltered top-k.
- **Store-level filter now** — touches five adapters owned by other work; deferred.
- **A separate date-parsing model call** — the H1 rules already resolve the phrases
  the planner copies, deterministically.
