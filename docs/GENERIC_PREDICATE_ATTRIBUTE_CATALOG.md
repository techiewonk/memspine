# Generic predicate and attribute catalog for review

## Owner selections — 2026-10-21 (supersedes earlier alternatives)

BI-Q1 A: unresolved source-bound mentions retain null canonical entity IDs and cited candidates/reasons. BI-Q2 B: adopt all **53 predicates and 44 property keys** in the coordinated rewrite; registration remains separate from tested extraction activation. BI-Q3 refined A: one versioned RoleSpec/RoleBinding contract is shared by events and claims; use event participants for occurrences and typed claim roles for non-event comparisons. No arbitrary qualifier dictionary or fabricated comparison event. BI-Q4 B: every effective ingestion-policy revision cancels/supersedes pending and in-flight old-revision work and requeues eligible sources under the latest revision. Cooperative cancellation alone is insufficient: commit fencing rejects old generations even if a model call finishes late. Preserve completed history; do not undo accepted effects or re-embed/copy historical data automatically. Current erasure/permission checks always apply. BI-Q5 B: every supported ingestion profile requires configured compatible embedding and vector capabilities; SQL-only ingestion is not a supported public profile. Lexical remains independently configurable and no generative LLM is required.

Selections are design acceptance, not completed runtime integration. Requeue must be durable, idempotent and bounded, with latest-revision coalescing, source-version/erasure checks and observable superseded/deferred states. Provider configuration is validated before admission; transient failure after raw acceptance must retain recoverable projection work rather than lose raw data or claim readiness. Role/type/unit constraints still need focused validation before extraction activation.

Date: 2026-10-18. Accepted starter: **43 predicates and 9 attribute keys**. Proposed additions: **10 predicates and 35 property keys**. This is a complete enumeration of this review catalog, not a claim to exhaust every possible domain. No runtime registry or extractor is implemented by this document.

## Generic design rule

Predicates describe relationships/actions/states; property keys say which property a generic attribute/measurement/count describes. Values retain their type and units. Domain events carry participants and occurrence identity. Evidence, modality, time, confidence, source identity and erasure status remain structured system fields, not arbitrary attributes.

One versioned registry with a generic core and explicit domain packs is recommended. Human family/residence relations are reusable within a human-domain pack; they are not universally valid for all entity types. No Caroline/Melanie/studio/Pride-specific predicates, location-specific attributes or gold-answer rules. Adding an ontology entry does not automatically add a safe extraction rule.

## Accepted predicate names — 43

| # | Predicate | Generic meaning / boundary |
|---|---|---|
| 1 | resides_in | Supported residence; multiple homes allowed |
| 2 | works_in | Work location; not residence |
| 3 | home_country | Source-described home country; not citizenship or birthplace |
| 4 | born_in | Explicit birthplace; human/appropriate living-entity domain |
| 5 | grew_up_in | Explicit childhood location; not current location |
| 6 | move_origin | Origin of one identified move |
| 7 | move_destination | Destination of that same move |
| 8 | visited | Visit occurrence; not residence |
| 9 | temporarily_present_in | Time-bounded presence without residence claim |
| 10 | parent_of | Explicit parent relationship; role/domain constraints required |
| 11 | sibling_of | Explicit sibling relationship |
| 12 | spouse_of | Explicit spouse relationship; no automatic partner equivalence |
| 13 | friend_of | Explicit friendship; coattendance insufficient |
| 14 | member_of | Membership in a group/organisation |
| 15 | employed_by | Employment, not mere contact or volunteering |
| 16 | owns | Ownership; possession or use alone insufficient |
| 17 | adopted | Explicit adoption event with appropriate typed target |
| 18 | made | Maker/creator action; does not imply ownership or gift |
| 19 | gave | Giver in transfer event; recipient/item roles required |
| 20 | received | Receiver in transfer event; same event ID prevents double-counting |
| 21 | commissioned | Commissioner, distinct from maker and recipient |
| 22 | read | Reading assertion with supported stage/modality |
| 23 | listened_to | Listening occurrence; not necessarily preference |
| 24 | watched | Viewing occurrence; not necessarily creation |
| 25 | played | Play/use occurrence; domain/type distinguishes game/instrument/activity |
| 26 | participated_in | Supported participation in identified activity/event |
| 27 | attended | Attendance; invitation/registration is insufficient |
| 28 | offered | Offering a course/service/item; not recipient adoption |
| 29 | likes | Explicit positive preference, scoped in time |
| 30 | dislikes | Explicit negative preference; absence of likes is insufficient |
| 31 | prefers | Preferred option; alternative requires an explicit comparison role |
| 32 | interested_in | Interest, distinct from skill/ownership/participation |
| 33 | intends | Intention toward action/goal; not completion |
| 34 | suggested_to | Recommendation with proposer, recipient and suggested action |
| 35 | started_on | Supported lifecycle start, not first mention |
| 36 | opened_on | Supported opening event, not intention to open |
| 37 | completed_on | Supported completion time |
| 38 | occurred_on | Occurrence time with event identity |
| 39 | has_quantity | Explicit quantity; proposed quantity key makes subject of counting explicit |
| 40 | has_measurement | Measured property; proposed dimension key distinguishes distance/mass/etc. |
| 41 | explicit_frequency | Explicit recurrence; observed intervals alone insufficient |
| 42 | explicit_reason | Stated explanation, not inferred causation |
| 43 | has_attribute | Allowlisted descriptive property with explicit attribute key |

