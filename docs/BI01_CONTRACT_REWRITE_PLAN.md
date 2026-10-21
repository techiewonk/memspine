# BI-01 contract freeze and coordinated rewrite preparation

## Owner selections — 2026-10-21 (supersedes earlier alternatives)

BI-Q1 A: unresolved source-bound mentions retain null canonical entity IDs and cited candidates/reasons. BI-Q2 B: adopt all **53 predicates and 44 property keys** in the coordinated rewrite; registration remains separate from tested extraction activation. BI-Q3 refined A: one versioned RoleSpec/RoleBinding contract is shared by events and claims; use event participants for occurrences and typed claim roles for non-event comparisons. No arbitrary qualifier dictionary or fabricated comparison event. BI-Q4 B: every effective ingestion-policy revision cancels/supersedes pending and in-flight old-revision work and requeues eligible work under the latest revision (eligibility frozen by RM-43 A below: scoped pending, in-flight and retry-eligible intents only). Cooperative cancellation alone is insufficient: commit fencing rejects old generations even if a model call finishes late. Preserve completed history; do not undo accepted effects or re-embed/copy historical data automatically. Current erasure/permission checks always apply. BI-Q5 B: every supported ingestion profile requires configured compatible embedding and vector capabilities; SQL-only ingestion is not a supported public profile. Lexical remains independently configurable and no generative LLM is required.

Selections are design acceptance, not completed runtime integration. Requeue must be durable, idempotent and bounded, with latest-revision coalescing, source-version/erasure checks and observable superseded/deferred states. Provider configuration is validated before admission; transient failure after raw acceptance must retain recoverable projection work rather than lose raw data or claim readiness. Role/type/unit constraints still need focused validation before extraction activation.

Date: 2026-10-21, updated 2026-10-22. This is a design and code-impact note, not another task tracker.
All live task statuses and milestones remain in [PROVIDER_TODO.md](PROVIDER_TODO.md).
Owner requested BI-01 with RM-22/23/24, with remaining questions answered before
implementation. The selected scope now permits the pure semantic foundation; the full canonical envelope cutover remains coordinated.

## Accepted boundaries

- Q-S1 B: replace affected envelopes together, with one compiler/gateway path.
  No aliases, parallel writers, compatibility decoders or automatic data conversion.
- Q-S2 B: resolved times are aware datetimes. Unsupported civil dates retain
  original text and precision; never manufacture a timezone or midnight.
- Q-S3 A: tagged entity/text/number/boolean/temporal values and predicate policies.
- Q-S4 A: source-backed domain event occurrences, separate from event-log IDs.
- Q-S5 B: persisted selected excerpts, pinned references, copy manifests and
  cascade erasure. Read optimisations cannot weaken copy accounting.
- O1..O4: accepted balanced starting limits, 4,096-token rendered evidence budget,
  gated vocabulary (expanded to 53 predicates/44 keys by BI-Q2 B), immediate tombstone denial with tracked
  cleanup, development backups off and production content backup expiry within
  seven days. Starting limits are not measured service guarantees.
- No generative ingestion, hidden model downloads or semantic escalation. Optional
  non-generative models propose candidates; they never author trust or identity.
- Preserve approved IDs, sender/role separation, created_at/updated_at semantics,
  session -> conversation containment and explicit parent consistency. For independent roots, missing session IDs keep the approved generate-new default.
  Same-batch replies currently inherit their parent session/conversation. Stored-parent
  replies currently require an explicit matching session; RM-36 A is now selected: omission inherits the authorised stored parent session
  and conversation. Refined 2026-10-22: an explicit session that differs from the stored
  parent session is accepted in the supplied session, keeps reply_to as a cross-session
  link and starts a new conversation there; an explicit conversation that conflicts with
  the inherited one, cross-namespace parents and unresolved parents are rejected. This
  target change is not implemented by this documentation pass.
  Never drop a supplied reply link or rewrite conflicting supplied IDs.

## Selected BI-Q decisions

