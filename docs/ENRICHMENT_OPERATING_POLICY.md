# Enrichment budgets, predicate vocabulary and copied-data erasure

## Owner selections — 2026-10-21 (supersedes earlier alternatives)

BI-Q1 A: unresolved source-bound mentions retain null canonical entity IDs and cited candidates/reasons. BI-Q2 B: adopt all **53 predicates and 44 property keys** in the coordinated rewrite; registration remains separate from tested extraction activation. BI-Q3 refined A: one versioned RoleSpec/RoleBinding contract is shared by events and claims; use event participants for occurrences and typed claim roles for non-event comparisons. No arbitrary qualifier dictionary or fabricated comparison event. BI-Q4 B: every effective ingestion-policy revision cancels/supersedes pending and in-flight old-revision work and requeues eligible sources under the latest revision. Cooperative cancellation alone is insufficient: commit fencing rejects old generations even if a model call finishes late. Preserve completed history; do not undo accepted effects or re-embed/copy historical data automatically. Current erasure/permission checks always apply. BI-Q5 B: every supported ingestion profile requires configured compatible embedding and vector capabilities; SQL-only ingestion is not a supported public profile. Lexical remains independently configurable and no generative LLM is required.

Selections are design acceptance, not completed runtime integration. Requeue must be durable, idempotent and bounded, with latest-revision coalescing, source-version/erasure checks and observable superseded/deferred states. Provider configuration is validated before admission; transient failure after raw acceptance must retain recoverable projection work rather than lose raw data or claim readiness. Role/type/unit constraints still need focused validation before extraction activation.

Date: 2026-10-18. **Accepted starting design, not runtime configuration.** Owner selected **O1-A/O2-A/O3-A/O4-A**, including **4,096 evidence tokens**, balanced limits, the full gated vocabulary, asynchronous tracked erasure and development-off/production-seven-day backup policy. Numeric defaults remain unmeasured starting points. No model, provider or dependency is activated.

Authority: [accepted enrichment policies](INGESTION_CONTEXT_ENRICHMENT.md), [schema contracts](ENRICHMENT_SCHEMA_CONTRACTS.md), ADR-078 / D-100. Configuration names in the review JSON are proposed target names, not existing accepted Engine arguments.

## Review artifacts

- [Numeric defaults](schema_review/operating_defaults.json)
- [43-predicate vocabulary table](schema_review/predicate_vocabulary.md) and [exact registry data](schema_review/predicate_vocabulary.json)
- [20 copied-data classes and erasure checks](schema_review/copy_erasure_checks.json)

The JSON records acceptance separately from runtime implementation and measurement. Final schema/config integration follows the selected coordinated breaking rewrite, not compatibility aliases or a second ingestion route. Earlier proposed wording below describes now-selected starting values, not performance guarantees.

## 1. Numeric defaults

