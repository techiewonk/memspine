# MemSpine v2 architecture

> **Latest accepted amendments:** [ER-06, ER-24 and ER-31](DECISION_AMENDMENTS_2026-10-13.md)
> take precedence over older shorthand here: durable acceptance with per-capability readiness and optional waits;
> adaptive complete evidence bundles with exact token counting; scope/version-aware caches disabled until their
> contract exists. These are planning decisions, not implemented or measured capabilities.

The v2 design as the owner decided it (2026-10-11), end to end. Each block names its decisions; the reasoning,
predicted effect and v1 file:line for every one is in `areas/<area>.md`. Readings settled by the owner's
follow-up answers are marked (Q1..Q9), see `VERIFY.md` section 0.

```mermaid
flowchart TD
  IN[Source envelope: conversation, document, tool log, asset] --> CM[Commit: authoritative SQL rows + outbox intents (atomic); receipt with per-capability readiness]
  CM --> RX[Raw indexes: SQL now; BM25/vector when ready (optional bounded wait)]
  CM --> OB[(Durable local outbox Q2)]
  OB --> SEG[Adaptive window + time-gap cut, token-batched]
  SEG --> X1[Call 1: one example-free extraction call, ItemV1 list]
  X1 --> GR[Validate, ground spans, firewall, trust cap]
  GR --> RES[Resolve names in code, key with kind + occurrence]
  RES --> INT[Conflict ladder + cardinality registry]
  INT --> L1[L1 assertions; graph compiled from them Q1]
  CM -- session closed --> X2[Call 2: summary, stitched episode, profile ops]
  L1 --> SL[Sleep, per namespace job: reconcile, contradictions, near-dupes, histories, summaries, profile, maintenance]
  X2 --> SL
  Q[Question + depth tier] --> AN[Typed query contract, zero calls]
  AN --> LEGS[Legs: vector, BM25, temporal; entity, graph, episode when present]
  LEGS --> FU[Family fusion, RRF k=60, protected pool]
  FU --> LIFT[Lift to turns, re-admit, dedupe, cap ≤64]
  LIFT --> RR[Jina v3.5 rerank Q5]
  RR --> GATE[Relevance gate]
  GATE --> PK[Windows, rank-order packer, typed render with provenance]
  PK --> G[Reader model]
  G -- reply written back --> IN
```

## 1. Insert (write path): `areas/write.md`

1. **One intake shape.** A `SourceEnvelope` (conversation, document, tool log or asset) with ordered segments; each
   segment is one raw L0 record. Idempotency by caller id or a content fingerprint (W-01, W-03, W-24).
2. **Stamped once, never guessed.** Source time (nullable), declared time zone, recorded time; speaker, addressee
   and participants as typed columns, with the participant tag still written; provenance class from the role
   (W-04, W-05, W-08).
3. **Firewall at three gates**: the raw write, the extraction prompt (quarantined turns never enter it) and every
   derived item, with trust capped at the weakest parent (W-07, G-06).
4. **One commit, one receipt.** Authoritative SQL data and outbox intents commit atomically; independent indexes are
   projected after the commit. The receipt reports readiness per capability; callers may wait on a bounded barrier
   with a timeout (ER-06 A). The receipt returns a derivation intent id whose status can be read (W-02, W-10). New
   writes are rejected before acceptance when storage or quota limits are reached (ER-08).
5. **Durable derivation (Q2: decided).** A leased outbox in the same store, drained by the inline runner; acknowledged only
   after the deposit commits; under load windows are coalesced and marked underived, never dropped (W-13, W-25, G-19).
6. **Segmentation.** Adaptive window plus a time-gap cut; closed windows batched by tokens into one call, each a
   numbered block; per-turn mode stays a setting (W-11).
7. **Online extraction, after the write returns.** Example-free prompt v2 (about 353 tokens) with NEW, CONTEXT and
   KNOWN lines; schema-constrained decoding; per-item validation and one repair re-ask; the model emits `ItemV1`
   and code compiles the stored `AssertionEnvelope` (W-12, W-14, W-15, W-16, P-04).
8. **Grounding and resolution in code.** Evidence spans per assertion and endpoint, rule checks on numbers, polarity
   and names; entity ladder online without a model, uncertain names `pending_merge`; key = (scope, src, rel, dst,
   kind) plus occurrence (W-17, W-19, W-20, M-17).
9. **Integration.** Conflict ladder seeded by kind and transition wording; the cardinality registry sets the default
   for undeclared attributes (Q7: decided); unresolved cases deferred to K_C (W-21, W-22, M-12).
10. **Cost ledger** per call, layer and namespace, with an optional budget that defers rather than drops (W-26, P-22).

## 2. Store and layers: `areas/model.md`