| Question | Selected contract | Completion boundary |
|---|---|---|
| BI-Q1 | A: source-bound unresolved mentions; canonical entity only with supported identity | Canonical schema and evidence/ambiguity tests still required |
| BI-Q2 | B: 53 predicates / 44 keys | Registration is not extraction activation or generic-domain completeness |
| BI-Q3 | Refined A: shared typed RoleSpec/RoleBinding for events and claims | Role slots, multiplicity, evidence and identity checks remain cutover work |
| BI-Q4 | B: cancel/requeue old-revision unfinished work on effective policy change | RM-43 A freezes scoped unfinished/retry-eligible work and explicit activation triggers; no implicit historical backfill |
| BI-Q5 | B: embedding/vector required for public ingestion profiles | Preserve core/simple mechanics using configured test providers; lexical remains independent |

The former recommendation table is removed to avoid competing instructions.
Additional generic-review questions were separately answered on 2026-10-22: RM-36 A (refined: explicit different session starts a new conversation with a cross-session reply link), RM-42 A, RM-43 A, RM-55 B (refined: merges visible only in the grantee view). Also decided 2026-10-22: RM-33 keep Base, RM-41 typed edge qualifiers inside BI-08 (temporal validity and known-time, domain/vocabulary family, provenance, assertion status, trust, scope/grant, sensitivity and context, all usable as traversal filters), RM-45 scoped one-value expectation plus conflict flag, RM-50 first families commitments/deadlines, decisions/work status, documents/citations plus a benchmark-informed survey. They supplement, rather than reopen, BI-Q1..Q5.

GLiNER Base versus Small (RM-33) is a later stage/profile promotion decision, not
a reason to block defining a model-independent schema. Keep the current default
until selected. Qualified graph queries (RM-41, inside BI-08) remain a capability gap until built: typed temporal, domain,
provenance, assertion-status, trust, scope, sensitivity and context qualifiers with filtered traversal.
Metadata properties alone are not a validated filtered-traversal contract.

## Proposed contract ownership

### Shared role contract selected for the cutover

Use one `RoleSpec` registry and one future canonical `RoleBinding` shape:
`binding_id, role_name, role_version, value, evidence_ids`. The value uses the
single tagged Value union; entity values point to EntityRef, including unresolved
source mentions. Evidence IDs must resolve to permitted pinned support/binding
evidence. Role acceptance never follows from the role name or model confidence.
Role cardinality belongs to a predicate/event role slot, not a universal assumption
that every role has one participant. Validate per-slot minimum/maximum, expected
value/entity types, unit compatibility and distinctness where applicable.

| Input | Representation | Avoid |
|---|---|---|
| Mira gave Leo a guitar | One transfer occurrence; giver=Mira, recipient=Leo, item=guitar. Source-perspective gave/received claims reference that occurrence and reuse its bindings | Duplicate transfer events or fabricated independent corroboration |
| Mira prefers tea to coffee | One preference claim; preferred=tea, alternative=coffee, supported by exact spans | Fabricated occurrence or inferring dislike of coffee |
| Agent suggested a route to Mira | Suggestion occurrence with proposer, recipient and action; role authority remains separate from assertion owner | User-confirmed intention or completed journey |
| Alex gave me a book; Alex ambiguous | Giver binding points to the unresolved Alex mention; candidate entity IDs remain annotations | Choosing a canonical Alex from lexical/vector score |

The first pure registry slice provides RoleSpec definitions and required-role-name
checks. It does **not** implement RoleBinding persistence, per-slot multiplicities,
source hydration or event-identity reconciliation; those remain cutover gates.

### Policy activation and cancellation contract selected for the cutover

For each effective policy change, durably publish the new desired policy revision
and invalidate old-revision commit authority at the same activation boundary.
Cancel/supersede old pending work, request cancellation of in-flight work and
reconcile eligible source manifests to replacement jobs for the latest revision.
Eligibility and activation/digest triggers are selected under RM-43 A below.
No completed historical work is implicitly backfilled.
Scanning/requeue is bounded and resumable; rapid successive changes coalesce to
the latest desired revision. A source/window plus policy revision provides a
retry-stable job identity. Completed historical effects remain as recorded;
automatic historical re-embedding or replay is not implied.