| Stage | Proposed default | Behaviour at limit |
|---|---:|---|
| Extraction window | 6 turns / 1,500 estimated tokens | Flush when either bound reached; hard conversation boundary |
| Idle window flush | 120 seconds | Flush eligible source snapshots; explicit close can flush sooner |
| Oversize-source scan chunk | 6,000 characters; 256 overlap | Keep original source intact; process source-offset chunks with continuation; never discard accepted text |
| Prefill context | 32 sources / 24,000 characters | Rank bounded candidates; record incomplete search |
| Reply traversal | 2 hops | Keep deeper dependencies unresolved/deferred |
| Exact-ID/alias candidates | 16 | Candidate limit never proves global uniqueness |
| BM25 candidates | 40 | Scoped retrieval; no gold-aware ranking |
| Optional vector candidates | 40 | Only configured measured non-generative provider |
| Date candidates | 16 | Correct event/owner before time arithmetic |
| Fused candidate pool | 96 unique records | Preserve branch coverage; record pruning reasons |
| After neighbour expansion | 128 source records | Same scope/conversation/version; no future-known evidence |
| Neighbours | 2 before / 2 after | Later sources excluded by known-time cutoff |
| Dependency expansion | 3 hops / 8 sources per bundle | Never chop required dependency; partial/unresolved result |
| Optional reranker pool | 48 candidates; batches of 16 | Unscored tail stays unscored; no assumed zero score |
| Selected bundles | Up to 12 | Fewer if budget exhausted; full requested coverage still required for complete outcome |
| Evidence assembly | **4,096 tokens — owner selected** | Count the full rendered evidence, citations, anchors and labels |
| Character safety cap | 16,384 characters | Independent hard cap; not proof of token compliance |
| Deep evidence budget | 4,096 tokens initially | No automatic larger allocation; separate future tuning |
| Active background workers | 1 per Engine / 1 per namespace | Fair namespace scheduling |
| Pending-window high water | 256 per namespace / 1,024 per Engine | Pause discovery; retain durable accepted work and visible pending status |
| Reconciliation batch | 100 items / 2-second cooperative slice | Save cursor and resume; never rescan full history per input |
| Durable discovery poll | 5 seconds | Timer is a starting cadence, not an end-to-end latency promise |
| Lease / heartbeat | 60 / 10 seconds | Fenced claims and commits; no lock stealing by age alone |
| Recovery attempts | 3 total; waits 1 and 5 seconds | Persist outputs; permanent validation failures are not retried blindly |
| Shared-model concurrency | 1 per effective settings key | Queue bounded work; do not duplicate weights |
| NER operations | Up to 6 source operations per window job | Allows one source-bound pass per turn; cached successful outputs reused |
| Embedding operations | Up to 2 logical operations per job | Provider batching recorded; raw vector projection has its own existing admission path |
| Reranking operations | 1 logical operation per read | Pool 48 / batch 16 means at most 3 internal inference batches |
| Optional model deadline | 30 seconds per logical operation | Observable optional degradation; required service fails clearly |
| Generative LLM calls | 0 | No hidden generative fallback or schema-repair call |
| Automatic downloads | Off | Missing optional models remain unavailable visibly |
| Unresolved retention | Until source erasure or explicit retention policy | Do not silently expire candidates needed for later linking |

Limits are independently configurable and validated for consistency. Candidate caps precede source expansion, source bounds precede assembly. No numeric similarity threshold may authorise identity merging, current-state overwrite or grounding. Scores rank candidates only.

Source chunking uses offsets in the original text. If a supported phrase crosses an overlap or needs more context than permitted, continue/retry with explicit bounded context or leave unresolved; never splice two snippets into a fake exact quote. Duplicate overlapping discoveries share occurrence identity.

A cooperative two-second slice applies between interruptible units, not as a false promise that a native kernel can be stopped safely after two seconds. Model timeout/cancellation does not prove native work stopped; lifecycle ownership, concurrency slots and late-result fencing must remain correct.

**4K sample:** a move answer plus country antecedent, date anchor and correction fit into a single dependency bundle. If that complete bundle costs 4,300 tokens, it cannot be labelled complete under the 4,096-token budget. Select a smaller exact source window only if every required span remains; otherwise return incomplete/unresolved. A 16,000-character passage may still exceed 4,096 actual tokens.

## 2. Initial predicate vocabulary

Every predicate has versioned subject/value constraints, separate modality, open-world semantics and evidence-origin policy. All start with `cardinality=many`; time-valid/predicate-specific constraints—not a blanket one-value overwrite—control integration. Explicit-only transitions never arise from similarity. No inference-rule IDs are enabled initially.

| Group | Predicates |
|---|---|
| Place roles (9) | resides_in, works_in, home_country, born_in, grew_up_in, move_origin, move_destination, visited, temporarily_present_in |
| Relationships (6) | parent_of, sibling_of, spouse_of, friend_of, member_of, employed_by |
| Objects, works and participation (13) | owns, adopted, made, gave, received, commissioned, read, listened_to, watched, played, participated_in, attended, offered |
| Preferences and intentions (6) | likes, dislikes, prefers, interested_in, intends, suggested_to |
| Lifecycle times (4) | started_on, opened_on, completed_on, occurred_on |
| Quantities (3) | has_quantity, has_measurement, explicit_frequency |
| Explicit reason / attributes (2) | explicit_reason, has_attribute |

Complete subject/value/entity-type constraints are in the registry JSON/table. `has_attribute` requires `Claim.attribute_key` from: colour, material, breed, model, title, language, genre, activity_type, art_form. Other predicates set attribute_key null initially. Do not infer nationality, diagnosis, religion, sexuality or other personal traits through this catch-all. Explicit unsupported concepts remain source-preserved/unresolved until a reviewed predicate extension exists; this vocabulary does not promise complete LoCoMo semantic coverage.

