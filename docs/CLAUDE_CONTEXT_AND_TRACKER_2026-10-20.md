# MemSpine: Claude project context and engineering handoff

## Follow-up verification: 2026-10-21

The selected DS-01, DS-02 and DS-21 fixes now pass the combined focused gate: **185 passed, no failures or skips** (10 existing LanceDB cleanup warnings). [Recorded verification](provider_verification/ds-fixes-final-state-2026-10-21.json) links the test log, JUnit report and isolated before/after probes. This is a local targeted gate, not full-suite, model, deployment or complete BI-package acceptance.

Staged unaccepted jobs now survive enqueue errors/cancellation; direct readiness waits apply a cooperative deadline around inline work; English rules suppress unsupported conditional/reported/uncertain assertions. Cancellation waits for enqueue acceptance to settle, unknown durable acceptance remains at-least-once, blocking providers need native deadlines, and rule filtering does not implement unresolved annotation storage. BI-01..12 remain planned. Original review observations below describe the recorded baseline.

All current dispositions, completed work and next milestones live only in [PROVIDER_TODO.md](PROVIDER_TODO.md), shared by Claude and Codex. This document is context and evidence.

Prepared 2026-10-20 for handoff to Claude. Repository: `C:/Users/hembad/Documents/GitHub/memspine-restructure`, branch `restructure`. Recorded implementation baseline: `e6596f44`. The current working tree contains later research/documentation and other changes; this document is not a claim that everything is committed or newly code-audited.

## Subsequent code review - 2026-10-21

Read [the current deep review](DEEP_SCAN_REVIEW_2026-10-21.md) alongside this handoff. It adds21 evidence-classified DS findings/gates, including reproduced staged-work loss, a low-level wait deadline issue and unsafe conditional/reported rule candidates. These describe baseline implementation risks; see the follow-up verification above for the three targeted fixes. This dated handoff remains policy context. Provider task gates and planned BI packages were not advanced by these fixes.

## Read this first

The provider foundation and selected local GLiNER/memory-type gates exist. The new durable, non-generative background-enrichment program **BI-01..BI-12 is still PLANNED**. Schemas, policies, fixtures and kernel benchmarks are not the integrated runtime. DONE means the specific recorded gate passed; it does not mean all providers, all memory types and production deployments were exhaustively verified.

The original 2026-10-20 handoff consolidated existing records without new runtime tests or model inference. The 2026-10-21 follow-up above records the subsequent focused checks. The five examples below are fictional discussion fixtures, not engine outputs. For every current task status, completed result, owner and next milestone, use [the single live register](PROVIDER_TODO.md). This handoff is context only and must not maintain a copied status inventory.

## 1. Product goal and engineering constraints

Build a generic memory engine that keeps original information recoverable, extracts grounded information in the background, and retrieves compact, complete evidence. Improvements must generalise beyond LoCoMo and must not introduce benchmark-specific names, aliases or answer rules.

| Constraint | Accepted direction |
|---|---|
| Ingestion | One configurable `eng.ingest_messages(...)` path; no parallel intake_v2 or compatibility writer |
| Compatibility | Replace affected contracts directly; no backward-compatibility aliases |
| Generative models | No generative ingestion calls or hidden fallback in the selected target profile; future generative enhancement is a separate explicit selection |
| Non-generative models | BM25, static/contextual embeddings, GLiNER and cross-encoder reranking are allowed engineering tools, not proof of factual truth |
| Raw source | Recoverable original content with lossless compression where selected; summaries stay separate |
| Persistence | Canonical raw/derived records are authoritative; lexical/vector/graph surfaces are projections with separate readiness |
| Identity and time | Preserve source identity, historical time, exact spans, temporal precision and when later evidence became knowable |
| Uncertainty | Store unresolved annotations/reasons/candidates; never guess a fact to fill the schema |
| Scope | Namespace/session/conversation, sender, viewer permissions and source versions constrain every lookup/commit |
| Provider choice | Same configurable approach for storage, lexical, vector, graph, embedding, model and other services; per-provider settings/defaults, named model/task profiles |
| Graph | Controlled replaceability including Ladybug/Neo4j; mention links never imply asserted factual relationships |
| Orchestration | Temporal macro orchestration; embedded Resonate micro execution with application-owned durable journal; DBOS removed from selected maintenance runtime |
| Recovery caveat | Resonate in-process memory is not an embedded SQLite checkpoint; local journal recovery on another worker requires accessible durable storage |
| Work limits | Bounded candidates, jobs, windows and reconciliation; excess accepted work is durable and visibly deferred |
| Verification | Focused checks and kernel benchmarks; no unsolicited whole-engine suite, paid eval or deployment |
| Excluded | Database copying, historical re-embedding and implicit historical backfill |

