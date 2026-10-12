# Consistency audit 2026-10-13 (input for the doc-reconciliation pass)

Statements in active `docs/v2/` files that contradicted the confirmed amendments (ER-06, ER-21, ER-24, ER-27, ER-29,
ER-31) and later owner decisions (I-7 graph on, OF-1 non-blocking sleep, OF-2 triggers, R-Q3 targets, ER-39 call
accounting). Paths relative to `docs/v2/`. Rows marked *historical* get a "superseded by ..." note, not a rewrite.

## ER-06 durable acceptance + per-capability readiness
- ARCHITECTURE.md:14 `Commit: event log + hot projections + outbox rows, receipt` -> `Commit: authoritative SQL rows + outbox intents (atomic); receipt with per-capability readiness`
- ARCHITECTURE.md:15 `Raw searchable at once: SQL, BM25, vector` -> `Raw indexes: SQL now; BM25/vector when ready (optional bounded wait)`
- ARCHITECTURE.md:45-46 one SQL transaction with hot projections -> "Authoritative SQL data and outbox intents commit atomically; independent indexes are projected after the commit. The receipt reports readiness per capability; callers may wait on a bounded barrier with a timeout (ER-06 A)."
- areas/write.md:34 -> "One SQL transaction holds the event, SQL-resident projections, a derivation intent and outbox rows for every non-SQL index. The receipt returns the intent id and readiness per capability."
- areas/write.md:53 `Receipt{event_ids[], intent_id, raw_searchable: bool}` -> `Receipt{event_ids[], intent_id, readiness (illustrative; fields not frozen, ER-06)}`
- areas/write.md:71-72 W-02 heading: keep, add "Superseded by ER-06 A: durable acceptance plus per-capability readiness; raw search readiness is reported, not guaranteed at return."
- pipelines/insert.md:11 "one commit that makes the raw record searchable" -> "one commit that durably accepts the raw record and persists a derivation intent"
- pipelines/insert.md:816 `raw_searchable: bool ...` -> `readiness_by_capability: map, required  illustrative; schema not frozen (ER-06)`
- pipelines/insert.md:728 "Raw searchable at return" -> "Durable at return; lexical/vector readiness reported; searchable after readiness or a bounded wait"
- areas/models.md:389-390 "raw turn searchable at commit" -> "raw turn durable at commit; time-to-searchable per index measured as an SLO"
- pipelines/README.md:20 and offline.md:816 "(raw stays searchable)" -> "(raw stays durable; readiness reported per index)"
- packages.py W2-01 gate: append "; receipt never claims an unavailable capability; crash tests at every commit/projector boundary"
- historical: DECISIONS.md:22, DECISION_CHECKLIST_2026-10-13.md:14

## ER-24 complete evidence bundles, exact budget
- areas/read.md:486 -> "An oversize bundle is omitted or replaced by a validated smaller complete bundle; it is never truncated mid-evidence (ER-24)."
- areas/read.md:494 -> "In exact mode the rendered context, headers, citations and reserves never exceed the reader-tokenizer budget; estimated mode is labelled and makes no hard guarantee."
- areas/read.md:504 append "; multilingual and long-token probes; estimated mode asserted as labelled"
- pipelines/retrieve.md:637 scenario 25 -> "omitted or replaced by a smaller complete bundle; packing continues; exact-mode total within the reader-tokenizer budget"
- packages.py R3-09a/b gates: exact-tokenizer and multilingual tests; adaptive bundle selection gets a separate quality screen

## ER-31 scoped, versioned caches, off until built
- areas/read.md:703-704 key -> "keys include effective scope, viewer/purpose, time view, policy/model versions, record versions and invalidation generations; admission rechecked at use (ER-31 B)"
- areas/read.md:57, 700: state the snapshot cache key and scope, or move result-content caches under "disabled until ER-31 keys exist"
- pipelines/retrieve.md:220 cache key -> `(encoder id + weights/config fingerprint, instruction, normalisation, text)`
- packages.py R3-15: "caching (result/context caches disabled until ER-31 keys and use-time admission tests pass)"
- historical: DECISION_EXPORT_REVIEW_2026-10-13.md:41, DECISION_CHECKLIST_2026-10-13.md:39

