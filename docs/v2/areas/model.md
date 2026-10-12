# Memory layers and data model

This area decides what a v2 memory *is*: the source of truth, the layer stack, the memory types, the
physical stores, the isolation scope, the shape of a record (assertion envelope, provenance, axes,
lineage, time), identity and cardinality rules, the graph objects (entities, edges, communities), the
derived forms (value histories, episodes, profile, working memory, compressed views, embeddings,
per-observer views), and how all of it is erased and migrated.

26 questions (M-01..M-26). 24 took the recommendation; 2 chose differently (M-02 chose D "two layers
only" instead of B; M-18 chose A "auto Ladybug else SQL adjacency" instead of D "pluggable with a
capability contract"). Five answers carry notes that combine or add options: M-02 "B + D", M-04 "SQL
graph to neo4j", M-10 "B + C", M-16 "C + D if possible", M-19 "A + D add extra, not remove
communities".

The notable points:

- **M-02 is read as "B + D"**: the explicit `layer` field exists on every record (B), but the base
  template populates L0 raw always, and L1 facts and L3 graph when a worker model is configured (I-7); summaries, episodes, graph, profile and
  communities stay in the schema as opt-in layers, switched on by templates. Read literally ("drop
  summaries, graph and observations"), D would contradict nine other answers in this area and seven in
  the offline area, so the combined reading is the only compatible one. Confirmed by the owner (Q1, 2026-10-12).
  Note that v1's base template already enables only working, episodic and semantic
  (`config/templates/base.yaml:42-47`), so the two-layer default is today's base behaviour made explicit.
- **M-13 (value histories + observations) conflicts with O-15 (no observation layer)**. The value-history
  half ships (it is the same thing as O-14 B, a deterministic code view); the observation half is held.
- **M-04 note and M-18 choice pull in different directions** on the server-profile graph. The proposal is
  A for the embedded profile now, SQL tables inside Postgres as the server default, Neo4j as an optional
  server-profile adapter later. Needs owner confirmation.
- **M-12 default for undeclared attributes** (coexist) differs from the write area's W-22 D (states
  contest, events coexist, learned cardinality); W-22 D is adopted as the registry's fallback rule.

## Design in one page

| # | Component | v2 design as decided | Ids |
|---|---|---|---|
| 1 | Truth | Append-only `memory_events` log is the only truth; SQL records, vectors, lexical, graph are replayed projections; a one-way Markdown export view per namespace whose edits re-enter as ordinary writes. Rebuild covers retained events only, per retention profile (ER-27 C). | M-01 |
| 2 | Layers | One record shape with an explicit `layer` field (L0 raw, L1 assertions, L2 episodes/summaries, L3 graph/temporal, L4 observations/profile, L5 standing answers) plus `derivation` (policy, prompt, schema, model, key versions). L0 always; L1 and L3 when a worker model is configured; L2, L4, L5 opt-in (I-7). | M-02, M-26 |
| 3 | Types | The nine memory types stay as a `type` tag mapped onto layers: working = context, episodic = L0/L2, semantic = L1, associative = L3 projection, reflective = L4, procedural/prospective = L5, resource = L0 documents; shared is a mechanism (grants), not a type. | M-03 |
| 4 | Stores | Two storage profiles from one event schema: **embedded** (SQLite + LanceDB + Tantivy + graph) and **server** (Postgres + pgvector + Postgres FTS + graph). Graph: embedded = auto Ladybug else SQL adjacency (today); server = **Neo4j** for the graph, fed only through the outbox, with records in Postgres (owner, Q4). | M-04, M-18 |
| 5 | Scope | Typed scope tuple (tenant, user, agent, session) on every row and inside every identity, cache and index key; the namespace path is derived from it. Mandatory viewer for multi-party reads (G-12). | M-05 |
| 6 | Record | Versioned assertion envelope: text, typed value, evidence spans, assertor + subject, modality + polarity, kind + occurrence, times, optional graph projection; unknown enum values fail visibly. | M-06 |
| 7 | Trust | Closed provenance class `src(m)` in {user_stated, assistant_proposed, user_confirmed, tool, document, external, derived}, per-class trust cap, plus an independent-support (proof) count. | M-07 |
| 8 | Axes | Typed indexed columns `spk`, `sub[]`, `addr`, `ask`, `modality`, `polarity`; a participants list kept for visibility (W-05 note); tags stay for free labels. | M-08 |
| 9 | Lineage | Lineage edge table (child, parent, parent_version, role) indexed both ways, with evidence span and source version per endpoint and derivation versions. | M-09 |
| 10 | Time | Tri-temporal with precision (source time, recorded_at, event interval with precision, validity for states) on every record; compiled graph edges expose the four Graphiti-style timestamps as a view of the same fields. Two-axis as-of reads (valid and known). | M-10, M-11 |
| 11 | Cardinality | Attribute registry (one/many, functional, exclusivity) with built-in defaults and namespace overrides; undeclared attributes follow W-22 D. | M-12 |
| 12 | Histories | Deterministic value-history view per (subject, attribute), built in code. Observations (consolidated beliefs) specified but **not built** while O-15 stays A. | M-13 |
| 13 | Grouping | Turn -> episode -> session -> saga, with membership records. Episodes and sagas are L2, opt-in. | M-14 |
| 14 | Entities | Opaque entity id, alias table, type and scope; merges and splits are reversible logged events; ambiguous mentions stay unresolved. | M-15 |
| 15 | Relations | Assertion-centric: the envelope is truth, binary edges compiled from it, n-ary facts keep a reified assertion node with role edges; plus untyped links (temporal, semantic, causal) between assertions as a second projection kind. | M-16 |
| 16 | Identity key | Versioned key (scope, src, rel, dst, kind) + occurrence id for events; archived keys go to the ladder; old-key aliases. | M-17 |
| 17 | Communities | Leiden + LPA communities with summary parents, opt-in (today); entity summaries added as an extra grouping kind; observation scopes wait on O-15. | M-19 |
| 18 | Profile | Capped typed card (identity / attribute / relationship / instruction), stated-only by default, each entry citing facts, relevance-gated; caller persona stays a separate pinned block. | M-20 |
| 19 | Working memory | Persisted working records with paging plus a per-session rolling summary of evicted turns. | M-21 |
| 20 | Compression | Multi-resolution per derived unit: gist (one line), note (~50 tokens), full pointer; raw stays authoritative. | M-22 |
| 21 | Embeddings | Multilingual small model as default (one vector); English model as a preset; GPU profile per P-17. | M-23 |
| 22 | Sharing | Type-scoped revocable grants with trust cap (N1) plus derived "about X as seen by Y" views at sleep. | M-24 |
| 23 | Erasure | Cascade delete of derived records at once, then deferred re-derivation from survivors with a generation check. | M-25 |
| 24 | Evolution | Per-event-kind schema version with upcaster chains; derived records store their derivation policy so a change re-derives only affected records. | M-26 |

Interface sketches (one per major component; no code):

```text
Record (one shape, all layers)
  id, scope{tenant,user,agent,session}, layer: L0..L5, type: one of nine, derivation{policy,prompt,schema,model,key_v}
  envelope{text, value, evidence[{source_id, source_v, span}], spk, sub[], addr, ask, modality, polarity,
           kind, occurrence_id, source_time, recorded_at, event{start,end,precision}, valid{from,to}}
  src_class, trust, support_count, res{gist, note, full_ptr}

StorageProfile (embedded | server)
  log: EventLog; records: RecordProjection; vectors: VectorProjection; lexical: LexicalProjection;
  graph: GraphProjection    # embedded: Ladybug | sqlite_adjacency ; server: pg_tables | neo4j (optional)

GraphProjection (existing GraphStore protocol, services/graph/base.py:88, extended)
  apply(event) ; neighbours(node, scope, depth) ; erase(source_ids) ; rebuild(from_offset) ; scope_check()
```

### Effect of the two-layer default (M-02) on the offline and read areas

- **Offline.** O-15 A (no observation layer) is consistent with the two-layer default. O-14 B (value
  history per key) is a code view over L1, so it needs no new layer. O-07/O-08/O-09 (session-close call,
  summaries, stitched episodes) produce L2 records; O-16/O-17 (entity summaries, communities) produce L3;
  O-18/O-19/O-20 (profile card, reflection) produce L4; O-21 (standing answers) produces L5. Under the
  B + D reading all of these keep their answers but run only when a template enables their layer; the
  base template runs none of them. O-21 depended on O-15 B; with O-15 A, standing answers cite L1 facts
  directly instead of observations.
- **Read.** R-17 B (lift derived hits to parent turns, one reranked list) gets simpler: in the base
  template the only derived layer is L1, so every fact hit lifts to its L0 turns through the M-09 lineage
  table and one reranker score ranks L0 only. When a template enables L2-L5, the same lifting rule
  applies (summaries and episodes lift through their membership/lineage rows; the R-10 episode leg only
  exists when L2 is populated). R-02 C's "derived as capped notes" carries L1 notes only by default.
- **No-model base.** G-21 B ships the base without a model, so a no-model base populates L0 only; L1
  appears once the write path has a model (W-12 online deposit).

## Decisions

### M-01 Source of truth for memory: E Event log + derived Markdown export view

- **Decision**: the append-only event log stays the only truth; all stores are projections; add a
  one-way Markdown export view per namespace, whose edits re-enter through the normal write door (W-01
  envelope) as `document`-class writes.
- **Verdict**: ok.
- **What will happen**: no read or write behaviour change; a new export verb. Quality 0, latency 0,
  cost small (export on demand), risk + (audit and replay unchanged; edits pass the firewall). Touches
  all types; operator D (deposit of edits).
- **Where**: `core/events.py:33-48` (EventLogMode), `config/schema.py:43` (EventLogConfig), `:79`
  (StorageConfig) -> v2 `log` (unchanged) + new `export.markdown_view`.
- **Depends on / affects**: G-14 retention and G-15 audit rely on the log; W-01 envelope receives the
  edits; M-26 upcasters keep the log readable.
- **Migration**: none for the log; the view is new.
- **Test or gate**: round trip test: export, edit one line, re-import, log shows one new WRITE with
  `src = document` and lineage to the exported record.

### M-02 Derived layer stack L0-L5: D Two layers only (raw + facts), note "B + D"

- **Decision**: combined reading. B is the schema: every record carries `layer` in {L0..L5} and a
  derivation-policy version, one store, per-layer indexes and budgets. D is the **default population**:
  the base template writes L0 raw and L1 assertions only. L2 (episodes, summaries), L3 (graph,
  temporal), L4 (profile, reflections), L5 (standing answers) remain defined and are enabled per
  template (`graph`, `multi_agent`, `personal` and so on). Nothing is dropped from the code.
- **Verdict**: needs-owner. Literal D (drop summaries, graph and observations) conflicts with M-13,
  M-14, M-16, M-19, M-20, M-22 and offline O-07, O-17, O-19, O-21; the B + D reading resolves all of
  them. Question: "Does M-02 mean an explicit layer field with only raw and facts populated by default,
  and the other layers kept as opt-in templates rather than removed?"
- **What will happen**: base behaviour matches today's base template (working, episodic, semantic on).
  Quality 0 on the base (estimate); latency 0; cost + (no background summarisation by default); risk +
  (erasure and budget can reason per layer; fewer derived copies to erase). Gives G-W25 per-layer read
  budgets a field to key on. Operators D, K_C.
- **Where**: `core/records.py:174-182` (MemoryRecord, "specialise behaviour, not shape"; layer implied
  by `memory_type` and tags), `config/templates/base.yaml:42-47` -> v2 `Record.layer` + `Record.derivation`
  and a `layers:` block in templates.
- **Depends on / affects**: depends on M-01. Affects M-03 (type-to-layer map), M-13, M-14, M-19, M-20,
  M-22 (all become non-default layers), offline O-07..O-21 (run only when their layer is enabled), R-02
  and R-17 (only L1 lifted by default), G-21 (base presets).
- **Migration**: on replay, assign `layer` from v1 `memory_type`, `source.channel` and tags (raw turns
  L0, window/mined facts L1, summaries/episodes L2, edges L3, reflections/profile L4).
- **Test or gate**: replay of a v1 store yields the same base read results (retrieval-only screen on
  LoCoMo, R@k within the noise band, G-03); every record has a layer; per-layer erasure test.

### M-03 Which memory types v2 keeps: B Nine as a type tag, mapped to layers

- **Decision**: nine names kept as a record type; fixed type-to-layer map (working = context,
  episodic = L0/L2, semantic = L1, associative = L3 projection, reflective = L4, procedural and
  prospective = L5, resource = L0 documents); shared becomes the grant mechanism.
- **Verdict**: ok. Holds under the B + D reading of M-02: types whose layer is not populated by default
  (associative, reflective, procedural, prospective) are template opt-ins, as today.
- **What will happen**: fewer store-like code paths (associative and shared stop being stores);
  quality 0 to + (estimate), cost +, risk +. Operator D.
- **Where**: `memories/{working,...,shared}` packages, `config/templates/base.yaml:42-47`,
  `core/namespace.py:56-82` (grants) -> v2 `TypeRegistry` (type -> layer, enabled-by-template).
- **Depends on / affects**: M-02; M-24 (shared = grants); O-24 procedural; O-23 prospective.
- **Migration**: map v1 `memory_type` to type tag + layer on replay; shared records become grants.
- **Test or gate**: every v1 record type maps to exactly one (type, layer); template opt-ins unchanged.

### M-04 Storage backend profile: C Two profiles from one log, note "SQL graph to neo4j"

- **Decision**: two storage profiles, both pure projections of one event schema. Embedded: SQLite +
  LanceDB + Tantivy + graph (M-18 A). Server: Postgres + pgvector + Postgres FTS + graph. The note asks
  for Neo4j in place of the SQL graph. Closest compatible reading: in the server profile the graph
  default is SQL tables in Postgres (keeps one transaction and one erasure surface, the reason C was
  recommended), and a Neo4j adapter is an optional server-profile graph backend applied through the
  outbox (W-10 C already sends graph projections through the outbox).
- **Verdict**: needs-owner. Question: "In the server profile, should Neo4j replace the SQL graph, or be
  an optional adapter beside a Postgres-tables default?" If Neo4j replaces it, encryption (ADR-035) and
  erasure proof (ADR-039) again span two systems.
- **What will happen**: embedded profile unchanged; server profile closes the "Lance/Tantivy files
  outside Postgres" gap. Quality 0 (Postgres FTS is weaker than Tantivy BM25, estimate small loss on
  lexical-heavy questions), latency 0, cost: two projection sets to test, risk +. Neo4j is new work: v1
  `services/graph/neo4j.py:16-18` is a stub that always raises `MissingServiceError`. Operator R.
