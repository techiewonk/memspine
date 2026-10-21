# Enrichment schemas and behavioural contracts

## Generic review selections — 2026-10-22

The [BI-01 generic-memory review](BI01_CONTRACT_REWRITE_PLAN.md#generic-memory-review-and-bi-01-freeze-gates--2026-10-22) refines the target: RM-36 A inherits an authorised stored-parent session/conversation when omitted; RM-42 A preserves unsupported statements as source-bound unmapped annotations; RM-43 A requeues only scoped unfinished/retry-eligible work on explicit effective-policy activation (including an activated registry digest), without historical backfill; RM-55 B permits read-grant-authorised cross-namespace identity merges with stable/cited identity evidence and revocation dependencies. The proposed merge mechanism is a reversible, grant-scoped projection over namespace-owned identities, not destructive global ID replacement. This supersedes earlier separate-identity-authority recommendations. Existing runtime and review JSON schema are not updated by this notice; open structural gates remain in the single tracker.


## Owner selections — 2026-10-21 (supersedes earlier alternatives)

BI-Q1 A: unresolved source-bound mentions retain null canonical entity IDs and cited candidates/reasons. BI-Q2 B: adopt all **53 predicates and 44 property keys** in the coordinated rewrite; registration remains separate from tested extraction activation. BI-Q3 refined A: one versioned RoleSpec/RoleBinding contract is shared by events and claims; use event participants for occurrences and typed claim roles for non-event comparisons. No arbitrary qualifier dictionary or fabricated comparison event. BI-Q4 B: every effective ingestion-policy revision cancels/supersedes pending and in-flight old-revision work and requeues eligible sources under the latest revision. Cooperative cancellation alone is insufficient: commit fencing rejects old generations even if a model call finishes late. Preserve completed history; do not undo accepted effects or re-embed/copy historical data automatically. Current erasure/permission checks always apply. BI-Q5 B: every supported ingestion profile requires configured compatible embedding and vector capabilities; SQL-only ingestion is not a supported public profile. Lexical remains independently configurable and no generative LLM is required.

Selections are design acceptance, not completed runtime integration. Requeue must be durable, idempotent and bounded, with latest-revision coalescing, source-version/erasure checks and observable superseded/deferred states. Provider configuration is validated before admission; transient failure after raw acceptance must retain recoverable projection work rather than lose raw data or claim readiness. Role/type/unit constraints still need focused validation before extraction activation.

Date: 2026-10-18. **Proposed exact target contracts for review; not runtime implementation.** Accepted policies remain in [ADR-078](adr/ADR-078-engineering-first-context-enrichment.md). New field names, enumerations and API signatures below need review. No backward compatibility layer or alternate intake path is proposed.

**Latest owner selections:** Q-S1 B (coordinated replacement of affected envelopes), Q-S2 B (aware datetimes only; unresolved civil dates stay text), Q-S3 A (typed values), Q-S4 A (explicit domain events), Q-S5 B (persist full excerpt copies with cascade erasure). Schema/examples below reflect these selections; earlier alternative recommendations are superseded.

## Review artifacts and authority

- [JSON Schema, Draft 2020-12](schema_review/enrichment.schema.json): complete field definitions, required keys, nullability, types and enumerations; unknown fields rejected. Validate a review document as `{ "kind": "Claim", "value": { ... } }`. This wrapper is a review/validation convention, not a proposed public API envelope.
- [Complete structural examples](schema_review/examples.json): synthetic data, no benchmark gold. Entity/event references illustrate separately stored objects; these are not a complete database/event-log fixture.
- [52-gap coverage mapping](schema_review/gap_coverage.json): each NL requirement maps to schema definitions and behavioural checks. Mapping does not mean implementation or test completion.
- [Readable 52-row matrix](schema_review/gap_coverage.md): the same mapping in tabular form.
- [Generator](schema_review/build_contracts.py): documentation tooling only; engine imports none of it.
- [Focused shape validator](schema_review/validate_contracts.py) and [results](schema_review/validation.json): require `jsonschema`; explicitly enable date/date-time format checks. Without a format checker, JSON Schema format is not a reliable rejection check.

Every declared property is required unless the schema explicitly says otherwise. Nullable means an explicit unknown/not-applicable value; it does not authorise inference. Arrays may be empty only where allowed. Source IDs, authorities, status, producer identity and acceptance decisions are system-authored. A future model may propose text/spans/content, never assign these trusted fields.

## Existing types: reuse and intentional evolution

| Existing implementation | Target treatment |
|---|---|
| `schemas/durable/common.py::OwnerScope` | Preserve tenant/owner identity; add resolved namespace binding in the proposed context objects, validated by the canonical gateway |
| `assertion.py::AssertionEnvelope`, `Mention` | Replace affected envelope shapes in a coordinated release; Claim is the single replacement contract, not a second stored fact format |
| `identity.py::AssertionKey`, write/occurrence IDs | Preserve distinct operation, proposition, occurrence and record identities; do not use a source occurrence ID as a domain event ID |
| `evidence.py::Evidence`, `TurnRef`, `Span` | Reuse support/binding/context semantics; proposed shape adds pinned snapshot, provenance and mandatory exact spans for raw support/binding |
| `temporal.py::TemporalRef`, `TimeAxes` | Replace shape while reusing correct parser semantics; aware datetime values only, unresolved civil text, known-time sequence and separate occurrence versus state validity |
| `assessment.py::Assessment` | Preserve modality/negation and trust/support separation; explicit proposed projection shown in schema |
| `schemas/public/read.py::EvidenceBundle`, `read/tiers.py::fit_units` | Evolve current bundles with explicit dependencies, coverage and cost accounting; not a second assembly path |
| `schemas/durable/events.py::MemoryEvent`, actual store event types | Event-log envelope remains distinct from a domain `EventOccurrence`; proposed Effect describes a payload to map into canonical log kinds during implementation |

Current code is NOT already compatible with these target JSON shapes. Replace all affected serialized assertion/evidence/temporal/read-bundle shapes together, and coordinate compiler, gateway, persistence, projector, graph, reader, API serialization and focused fixtures. Keep one canonical intake/fact path; no aliases, dual-write, fallback decoder or parallel old envelope. Reuse correct algorithms and provider ports rather than rewriting them merely to change shape. Event-log source-of-truth semantics remain intact. No automatic historical data conversion, database copying or re-embedding is added; existing-data reset/import handling must be explicit when implementation is planned.

## 1. Entity, mention and identity

| Contract | Exact principal fields | Meaning |
|---|---|---|
| Entity | `entity_id, revision, scope, type, world, label, identity_basis, identity_evidence_ids, status, known_since, known_seq` | Canonical identity, not an asserted profile |
| Mention | `mention_id, source, seg_index, span, entity_type_hint, world_hint, resolution, entity_id, candidate_entity_ids, reasons, asserted` | Exact source occurrence; `asserted` is always false |
| IdentityLink | `link_id, scope, relation, left, right, evidence_ids, status, producer, known_since` | Evidence-backed alias/same/distinct relationship, with reversible acceptance |
| EntityRef | `entity_id, mention_id` | At least one must exist; a mention may remain unresolved |

Entity types: person, agent, group, organisation, place, object, work, project, species, other. World: real, fictional, symbolic, depicted, unknown. Labels are display values, never unique keys. Identity basis: authenticated stable ID or explicit cited identity. Unknown unnamed subjects remain mentions until identified; do not create canonical entities from a nearest-name guess.

**Sample:** sender `user-123` maps to an authenticated person entity. In “my sister moved,” the subject is a separate sister mention, not the sender. A stuffed dog, a fictional dog and a real dog must not merge merely because all have the same name.

Recommended: namespace-scoped entity IDs reused across its sessions/conversations. Cross-namespace identity requires an explicitly authorised future linkage policy; no automatic global merge.

## 2. Source, evidence and trust

SourceSnapshot pins source ID/version, scope, hash, message/session/conversation/sender IDs, original/generated clock provenance, erasure generation and availability time/log sequence. Evidence records add evidence ID, role, origin, segment, exact span and source family.

| Field / role | Contract |
|---|---|
| support | Source states the assertion; interrogatives, bare mentions and collection metadata cannot independently establish it |
| binding | Resolves identity, reference or temporal anchor; does not independently assert the new fact |
| context | Helpful context only; never promotes grounding or trust |
| origin | raw_message / document / attachment_text / image_caption / catalogue / collection_metadata |
| span | `[start,end)` Python Unicode code-point offsets in the exact pinned source segment; `quote == raw[start:end]` |
| source_family | Engine-assigned origin principal/family; summaries inherit original families rather than inventing corroboration |

Raw support/binding require nonempty exact spans. Generated annotation does not increment the raw source-content version. Original bytes/text remain recoverable; Unicode-normalised index text never changes evidence offsets. Caption support applies to what the caption says and is identified as caption evidence, not proof of unseen visual content. Catalogue facts have catalogue source/version and cannot masquerade as a person's assertion.

## 3. Claim and predicate contracts

Claim principal fields: `claim_id, revision, scope, subject, predicate, attribute_key, object, event_id, asserted_by, reported_by, source_class, text, time, assessment, status, evidence, links, producer, effect_id`.

| Field | Rule |
|---|---|
| claim_id | Stable assertion-occurrence identity; retry-stable, not the person ID or event ID |
| revision | Monotonic materialised revision, append-log driven; stale compare-and-set rejected |
| predicate / attribute_key | Registered versioned PredicateSpec; `has_attribute` requires an allowlisted attribute key, others initially null; arbitrary unrecognised phrases stay unresolved |
| object | Tagged entity/text/number/boolean/temporal value; numbers retain nullable units/currency |
| event_id | Domain occurrence if applicable; a state may cite a supported event without becoming that event |
| asserted_by / reported_by | Asserting principal versus reporting principal; sender/subject/authority remain separate |
| assessment | Trust, modality, binding, original source families and grounding; retrieval relevance is not trust |
| links | corrects / supersedes / contradicts / equivalent_to / derived_from, with pinned target versions |
| status | active / tentative / contested / superseded / hidden; status changes require canonical events |

Modality remains asserted / negated / hypothetical / planned / reported / question. For asserted negation use `modality=negated, negated=false`; planned/reported/hypothetical negative content uses the appropriate modality plus `negated=true`, matching existing invariants. A question is not a grounded answer claim.

PredicateSpec declares subject types, tagged value kinds, state/event/relation/attribute semantics, one/many/unknown cardinality, explicit-only/append-only transition policy, versioned inference rules and allowed evidence origins. `closed_world=false` is fixed initially: absence is not negation.

The proposed [43-predicate vocabulary](schema_review/predicate_vocabulary.md) is now the authoritative initial vocabulary proposal: separate `resides_in`, `works_in`, `visited`, `move_origin`, `move_destination`, `home_country`, `intends`, `attended`, `offered`, `suggested_to`, `made`, `gave`, `received`, `owns`, with typed roles rather than duplicate inverse names. Health/trait predicates cannot be substituted through embedding similarity. Vocabulary registration is required but does not imply every extraction form is implemented.

**Residence sample:** explicit move Porto → Lisbon produces a move occurrence and cited origin/destination claims. A residence transition is a separately justified state effect. Existing Porto residence ends only where supported; buying another home may add a residence without closing either. Month-level move evidence yields an uncertain transition boundary, not a fabricated exact date.

**Correction sample:** “Correction: I meant Coimbra, not Lisbon” creates a new supported destination claim linking `corrects` to the exact prior claim. The old claim's current projection becomes corrected/superseded only after authority validation. Do not create a Lisbon-to-Coimbra move from the correction.

## 4. Domain event occurrences

EventOccurrence fields: `event_id, revision, scope, event_type, participants, stage, participation_mode, time, claim_refs, same_event_links, identity_status, producer`.

Stages: proposed / planned / started / ongoing / completed / cancelled / unknown. Participation: physical / remote / mixed / unknown. Each participant has a role, EntityRef and evidence IDs. Event identity may be resolved/provisional/contested. Same-event links are candidates until verified against event type, participants, source references and time compatibility.

One trip retold three times is one event with multiple source occurrences. Two physically different cars of the same model are separate entities. An ongoing project does not reveal its start. An invitation does not prove attendance. A remote chat does not imply travel.

Domain `event_id` and append-log `MemoryEvent.event_id` are distinct identity domains. Neither can be substituted for the other. Proposed Effect kinds in JSON are payload operations, not already registered EventKind values. Raw WRITE and the existing event log remain canonical.

An Effect contains its complete typed payload as well as target/revision and payload hash; a hash or projector reference alone is insufficient for replay. The committed payload's type/identity must match its operation and target. Invalidation has a typed target, reason and source references. Authoritative log retention and accepted erasure policy govern raw/payload retention; projection cleanup never becomes the sole record of a change.

## 5. Temporal references and state validity

| Contract | Fields / semantics |
|---|---|
| SourceClock | `created_at, original_text, timezone, timestamp_origin, timezone_origin`; resolved datetime must be aware; original unresolved clock text preserved |
| TemporalRef | `temporal_id, text, resolution, value, precision, text_precision, approximate, basis, timezone, anchor, relation, evidence_ids` |
| AwareTimeValue | `at, end_exclusive`; both resolved endpoints are offset-aware; optional end encodes a supported coarse period |
| Anchor | Source-clock or event-boundary reference plus supporting/binding evidence |
| Interval | Start/end TemporalRefs plus explicit endpoint status known/unknown/unbounded and end semantics |
| TimeAxes | `valid, occurred, source_clock, recorded_at, known_since, known_seq` |
| Duration | Minimum/nullable maximum, units, elapsed/inclusive/calendar convention and approximate flag |

`valid` describes a supported state-validity interval. `occurred` describes the occurrence time/period. They must not conflict where they refer to the same boundary; one is not silently copied into the other. Unknown end is not evidence of an infinitely continuing state. Unknown start is not minus infinity. Unbounded means the asserted/query domain explicitly has no bound, not merely that extraction failed.

Basis is explicit/source_time/relation/none. Do not persist an unsupported inferred exact date. Unresolved relations may retain a target/offset while value stays null; basis relation then describes the unresolved dependency, not a resolved answer.

Examples:

```json
{"at":"2026-09-01T00:00:00+05:30","end_exclusive":"2026-10-01T00:00:00+05:30"}
```

For “last month” said on 18 October 2026 with an explicit Asia/Kolkata source clock, retain month precision and source-clock evidence. The endpoints encode a month period, NOT a move exactly at its start. No period endpoints are assigned without a supported clock/zone basis.

```json
{"text":"7 May 2023","resolution":"unresolved","value":null,"precision":"unknown","text_precision":"day","basis":"none"}
```

A source that supplies 7 May 2023 without a supported timezone remains unresolved text under Q-S2-B. `text_precision=day` preserves what the phrase says; `precision=unknown` describes the unresolved datetime. This fragment omits other required fields for readability; complete examples are in examples.json. Do not assign UTC, device timezone or guessed residence timezone to make it resolve. Such a source may remain searchable but cannot supply exact resolved-time arithmetic until an anchor is supported. This deliberately reduces first-phase temporal coverage compared with the rejected civil-date representation.

Preserve a date-only or timezone-free source clock in `SourceClock.original_text`, not a typed naive datetime or invented midnight. An offset alone establishes an instant but does not establish a geographic timezone or historical DST rules. Generated created_at cannot replace an unknown original clock.

`known_since`/`known_seq` represent actual canonical commit visibility, not the original historical message date. A June claim resolved using July evidence cannot appear resolved in an earlier known-time view. Sources imported in October were not in this system in June. Current-state queries and original source-date queries remain distinct.

## 6. Evidence bundles, reranking and trimming

Bundle fields: `bundle_id, scope, claims, evidence, copy_manifest, dependencies, temporal_refs, coverage, cost, producer`.

Q-S5-B: persist full selected excerpts in `evidence[].span.quote` with their pinned raw references. Each has a copy manifest entry (`copy_id, evidence_id, source, excerpt_hash, erasure_generation, child_copy_ids`). Full excerpt means the entire selected source span, not an unconditional duplicate of every full message. Source references remain mandatory even though text is copied. A reverse source-to-copy index is required for all derived persisted copies.

Before serving a copied excerpt, validate current permission/source-erasure generation; stored text is not permission to bypass hydration-time checks. Cascade erasure must remove or render unreadable the original and every bundle/log payload/cache/index/export copy under engine control, plus inactive generations. Retain only non-content tombstones where policy permits. Backups or external exports outside deletion control require explicit retention/key-destruction evidence; never report physical erasure complete merely because the active bundle row disappeared. Erasure invalidates the bundle; do not serve a stripped but apparently complete result. Register new descendant copies atomically with their effect so a race cannot produce an untracked surviving copy.

Dependencies declare support/entity binding/time anchor/polarity/modality/list member/join branch/duration endpoint, explicit evidence/claim references, mandatory flag and branch ID. Cyclic references must not create self-support; source dependency expansion is bounded and cycle-checked.

1. Candidate retrieval uses exact IDs/aliases/replies, BM25 and optional validated non-generative components.
2. Apply scope, source lifecycle and known-time constraints before ranking and again after hydration.
3. Rank compatible evidence bundles with coverage by requested facet/branch. A high score cannot repair an invalid identity, event or source link.
4. Form dependency closure, then select source-preserving spans/sentences. Keep actor, negation, modality, antecedent, quantities, date anchor and every required join/list endpoint.
5. Count rendered cost, including source labels and formatting overhead, with one consistent method. Estimates are labelled; exact downstream token compliance requires the actual tokenizer or explicit conservative handling.
6. Return Selection with kept IDs, dropped IDs/reasons, cost, coverage, warnings and complete/partial/unresolved status. Never imply a complete answer when a mandatory branch did not fit.

Coverage declares complete_scope/observed_subset/unknown, requested/enumerated scope, snapshot hash, continuation, reasons and evidence proofs. Complete database enumeration is not proof of a person's complete lifetime history. Count results distinguish exact/lower_bound/interval/unresolved. Set joins require typed compatible operands and per-branch support. Zero-length elapsed duration is allowed where endpoints truly coincide.

**Sample:** “there last year” needs the current answer span, earlier place antecedent and reliable calendar anchor. They form one required bundle. Trimming just the highest-scoring sentence would destroy its meaning; defer/mark incomplete if the bundle cannot fit.

## 7. Non-LLM engineering interfaces (proposed signatures)

```python
def propose_mentions(source: SourceSnapshot, text: str, policy: Policy) -> list[Mention]: ...
async def retrieve_candidates(context: ContextBundle) -> CandidatePage: ...
def resolve_identity(mention: Mention, candidates: CandidatePage) -> ResolutionDecision: ...
def normalise_time(phrase: str, anchor: Anchor | None, policy: Policy) -> TemporalDecision: ...
def compile_claim(proposal: ExtractionProposal, context: ContextBundle) -> ClaimDecision: ...
def reconcile_state(claim: Claim, snapshot: StateSnapshot) -> IntegrationDecision: ...
def build_bundles(claims: list[Claim], snapshot: SourceSnapshotSet) -> list[Bundle]: ...
def select_evidence(bundles: list[Bundle], budget: Cost) -> Selection: ...
async def commit_effects(batch: EffectBatch, fence: LeaseFence) -> DerivationResult: ...
```

These signatures name orchestration boundaries, not additional exported Engine APIs. `Policy`, `CandidatePage`, proposal/decision types and transaction adapters require implementation-level definition after review; do not claim executable type completeness from this pseudocode. JSON defines concrete durable/context records, not all internal function locals. Use existing canonical gateway and provider ports, not a parallel writer.

| Boundary | Input guarantee | Output / failure guarantee |
|---|---|---|
| Propose/extract | Pinned original source, rule version | Candidate spans/values; unsupported form remains unresolved |
| Retrieve | Authorised scope, known cutoff and explicit budget | Candidates + coverage/cursor; similarity is not proof |
| Resolve/compile | Candidate snapshots and original support | Typed grounded decision or explicit reason; no guessed IDs |
| Integrate | Predicate policy, target revision and authority | Append/coexist/contest/supersede plan; no direct projection writes |
| Commit | Source versions/hashes/erasure and fenced lease | Canonical atomic effect identity + log commit; stale/CAS failure returns retryable/rejected, never false success |
| Project | Committed sequence and typed effect | Idempotent rebuildable SQL/search/graph views; separate readiness |
| Select | Full source dependency references | Source-preserving selection or visible incomplete outcome |

Producer kind makes actual execution visible: rule/lexical/embedding/ner/reranker/catalogue/generative_llm. A generative reranker remains generative. The default permits configured measured non-generative components; unavailable optional components degrade observably, required ones fail clearly. No implicit download or generative fallback.

## 8. Normative behavioural checks

JSON Schema checks shapes only. These rules require source resolvers, permissions, predicate policies or transactions; validation artifacts must distinguish them.

| Check | Required invariant |
|---|---|
| C01 | Pinned source version/hash and exact span match; source mutation/erasure invalidates dependent effects |
| C02 | Canonical scope/namespace mapping and access filters validated before lookup and after hydration; no cross-scope pointers leak |
| C03 | Identity requires authenticated stable ID or cited identity; unique names/fuzzy score/reply alone do not prove it |
| C04 | Evidence role/origin valid for predicate; context, crawler metadata and derived echoes cannot supply missing primary support |
| C05 | Every referenced evidence/source/claim exists at the pinned version; mandatory dependencies close without self-support |
| C06 | Predicate roles, modality, negation, object type and world context compatible; no unsupported health/trait inference |
| C07 | Reliable temporal anchor; correct local calendar arithmetic/precision; generated source clock cannot anchor history |
| C08 | Known-time view bounded by committed visibility of all dependencies; imported history cannot backdate system knowledge |
| C09 | Domain event identity/stage/participants established before joins, counts or arithmetic; retellings not extra events |
| C10 | Calendar fields valid; endpoint order/units/convention checked; unknown versus unbounded distinct; no naive-UTC invention |
| C11 | Supported authorised correction/transition targets exact prior claim; multiple homes and competing claims preserved |
| C12 | State validity separate from origin/visit/work/plans; source authority not inferred from similarity |
| C13 | Exact total requires complete declared scope or explicit stated total; otherwise lower bound/unknown; typed distinct operands |
| C14 | Every selected list/join branch and required span retained or output visibly partial/unresolved |
| C15 | Relevance scores never alter truth/trust; unscored candidates not zero; benchmark gold never ranking input |
| C16 | Exact span trimming and overlap dedup preserve provenance and mandatory semantics; actual rendered budget respected |
| C17 | Supported language/rule/unit/catalogue version declared; unsupported or ambiguous values remain unresolved |
| C18 | Commit source/CAS/erasure/lease checks and effect identity in same authoritative transaction; retries do not duplicate |
| C19 | Bounded resumable discovery/reconciliation, cycle detection and observable deferral; top-k does not prove uniqueness |
| C20 | Dataset gold/citation/premise issues tracked separately; no benchmark-specific aliases or blanket category refusals |
| C21 | Provider gate: namespace/upsert/delete/exists/reopen/corruption/lifecycle parity; lexical backend not source truth |
| C22 | Measure evidence recall, correctness/abstention and kernel performance separately; no speed-to-accuracy inference |

The [coverage map](schema_review/gap_coverage.json) enumerates every NL-01..NL-52. NL-41..44 and NL-45..48 intentionally include evaluation/deployment checks: JSON fields alone cannot solve those requirements.

## 9. Implementation and test checklist

1. Resolve remaining schema choices below; update ADR-078 and D-100 only for accepted changes. *(Resolved: Q-S1..5 selected B/B/A/A/B under "Owner-selected representations".)*
2. Freeze target version, predicate vocabulary and current-to-target field mapping. No backward compatibility aliases.
3. Implement pure value validators and synthetic source fixtures first: aware/unknown clocks, date-only text, identity ambiguity, negation/plans, distinct object instances and corrections; add copied-excerpt cascade/backup-boundary tests.
4. Wire existing temporal/parser/compile and read bundle machinery; retain canonical acceptance and producer boundaries.
5. Add durable annotations and bounded candidate/reconciliation jobs through BI-01/04/06/07; fenced effects before graph projection.
6. Focused negative tests for wrong namespace, stale source, bad spans, unknown units, duplicate effects, unresolved target and erased source. Do not implement permissive fallback to make tests pass.
7. Validate ranking/packing at equal rendered budgets, list/join dependency coverage and explainable dropped reasons. Kernel-only performance checks.
8. New lexical providers remain offline experiments until their separate parity gates pass. Existing Tantivy/OpenSearch choices remain accepted.
9. Independent synthetic/paraphrased cases before generalisation claims; existing inspected LoCoMo split is not untouched design holdout. Full context/media issues require adjudication rather than runtime hacks.

## Owner-selected representations — 2026-10-18

| Choice | Selected | Consequence |
|---|---|---|
| Q-S1 | B: replace affected envelope shapes together | Coordinated breaking rewrite; one path; no compatibility aliases |
| Q-S2 | B: aware datetimes only | Timezone-free dates stay unresolved text; no fabricated UTC |
| Q-S3 | A: tagged typed values and predicate policies | Strict type/role/modality checks |
| Q-S4 | A: explicit domain events | Stable source-backed occurrence identity |
| Q-S5 | B: persist full excerpt copies | Reverse copy manifest, lifecycle checks and cascade erasure required |

All five representation choices are accepted. Exact implementation scheduling, numeric budgets, concrete model settings and deployment-level erasure coverage remain to be specified; no runtime implementation is started by this document update.

Scope/identity lifetime recommendation is namespace-scoped across sessions/conversations, matching the accepted linking boundary; final resolver integration must explicitly reconcile OwnerScope with the gateway namespace mapping. Canonical field names above are proposed, not a reason to duplicate existing durable types.

## Operating policy draft — 2026-10-18

> **Superseded:** the proposed values below were accepted as O1-A..O4-A in the next section.

[Numeric budgets, vocabulary and erasure checks](ENRICHMENT_OPERATING_POLICY.md) supplies proposed limits, 43 typed predicates and 20 copy-class checks. Owner explicitly selected 4,096 evidence tokens; other numeric defaults and backup/erasure operating choices remain proposed. Claim.attribute_key is explicit for the proposed attribute allowlist. No runtime implementation/default is changed by review JSON.

## Operating selections accepted — 2026-10-18

Operating selections O1-A/O2-A/O3-A/O4-A accepted. ENRICHMENT_OPERATING_POLICY.md and schema_review/operating_defaults.json now record accepted starting design, not merely proposed values. Runtime contract integration, C01-C22 behavioural proof, actual backend erasure and model-specific acceptance remain pending.

## Generic vocabulary review — 2026-10-18

> **Superseded:** BI-Q2 B (owner selections, 2026-10-21, top of this file) adopts all 53
> predicates and 44 property keys in the coordinated rewrite.

See [complete generic catalog](GENERIC_PREDICATE_ATTRIBUTE_CATALOG.md): accepted 43 predicates/nine keys preserved; 10 predicates and 35 keys proposed for broader reuse. New review findings: measurement/count/frequency property keys, domain-constrained biological roles, multi-party/comparison qualifiers and catalogue provenance. Current schema restrictions remain unchanged until these refinements are selected. No runtime registration or extraction implementation is claimed.

## Semantic structure and usage guide — 2026-10-18

[SEMANTIC_REGISTRY_STRUCTURE_AND_USAGE.md](SEMANTIC_REGISTRY_STRUCTURE_AND_USAGE.md) describes the proposed pure semantic registry, one-pack ownership of all 43 starter predicates, property categories, role handling, ingestion/read flow and memory-layer mapping. Existing accepted policies remain unchanged. Proposed key-category/role/type refinements and extra 10/35 vocabulary items are not activated; target schema changes await review.