- **Truth** is the append-only event log; SQL records, vectors, lexical index and graph are replayed projections; a
  one-way Markdown export per namespace (M-01); rebuild covers retained events only, per retention profile (ER-27 C).
- **One record shape with an explicit `layer` field** (L0 raw ... L5 standing answers). L0 always; L1 and L3 when a
  worker model is configured; L2, L4, L5 opt-in (I-7; Q1 decided). The nine memory types become a `type` tag mapped onto layers (M-02, M-03).
- **Assertion envelope**: text, typed value, evidence spans, asserter and subject, modality and polarity, kind and
  occurrence, tri-temporal times with precision, provenance class and trust, support count, derivation fingerprint
  (M-06, M-07, M-08, M-10, P-12).
- **Two storage profiles from one event schema**: embedded (SQLite, LanceDB, Tantivy, Ladybug or SQL adjacency) and
  server (Postgres, pgvector, Postgres FTS for records; **Neo4j for the graph**, fed only through the outbox; erasure, isolation and rebuild proven on both systems) (M-04, M-18, Q4).
- **Scope** tuple (tenant, user, agent, session) in every row and every key; a viewer is mandatory for multi-party
  reads (M-05, G-12).
- **Lineage** table both ways; erasure cascades through it and re-derives survivors (M-09, M-25, G-13).
- **Relations** are assertion-centric: edges compiled from assertions, n-ary facts as reified nodes, untyped links as
  a second projection (M-16). Communities and entity summaries stay opt-in (M-19).
- **Value histories** as a deterministic view, plus **pattern observations**: an opt-in L4 layer that groups recurring patterns in code and has the model word only changed groups, citing every member (M-13, O-14, O-15, Q3).
- **Profile card**: typed, capped, stated-only by default, each entry citing facts (M-20).
- **Compression**: gist, note and full pointer per derived unit; raw stays authoritative (M-22).

## 3. Offline memory: `areas/offline.md`

- **Three tiers**: call 1 per window; call 2 at session close (summary, stitched episode, profile operations); sleep
  (call 3) per namespace. Sleep re-runs a session task only when call 2 failed or never ran (O-01, O-07).
- **Triggers**: (N deposits AND M idle) OR context pressure C, both behind gap K; manual `sleep()` is non-blocking
  and returns a job handle (OF-1, OF-2); off by default so evaluations stay deterministic (O-02).
- **Unit of work**: one job per dirty namespace with a persisted lease and per-stage watermarks (O-03).
- **Stage order**: retention, catch-up extraction, reconcile backlog and entity resolution, contradictions,
  near-duplicates, value histories, session catch-up, entity summaries, communities, profile and reflection, observer
  views, standing answers, watches and cues, procedures, decay, compression, log prune (O-04).
- **Staged ladders** for entity resolution and contradictions: rules, scoped candidates, then one batched model call;
  low confidence gives a "possibly same" link, not a merge (O-11, O-12).
- **Budget and freshness**: token budget per namespace per cycle spent in stage priority; every derived record
  carries the newest source it covers, and raw evidence outranks a stale derivation (O-05, O-06).
- **Personalisation**: caller persona stays pinned; call 2 proposes guarded operations against the card; reflections
  are cited records that feed the operations path (O-18, O-19, O-20).

## 4. Retrieve and compose: `areas/read.md`

- **Depth tiers** per call (fast, standard, deep) within what config allows; fast is the default (R-01).
- **Typed query contract** by rules, zero calls, on by default, pending its full run and blind sets (R-03).
- **Legs**: vector, BM25 (language-aware), temporal (event intervals) always; entity (inverse-degree penalty),
  graph (one hop), episode (lifted to turns) when the router turns them on and the layer exists (R-05..R-10).
- **Fusion** within source families, then RRF k=60 across them; protected pool slots per family (R-11, R-12).
- **Lift, re-admit, dedupe, cap**: derived hits lifted to their turns under one score, re-admitted, deduplicated and
  capped (at most 64, R-Q4) before any rerank (R-Q1, ER-21).
- **Rerank** with Jina v3.5 on the capped set (Q5, Q6: decided); recency and reinforcement band built but off until
  screened (R-14, R-15, R-16).
- **Gate, pack**: store-calibrated relevance gate with a named-reference bypass; windows expanded after rerank;
  rank-order packer that skips oversize items and never truncates a bundle mid-evidence (fixes the v1 early break)
  (R-17, R-18, R-19, R-21, ER-24). Generation and permissions are revalidated just before the result returns
  (POL-1); derived records are admitted only if the viewer may see their lineage (POL-2).
- **Deep tier only**: one sufficiency judge and at most one rewritten search, behind its own screen (R-22).

## 5. Models, prompts and speed: `areas/models.md`

- Two named tiers, **worker** and **reader**; write-side roles stay local; the reader may be hosted with tier-gated
  context (P-01, P-02).