| Sample input | Supported output | Boundary |
|---|---|---|
| I work in Lisbon | works_in → Lisbon | Does not imply residence |
| I moved from Porto to Lisbon | Move event + move_origin/move_destination | Residence changes need separate supported state integration |
| I also have a home in Porto | Additional residence if statement establishes it | Do not close Lisbon automatically |
| A friend made my guitar | made, with friend as maker | Does not establish gift or commissioner |
| I plan to read it | read candidate with planned modality | Not completed reading |
| The studio offers yoga | offered, studio as subject | Not the speaker's attendance |
| I prefer swimming | prefers → swimming | Does not mean dislikes running |
| My dog is a Labrador | has_attribute/breed | Competing breed claims retained, not replaced by recency alone |
| It opened last month | opened_on with bound studio and aware period | Unknown timezone leaves temporal value unresolved text |
| I walk 5 km | has_measurement with unit | No invented daily frequency |

There is one canonical ontology. Add predicate specifications/rule packs through the registry, not dynamically invented relations per question. Domain events carry participants/stage/time; source facts carry provenance. A curated catalogue's author/country/genre links require separate catalogue-origin predicates and provenance, not fabricated personal-memory claims. Catalogue activation remains off pending a concrete validated dataset/provider selection.

## 3. Copy accounting and erasure state

Each source has a monotonic erasure generation. Every persisted excerpt has original source/version/hash plus copy ID and reverse source-to-copy index. Child copies must register atomically with their effect. Source erasure and copy creation race under the same authoritative fence. No copy can become readable while its source is erased.

Recommended operation outcomes are distinct:

| State | Meaning |
|---|---|
| accepted / blocked | Canonical tombstone committed; API/search/hydration deny source and descendants |
| purging | Known managed copies being removed/redacted |
| verified_active | Active logical/API copies checked absent/unreadable; NOT physical/backups completion |
| pending_retention | Backups, backend segments or external history still retain content |
| verified_managed | All declared managed surfaces meet their stated content-purge guarantees |
| failed / outside_control | Missing proof or unmanaged copy remains; scope/reason reported |

Do not report universal physical erasure or SSD/RAM forensic sanitisation. `exists=false` only proves the adapter's lookup result, not absence in disk pages, WAL, segments, workflow history or backups. Payload-bearing event logs require actual supported redaction/purge or encryption/key destruction; a new tombstone alone does not remove old plaintext. Keep logical history/tombstones without recoverable erased text. Event replay must never restore erased payloads.

## 4. All-copy verification checklist

| # | Copy class | Required check |
|---|---|---|
| 1 | Raw records/assets | Deny lookup and remove retained source content under declared backend guarantee |
| 2 | Claim payloads | Remove affected text/values; recompute independently supported state |
| 3 | Bundle excerpts | Reverse enumeration, purge copies, invalidate affected bundles |
| 4 | Event-log payloads | Purge/redact source-bearing raw/effect/audit payloads; non-content tombstones retained |
| 5 | SQL journals/WAL/freed pages | Backend-specific purge/compaction evidence; DELETE alone insufficient |
| 6 | Vector indexes | Remove stored text/metadata/vector entries; account for old segments |
| 7 | Lexical indexes | Verify active results and relevant inactive/segment retention |
| 8 | Graph | Remove occurrences/edges/labels dependent on source; retain independent support only |
| 9 | Caches | Invalidate source generations and result/model-result caches |
| 10 | Queue/outbox | Cancel/invalidate text-bearing pending/retry jobs |
| 11 | Local step journal | Purge source-bearing outputs; resumed jobs must recheck tombstones |
| 12 | External workflow history | IDs-only inputs/results by default; accidental text has explicit retention status |
| 13 | Summaries/profiles/observations | Invalidate and rederive from remaining original support |
| 14 | Media derivatives | Purge engine-owned OCR/captions/thumbnails/assets and versions |
| 15 | Temporary files | Close handles and purge tracked spills/staging files |
| 16 | Logs/traces/crash dumps | Content logging off; accidental copies tracked, not exempted |
| 17 | Inactive generations | Include old provider/corpus generations in cascade |
| 18 | Exports | Purge engine-owned copies; declare external recipients outside control |
| 19 | Backups/replicas/object versions | Enforce retention or valid key destruction; restore gate before access |
| 20 | Process buffers | Fence/cancel stale reads and writes; invalidate owned buffers without claiming forensic RAM wiping |