Existing optional engine generation capabilities are not deleted by this documentation update. They must not become a hidden requirement of the selected non-generative ingestion profile.

## 2. Message contract decisions already made

| Topic | Decision |
|---|---|
| Grouping | Namespace -> session -> conversation -> messages; conversations belong to sessions |
| IDs | Preserve supplied message/session IDs; generate missing ones; message IDs are unique within a namespace |
| Conversations | Use an explicit ID; otherwise inherit through reply_to; independent roots start separate conversations |
| Reply parents | Parent is stored already or earlier in the batch; reject unresolved or inconsistent scope/session/conversation references |
| Timestamp names | Use created_at and updated_at; not sent_at/source_created_at aliases. Engine ingested_at is distinct |
| Historical input | Preserve supplied source timestamps; generate created_at only when absent |
| Sender | role plus sender.id, sender.kind and optional display name; identity is not inferred from a display name |
| Retry | Same message ID with identical supplied data returns the existing result; changed data is rejected until explicit revision support |
| Schema rewrite | Q-S1 B: coordinated replacement of affected envelopes; no second writer |
| Time representation | Q-S2 B: aware datetimes for resolved temporal values; unsupported timezone-free civil dates remain unresolved text, not invented UTC instants |
| Claim values | Q-S3 A: tagged typed values with versioned predicate policies |
| Domain events | Q-S4 A: explicit source-backed occurrences, separate IDs from event-log entries |
| Evidence copies | Q-S5 B: full copied excerpts per durable bundle, with reverse lineage and cascade erasure |

Exact target schemas are review artifacts in [ENRICHMENT_SCHEMA_CONTRACTS.md](ENRICHMENT_SCHEMA_CONTRACTS.md), not already substituted runtime types.

## 3. Context-enrichment decisions to preserve

| Area | Accepted behaviour |
|---|---|
| First person | Bind I/me to sender; mentioned people remain separate |
| Named entities | Stable IDs or explicit cited identity/alias evidence; same names are not merges |
| Pronouns/groups | Unique evidence-bound local/reply reference only; do not assume all chat participants belong to we |
| Locations | Use explicit conversation evidence; ambiguous Cambridge remains unresolved |
| Residence | Visit/work/plan is not residence; supported transitions preserve history and possible multiple homes |
| Corrections | Explicit or uniquely supported target; preserve corrected claim; do not invent a move |
| Conflicts | Preserve competing source-bound claims; supersede only on supported correction/transition authority |
| Later evidence | Bounded retrospective linking with both citations and known-time history; no future-evidence leak into earlier views |
| Dates | Reliable source-local anchor, preserve precision, unique event anchors only; ambiguity remains unresolved |
| Counts | Distinct scoped occurrences; repeated retellings are not new events; report lower bounds unless completeness is supported |
| External catalogues | Optional attributed/versioned local catalogues after validation; no automatic web/generative fallback |
| Language | Tested English semantic rules first; preserve other languages and language-independent IDs/links |
| Graph | Source-bound mention occurrences are unasserted; semantic relationships require independent grounding |
| Ontology | 43 predicates /9 attribute keys accepted as starter vocabulary; extraction forms remain gated. Additional 10 predicates /35 keys are proposals |

The explanatory raw/grounded/contextual layers in the chat are not replacement names for the nine registered memory types.

## 4. What has actually been completed