Commit compares the desired revision, lease epoch, pinned source content version,
erasure generation and current permission/revocation state. A cancelled model
call that returns late cannot commit. If the replacement policy is invalid or
required providers unavailable, surface deferred/failed work and preserve raw
recoverability; do not silently use the old policy. Cancellation is cooperative,
while commit rejection is authoritative.

### Required capabilities selected for the cutover

All supported public ingestion profiles require configured compatible embedding
and vector providers. Missing capabilities fail configuration/admission clearly;
this does not require generating text or making every write wait for projection.
After durable raw acceptance, transient projection failure remains retryable and
visible. Lexical and graph remain independently configured optional projections.
Pure schema/registry tests may use fixtures and do not constitute an SQL-only
public ingestion profile. No existing profile enforcement has changed yet.

| Surface | Contract / invariant | Existing owner to replace or evolve |
|---|---|---|
| Input | Strict message envelope; unknown keys rejected; sender is distinct from subject/asserting principal | ingest/message_schema.py, message_identity.py, message_ingestion.py |
| Scope and IDs | Tenant/owner/namespace validated by gateway; stable message/claim/occurrence/effect IDs remain distinct | schemas/durable/common.py, identity.py; ingest/gateway.py |
| Source | Pinned source-content revision/hash/clock/erasure generation; annotation revision does not mutate raw version | schemas/durable/evidence.py; ingest/intake_store.py; store/records.py |
| Entity/mention | Stable evidenced identity versus exact unasserted source occurrence | schemas/durable/entity_mentions.py, assertion.py; derive/entity_registry.py |
| Claim | One canonical typed claim with modality, authority, time, evidence and lineage | schemas/durable/assertion.py, assessment.py; derive/compile.py |
| Time | Aware resolved values, unresolved original text, occurrence/state/known-time separation | schemas/durable/temporal.py; derive/temporal.py |
| Domain events | Typed participants, stage and source-backed identity; separate event-log envelope | schemas/durable/events.py and explicit domain occurrence definition |
| Commit | Complete replayable typed effects, CAS revision/fencing, source validity and erasure checks | ingest/gateway.py; store/events.py, envelope.py; derive/integration_stage.py |
| Read bundles | Full selected excerpt copies, dependencies, coverage and rendered-token cost | schemas/public/read.py; read/tiers.py, obligations.py; read/pipeline |
| Semantics | Pure versioned specs and validated starter data; no model/database/provider constructors | New semantics package, separate from providers/registry.py |

Naming draft: use a distinct source-evidence snapshot name rather than colliding
with providers/switching.SourceSnapshot. Keep annotation changes as dedicated
system-authored events, not fake fact writes or raw-content revisions. Candidate
API names ingestion_status and wait_for_derivation require final signature design
against the existing Engine facade; this note does not create callable APIs.

## RM-22 fixture specification

Fixtures are invented discussion cases, not benchmark gold or observed outputs.
Executable behavioural assertions follow the schema choices; JSON validity alone
does not prove identity, grounding, time arithmetic or erase/replay correctness.

| Fixture | Input and expected meaning | Required negative / unresolved variants |
|---|---|---|
| F1 residence | Authenticated sender: moved Porto to Lisbon last month; supported source clock yields a period, not an exact move day | Visit/work/buying another home cannot silently replace residence; missing timezone remains unresolved; multiple homes allowed |
| F2 identity | Met Alex in Cambridge; later pronoun can bind a unique local mention without selecting a canonical Alex/place | Two Alex candidates, two antecedents, cross-namespace alias, repeated Unicode spans; no nearest-name merge |
| F3 correction | Reply: Correction, I meant Coimbra, not Lisbon; target the supported prior claim | No fabricated Lisbon-to-Coimbra move; ambiguous target remains unresolved; unauthorised principal cannot replace another claim |
| F4 modality | Agent suggests moving; user declines and separately reports a visit | Suggestion is not user intent, visit is not residence, denial is not permanent personality; quoted/conditional assertions rejected or annotated |
| F5 later evidence | Earlier home-country origin; later explicit binding identifies Sweden | Both supports required; earlier known-time view stays unresolved; no duplicate move or exact-date invention; erased binding cannot reauthorise resolution |

