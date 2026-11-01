# Decision waves (owner decision rounds, 2026-11-01)

Plan only. **Statuses live in [PROVIDER_TODO.md](PROVIDER_TODO.md)**: this file names the work and its order.
Decisions: ADR-087 to ADR-096 (register D-111 to D-120).

## Owner decisions

| Round | Topic | Decision | ADR |
|---|---|---|---|
| 1 | Verification records | Commit + tree hash + clean tree; refuse dirty | 087 |
| 1 | Baseline gate | Local gate (lint, unit, integration, combinations, evals/tests); live gates separate | 087 |
| 1 | Flakes | Explicit quarantine list naming a tracker row; timing tests behind `perf` marker | 087 |
| 1 | Freeze point | After these waves; again at release | 087 |
| 2 | Evals (phase D) | Reopen: one-time rewrite of own arm files; history untouched | 096 |
| 2 | Claim schema | SemanticEnvelope canonical; AssertionEnvelope removed | 088 |
| 2 | Temporal | Activity drains the outbox; outbox stays the truth | 091 |
| 2 | Naive time | Civil text, no guessed zone; `read.naive_timezone` removed | 090 |
| 3 | Windows | Keyed per (namespace, conversation/session) | 090 |
| 3 | Message fields | Strict, plus a verbatim `metadata` bag | 090 |
| 3 | Unresolved candidates | Persist as source-bound unasserted annotations | 090 |
| 3 | Entity ids | Opaque ids + name index; `surface:` prefix | 089 |
| 4 | Auto-link | Unique exact name/alias + compatible type in namespace | 089 |
| 4 | Budget | Hard cap; truncate at a sentence boundary; `oversize` status | 092 |
| 4 | Packing | Bundle-turns config + dependency manifest + packer parity test | 092 |
| 4 | Reranker | MiniLM cross-encoder default in base/simple | 093 |
| 5 | Missing weights | `memspine models fetch` + MissingServiceError | 093 |
| 5 | Timing host | This dev machine, controlled protocol | 096 |
| 5 | PA-053 budgets | Measure first, then propose with headroom | 096 |
| 5 | Model path | Bring `window_extract` to parity | 090 |
| 6 | Push | Standing: push restructure after each green wave | 096 |
| 6 | Release | PR after phase C; tag + manual PyPI approval later | 096 |
| 6 | Write path | One durable path for `Engine.write` | 090 |
| 6 | Erasure | Gate across every copy class | 095 |
| 7 | Domains | Per-family domain from a fixed generic set | 094 |
| 7 | Families | Activate per family after fixtures pass | 094 |
| 7 | RM-66 | Close as not applicable; keep the tool | 094 |
| 7 | RS-05 | Local held-out natural set (~200 messages), no paid runs | 096 |
| 8 | CPU envelope | Run now on the reference host (threads, batch, int8 parity) | 093 |
| 8 | RS-07 | Span-dedup experiment only | 092 |
| 8 | Deploy checks | Local docker compose stack; real cluster rows stay open | 096 |
| 8 | Execution | Waves with parallel worktree agents | — |

## Waves

Each wave: build in worktrees (one stream per agent), merge, run chunked tests, commit, push.

| Wave | Streams (parallel) | Tracker rows |
|---|---|---|
| 1 Tooling + evidence | Revision-bound `record_verification_run.py`, quarantine list + `perf` marker; lock upgrade + ladybug 0.22 trial; RM-122 close | RM-128, PA-113, RM-122 |
| 2 Schema | SemanticEnvelope canonical (RM-24, RM-57, DS-19), merged first; opaque entity ids + auto-link + `surface:` prefix (DS-10, RM-34, RM-127), rebased on it; `memspine models fetch` + CI cache (moved from wave 4) | RM-129, RM-130, RM-134 (fetch part) |
| 3 Ingestion | One durable write path (DS-05); session-keyed windows + draft versions (DS-06, DS-07); strict messages + civil time (DS-08, DS-09); unresolved annotations (RM-42); Temporal outbox Activity (DS-17) | RM-131, RM-132 |
| 4 Read/compose + models | Hard budget + bundle manifest + parity (DS-13..DS-15); MiniLM default + `models fetch` (RS-09, RM-44); model-path parity (DS-04, DS-11, DS-12, DS-18) | RM-133, RM-134 |
| 5 Fixtures + evals + erasure | F1..F12 fixtures (RM-22, RM-54); family domains + activation (RM-107, RM-62, RM-63); eval rewrite (RM-19..RM-21, PA-107); erasure gate (DS-16) | RM-135 |
| 6 Perf + services | CPU envelope (RM-25, RM-26, RS-06); PA-053 measurement; compose stack + RM-16/RM-31; RS-05 set; RS-07 dedup | RM-136 |
| 7 Freeze | Phase C recorded run (RM-15, PA-039); PR packet (PA-095) | — |

## Direction review amendments (2026-11-01)

ADR-088, 089, 090, 092, 093 and 095 carry amendments: kept claim vocabularies, event-logged entity ids, provisional unique-name links, `write.stamp_default_tz` removed, reranker default only after the fetch command, the CI cache and measurement exist, and more erasure copy classes. Merge order is RM-129, then RM-130, then RM-131. BI-04 is reopened.