## Proposed additional generic predicates — 10, not yet accepted

| Predicate | Meaning | Boundary |
|---|---|---|
| located_in | Location of an object, organisation or facility | Not personal residence |
| part_of | Structural/compositional membership | Not social membership; cycles/type constraints checked |
| uses | Explicit use of resource/tool/object | Not ownership or endorsement |
| requires | Requirement for an action/project/service | Not causal proof or preference |
| depends_on | Explicit dependency | Not merely co-occurrence |
| manages | Management responsibility | Not ownership or employment inferred |
| has_role | Explicit role in a scoped organisation/event/project | Role must carry context/time |
| assigned_to | Explicit task/project/resource assignment | Does not prove completion |
| communicated_with | Contact/communication occurrence | Does not imply friendship, travel or agreement |
| has_capability | Explicit capability description | Not demonstrated reliability or completed action |

Avoid adding inverse duplicates automatically. `made` versus `created_by`, `part_of` versus `contains`, and transfer roles can be presented as query views over canonical relations/events. Retain independently sourced claims where actually asserted; do not count inferred inverses as new evidence. `gave` and `received` are accepted source-perspective predicates sharing a transfer event, not two physical transfers.

## Attribute/property keys — 44 in full review catalog

### Accepted descriptive keys — 9

| Key | Value kind | Boundary |
|---|---|---|
| colour | text | Original wording; ambiguous shades retained |
| material | text/entity | Distinguish substance from maker |
| breed | text/entity | Explicit breed; not species or visual guess |
| model | text/entity | Product model does not identify physical instance |
| title | text | Exact title; not person identity |
| language | text/entity | Content language, not nationality |
| genre | text/entity | Source/canonical taxonomy provenance required |
| activity_type | text/entity | Type/category, not proof of participation |
| art_form | text/entity | Form such as painting; not a benchmark-specific activity |

### Proposed descriptive keys — 15

| Keys | Value kind | Required boundary |
|---|---|---|
| description, purpose | text | Source-stated description/purpose; no guessed motive |
| category | text/entity | Versioned category vocabulary; not entity identity |
| format, medium, style, shape | text/entity | Keep meanings distinct; avoid modifier attachment errors |
| condition | text | Object condition, not a generic medical diagnosis field |
| brand, manufacturer | text/entity | Distinct from owner, seller or maker in a specific event |
| edition, version | text | Domain version, separate from source/schema revisions |
| external_identifier | text | Identifier scheme/issuer needed; never replaces Engine IDs |
| domain_status | text/entity | Domain status with context/time; separate from claim grounding/status |
| skill_level | text/entity | Explicit self/source description; not inferred competence |

### Proposed measurement keys — 15