| Work | Recorded result | Boundary / evidence |
|---|---|---|
| Provider architecture | 136-ID inventory; 103 DONE,5 OPEN plus separate disposition categories | Local gates only; live inventory is in PROVIDER_TODO.md |
| Provider configuration | Registry/catalog, settings, ownership, controlled changes and selected remote adapter contracts | Specific selected configurations, not a complete backend Cartesian product |
| Temporal/Resonate maintenance replacement | DBOS replaced; selected local journal and live retry/reopen gates recorded | Ingestion transport integration and production volume failover remain separate |
| GLiNER PA-118..123 | Shared ownership, exact grounding, typed candidates, real Python3.13 ingestion, Small/Base comparison and offline/revision checks | These building blocks do not complete BI orchestration |
| Skip guard work | Six selected guard defects repaired; focused result recorded as80 passed /2 legitimate Temporal-address skips | Not a newly run full suite; optional/deferred gates remain |
| Existing memory checks | PA-075..081 and083 DONE for selected gates | Prospective PA-082 is DEPLOYMENT_CHECK; new enrichment is not covered by those old gates |
| LoCoMo review | Structural inventory of1,986 questions; source/evidence review records;52 generic requirements | No measured non-generative answer accuracy or complete image adjudication |
| Schema research |41 definitions,15 positive examples,12 negative checks and52 coverage mappings recorded | Shape validation, not runtime behaviour completion |
| Vocabulary design |43/9 accepted starters and proposed10/35 extensions; generic registry/pack guide | Registration does not create safe extraction patterns |
| CPU model research | Metadata/package checks and role-specific cached-model probes | No global best-in-class claim; model selection remains conditional |
|48-candidate experiment | Six methods,64/128/256-token synthetic messages, batch16,2K/3K budgets |16 unique query templates; required sources supplied in each pool |
| Packing validation |1,728 outcomes replayed; exact source spans/token lengths, dependencies and costs checked | Correct links/spans supplied; no automatic-trimmer or final-answer validation |

### Nine memory types: keep their roles distinct

| Type | Role | Current recorded acceptance |
|---|---|---|
| Semantic | Supported facts/state/claims | PA-075 DONE local gate |
| Working | Active context/overflow | PA-076 DONE local gate |
| Episodic | Messages, chronology and sessions | PA-077 DONE local gate |
| Resource | Documents/chunks and lineage | PA-078 DONE local gate |
| Procedural | Skills/plans/lessons and promotion | PA-079 DONE local gate |
| Reflective | Parent-grounded reflections with trust/depth restrictions | PA-080 DONE local gate |
| Associative | Graph links/traversal and entity relationships | PA-081 DONE local gate |
| Prospective | Watches/due work | PA-082 DEPLOYMENT_CHECK |
| Shared | Grants/revocation and scoped visibility | PA-083 DONE local gate |

Detailed provider seams: [MEMORY_TYPE_PROVIDER_MATRIX.md](MEMORY_TYPE_PROVIDER_MATRIX.md). Its stale proposed labels were reconciled for this handoff; no tests were rerun to relabel them.

## 5. Target flow and storage meaning

```text
Receive/validate -> resolve IDs, replies, session/conversation -> policy/provenance
 -> durable acceptance + required-work manifest -> recoverable raw source
 -> configured raw projections + receipt with per-layer status
 -> durable discovery/window snapshots -> fenced background job
 -> scoped cited context candidates -> entities/references/time/roles/polarity
 -> rules + optional non-generative extraction -> typed grounding validation
 -> canonical annotation/claim/event effects -> graph/search projections
 -> readiness; bounded reconciliation on later evidence
 -> separately selected summaries/episodes; lossless source recovery remains available
```

Raw acceptance must survive downstream index/model failure. Canonical derived records, graph mentions, semantic graph relationships and search projections are separate surfaces. No distributed atomicity across SQL and every external index is assumed. Required work needs durable discovery/replay, stable effect identities, source-version/erasure checks and fenced commits.

## 6. Five discussion fixtures, not executed runtime results

| Fixture | Input | Grounded/second-layer expectation | Must not infer |
|---|---|---|---|
| F1 Move | Asha,2026-10-01+05:30: I moved from Porto to Lisbon last month; prior supported Porto residence | Source-bound completed move, origin/destination, September precision; supported residence history | Exact move day, citizenship, only one home |
| F2 Ambiguity | I met Alex in Cambridge yesterday. He gave me a book; two Alex identities exist | I/me binds sender; unique local pronoun may bind the Alex mention while canonical Alex/place stay unresolved | Guess an Alex, city or known book from popularity/similarity |
| F3 Correction | I live in Porto; five minutes later explicit reply: Correction, I meant Lisbon | Original raw claim retained, corrected status and replacement with known-time link | Porto-to-Lisbon relocation or deletion of old source |
| F4 Modality | Agent: You could move to Lisbon. User: No, I am not moving. I visited last weekend | Agent proposal, scoped rejection and separate visit; residence unchanged | User-confirmed move plan or permanent never-moving trait |
| F5 Later evidence | June: moved from my home country four years ago; July explicitly identifies the country in that message as Sweden | Same approximate2022 event enriched with two-source binding; June-known view remains unresolved | Exact day, duplicate move or July knowledge leaking into June |