- **Where**: `config/schema.py:79` (StorageConfig), `:88`, `:139` (VectorConfig), `:173-186`
  (GraphConfig), `services/storage/`, `services/vector/lancedb_store.py:27`, `services/lexical/` -> v2
  `StorageProfile{embedded, server}` with per-profile projection sets.
- **Depends on / affects**: M-01; M-18 (graph backend), M-05 (scope keys in each store), M-23 (pgvector
  vs Lance index), M-25 and G-13 (erasure per store), W-10 (outbox), G-19 A (library-first: the server
  profile is a storage option, not a deployment mode).
- **Migration**: embedded v1 stores replay into the embedded profile; Postgres v1 stores replay into the
  server profile, dropping the side Lance/Tantivy files.
- **Test or gate**: one contract suite run against both profiles (write, search, update, delete, rebuild,
  erase, isolation, RV21); lexical screen comparing Postgres FTS with Tantivy on a retrieval-only set.

### M-05 Isolation and tenancy model: B Typed scope tuple on every row and key

- **Decision**: explicit tenant/user/agent/session columns, included in every identity, cache and index
  key; namespace path derived from the tuple. Per-namespace partitions (C) and database per tenant (D)
  remain deployment options.
- **Verdict**: ok. Consistent with G-12 B (namespace plus mandatory viewer) and M-17 (scope in the key).
- **What will happen**: closes the "key omits scope" bug class (RV07, RV21); quality 0, latency 0, cost
  small (more columns), risk ++. Operator R (all reads filter on it), D.