All fixtures also require deterministic retry identities, source-version mismatch,
copy-erasure accounting and missing-evidence cases where applicable. Pure validator
tests belong with schemas/semantics; live persistence, queue and model acceptance
belong to their BI packages and must not be claimed by a shape test.

## RM-23 semantic registry preparation

Build PredicateSpec and attribute/role/type definitions without importing models,
database clients or runners. One immutable versioned registry owns the starter
names; loading rejects duplicate definitions, unknown versions and invalid role,
type, value or unit combinations. Registration never activates an extraction rule.
Preserve explicit-only transitions, multiple residences and open-world semantics.
Use the accepted vocabulary data as review input, not a runtime dependency on
docs/schema_review files. Put runtime data inside the installed package.

First foundation implemented on 2026-10-22: `src/memspine/semantics` packages
53 predicate definitions, 44 property definitions and shared role definitions.
It checks closed-world/transition constraints, types, registered categories/keys,
explicit units, count integrality/context and required role names. Its digest is
deterministic and public definition maps/models are read-only. It creates no
facts, entities, bindings or events and activates no extraction rules. Predicate
compatibility does not prove evidence, authority, count completeness, comparison
truth or source identity. Canonical type/role consumers still need integration.

Focused foundation gate: 23 tests pass; Ruff check/format pass; mypy reports no
issues in the three Python source files. See the dated verification artifacts.

## RM-24 coordinated cutover and focused gates

1. Apply the recorded BI-Q1..Q5 answers and freeze exact schema/semantic invariants. Retain Q-S/O
   decisions; do not reopen them or infer acceptance of proposed extensions.
2. Produce a source/import/serialization map and select the canonical exports.
   Reuse algorithms; replace affected public/durable shapes directly.
3. Implement pure schema and semantic validation with F1..F5 focused fixtures.
4. Update compiler, gateway, persistence, projectors, graph, readers and API
   serializers together. No partially compatible production path or dual-write.
5. Run focused consumer and negative tests; verify zero generation and no hidden
   alternate ingestion path in the selected profile. Map remaining feature gates
   to BI-02 onward rather than declaring them implemented by contract work.
6. Promotion requires the relevant BI integration gate. No automatic historical
   database conversion, copying, re-embedding, commit, push or deployment.

Current preparation checks validate only the existing review schema, examples,
52-requirement mapping, starter name uniqueness, source inventory and tracker/view
consistency. They do not certify the proposed runtime or change any defaults.

## Generic-memory review and BI-01 freeze gates — 2026-10-22

This section reconciles the attached review with current code. It is design
context; all current priorities, ownership and task statuses live only in the
single tracker (RM-36 and RM-42..RM-56). The 53/44 catalogue is an initial generic
vocabulary, not an exhaustive ontology. Existing nine-memory implementations and
provider checks do not prove the new contracts integrated across all types.

### Verified observations and qualifications

- Packaged definitions contain 53 predicates, 44 properties and 12 role specs;
  all 53 predicates have cardinality many, closed_world false and no enabled
  extraction rules. Nine predicates declare required roles. Origin allowlists
  contain raw_message, document and attachment_text only.
- `message_identity.normalise_messages` inherits same-batch parent scope, but
  explicitly rejects a stored-parent reply with no supplied session. Therefore
  the review identifies a real target-policy gap, not failure of every reply.
  RM-36 A now selects authorised inheritance as the target; current rejection remains until implementation. Never silently
  delete reply_to or bypass session/namespace checks.
- Tool messages are already accepted through role=tool; SourceEnvelope also
  declares kind=tool_log. Gateway adapters cover messages, documents and assets.
  The gap is a complete typed tool/structured-source contract and provenance
  mapping, not proof that tool content cannot enter the engine.