These still need executable behavioural fixtures, exact schema representations, ambiguous/negative variants, event identity checks and source-erasure cases. Temporal examples must obey the selected aware-datetime/precision policy. Do not treat fictional fixture text as a benchmark-specific extraction rule.

## 7. Numeric policy and benchmark-based proposals

Accepted starting design:6 turns/1,500 estimated tokens/120s idle;96 fused candidates/128 after expansion; optional rerank48 in batches16; up to12 bundles; one background worker; reconciliation100; evidence4,096 tokens. Full starting values are in ENRICHMENT_OPERATING_POLICY.md and operating_defaults.json; most are not deployment SLOs.

Erasure: immediate tombstone read denial; up to2s wait before returning a durable cleanup operation with per-surface status. Do not claim physical deletion from timeout or exists=false alone. Development backups off; production content backup retention at most7days with tombstones applied before restored data serves. Copied durable excerpts and derivative copies require cascade accounting.

At 256-token messages, measured median query ranking and complete linked/full evidence:

| Method | Rank48 median ms |2K complete /16 |3K complete /16 |
|---|---:|---:|---:|
| BM25 small reference kernel |0.043 |14 |16 |
| Model2Vec8M, cached document vectors |0.056 |14 |16 |
| Model2Vec32M, cached document vectors |0.062 |14 |16 |
| MiniLM sentence encoder, cached document vectors |3.299 |8 |13 |
| TinyBERT cross-encoder |67.032 |15 |16 |
| MiniLM cross-encoder |1149.564 |14 |16 |

Recommendations, not accepted runtime switches: prefer3K for full-message evidence in this comparison profile;2K needs explicit incomplete status or separately validated compact selection. Keep TinyBERT optional; do not mandate MiniLM based on the earlier eight short cases. Model2Vec8M is cheap candidate matching, not factual validation.32M improved first-source order on the long fixture but not3K completeness. GLiNER2.5 Small is the proposed entity candidate profile; GLiClass remains unselected after poor label sensitivity.

Measurements use a common pinned BERT tokenizer, evidence-only caps, high-end Ryzen9950X3D/four threads, synthetic padding, guaranteed required sources and annotated links/spans. Model2Vec indexing costs are excluded from warm query scoring. They do not measure corpus retrieval recall, deployment contention, reader answer quality or automatic linking/trimming. Accepted4K is unchanged. See CPU_EVIDENCE_BUDGET_BENCHMARK.md and CPU_NON_LLM_BENCHMARK_RESULTS.md.

## 8. Immediate implementation sequence and remaining choices

1. BI-01: reconcile exact runtime contracts, predicate policies, task profiles, status/receipt shape and additional retry settings. Preserve Q-S1..5 and accepted operating choices; do not reopen them as if undecided.
2. Turn F1..F5 into schema/behaviour fixtures with explicit unresolved/negative variants. These are needed to validate meaning, not just JSON shape.
3. BI-02/03: preserve accepted source/work and prove durable discovery before background activation.
4. BI-04/05/06/07: isolated windows, persisted mentions, non-generative grounding and fenced effects; source deletion must win races.
5. BI-08/09/10: graph occurrences, readiness and recoverable compression; then focused BI-12 integration. BI-11 remains separately selected work, not a hidden generative dependency.

Remaining proposals: extra10/35 vocabulary entries, exact unresolved/provisional entity representation, optional model selection/promotion,2K/3K comparison profiles, automatic span trimming, obligation-aware packing, token-cost caching and read-time span deduplication. Do not change durable copied-excerpt/erasure semantics to implement a read optimisation.

Full-suite/release/deployment work remains separate from focused ingestion/kernel checks. PA-107 eval migration remains deferred. Source/evidence links describe previous gates, not current execution or a running agent. This handoff does not authorise external messaging, publishing or deployment.

## 9. Reconciliation performed for this handoff