- **Where**: `core/namespace.py:25` (validate_namespace), `core/records.py:191-197` (group_id, tags),
  `config/schema.py:170` (vector isolation), alembic 0005 (session_key) -> v2 `Scope` value object used by
  every projection and cache key.
- **Depends on / affects**: M-04, M-15 (entity scope), M-17 (key), M-24 (grants), G-12, G-16 (scoped
  handles).
- **Migration**: derive the tuple from the v1 namespace path segments on replay.
- **Test or gate**: extend the I19 leakage probe (12 configs, 0 leaks today) to every key and cache, plus
  shared-grant cases.

### M-06 Assertion envelope on records: B Versioned assertion envelope

- **Decision**: derived facts are versioned assertion envelopes (text, typed value, evidence[],
  assertor + subject, modality + polarity, source and event times with precision, kind + occurrence,
  optional graph projection, schema version); unknown enum values fail visibly.
- **Verdict**: ok. Same shape as W-15 C (note "B + C": the G-W49 item list is envelope v1).
- **What will happen**: negation, plans and attribution survive storage (RV03); quality + to ++
  (estimate), cost - (larger rows, more validation), risk +. Operator D. Types semantic, episodic.
- **Where**: `core/records.py:183-197`, `workers/windows.py:829` (fields dropped on deposit),
  `workers/pipelines.py:1882` -> v2 `AssertionEnvelope` (v1 = the G-W49 item list).