- Core is not declared SQL-only: the common runtime builds configured embedding
  and vector services. A bare mechanics profile is not equivalent to an absence
  of providers. Preserve core/simple focused tests with compatible test providers;
  do not reinterpret BI-Q5 B or require a generative model or external cluster.
- SemanticRegistry accepts supplied definitions through its constructor. There
  is no supported versioned domain-pack loader/plugin contract yet, and version
  values other than 1 are rejected. Existing MOD-02 plugins are not automatically
  semantic packs. No arbitrary dynamic predicates or last-loaded-wins overrides.
- Most memory paths cross common record/write/derive surfaces indirectly;
  associative/graph_outbox also imports durable OwnerScope. Import-name scans
  alone cannot establish the full cutover scope or prove a path unaffected.

### Raw-first fallback and policy activation (selected 2026-10-22)

Raw accepted, policy-permitted source remains the durable foundation. Registered
facts are optional enrichment of content, not a condition for preserving source.
Embedding/vector requirements concern ingestion capabilities, not a requirement
that every sentence match the ontology. Unknown concepts must not be forced into
the nearest predicate using BM25, embeddings or a model score.

RM-42 A selected: preserve source/mentions and add a typed unmapped-statement
annotation only when a bounded non-generative stage locates a supported statement.
It records pinned source/span, reason, producer revision and retry eligibility;
it is not an asserted fact and creates no graph truth. Unsupported language may
remain raw plus mentions without fabricated statement boundaries. Raw/mentions-only was the unselected alternative. No generative fallback or automatic vocabulary creation.

RM-43 A selected: eligibility includes pending, in-flight and retry-eligible
failed derivation intents within the activated policy's scope. Exclude completed
historical work, terminal failures without separately authorised retry, erased
or excluded source, and revoked access. Publish a revision when effective scoped
policy changes; an activated semantic-registry digest is part of that revision.
An unactivated catalogue edit or unrelated documentation change does not trigger
requeue. Replacing all relevant unfinished jobs preserves BI-Q4 B. Historical
backfill stays a separately selected bounded operation; no automatic re-embedding.

### Registry contract gaps and proposed remedies

| Gate | Gap in the foundation | Proposed generic contract |
|---|---|---|
| RM-45 | All predicate cardinalities are many | Selected 2026-10-22: scoped one-value expectation plus conflict flag. Express scoped subject/property/role cardinality separately from counts of source claims. A one-value expectation flags competing evidence; it never authorises deletion, unsupported supersession or collapsing different precision/time contexts |
| RM-46 | No typed symmetry/inverse/event-family semantics | Declare logical symmetry, query-view inverses and shared event families. sibling/spouse/friend symmetry is a view over supported claims, not a fabricated independent source. child_of can remain a query view rather than silently adding a 54th predicate. gave/received share a transfer identity only with supported matching |
| RM-47 | General event roles and role-slot cardinality absent | Add typed actor/location/time/instrument/beneficiary/topic roles only with compatible value types, evidence and per-slot min/max. Time uses the canonical TemporalRef; multiple participants need not be invented groups. One RoleBinding contract is shared by events and claims |
| RM-48 | Three source-origin allowlists lack generic source contracts | Define tool output, structured/API records, observation/reflection, caption, OCR, transcript and catalogue provenance through canonical adapters. Origin, sender, assertion owner and trust are distinct. Tool/reflection/summary content does not establish execution success or independent corroboration by itself |
| RM-51 | Taxonomy lacks explicit concept/document/software/event distinctions | Review a small typed hierarchy with open-world unresolved handling; preserve instance/type and real/fictional distinctions. Do not coerce unknown entities into people or merge document/event IDs with raw-source/event-log IDs |
| RM-52 | No predicate-level sensitivity/retention hints | Optional policy labels may strengthen source-derived restrictions, never weaken firewall, scope, erasure or legal-hold obligations. Sensitive content can appear in any predicate; predicate labels alone cannot classify all personal data |
| RM-53 | No explicit domain-pack lifecycle/plugin contract | Versioned data manifests, scoped names, dependencies, pinned digests and conflict rejection; no executable extraction enabled by installing a pack. Withdrawn definitions stop new jobs; retain necessary historical payload/erasure provenance without compatibility aliases or silent reinterpretation |