| Keys | Value | Boundary |
|---|---|---|
| length, width, height, depth, distance | number + unit | Axes and measured subject explicit |
| area, volume | number + unit | Squared/cubed units distinguished |
| mass, temperature | number + unit | Explicit unit; no guessed kg/lb/C/F |
| duration | number + unit or computed interval | Endpoint identity and arithmetic convention retained |
| speed, capacity | number + unit | Unit dimension and capacity kind required |
| age | number + unit | Time-valid observation, never timeless inferred profile |
| price, cost | number + currency/unit | Offered price versus incurred cost; currency must be supported |

### Proposed quantity keys — 3

| Key | Value | Boundary |
|---|---|---|
| item_count | nonnegative integer | Declared collection/scope and distinct item identity |
| event_count | nonnegative integer | Distinct event identity; no repeated-retelling count |
| participant_count | nonnegative integer | Supported event membership, not chat participant count |

### Proposed frequency keys — 2

| Key | Value | Boundary |
|---|---|---|
| occurrence_rate | number + per-unit | Explicit rate such as three/week; scope/activity required |
| recurrence_interval | number + time unit | Explicit interval such as every two weeks; not inferred from two observations |

Count: 9 accepted + 15 descriptive + 15 measurement + 3 quantity + 2 frequency = **44 keys**, of which **35 are proposed**. These are distinct key namespaces/categories, not universally valid attributes on every entity.

## Schema refinements required before adopting proposed keys

1. Current review schema permits attribute_key only with has_attribute. To use measurement/count/frequency keys, extend its predicate-key validation explicitly; do not silently store a key where the current schema rejects it.
2. Proposed registry entries need property category, applicable entity types, value kind, unit dimension, permitted cardinality, evidence origins and any required context qualifiers. Unknown keys remain unresolved.
3. Current Value.number unit/currency fields remain authoritative. Do not duplicate them as arbitrary attributes. Cost/price need currency; capacity needs dimension; IDs need scheme/issuer.
4. Multi-party predicates need roles: gave/received/commissioned, suggested_to and comparative prefers cannot be fully represented by ambiguous binary text alone. Use the existing event-participant mechanism where applicable; add typed claim qualifiers only after review.
5. Current role allowlists are starter drafts: born_in/grew_up_in/kinship should not apply to every agent or group. Objects such as facilities need located_in rather than resides_in. Animal/pet typing needs an explicit reviewed entity taxonomy, not automatic human/object coercion.
6. Catalogue-origin author/genre/place mappings remain a separate optional domain pack with provenance; their absence cannot be filled through name similarity.

These are newly identified design refinements, not implementation fixes and not newly accepted defaults.

## Samples

Accepted shape (other Claim fields omitted):

```json
{"predicate":"has_attribute","attribute_key":"colour","object":{"kind":"text","text":"blue","language":"en"}}
```

Proposed extension, not valid under the current key restriction until adopted:

```json
{"predicate":"has_measurement","attribute_key":"distance","object":{"kind":"number","amount":5,"unit":"km","currency":null}}
```

For a transfer, participants are giver/recipient/item with one shared event ID; two source claims do not create two events. For a preference comparison, swimming and running must retain preferred/alternative roles; liking swimming does not imply disliking running.

## Recommendation for review

Keep the accepted 43/9 starter vocabulary, add the 10/35 extensions only after their roles/types/units are finalised, and implement tested rule coverage incrementally. Use one registry with versioned generic-core and domain packs. No per-benchmark names, uncontrolled dynamic attributes or duplicate inverse fact generation.

[Machine-readable review catalog](schema_review/generic_catalog_review.json) separates accepted names from proposals; count/uniqueness checks passed for 53 predicate names and 44 property keys. This does not validate extraction or runtime role semantics.

## Semantic structure and usage guide — 2026-10-18

[SEMANTIC_REGISTRY_STRUCTURE_AND_USAGE.md](SEMANTIC_REGISTRY_STRUCTURE_AND_USAGE.md) describes the proposed pure semantic registry, one-pack ownership of all 43 starter predicates, property categories, role handling, ingestion/read flow and memory-layer mapping. Existing accepted policies remain unchanged. Proposed key-category/role/type refinements and extra 10/35 vocabulary items are not activated; target schema changes await review.