- **Depends on / affects**: M-02 (L1 is the envelope layer); M-07..M-12, M-16, M-17, M-26; W-15, W-17.
- **Migration**: v1 semantic records upcast to envelope v0 (text + key, unknown fields null, never guessed).
- **Test or gate**: every envelope field has a named consumer test (RV03); unknown enum fails.

### M-07 Provenance class and trust semantics: C Class + proof count

- **Decision**: closed `src(m)` set with per-class trust cap; `assistant_proposed` never becomes a user
  fact without explicit assent (W-08 C checked assent); plus a support count of independent sources.
- **Verdict**: ok.
- **What will happen**: stops the assistant-suggestion-to-user-fact loop; quality +, risk ++; N1 trust
  arithmetic gets a closed input set. Operator D. Types semantic, episodic, shared.
- **Where**: `core/records.py:111-128` (SourceInfo), `:231-234` (trust, corroborations) -> v2
  `Provenance{src_class, trust, support_count}`.
- **Depends on / affects**: M-06; W-08, W-07, G-06, G-07 (reach bound), R-16 (reinforcement boost).
- **Migration**: map v1 `source.role` / `channel` to a class; unmappable values become `external` with
  the existing trust (lower of the two).
- **Test or gate**: a model-proposed fact without assent never reaches `user_stated`; firewall tests.

### M-08 Speaker, subject and asker axes: B Typed indexed columns

- **Decision**: `spk`, `sub[]`, `addr`, `ask` as indexed columns (entity ids where resolved), modality
  and polarity as enums; tags stay for free labels; W-05's note keeps a participants list as well.
- **Verdict**: ok.
- **What will happen**: perspective filters become indexed column filters; latency +, quality +, risk +.
  Operator D (write), R (filter). Types episodic, semantic.
- **Where**: `core/perspective.py:1-30`, `:163-177` (Perspective.tags) and
  `config/schema.py:914` (perspective_mode) -> v2 `RecordAxes` columns.
- **Depends on / affects**: M-06; M-24 and O-26 (views derived from these axes); G-12 (participants).
- **Migration**: parse v1 `spk:`, `addr:`, `sub:`, `ask:`, `mod:`, `pol:` tags into columns on replay.
- **Test or gate**: tag-to-column parity on a replayed v1 store; perspective read results unchanged.

### M-09 Lineage representation: C Lineage edge table + evidence spans and source versions

- **Decision**: (child, parent, parent_version, role) rows indexed both ways, each assertion with source
  id, version, line and span per endpoint, plus prompt/schema/model versions; the v1 parents list stays
  as a field.
- **Verdict**: ok. Also the mechanism R-17 lifting and M-25 cascades use.
- **What will happen**: reverse lookups become key reads (faster cascades and cache invalidation);
  quality +, cost - (storage), risk ++. Operator K_C, D.
- **Where**: `core/records.py:128` (source.parents), `core/erasure.py`, ADR-039 walk -> v2
  `LineageProjection`.
- **Depends on / affects**: M-06; M-13, M-22, M-25, M-26; R-17, G-08, G-13.
- **Migration**: build lineage rows from v1 parents and CONSOLIDATE member ids on replay; spans unknown
  for old records (null, not invented).
- **Test or gate**: reverse lookup equals an event-walk result on a replayed store; restart keeps
  reply-to-source membership (RV07).

### M-10 Temporal fields on a record: B Tri-temporal with precision, note "B + C"

- **Decision**: B on every record (source time, recorded_at, event interval with precision and
  uncertainty, validity interval for states). C adds the edge view: compiled graph edges expose
  `valid_at = valid_from`, `invalid_at = valid_to`, `created_at = recorded_at`,
  `expired_at = superseded_at`, derived from the assertion's fields, never stored separately.
- **Verdict**: ok-combined. Compatible: C's four edge timestamps are a projection of B.
- **What will happen**: coarse dates stop claiming day-one precision; durations and backfilled history
  become correct; quality ++ on temporal questions (estimate), risk +. Operator D. Types episodic,
  semantic, associative.
- **Where**: `core/records.py:199-210`, `workers/pipelines.py:2924` (coarse date to day one) ->
  v2 `Envelope.times` + `GraphProjection` edge timestamp mapping.
- **Depends on / affects**: M-06; M-11, R-07 (event-time index), R-24, W-04 (time zone).
- **Migration**: v1 `valid_from` becomes the event start with precision `unknown` when it came from a
  coarse parse; `happened:` / `said:` tags fill event vs source time.
- **Test or gate**: "in 2024" stays year precision; edge timestamps equal the assertion fields.

### M-11 As-of read semantics: B Two-axis as-of