## ER-29 viewer required on every multi-party read
- pipelines/retrieve.md:123-124 -> `viewer: str  required when multi_party; bound from the authenticated principal`; `viewer_rule: "full" | "viewer"`
- pipelines/retrieve.md:133-134 -> "raises `ViewerRequired` (ER-29 C); unknown visibility never broadens access."
- pipelines/retrieve.md:628 -> "rejected with `ViewerRequired`; probes include legacy and derived records"
- pipelines/retrieve.md:66 (E6) append "; multi-party internal reads also bind a viewer (ER-29)"
- areas/govern.md:354 -> "A multi-party read without a viewer is rejected (ER-29 C); the viewer comes from the authenticated principal."
- areas/govern.md:369 -> "...is rejected, on REST, MCP, internal and migration paths"
- areas/read.md:66 `viewer: str | None = None` -> `viewer: bound from handle principal; required in multi-party namespaces`
- packages.py F-16c title -> "viewer required on every multi-party read (ER-29 C), bound to the authenticated principal"
- pipelines/README.md R-Q5 row and Layers section: annotate superseded by ER-29 C
- historical: retrieve.md Q-R5 and R-Q5 rows, data/owner_followup_2026-10-13.json R-Q5, DECISION_CHECKLIST_2026-10-13.md:37

## ER-21 lift, re-admit, dedupe, cap, then rerank
- ARCHITECTURE.md:29-30 mermaid -> `FU --> LIFT[Lift to turns, re-admit, dedupe, cap ≤64] --> RR[Jina rerank] --> GATE[Relevance gate]`
- ARCHITECTURE.md:105-109 reorder: "Lift, re-admit, dedupe, cap -> Rerank -> gate -> windows -> pack"
- areas/read.md:47-48 swap to "7 Lift (re-admit, dedupe, cap)" and "8 Rerank"
- pipelines/retrieve.md:333-336 add "then re-capped to the tier pool size (≤64, R-Q4) with family coverage preserved; unscored entries kept distinct from scored zero"
- pipelines/retrieve.md:340-342 -> "Settled by R-Q1 and ER-21 B: lift, re-admit, dedupe, cap, then rerank."

## ER-27 bounded retention, limited rebuild
- ARCHITECTURE.md:63 and areas/model.md:36 append "; rebuild covers retained events only, per retention profile (ER-27 C)"
- ARCHITECTURE.md:144 -> "...by replaying the retained event log; rolling-log stores advertise limited rebuild"
- areas/govern.md:413 -> "migration by replay is possible for full-log stores; rolling/ephemeral stores fail clearly for pruned history"
- pipelines/offline.md:615 (S18) add "records the retained source/event/tombstone/lineage boundary; capability limits advertised"
- pipelines/insert.md:755 and VERIFY.md:31 "rebuild from the log" -> "from the retained log"

## Other superseded shorthand
- ARCHITECTURE.md:65-66 and areas/model.md:37 -> "L0 always; L1 and L3 when a worker model is configured; L2, L4, L5 opt-in (I-7)"
- areas/model.md:356 -> "Graph is L3, on by default with a worker model (I-7)."
- pipelines/SPEC_BRIEF.md:11 -> "(L3 on with a worker model; L2, L4, L5 opt-in; I-7)"
- README.md:21 -> "L1 and L3 on only when a worker model is configured; no generative call without one"
- ARCHITECTURE.md:85-86, areas/offline.md:36, 87 -> "(N deposits AND M idle) OR context pressure C, both behind gap K; manual `sleep()` is non-blocking and returns a job handle (OF-1, OF-2)"
- pipelines/offline.md:64 (E5) append "; enqueues and returns a handle; at most one follow-up per namespace (OF-1)"
- pipelines/offline.md:651 scenario 5 -> "manual coalesces onto the pending follow-up handle; `handle.wait()` blocks (OF-1)"
- ARCHITECTURE.md:122, ROADMAP.md:45, areas/models.md:60, 389 -> "fast-tier read p95: 300 ms without rerank (CPU), 350 ms with Jina (GPU), stated hardware/concurrency (R-Q3, ER-39)"
- pipelines/retrieve.md:14 -> "makes no generative call; embedding and rerank inference are counted separately (ER-39)"
- packages.py R3-01 gate -> "Fast tier zero generative calls; embed/rerank cost reported separately"
- historical: retrieve.md:768-772 blocking-sleep option; DECISIONS.md:53, 84
- insert.md:947/986 (I-5 "searchable at once"): add "subject to readiness (ER-06)"
- DECISION_CHECKLIST_2026-10-13.md: add a header that `data/decisions_reconciled_2026-10-13.json` supersedes it