Missing domain vocabulary (RM-50) needs requirements review before names are
added beyond the selected 53. Selected first on 2026-10-22: commitments/deadlines/schedules,
decisions/blocking/work status and authorship/citation/document versions, plus further
generic families found by surveying the repo evals and benchmarks (evidence only). Families include commitments/deadlines/schedules,
decisions/blocking/work status, authorship/citation/document versions, tool
attempt/outcome/experience and attributed beliefs/feelings. Prefer existing
typed event stage, lifecycle links, role slots and state fields where sufficient;
do not create redundant predicates for every workflow status. Reported beliefs
remain attributed assertions, not verified world facts or inferred psychology.

### Nine-memory cutover surface map (RM-49)

| Memory type | Write/lifecycle surface | Read surface and focused regression obligation |
|---|---|---|
| Working | manager page-out and common record/event doorway | Buffer enforcement, overflow and source ownership; no intrinsic model work added |
| Episodic | raw message acceptance, sessions, conversation and source clocks | Timeline/session/history reads preserve ordering and approved IDs |
| Semantic | compiler, canonical claims, typed values, identity, evidence and integration | Conflicts, state history, typed query constraints and complete supporting bundles |
| Resource | ingest_data/asset adapters, extraction/chunks/source versions and erasure | Chunk retrieval, reported content, parent lineage and copied-source cleanup |
| Procedural | skill/plan/lesson routing, promotion/deprecation events and lineage | Plan/advisory recall and validity; no hidden generated skill or source-trust promotion |
| Reflective | caller-supplied reflection with parent/depth/trust checks | Reflection lineage and inherited source families; not independent corroboration |
| Associative | source-bound mention/claim projections and graph outbox | Scoped traversal, occurrence identity, optional temporal capability and reverse erasure |
| Prospective | watches/commitments/deadlines and acknowledge/lifecycle events | Explicit now/due checks, idempotent triggering and source-backed cancellation |
| Shared | grants/revocations, source-qualified refs and dependent caches/copies | shared_search permission checks at hydration/commit; revoked access cannot survive in copied bundles |

Use MEMORY_TYPE_PROVIDER_MATRIX.md as existing-path evidence, not new cutover
acceptance. Cover core/simple mechanics and the affected public ingestion paths
with focused tests; a common provider boot is not nine-memory proof.

### Expanded synthetic fixtures (RM-54, supplements F1..F5)

| Fixture | Generic input | Required outcome / negative |
|---|---|---|
| F6 document | A release note states that project X requires version 4 | Attribute the report to a pinned document/chunk; no user assertion or demonstrated compatibility inferred |
| F7 tool | Tool result includes operation ID, timestamp and status | Preserve tool source/role and structured provenance; quoted success text alone is not authenticated external execution evidence |
| F8 commitment | Agent commits to a report by an aware deadline; later explicitly cancels | Separate commitment, actor, deadline and supported cancellation; no completion from intention or first mention |
| F9 group | Three named participants discuss travel; one says we may attend | Preserve individual senders/replies; do not infer all participants are attendees or collapse names into one actor |
| F10 preference | Initially likes tea; later explicitly dislikes it | Preserve time/authority/history; later evidence is not visible to an earlier known-time query |
| F11 numeric | Explicit distance 5 km and price EUR 12.34 | Typed property/unit/currency; reject kg-as-distance, missing currency, fractional counts and guessed conversions |
| F12 language | Non-English text with stable message IDs and explicit replies | Preserve original text, IDs, exact spans and supported links; unsupported semantic rules remain unresolved, not mistranslated facts |
| F13 shared grant | Namespace A grants B read access and cited identity evidence supports a merge, then A revokes access | Grant-authorised merge is source-qualified and reversible; revocation invalidates its dependent access/copies while independent local identity remains |

Fixtures specify expected contracts, not completed executable tests or additional
accepted vocabulary. Deletion, replay, scope and missing-evidence negatives must
be included. Model/generative calls and benchmark-gold rules remain excluded.