- **Decision**: `as_of_valid` and `as_of_known` as separate read parameters.
- **Verdict**: ok. R-24 C picks the default time view; M-11 supplies the capability.
- **What will happen**: audit replay ("why did it answer that last week"); latency 0, risk +. Operator R.
- **Where**: `core/read_filters.py:245-262`, `engine.py:1097` (`_current_at`) -> v2 `TimeView` filter.
- **Depends on / affects**: M-10; R-24, G-15.
- **Migration**: none (recorded_at and superseded_at already exist).
- **Test or gate**: a correction recorded later is hidden at `as_of_known` before it and shown after.

### M-12 Attribute cardinality schema: B Declared attribute registry with defaults

- **Decision**: registry attribute -> one/many, functional flag, exclusivity, built-in defaults,
  namespace overrides.
- **Verdict**: conflict with W-22. M-12 B says undeclared attributes default to coexist; W-22 D says
  events and facts coexist, states supersede only when declared or learned one-valued, otherwise
  contest. Proposal: W-22 D is the registry's fallback rule; learned cardinality entries are written to
  the registry as reviewable proposals. No owner question needed unless the owner wants undeclared states
  to coexist rather than contest.
- **What will happen**: lists stop being superseded by mistake; quality +, risk +. Operator D.
  Type semantic.
- **Where**: `core/query_contract.py:92` (cardinality only on the read side),
  `core/policies/conflict.py:60-90` -> v2 `CardinalityRegistry` consulted by the ladder.
- **Depends on / affects**: M-06; W-21, W-22, M-13, O-12, O-14.
- **Migration**: seed the registry from v1 `kind:event` and list-card classes (ADR-044).
- **Test or gate**: a two-valued list survives; a declared one-valued state supersedes; undeclared
  state contests.

### M-13 Value histories and observations: D Value lines for keyed state + observations for patterns

- **Decision**: as answered, both value lines and observations. But O-15 chose A (no observation layer),
  and M-02's two-layer default leaves L4 unpopulated. Closest compatible reading: build the value-history
  half now as a deterministic code view per (subject, attribute) (identical to O-14 B, note "A + B": the
  records keep their history and the view reads it); specify the observation half (proof counts,
  citations, trust = min of parents) but do not build it until O-15 is reopened.
- **Verdict**: conflict with O-15 (and M-02 literal, O-21). Needs owner: "Should v2 ship value histories
  only (as O-15 A implies), or also the observation layer M-13 D asked for?"
- **What will happen**: value-history half: compact "how did X change" answers at no model cost; quality
  + (estimate), latency +, risk +. Observation half (if built): fewer context tokens, LLM consolidation
  cost, drift risk. Operator K_C. Types semantic, reflective.
- **Where**: `core/records.py:165, 216` (ArchivedVersion, history), ADR-044 list cards -> v2
  `ValueHistoryView` (L1 view) and, later, `ObservationLayer` (L4).
- **Depends on / affects**: M-12, M-09; O-14, O-15, O-21 (standing answers then cite facts), R-24
  (history line rendering).
- **Migration**: none for the view (built from history and validity); observations would be new.
- **Test or gate**: A-B-A returns to A in the view (RV05); for observations, G-W29 gate: MAB-CR blind,
  context tokens down, no loss.

### M-14 Episodes, sessions and sagas: B Turn -> episode -> session -> saga records

- **Decision**: explicit membership records at each level; episodes by topic segmentation, sagas link
  sessions on one thread or goal.
- **Verdict**: ok, under the B + D reading of M-02 (episodes, summaries and sagas are L2, opt-in; the
  session id itself is L0 scope).
- **What will happen**: navigation and multi-session tasks; quality + where enabled, cost - (segmentation
  and summary calls), risk + (membership makes cascades exact). Operator K_C. Type episodic.
- **Where**: ADR-037 (session lifecycle), ADR-048 (summaries), `workers/windows.py:883` (episode
  fragments off), `engine.py:13771` (close_session) -> v2 `Grouping` records (episode, session, saga).
- **Depends on / affects**: M-02; O-07, O-08, O-09, R-10 (episode leg), M-21.
- **Migration**: v1 session ids become session records; v1 session summaries become L2 members.
- **Test or gate**: erasing one turn removes it from every membership and re-derives the summary (M-25).

### M-15 Entity identity model: B Opaque id + alias table + type + scope

- **Decision**: uuid entities with type, aliases and scope; merges and splits are reversible logged
  events; ambiguous mentions stay unresolved.
- **Verdict**: ok. Consistent with W-19 B and O-11 C.
- **What will happen**: homonyms stop merging; renames stop splitting; quality +, cost -, risk ++.
  Operator K_C. Type associative.
- **Where**: ADR-064 (name-derived id), `memories/associative/resolution.py` (resolution ladder) ->
  v2 `EntityRegistry` + MERGE/SPLIT events.
- **Depends on / affects**: M-05; M-16, M-17, M-08 (axes as entity ids), W-19, O-11, R-08.
- **Migration**: each v1 name-derived id becomes one entity with the old id as an alias.
- **Test or gate**: RV17: two people with one name stay two; a merge is undone by one event.

### M-16 Relation and edge representation: C Assertion-centric with reified n-ary nodes, note "C + D if possible"

- **Decision**: C as truth (envelope is truth, binary edges compiled, n-ary facts keep an assertion node
  with role edges). D added as a second projection kind: untyped links between assertions (temporal
  adjacency, shared entity, embedding similarity in code; causal only where the extractor marks it),
  carrying no relation semantics and never treated as facts.
- **Verdict**: ok-combined. Compatible, and D's links are what R-09 C (one-hop convergent expansion
  over shared-entity, semantic and causal links) needs.
- **What will happen**: no fake entity nodes; one fact truth; links give the graph leg convergent
  evidence; quality +, cost small (links built in code), complexity -. Graph is L3, on by default with a worker model (I-7).
  Operators D, R.