Each backend reports supported operations and boundaries; a non-enabled surface is `not_applicable`, not falsely verified. An unreachable surface is pending/failed, not skipped. Unknown copy location blocks complete verification. Erasure manifests contain IDs/generations/status only, no quotes, summaries or low-entropy content hashes retained unnecessarily.

### Proposed erasure numbers

- Logical read denial: at tombstone commit; no grace period.
- Foreground wait: 2 seconds, then return operation ID and pending status rather than blocking indefinitely.
- Purge pages: 100 items; poll every 5 seconds; 3 verification attempts with 1/5-second retry waits before escalation.
- Active managed copy cleanup target: 60 seconds, **unmeasured operational target**, not a guarantee or a success timeout.
- Content logging: disabled. New persistent workflow history uses opaque source/job IDs, not excerpts or model outputs.
- Backup content retention: **proposed maximum 7 days in production**, not activated here. Development may keep zero backups. Restore remains closed until tombstones are applied. Backup expiry alone cannot prove deletion of outside-control exports.
- Non-content erasure tombstones: retained while any backup/replay source can resurrect content; proposed indefinite retention until a verified safe retention-boundary policy is selected.

**Erasure sample:** raw source S creates claim C, copied bundle B, summary P and lexical generation G-old. Deleting S first commits the generation fence and denies all four. Purge then visits C/B/P/G-old plus raw/event/journal/cache copies. The receipt may report verified_active but pending_retention while backup content remains. A worker finishing after deletion cannot recreate B because commit rechecks the generation.

## 5. Focused validation gates

1. JSON shape/registry uniqueness and budget consistency: candidate caps, window limits, dependency closure, 4K accounting and explicit partial outcomes.
2. Tiny synthetic cases for each predicate family; wrong subject/object, plans, negation, offered/attended and attribute-key mismatches must fail compilation or remain unresolved.
3. Retry/crash/source-edit/erase race with a copied bundle: no unregistered copy or resurrected payload.
4. Enumerate all enabled surfaces and verify checks plus declared not-applicable/unsupported boundaries. Count results, not just calls.
5. Restore a tiny fixture containing erased source text; refuse serving until the erase manifest is applied.
6. Kernel-only latency/memory experiments for ranking/trimming and provider changes. No broad evaluation migration, downloads or live deployment activation as part of this planning step.

## Accepted operating decisions

| Decision | Selected policy |
|---|---|
| O1-A | Balanced configurable limits around 4K evidence; excess work deferred visibly |
| O2-A | Full 43-predicate/nine-attribute vocabulary; extraction forms gated on focused tests |
| O3-A | Immediate denial at tombstone commit; up to two-second foreground wait then durable asynchronous per-surface receipt |
| O4-A | Development backups off by default; production content retention maximum seven days, pending-retention status and blocked restores until tombstones applied |

These choices are final for the starting design and need not be re-asked. Concrete model IDs, provider-specific purge mechanics and deployment evidence remain implementation/verification work. Missing backend capabilities cannot be replaced by a success claim. No backup infrastructure is configured by these selections.

Review-data validation: schema_review/validate_operating_defaults.py verifies predicate schema/uniqueness, budget consistency and copy-class coverage; results in schema_review/operating_validation.json. This is not a kernel benchmark or actual erasure verification.

## Generic vocabulary review — 2026-10-18

See [complete generic catalog](GENERIC_PREDICATE_ATTRIBUTE_CATALOG.md): accepted 43 predicates/nine keys preserved; 10 predicates and 35 keys proposed for broader reuse. New review findings: measurement/count/frequency property keys, domain-constrained biological roles, multi-party/comparison qualifiers and catalogue provenance. Current schema restrictions remain unchanged until these refinements are selected. No runtime registration or extraction implementation is claimed.

## Measured comparison profiles - 2026-10-18

[CPU evidence-budget report](CPU_EVIDENCE_BUDGET_BENCHMARK.md) now measures 48-candidate ranking/batch16 at 64/128/256 token lengths and 2,048/3,072 evidence-only caps. Recommend 3K for full-message bundles in this bounded profile; 2K requires explicit incomplete behaviour or a validated compact selector. Manual exact-span fixtures give an optimistic bound, not a deployed trimmer. No authorised 4K default is overwritten. The remaining window/worker/reconciliation limits remain unmeasured; these measurements are not overall SLOs.