### Shared identity boundary (RM-55 B selected 2026-10-22)

The owner selected **authorised read grants may create cross-namespace identity
merges**, superseding the recommendation to require a separate identity-link
permission. A grant supplies authority, not identity evidence. Existing E1 still
requires stable identity or explicit cited identity/alias evidence; matching names,
embeddings or coattendance cannot merge entities. A grant alone is not evidence
that two identities denote the same entity.

Design merges as source-qualified, grant-scoped reversible projections over stable
namespace-owned identities, rather than destructive global ID rewriting. Each
binding records grant ID/revision, allowed scopes, pinned identity evidence,
known-time visibility and dependent claims/bundles/indexes. Writes to original
namespaces still require their existing write authority; read permission does not
become permission to mutate another owner's source records.

Visibility (selected 2026-10-22): the merge and the grantee's identity evidence exist only in
the grantee view. The grantor never sees them unless a separate grant runs in the other
direction; F13 must assert this.

Revocation or identity-evidence erasure invalidates the grant-derived merge and
its dependent visibility/copies. Independent local identities/evidence remain;
if several independent grants/evidence paths justify the same merge, retaining
it requires an independently valid authorised path, not a stale cache. Earlier
known-time views cannot reveal later merge evidence, and current revoked reads
remain denied regardless of historical visibility. Tests must include conflicting
identity evidence, partial grant scope, chained grants without assumed transitive
rights, revoke-during-derivation, cached bundle hydration and source erasure.

This is the selected design boundary and proposed reversible mechanism, not an
implemented shared-identity feature or a promotion of existing PA-083 evidence.

## Benchmark-informed vocabulary and qualifier survey — 2026-10-22 (RM-50, RM-41)

Requested by the owner with the RM-50 decision. Repo evals and benchmarks were used as
**evidence of generic requirements only**: no dataset-specific names, aliases or answer
rules enter the vocabulary. Proposals below are not registered or activated; each needs
owner review (tracker rows RM-57..RM-63) and BI-06 gating before extraction.

### Sources surveyed

Data present: LoCoMo, OP-Bench (on LoCoMo), MemoryAgentBench conflict-resolution split,
ConvoMem. Loaders or fixtures only: LongMemEval (owner-excluded from the suite), BEAM,
HaluMem, PerLTQA, PersonaBench, PrefEval, LoCoMo-Plus, LaMP-2, MemoryCD, TOFU/MUSE-News,
CPB Live, ASB, StateMemBench (registry only), plus in-house sets in `evals/labelled/`.
Loaders live in `evals/memspine_evals/datasets/`; requirement mapping reuses
NON_LLM_GAP_REGISTER.md (NL-xx).

### Not vocabulary (engine capabilities)

Knowledge update and supersession, conflict resolution, abstention / memory boundary /
false premise, temporal ordering and arithmetic, counting completeness, multi-hop
traversal, summarisation, erasure, relevance gating (over-personalisation) and
poisoning/trust. These are claim-state, qualifier, retrieval and policy work; do not
model them as predicates.

### Selected first families (owner, 2026-10-22)

One **shared lifecycle-stage list** serves commitments, work status and event lifecycle
(RM-57): proposed, planned, scheduled, accepted, declined, open, in_progress, blocked,
done/fulfilled, missed, cancelled, postponed, rescheduled, superseded. No predicate per
status.

| Family | New candidates | Reuse |
|---|---|---|
| (1) Commitments and deadlines | `committed_to` (obligor, beneficiary/recipient, action), `due_by`, `requested_of` (requester, addressee); key `priority` | `assigned_to`/assignee, `intends` (no obligation), `completed_on`, `recurrence_interval`; due triggers connect to existing watches |
| (2) Decisions and work status | `decided` (decider; chosen/rejected via preferred/alternative), `works_on`; keys `progress`, `outcome` | `depends_on` + stage blocked, `has_role`, `manages`, `domain_status`, `explicit_reason`; reversal is supersession |
| (3) Documents and citations | `cites` (+ key `locator`), `about`, `derived_from`, `revision_of`; keys `publisher`, `licence` | `made`/maker for authorship, `part_of` for sections, `occurred_on` + stage published, title/version/edition/format/language/external_identifier/category |