- **Where**: `core/events.py:57-61` (LINK), `workers/pipelines.py:1882` (`_edge_key`) -> v2
  `EdgeCompiler` (assertion -> edges, n-ary nodes) + `LinkProjection`.
- **Depends on / affects**: M-06, M-15; M-17, M-18, R-09, O-17.
- **Migration**: v1 edge records become envelopes with a compiled binary edge; LINK events keep their
  weight as untyped links.
- **Test or gate**: three-role fact gives one assertion node with three role edges; G-C3 / R-09 graph
  screen before any link is read.

### M-17 Assertion and edge identity key: B Versioned key with kind and occurrence

- **Decision**: (scope, src, rel, dst, kind) + occurrence id from cited turns for events; archived keys
  go to the ladder; key version with old-key aliases.
- **Verdict**: ok. W-20 C (versioned id plus event resolution) is the same key with re-mention
  attachment; the two are compatible and W-20 C defines the resolver step.
- **What will happen**: A-B-A returns to A; same-day repeated events stay separate; quality +, risk +.
  Operator D. Types associative, semantic.
- **Where**: `workers/pipelines.py:1882-1895` (`_edge_key`, G-W50) -> v2 `AssertionKey` (versioned).
- **Depends on / affects**: M-16; M-26 (aliases), W-20, G-23 (no duplicates on replay).
- **Migration**: recompute keys on replay, keep v1 keys as aliases.
- **Test or gate**: A-B-A, two same-day events stay two, replay idempotent (RV05).

### M-18 Graph store backend: A Auto Ladybug else SQL adjacency (today)

- **Decision**: chose A over D. The embedded default stays `graph.provider: auto` (LadybugDB when
  installed, else SQLite adjacency). Reconciled with the M-04 note: A now for the embedded profile; the
  server profile graph is decided under M-04 (Postgres tables default, Neo4j optional later). Without D
  there is no formal plug-in capability contract, but the existing `GraphStore` protocol stays and each
  backend that ships still gets the isolation, erasure and rebuild tests RV21 requires.
- **Verdict**: conflict with M-04 (note asks for Neo4j; A has no server option). Resolution proposed
  above; confirmed together with the M-04 owner question.
- **What will happen**: no change for embedded users; LadybugDB still runs without HNSW and without
  parameterised FTS because of three crashes (ADR-064: HNSW crashes 2 of 5 runs); exact in-engine cosine
  instead. Quality 0, latency + on embedded, risk - (young engine). Operator R. Type associative.
- **Affected decisions whose recommendation assumed D**: M-04 (server default "SQL tables" came from D;
  must be settled, see above), M-19 (communities run on whichever backend; holds), R-09 (graph leg;
  holds), M-25 / G-13 (erasure must reach the graph backend; holds if the tests are kept), W-10 (graph
  via outbox; holds), G-23 (ADR-064 default; holds). Only M-04 must be revisited.
- **Where**: `config/schema.py:173-186` (GraphConfig), `services/graph/base.py:88` (GraphStore),
  `services/graph/ladybug.py`, `sqlite_adjacency.py`, `neo4j.py:16-18` (stub) -> v2 `GraphProjection`.
- **Depends on / affects**: M-04, M-16; M-19, R-09, R-08, O-16, O-17.
- **Migration**: none (default unchanged); graph rebuilt by replay into the new edge model.
- **Test or gate**: per-backend isolation, erasure and rebuild tests; Ladybug crash probes rerun on each
  Ladybug release.

### M-19 Community and cluster layer: A Leiden + LPA hybrid, opt-in, note "A + D add extra, not remove communities"

- **Decision**: communities stay (Leiden + LPA, warm start, collapse guard, summary parents for
  communities of 3 or more, opt-in). D is read as extra grouping kinds alongside communities, not
  instead: entity summaries (O-16 B, already built, opt-in) now; observation scopes only if O-15 is
  reopened.
- **Verdict**: ok-combined. The observation-scope half conflicts with O-15 A; entity summaries are fine.
  Consistent with O-17 B (off by default, no further investment until a read leg consumes it).
- **What will happen**: no default behaviour change (L3, opt-in); quality unmeasured (no benchmark in
  scope shows a gain); cost of summaries only where enabled. Operator K_C. Types associative, reflective.
- **Where**: `workers/pipelines.py:1176` (reorganize), `memories/associative/communities.py:204, 348`,
  `config/constants.py:74-112`, `workers/pipelines.py:2621` (summarize_entities) -> v2 `GroupingKinds`
  {community, entity_summary, observation_scope (held)}.
- **Depends on / affects**: M-18, M-02; O-15, O-16, O-17, R-09.
- **Migration**: v1 MARKER partitions (ADR-043) replay unchanged.
- **Test or gate**: KB-8 calibration on real memory graphs; a global-question workload before any read
  leg uses communities.

### M-20 Profile and persona representation: B Capped typed card citing facts

- **Decision**: typed card (identity / attribute / relationship / instruction), code caps, stated-only by
  default, each entry citing source facts, relevance-gated injection; caller persona stays pinned.
- **Verdict**: ok. Consistent with O-18 B, O-19 B. Under M-02 the card is L4, opt-in.
- **What will happen**: compact auditable profile; must not leak into unrelated answers; quality + where
  enabled, risk +. Operator K_P. Types reflective, working.
- **Where**: `memories/working/persona.py:17`, `memories/working/paging.py:13`, `config/schema.py:777`
  (reflect_profile), `workers/pipelines.py:3484`, `config/constants.py:705` (PROFILE_PACK_MAX_SHARE) ->
  v2 `ProfileCard`.