- No model by default; an enabled feature without a role fails at start, or falls back to rules when configured
  (P-03, G-22).
- Example-free, byte-stable prompts; blocking lint over every prompt, partial and harness prompt (P-07..P-13).
- Thinking off on every hot-path call; capped only for background judges after a screen (P-15).
- Embedders: CPU multilingual default, GPU instruction embedder profile; one device plan at start (P-17, P-19).
- Targets as regression gates: at most one call per window, static prompt at most 500 tokens, write p95 at most
  250 ms and fast-tier read p95 300 ms without rerank (CPU), 350 ms with Jina (GPU), on stated hardware and concurrency (R-Q3, ER-39) (P-14, P-21).
- Model upgrades through versioned vector spaces and selective re-derivation of stale fingerprints (P-25).

## 6. Trust, evaluation, API and operations: `areas/govern.md`

- **Adoption gate in code**: paired against a named reference with a matching manifest, every set present,
  per-dataset uncertainty, behavioural suite, one sealed blind run (G-01..G-05).
- **Benchmarks**: LoCoMo (with cat 5) and OP-Bench for development; MAB-CR, ConvoMem and PrefEval blind; no
  LongMemEval (G-02).
- **Firewall and N1**: base signals on every channel plus extended and semantic signals; trust horizon on whenever a
  grant exists; lineage always recorded on agent and tool channels (G-06..G-08).
- **Privacy, consent, erasure, audit** per profile (G-10, G-11, G-13..G-15).
- **API**: a small loop-verb core on a scoped handle; REST on loopback unless auth is set; MCP over stdio; framework
  adapters deferred (G-16..G-18).
- **Runtime**: embedded library, inline runner draining the durable outbox (Q2: decided) (G-19).
- **Reproducibility**: full run manifest with environment freeze hash; a venv per pinned run from a corrected lock
  (G-24, G-25).

## 7. Relation to v1 (owner decision, Q9)

memspine itself is restructured on the same principles (owner, 2026-10-12): replaced code is deleted rather than
aliased; behaviour changes become default only through the gate; the current reference stays reproducible by commit
(`pinned_run.sh`); stores migrate by replaying the retained event log; rolling-log stores advertise limited rebuild (G-16, G-23, G-25). See `RESTRUCTURE.md`.

## 8. Runtime and modular structure (owner, 2026-10-13)

- **Queue:** the SQL outbox is the truth; taskiq is the transport (in-memory broker in library mode, external broker and worker processes in server mode); unfinished rows re-sent on start.
- **Concurrency:** async core with TaskGroup, timeouts and cancellation; dedicated executors for GPU encoders (micro-batched) and blocking I/O; a global model semaphore; four priority classes (read, write, online extraction, sleep and maintenance) with guaranteed minimum shares; backpressure coalesces and never drops.
- **Sleep:** non-blocking job handles; one running job and one pending follow-up per namespace; leases and per-stage watermarks; triggers (N deposits AND M idle) OR context pressure C (70%, configurable), both behind the gap K; daily model-free maintenance.
- **Modules:** typed registries with entry points for stages, legs, backends, providers and prompts; dishka dependency injection with app, job and request scopes; import-linter layer contracts in CI; versioned Pydantic schemas in `memspine.schemas`, exported as JSON Schema to `docs/v2/schemas/`.
- **Fencing:** every lease takeover bumps a monotonic claim epoch; a deposit commits only if its epoch, source versions and erasure generation still match, so a stale worker cannot write, acknowledge or advance watermarks (ER-07).
- **Progress tracking:** progress is a contiguous watermark plus explicit holes; projector progress, derivation coverage and source membership are tracked separately; a wait captures a fixed target that later writes cannot extend (ER-09).
- **Shutdown:** every job is tracked; `close()` drains within a deadline and persists unfinished jobs; a cancelled job cannot commit; per-tenant fair shares (ER-10).
- **Overload:** accepted work is never dropped; at storage or quota limits new writes are rejected before acceptance with a distinguishable error, and emergency space is reserved for erasure and recovery (ER-08).
- **Default layers:** L0 always; L1 and L3 (graph) when a worker model is configured; L2, L4, L5 opt-in (I-7).

## Change log
- 2026-10-11: first version from the 155 answered decisions and the six area reviews.
- 2026-10-11 (later): section 7 rewritten for Q9 (v2 inside `memspine`).
- 2026-10-12: owner answers Q1-Q8 applied (pattern observations; Neo4j server graph).
- 2026-10-12 (later): stale owner-confirmation markers resolved from the Q1-Q8 answers.
- 2026-10-12 (owner): not a separate v2; memspine is restructured as a whole along this plan; replaced code is deleted, no compatibility shims.
- 2026-10-13: section 8 (runtime and modular structure) from the owner's answers.
- 2026-10-13: reconciled with ER amendments and owner decisions (consistency audit)