| Change | Outcome |
|---|---|
| Background plan diagram/BI-06/model bindings/recovery/checklist | Removed mandatory generative extraction from the target profile; retained separate future explicit enhancement boundary |
| Receipt wording | Requires one reviewed receipt contract; no implicit backward-compatibility obligation |
| Memory-type matrix | Matched PA-075..083 and PA-118 status labels to current provider tracker |
| Provider tracker | Added a current handoff entry; stable IDs/statuses unchanged |
| Verification | Inventory counts/uniqueness, all 12 BI rows, source links and stale mandatory-generative wording checked; no runtime tests rerun |

Supporting sources: PROVIDER_TODO.md, BACKGROUND_INGESTION_PLAN.md, INGESTION_CONTEXT_ENRICHMENT.md, NON_LLM_GAP_REGISTER.md, ENRICHMENT_SCHEMA_CONTRACTS.md, GENERIC_PREDICATE_ATTRIBUTE_CATALOG.md, SEMANTIC_REGISTRY_STRUCTURE_AND_USAGE.md, ENRICHMENT_OPERATING_POLICY.md, GLINER_SHARED_RUNTIME.md, GLINER_INGESTION_ACCEPTANCE.md, TEST_SKIP_REVIEW.md, TEMPORAL_RESONATE_CHANGE_DIRECTIVE.md and the two CPU reports.


## Task-status authority

**Both Claude and Codex update only [PROVIDER_TODO.md](PROVIDER_TODO.md).** It contains the PA, BI, DS, research and imported roadmap tasks in one current register. Each row includes current status, completed work/evidence, owner, next milestone and dependencies. This handoff describes decisions and historical evidence; it is not a second task-status store.

The duplicated provider appendix and JSON status snapshot have been removed. The old roadmap is a reference note whose checklist was imported into the live register. The optional docs/v2/tracker.html is a read-only generated view, refreshed with `python scripts/refresh_tracker.py`; it has no separate editable state. Source plans and audit logs remain useful evidence, not competing status authority.

## BI-01 preparation follow-up — 2026-10-21

See [contract rewrite preparation](BI01_CONTRACT_REWRITE_PLAN.md) for the owner-requested contract review, five remaining schema questions, synthetic fixture specifications and cutover impact map. Questions require owner answers before schema/runtime implementation; accepted Q-S/O decisions remain fixed. Focused preparation evidence is in [the verification artifact](provider_verification/bi01-preparation-2026-10-21.json). All current task states and next milestones remain solely in [PROVIDER_TODO.md](PROVIDER_TODO.md). Earlier all-PLANNED statements above describe their recorded dates.

## Owner selections and semantic foundation — 2026-10-22

All BI schema questions are answered: Q1 A, Q2 B (53 predicates/44 keys), Q3 refined A (shared typed roles for claims/events), Q4 B (cancel/requeue on effective policy changes), Q5 B (embedding/vector mandatory for public ingestion profiles). These supersede earlier 43/9 and optional SQL-only recommendations. The [BI-01 plan](BI01_CONTRACT_REWRITE_PLAN.md) records invariants and examples. The pure registry foundation passes 23 focused tests plus Ruff/mypy; it does not implement RoleBinding persistence, behavioural extraction, policy-change cancellation or the full envelope cutover. Use the [single tracker](PROVIDER_TODO.md) for all live statuses and [foundation verification](provider_verification/bi01-semantics-final-state-2026-10-22.json) for the bounded gate.

## Generic-memory review and selected refinements — 2026-10-22

The attached review has been reconciled in [the BI-01 plan](BI01_CONTRACT_REWRITE_PLAN.md). It adds source-origin, cardinality, symmetry/event identity, general roles, nine-memory cutover and broader fixture gates; see the single tracker RM-42..RM-56 for current ownership and status. Owner selected RM-36 A (stored-parent inheritance), RM-42 A (unmapped annotations), RM-43 A (explicit scoped unfinished-work requeue), RM-55 B (grant-authorised identity merges). Identity still requires stable/cited evidence; proposed merges are grant-scoped and reversible on revocation. Earlier separate-authority and raw/mentions-only alternatives are superseded. No runtime changes were made by this review. Focused verification is in provider_verification/bi01-generic-review-2026-10-22.json; it does not certify the expanded features implemented.