- **Depends on / affects**: M-07, M-13 (value lines feed it); O-18, O-19, P-24 (stable prefix), R-18.
- **Migration**: v1 persona records stay as the pinned persona; reflect_profile records become uncited
  candidates, not card entries.
- **Test or gate**: OP-Bench irrelevance not worse; hand-audit entry precision >= 90%.

### M-21 Working memory and context state: C Persisted working records + rolling summary of evicted turns

- **Decision**: today's paging plus a per-session rolling summary of evicted turns kept in context.
- **Verdict**: ok. The rolling summary is working-memory state, not an L2 layer, so the M-02 default does
  not switch it off; it runs only when paging evicts.
- **What will happen**: long sessions stay coherent; one summarise call per eviction batch (cost -);
  quality + on long sessions (estimate). Operator C. Type working.
- **Where**: `memories/working/paging.py:13-21` -> v2 `WorkingMemory` + `RollingSummary` (ADR-048 prompt).
- **Depends on / affects**: M-14; P-24 B (stable prefix), R-18.
- **Migration**: none.
- **Test or gate**: evicted-turn recall on a long synthetic session (Ilse and Bram planning a trip over
  200 turns); token cost per eviction batch logged.

### M-22 Compressed multi-resolution representations: D Multi-resolution per record

- **Decision**: every derived unit carries gist (one line), note (~50 tokens) and a full pointer; the
  reader picks a level per budget; raw stays authoritative; token-compressed text (C) only as an optional
  extra level.
- **Verdict**: needs-measurement. Gate: focus slice and blind run with context tokens down and no QA
  loss (G-W41, G-W25) before it becomes default.
- **What will happen**: fewer context tokens, no stale copies (RV09); quality + (estimate), cost +
  (read), write cost - (gist and note text); in the base only L1 facts carry levels. Operator C. Types
  episodic, semantic.
- **Where**: `core/records.py:244-245` (content_zstd), `config/schema.py:1007` (gist_after),
  `workers/windows.py:894` (summary dropped) -> v2 `Record.res{gist, note, full_ptr}`.
- **Depends on / affects**: M-02, M-09; R-02, R-18, R-19.
- **Migration**: gist and note derived lazily on first read or at sleep for v1 records.
- **Test or gate**: tokens down at no loss on the focus slice and one frozen blind run (G-01).

### M-23 Embedding model and multi-vector: B Multilingual default, one vector

- **Decision**: a small multilingual model as default, English model as a preset; one vector per record.
  P-17 C extends it with a hardware-detected GPU profile (Qwen3-Embedding 0.6B class); compatible.
- **Verdict**: needs-measurement. Gate: on the English suite the multilingual default must be within the
  noise band of bge-small (retrieval-only screen), plus a small non-English probe (I23).
- **What will happen**: generic across languages; re-embed on switch (replay); measured: Qwen3-Embedding
  0.6B gave 0.615 vs 0.594 retrieval sufficiency at equal wall-clock (P-17); the CPU multilingual default
  is unmeasured. Operator R. Types semantic, episodic.
- **Where**: `config/schema.py:96-105` (EmbeddingConfig, bge-small default),
  `services/vector/lancedb_store.py:27` -> v2 `EmbeddingProfile` pinned per store.
- **Depends on / affects**: M-04; P-17, P-25 (re-derivation), R-05.
- **Migration**: re-embed by replay; the old embedder table stays until the new one is complete.
- **Test or gate**: retrieval-only screen on LoCoMo and ConvoMem, R@k within the G-03 band.

### M-24 Shared memory and per-observer views: C Grants + derived per-observer views

- **Decision**: keep type-scoped revocable grants with the trust cap (N1); add derived "about X as seen
  by Y" views at sleep from the M-08 axes, multi-agent template only.
- **Verdict**: ok. Same as O-26 B.
- **What will happen**: theory-of-mind reads in multi-agent setups; extra derived records (cost -);
  risk + (views inherit trust caps). Operator R, K_C. Types shared, reflective.
- **Where**: `core/namespace.py:56-82` (grant_allows), `engine.py:12911` (shared_search) -> v2
  `Grants` + `ObserverViews`.
- **Depends on / affects**: M-05, M-08; O-26, G-07 (reach bound), G-12.
- **Migration**: v1 grants carry over.
- **Test or gate**: reach-bound invariant holds with views on; leakage probe with a grant.

### M-25 Forgetting and erasure of derived text: B Cascade + deferred re-derivation

- **Decision**: delete affected derived records at once, queue re-derivation from surviving sources off
  the erasure path, with a generation check so stale results never publish.
- **Verdict**: ok. Same as G-13 C. Crypto-shredding stays a later ADR for regulated templates.
- **What will happen**: memory stays useful after erasure; re-derivation cost (-); risk ++. In the base
  (L0, L1, L3) the cascade is short. Operator K_C. All types.
- **Where**: `core/erasure.py`, `engine.py:9657` (forget), `:9699` (erase_subject), `:10230`
  (verify_forget), `:10321` (residual_risks), `:14409` (`_deposit_mined_fact` I72 re-check) -> v2
  `ErasureService` over `LineageProjection`.
- **Depends on / affects**: M-09, M-04; G-13, O-06 (watermarks), every derived layer.
- **Migration**: none beyond the lineage table (M-09).
- **Test or gate**: verify_forget clean after restart and during an in-flight derivation (RV07).

### M-26 Schema versioning and migration: B Event upcasters + derivation versions

- **Decision**: each event kind has a schema version and an upcaster chain; every derived record stores
  its derivation policy; a change re-derives only affected records; old keys keep aliases.