### Further generic families found by the survey

| ID | Family | Candidates (reuse first) | Memory type | Priority |
|---|---|---|---|---|
| A | Social relationships | `partner_of`, `relative_of` + `kinship_type`, `colleague_of`, `acquainted_with`, `mentor_of`, `caregiver_of`, `neighbour_of`; key `relationship_type` | semantic, associative | P1 |
| B | Assistant-directed instructions and interaction preferences | `instructed` (instructor, addressee, action, context); `prefers` with interaction context; keys style/format/language reused, add `tone`, `detail_level` | procedural, semantic | P1 |
| C | Constraints and rules | `must`, `must_not`, `permitted` (or a polarity qualifier on `instructed`), `restricted_by`; roles condition, scope; key `rule_strength` | procedural | P1 (may merge with B) |
| G | Goals and wishes | `has_goal`, `wishes`; achievement is stage completed; keys `target_value`, `target_date` | prospective, semantic | P1 |
| K | Identity and contact attributes | `citizen_of`, `speaks`, `affiliated_with`; keys `occupation`, `pronouns`, `contact_channel`, `preferred_contact_time`, `nationality`; aliases stay in the identity system | semantic | P1 |
| D | Health and wellbeing | `has_condition`, `allergic_to`, `intolerant_of`, `takes_medication`, `experienced_symptom`; keys `severity`, `diagnosis_status` | semantic, episodic | P2, needs RM-52 sensitivity |
| E | Feelings, beliefs and values | `feels` (+ emotion, intensity), `believes` (holder + embedded claim), `values`, `worried_about`; attributed, never verified psychology | reflective, episodic | P2 |
| F | Skills, learning, education | `learning`, `teaches` (teacher, learner), `enrolled_in`, `studied_at`; reuse `has_capability`, `skill_level`; keys `field_of_study`, `qualification` | semantic, procedural | P2 |
| H | Communication acts | reuse `communicated_with` + key `speech_act` (ask, answer, inform, request, agree, decline, apologise, thank); roles speaker, addressee, content | episodic | P2 |
| I | Transactions and finance | `purchased` (buyer, seller, item), `sold`, `paid`, `lent`, `borrowed`; reuse gave/received transfer, price/cost; key `amount` | episodic, semantic | P2 |
| J | Possession lifecycle and opinions | `acquired`, `disposed_of`, `lost`, `repaired`, `returned` (close `owns` validity); `rated` + `rating`, `sentiment` | semantic, episodic | P2 |
| L | Habits and routines | `has_routine` (activity, context); reuse frequency/recurrence keys; keys `time_of_day`, `day_of_week` | procedural, prospective | P2 |
| M | Travel and trips | mostly covered; add roles origin, destination, companion; keys `transport_mode`, `participation_mode` | episodic | P3 |
| N | Tool and agent experience | `attempted` (agent, action, resource), outcome stage, `failed_with` + `error_type`, `workaround_for` | procedural | P3 (diagnostic) |

### Edge and claim qualifiers for filtered traversal (RM-41 inside BI-08)

| Tier | Qualifiers |
|---|---|
| P1 | valid time (half-open interval, precision, approximate); known/recorded time (as-of reads); assertion status (modality + polarity); lifecycle stage; claim state (current, superseded, contested, corrected, retracted + supersedes link); provenance/source origin (principal, source id/span, origin type, lineage root); authority/trust tier; subject/perspective binding; scope/visibility/grant/erasure state; event role binding + occurrence id; domain/vocabulary pack id and version |
| P2 | topical domain (relevance gating); sensitivity class; context/location/participation mode; applicability condition; extraction confidence and method (rule/model id, version, binding status); recurrence/occurrence series id; completeness (complete set vs observed lower bound) |
| P3 | independent-support count (distinct lineage roots) |

Qualifiers are typed fields on claims and edges, filled only from supported evidence or
system state; a missing qualifier stays unknown and is never inferred to pass a filter.