- **Verdict**: ok. Consistent with G-23 C (replay with a dual-read period).
- **What will happen**: safe evolution of the envelope, keys and time fields; cost - (upcaster tests);
  risk ++. Operator D. All types.
- **Where**: `core/events.py:122` (schema_version = 1), `services/storage/sqlite/alembic/versions` ->
  v2 `Upcasters` + `Record.derivation`.
- **Depends on / affects**: M-01, M-06, M-17; G-23, P-25.
- **Migration**: v1 events are schema version 1; upcasters 1 -> 2 produce the v2 envelope.
- **Test or gate**: replay of a v1 log through upcasters yields no duplicated facts (RV05).

## Conflicts and open points

| Id | Issue | Proposal | Needs |
|---|---|---|---|
| M-02 | Literal D drops summaries, graph and observations, which nine model answers and seven offline answers rely on | B + D: explicit layer field; base populates L0, plus L1 and L3 with a worker model (I-7); L2-L5 opt-in per template | owner confirmation |
| M-13 / O-15 | M-13 D wants observations; O-15 A says no observation layer | Ship the value-history view (= O-14 B) now; hold observations until O-15 is reopened and the G-W29 gate passes | owner: value histories only, or observations too? |
| M-04 / M-18 | M-04 note "SQL graph to neo4j"; M-18 A keeps embedded Ladybug/SQL with no server option; v1 Neo4j adapter is a stub | Embedded: A. Server: Postgres tables default, Neo4j optional adapter via the outbox, same contract tests | owner: replace or optional? |
| M-12 / W-22 | Default for undeclared attributes: coexist (M-12) vs kind-aware contest (W-22 D) | W-22 D is the registry's fallback rule; learned entries go to the registry as reviewable proposals | none unless the owner prefers coexist |
| M-19 / O-15 | M-19 note adds observation scopes as an extra grouping kind | Entity summaries now; observation scopes follow O-15 | follows the M-13 answer |
| O-21 | Standing answers recommended on top of observations (depends on O-15) | Standing answers cite L1 facts directly | none (offline area to note) |
| M-18 | No capability contract chosen | Keep `GraphStore` protocol and per-backend isolation/erasure/rebuild tests anyway (RV21) | none |
| M-04 / G-19 | Server storage profile while G-19 chose library-first inline runner | Server profile is a storage option of the library, not a deployment mode | none |

## Measurements to run before building

| What | Screen or dataset | Gate |
|---|---|---|
| M-02 layer field and replay of v1 stores | Retrieval-only LoCoMo screen on a replayed v1 store | R@k within the G-03 noise band; no duplicated facts (RV05) |
| M-04 server profile lexical | Postgres FTS vs Tantivy BM25, retrieval-only (LoCoMo, ConvoMem) | Loss within the noise band, or Tantivy kept as a server option |
| M-04 / M-18 graph backends | Contract suite on Ladybug, SQLite adjacency, Postgres tables (and Neo4j if kept) | Isolation, erasure, rebuild all pass; Ladybug crash probes clean |
| M-13 observations (only if O-15 is reopened) | MAB-CR blind | Context tokens down, no QA loss (G-W29) |
| M-16 links / graph leg | G-C3 arm `scr-gc3-graph-jina` and an R-09 one-hop arm | Gain on the focus slice and no blind-slice loss (G-01) |
| M-19 communities | KB-8 calibration on real memory graphs; a global-question workload | Any read use only after a measured gain |
| M-20 profile card | OP-Bench, PrefEval | Irrelevance not worse; hand-audit precision >= 90% |
| M-22 multi-resolution | Focus slice plus one frozen blind run | Context tokens down at no QA loss |
| M-23 multilingual default | Retrieval-only LoCoMo and ConvoMem; non-English probe (I23) | English R@k within the noise band; non-English recall up |
| M-05 / M-24 isolation | I19 leakage probe extended to every key, cache and grant | 0 leaks |
| M-25 erasure | verify_forget after restart and during inference | No retained payloads (RV07) |


## Owner follow-up (2026-10-12)

- **Q1 Layer field, two layers on** (as proposed): Every record has an explicit `layer` field; raw (L0) and facts (L1) are populated by default; episodes, summaries, graph, profile, observations and communities are opt-in layers.
- **Q3 Value histories plus pattern observations** (changed): Build the deterministic value-history view AND an observation layer for recurring patterns only (code-grouped, model-worded only for groups that changed). O-15 is reopened from 'none' to 'patterns only'; observations are an opt-in L4 layer (Q1). M-13 becomes histories plus pattern observations; M-19 keeps communities and entity summaries; observation scopes are allowed for patterns.
- **Q4 Neo4j is the server graph** (changed): Embedded profile: auto Ladybug else SQL adjacency (M-18 A). Server profile: Postgres + pgvector + Postgres full text for records, Neo4j for the graph, fed only through the outbox. M-04/M-18: server profile = Postgres + pgvector + Postgres FTS + Neo4j graph; embedded profile unchanged. The GraphProjection contract must cover apply, neighbours, erase, rebuild and scope_check on Neo4j.
- **Q7 Coexist unless marked** (as proposed): Undeclared attributes coexist (registry default); a new value contests the old one only when extraction marks a change; learned cardinality only as reviewable proposals.

## Change log

- 2026-10-11: first version; 26 decisions verified against the owner's answers and the write, offline,
  read, model-service and governance areas.
- 2026-10-12: owner follow-up answers recorded (Q1, Q3, Q4, Q7).
- 2026-10-12 (later): stale owner-confirmation markers resolved from the Q1-Q8 answers. M-04/M-18 summary row now says Neo4j (Q4).
- 2026-10-13: reconciled with ER amendments and owner decisions (consistency audit)
