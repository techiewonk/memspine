# MemSpine v2 decision record

The owner's answers to the 155 v2 architecture questions (answered 2026-10-11 on the decision page, https://claude.ai/artifact/2KXuPiBf2EgU1Ww8nx5bjj). Generated from `data/decisions_answered.json`; the verified reading of each answer, its predicted impact and where it lands in code are in `areas/<area>.md`.

Legend: **rec** = took the recommendation; **diff** = chose another option; **+** = the owner's note combines options or adds a request.

| | Took recommendation | Chose differently | With a combining note |
|---|---|---|---|
| Write path and ingest | 26 | 0 | 7 |
| Memory layers and data model | 24 | 2 | 5 |
| Offline memory | 25 | 1 | 9 |
| Retrieval and composition | 24 | 2 | 2 |
| Models, prompts, cost and speed | 25 | 0 | 0 |
| Evaluation, safety, API and operations | 25 | 1 | 1 |
| **All 155** | **149** | **6** | **24** |

## Write path and ingest

| Id | Decision | Chosen | Recommended | Owner note |
|---|---|---|---|---|
| W-01 | Unit of a write call | D · Typed source envelope, per-message raw (rec) | D · Typed source envelope, per-message raw |  |
| W-02 | What a write receipt guarantees | D · Raw searchable plus derivation intent (rec) | D · Raw searchable plus derivation intent |  |
| W-03 | Idempotency key for retried writes | D · Caller id first, fingerprint fallback, versioned (rec) | D · Caller id first, fingerprint fallback, versioned |  |
| W-04 | Times stamped on every raw input | C · B plus declared source time zone (rec) | C · B plus declared source time zone | + Combinre B + C |
| W-05 | Speaker, addressee and subject tagging | C · B plus extracted subject and asserter (rec) | C · B plus extracted subject and asserter | + also keep participact tag too |
| W-06 | Which roles and echoes enter memory | B · Store all raw, role-aware derivation (rec) | B · Store all raw, role-aware derivation |  |
| W-07 | Firewall placement on the derivation path | C · Three gates, cap always on (rec) | C · Three gates, cap always on |  |
| W-08 | Provenance class and confirmation at write | C · B plus checked assent (rec) | C · B plus checked assent | + combine B + C |
| W-09 | Routing writes to memory types | D · Raw first, typed by derivation (rec) | D · Raw first, typed by derivation |  |
| W-10 | Event log to projection hand-off | C · Split: hot inline, heavy via outbox (rec) | C · Split: hot inline, heavy via outbox | + Combine B + C |
| W-11 | Segmentation of turns for extraction | E · Adaptive window plus time-gap rule (rec) | E · Adaptive window plus time-gap rule | + B + E |
| W-12 | Online extraction: async, inline or deferred | D · Async online deposit plus read barrier (rec) | D · Async online deposit plus read barrier |  |
| W-13 | Durable derivation queue and acknowledgement | C · Leased outbox, at-least-once, idempotent effects (rec) | C · Leased outbox, at-least-once, idempotent effects |  |
| W-14 | Malformed output, retries and dead letters | C · B plus persisted failure state (rec) | C · B plus persisted failure state |  |
| W-15 | Schema of an extracted item | C · Versioned assertion envelope (rec) | C · Versioned assertion envelope | + B + C |
| W-16 | What the extraction call is shown | B · NEW + CONTEXT + KNOWN names (rec) | B · NEW + CONTEXT + KNOWN names |  |
| W-17 | How extracted items are grounded | C · B plus selective adjudication (rec) | C · B plus selective adjudication |  |
| W-18 | Extraction prompt rules and versioning | C · v2 plus shared partials, soft caps (rec) | C · v2 plus shared partials, soft caps |  |
| W-19 | Entity resolution on the write path | B · Online code resolution, pending merges (rec) | B · Online code resolution, pending merges |  |
| W-20 | Identity key of a graph assertion | C · Versioned assertion id plus event resolution (rec) | C · Versioned assertion id plus event resolution |  |
| W-21 | Who chooses the integration operator | B · Ladder seeded by extraction hints (rec) | B · Ladder seeded by extraction hints |  |
| W-22 | Default when cardinality is undeclared | D · Kind-aware default (rec) | D · Kind-aware default |  |
| W-23 | Images and attachments at ingest | B · Opt-in description at ingest (rec) | B · Opt-in description at ingest | + B + C |
| W-24 | Re-ingesting an edited document | C · B on source versions (rec) | C · B on source versions |  |
| W-25 | Backpressure when derivation falls behind | B · Coalesce and mark underived (rec) | B · Coalesce and mark underived |  |
| W-26 | Write-side cost ledger and budget | D · C plus enforced budget (rec) | D · C plus enforced budget |  |

## Memory layers and data model

| Id | Decision | Chosen | Recommended | Owner note |
|---|---|---|---|---|
| M-01 | Source of truth for memory | E · Event log + derived Markdown export view (rec) | E · Event log + derived Markdown export view |  |
| M-02 | Derived layer stack L0-L5 | D · Two layers only (raw + facts) (**diff**) | B · Explicit layer field on one record shape | + B + D |
| M-03 | Which memory types v2 keeps | B · Nine as a type tag, mapped to layers (rec) | B · Nine as a type tag, mapped to layers |  |
| M-04 | Storage backend profile | C · Two profiles from one log (rec) | C · Two profiles from one log | + SQL graph to neo4j |
| M-05 | Isolation and tenancy model | B · Typed scope tuple on every row and key (rec) | B · Typed scope tuple on every row and key |  |
| M-06 | Assertion envelope on records | B · Versioned assertion envelope (rec) | B · Versioned assertion envelope |  |
| M-07 | Provenance class and trust semantics | C · Class + proof count (rec) | C · Class + proof count |  |
| M-08 | Speaker, subject and asker axes | B · Typed indexed columns (rec) | B · Typed indexed columns |  |
| M-09 | Lineage representation | C · B + evidence spans and source versions (rec) | C · B + evidence spans and source versions |  |
| M-10 | Temporal fields on a record | B · Tri-temporal with precision (rec) | B · Tri-temporal with precision | + B + C |
| M-11 | As-of read semantics | B · Two-axis as-of (rec) | B · Two-axis as-of |  |
| M-12 | Attribute cardinality schema | B · Declared attribute registry with defaults (rec) | B · Declared attribute registry with defaults |  |
| M-13 | Value histories and observations | D · B for keyed state + C for patterns (rec) | D · B for keyed state + C for patterns |  |
| M-14 | Episodes, sessions and sagas | B · Turn -> episode -> session -> saga records (rec) | B · Turn -> episode -> session -> saga records |  |
| M-15 | Entity identity model | B · Opaque id + alias table + type + scope (rec) | B · Opaque id + alias table + type + scope |  |
| M-16 | Relation and edge representation | C · Assertion-centric with reified n-ary nodes (rec) | C · Assertion-centric with reified n-ary nodes | + C + D if possible |
| M-17 | Assertion and edge identity key | B · Versioned key with kind and occurrence (rec) | B · Versioned key with kind and occurrence |  |
| M-18 | Graph store backend | A · Auto Ladybug else SQL adjacency (today) (**diff**) | D · Pluggable with a capability contract |  |
| M-19 | Community and cluster layer | A · Leiden + LPA hybrid, opt-in (today) (rec) | A · Leiden + LPA hybrid, opt-in (today) | + A + D add extra, not remove Commodities |
| M-20 | Profile and persona representation | B · Capped typed card citing facts (rec) | B · Capped typed card citing facts |  |
| M-21 | Working memory and context state | C · A + rolling summary of evicted turns (rec) | C · A + rolling summary of evicted turns |  |
| M-22 | Compressed multi-resolution representations | D · Multi-resolution per record (rec) | D · Multi-resolution per record |  |
| M-23 | Embedding model and multi-vector | B · Multilingual default, one vector (rec) | B · Multilingual default, one vector |  |
| M-24 | Shared memory and per-observer views | C · A + derived per-observer views (rec) | C · A + derived per-observer views |  |
| M-25 | Forgetting and erasure of derived text | B · Cascade + deferred re-derivation (rec) | B · Cascade + deferred re-derivation |  |
| M-26 | Schema versioning and migration | B · Event upcasters + derivation versions (rec) | B · Event upcasters + derivation versions |  |

## Offline memory

| Id | Decision | Chosen | Recommended | Owner note |
|---|---|---|---|---|
| O-01 | Where offline derivation runs | B · Three tiers: window, session close, sleep (rec) | B · Three tiers: window, session close, sleep |  |
| O-02 | Sleep trigger policy | C · Volume + idle + minimum spacing per namespace (rec) | C · Volume + idle + minimum spacing per namespace |  |
| O-03 | Unit of work, locking and restart | B · Per-namespace job, dirty set, advisory lock (rec) | B · Per-namespace job, dirty set, advisory lock |  |
| O-04 | Call 3 stage order | B · Reconcile first, then summarise, then personalise, then maintain (rec) | B · Reconcile first, then summarise, then personalise, then maintain |  |
| O-05 | Cost caps for background work | C · Token budget per namespace per cycle, priority order (rec) | C · Token budget per namespace per cycle, priority order |  |
| O-06 | Freshness guarantee for derived layers | B · Staleness watermark on every derived item (rec) | B · Staleness watermark on every derived item |  |
| O-07 | Call 2 shape at session close | B · One structured call with three sections (rec) | B · One structured call with three sections | + A + B |
| O-08 | Session summary update policy | C · B plus degenerate-output guard (rec) | C · B plus degenerate-output guard |  |
| O-09 | Stitched episodes across windows | B · Stitch at session close in call 2 (rec) | B · Stitch at session close in call 2 | + A+B |
| O-10 | Reconciling the online deposit backlog | B · Persisted backlog with age and size caps (rec) | B · Persisted backlog with age and size caps | + A+ B |
| O-11 | Entity resolution and LLM confirmation | C · B with type and context, ambiguity preserved (rec) | C · B with type and context, ambiguity preserved | + A + B + C |
| O-12 | Cross-key contradiction and invalidation | C · Subject + predicate family + time + embedding candidates (rec) | C · Subject + predicate family + time + embedding candidates | + A + B + C |
| O-13 | Near-duplicate consolidation and reinforcement | B · Cosine merge plus reinforcement count (today, refined) (rec) | B · Cosine merge plus reinforcement count (today, refined) |  |
| O-14 | Value histories for keyed state | B · Deterministic history view per (subject, attribute) (rec) | B · Deterministic history view per (subject, attribute) | + A + B |
| O-15 | Observation layer (consolidated beliefs) | A · No observation layer (today) (**diff**) | B · Code-grouped, LLM-worded for changed groups |  |
| O-16 | Entity summaries | B · Sleep-time with fast path (today, opt-in) (rec) | B · Sleep-time with fast path (today, opt-in) |  |
| O-17 | Communities and reorganisation | B · Graph communities with summary parents (today) (rec) | B · Graph communities with summary parents (today) |  |
| O-18 | How the profile is updated | B · Guarded operations against a card (rec) | B · Guarded operations against a card | + A + B |
| O-19 | Profile card schema, caps and inferences | B · Typed card, code caps, stated-only by default (rec) | B · Typed card, code caps, stated-only by default |  |
| O-20 | Reflection and insight generation | B · Per session, cited, depth-capped (today, opt-in) (rec) | B · Per session, cited, depth-capped (today, opt-in) |  |
| O-21 | Standing answers (mental models) | B · Opt-in standing questions with delta refresh (rec) | B · Opt-in standing questions with delta refresh |  |
| O-22 | Decay, forgetting and compression | B · Access tiers plus cold compression (today) (rec) | B · Access tiers plus cold compression (today) |  |
| O-23 | Prospective memory and foresight | B · A plus anticipated-question cues (rec) | B · A plus anticipated-question cues | + A+ B |
| O-24 | Procedural memory: skills and lessons | B · Slot-filled procedure cases from trajectories (rec) | B · Slot-filled procedure cases from trajectories | + A + B |
| O-25 | Implicit feedback from conversation | A · Explicit feedback only (today) (rec) | A · Explicit feedback only (today) |  |
| O-26 | Per-observer views (theory of mind) | B · Derived 'about X as seen by Y' views at sleep (rec) | B · Derived 'about X as seen by Y' views at sleep |  |

## Retrieval and composition

| Id | Decision | Chosen | Recommended | Owner note |
|---|---|---|---|---|
| R-01 | Read tiers and who chooses depth | B · Per-call depth: fast / standard / deep (rec) | B · Per-call depth: fast / standard / deep | + A+ B |
| R-02 | Default unit of evidence returned | C · Mixed bundle: raw evidence first, derived as capped notes (rec) | C · Mixed bundle: raw evidence first, derived as capped notes |  |
| R-03 | Typed query contract adoption | B · Heuristic contract on by default (rec) | B · Heuristic contract on by default |  |
| R-04 | Routing and query rewriting | D · Router in deep tier only (rec) | D · Router in deep tier only |  |
| R-05 | Default retrieval leg set | D · Core three plus router-activated legs (rec) | D · Core three plus router-activated legs |  |
| R-06 | Lexical leg: BM25 vs learned sparse | D · BM25 with language-aware tokenisers (rec) | D · BM25 with language-aware tokenisers |  |
| R-07 | Temporal leg and relative-time anchoring | D · B plus event-time index (rec) | D · B plus event-time index |  |
| R-08 | Entity leg and hub damping | B · Entity leg with inverse-degree penalty (rec) | B · Entity leg with inverse-degree penalty |  |
| R-09 | Graph traversal method at read | C · One-hop convergent expansion (rec) | C · One-hop convergent expansion |  |
| R-10 | Episode and view-tag legs | B · Episode leg lifted to parent turns (rec) | B · Episode leg lifted to parent turns |  |
| R-11 | Fusion method across legs | C · RRF with leg de-correlation (rec) | C · RRF with leg de-correlation | + A + C |
| R-12 | Protected candidate pool policy | C · Per source family (rec) | C · Per source family |  |
| R-13 | Leg floors and pool widening | C · Pool by read tier (rec) | C · Pool by read tier |  |
| R-14 | Default reranker model and placement | B · Jina v3.5 default (rec) | B · Jina v3.5 default |  |
| R-15 | What the reranker scores | A · Hit only, windows after (today) (**diff**) | C · Whole dated window as the unit |  |
| R-16 | Recency, importance and reinforcement signals | B · Bounded multiplicative boosts (rec) | B · Bounded multiplicative boosts |  |
| R-17 | Evidence lifting and one score | B · Lift to parent turns, one reranked list (rec) | B · Lift to parent turns, one reranked list |  |
| R-18 | Context budget and block shares | D · B plus dynamic shares with redistribution (rec) | D · B plus dynamic shares with redistribution |  |
| R-19 | Neighbour window expansion shape | C · Token-sized, budget-scaled windows (rec) | C · Token-sized, budget-scaled windows |  |
| R-20 | Rendering format, order and dates | D · C plus contract header and integer aliases (rec) | D · C plus contract header and integer aliases |  |
| R-21 | Relevance gate and abstention | C · Store-calibrated gate with named bypass (rec) | C · Store-calibrated gate with named bypass |  |
| R-22 | Agentic multi-step read design | B · Sufficiency check plus one rewrite round (**diff**) | D · No engine loop; expose tools only |  |
| R-23 | External public knowledge at read | B · Opt-in, gated, separate block (today) (rec) | B · Opt-in, gated, separate block (today) |  |
| R-24 | As-of reads and default time view | C · Contract-driven time view (rec) | C · Contract-driven time view |  |
| R-25 | Read latency targets and parallel legs | C · B plus tier SLOs (rec) | C · B plus tier SLOs |  |
| R-26 | Read-path caching and invalidation | D · A plus provider prompt-cache layout (rec) | D · A plus provider prompt-cache layout |  |

## Models, prompts, cost and speed

| Id | Decision | Chosen | Recommended | Owner note |
|---|---|---|---|---|
| P-01 | One model or a model per role | C · Two tiers: small worker, large reader (rec) | C · Two tiers: small worker, large reader |  |
| P-02 | Local-first or hosted model default | D · Split by sensitivity (rec) | D · Split by sensitivity |  |
| P-03 | Behaviour when no model is configured | D · Fail fast, with explicit fallback key (rec) | D · Fail fast, with explicit fallback key |  |
| P-04 | Structured output method | C · Schema-constrained decoding by default (rec) | C · Schema-constrained decoding by default |  |
| P-05 | Repair after an invalid reply | D · Per-item validation, then targeted re-ask (rec) | D · Per-item validation, then targeted re-ask |  |
| P-06 | Output serialisation format | C · Compact keys JSON (rec) | C · Compact keys JSON |  |
| P-07 | Examples in prompts | B · No examples, shape only (rec) | B · No examples, shape only |  |
| P-08 | Cache-friendly prompt layout | C · B plus provider cache hints (rec) | C · B plus provider cache hints |  |
| P-09 | Identifiers shown to the model | B · Per-call integer aliases, strict (rec) | B · Per-call integer aliases, strict |  |
| P-10 | Language of stored items | B · Same language, shared partial (rec) | B · Same language, shared partial |  |
| P-11 | Length rules for items and outputs | D · C plus truncation detection (rec) | D · C plus truncation detection |  |
| P-12 | Prompt and model identity in provenance | C · B plus run manifest (rec) | C · B plus run manifest |  |
| P-13 | Gold-lint scope and strictness | C · B plus rendered prompts and overrides (rec) | C · B plus rendered prompts and overrides |  |
| P-14 | Write-side call and token targets | D · B with an optional C (rec) | D · B with an optional C |  |
| P-15 | Thinking mode per role | B · Per role default (rec) | B · Per role default |  |
| P-16 | Compression before model calls | B · Opt-in write compressor port (rec) | B · Opt-in write compressor port |  |
| P-17 | Default embedding model and host | C · Hardware-detected profile (rec) | C · Hardware-detected profile |  |
| P-18 | Reranker model and when to run it | C · Gated reranker (rec) | C · Gated reranker |  |
| P-19 | GPU and CPU placement of models | C · Device planner (rec) | C · Device planner |  |
| P-20 | Batching and concurrency of model work | C · B plus global LLM semaphore (rec) | C · B plus global LLM semaphore |  |
| P-21 | Latency budgets and time-to-searchable | C · B plus time-to-searchable SLO (rec) | C · B plus time-to-searchable SLO |  |
| P-22 | Cost ledger scope and spend limits | C · B plus persisted ledger and budgets (rec) | C · B plus persisted ledger and budgets |  |
| P-23 | Role of small local extractors | B · Small-model pre-pass for every window (rec) | B · Small-model pre-pass for every window |  |
| P-24 | Activation (KV-cache) memory | B · Stable-block prefix reuse (rec) | B · Stable-block prefix reuse |  |
| P-25 | Model upgrades and re-derivation | C · B plus selective re-derivation (rec) | C · B plus selective re-derivation |  |

## Evaluation, safety, API and operations

| Id | Decision | Chosen | Recommended | Owner note |
|---|---|---|---|---|
| G-01 | Adoption gate for any behaviour change | C · B plus one frozen blind run (rec) | C · B plus one frozen blind run |  |
| G-02 | Role of each benchmark in v2 | D · B plus dataset-free behavioural suite as a gate (rec) | D · B plus dataset-free behavioural suite as a gate |  |
| G-03 | Uncertainty and noise band method | C · B calibrated by repeat runs (rec) | C · B calibrated by repeat runs |  |
| G-04 | Metamorphic and behavioural controls | C · Invariance plus safety pairs, blocking (rec) | C · Invariance plus safety pairs, blocking |  |
| G-05 | Loop-level metrics in the v2 harness | D · C plus metrics as adoption guards (rec) | D · C plus metrics as adoption guards |  |
| G-06 | Memory firewall default posture | C · B plus extended and semantic signals on (rec) | C · B plus extended and semantic signals on | + A + C |
| G-07 | Cross-agent reach bound by default | B · Invariant on whenever a grant exists (rec) | B · Invariant on whenever a grant exists |  |
| G-08 | Lineage enforcement for derived records | B · Implicit parents per turn by default (rec) | B · Implicit parents per turn by default |  |
| G-09 | Low-trust content in the context window | D · B by default, C in protected profiles (rec) | D · B by default, C in protected profiles |  |
| G-10 | PII and secret handling default | C · B plus remote-model tier gate (rec) | C · B plus remote-model tier gate |  |
| G-11 | Consent and purpose scopes | C · Purposes plus do-not-derive (rec) | C · Purposes plus do-not-derive |  |
| G-12 | Isolation boundary and multi-party visibility | B · Namespace plus mandatory viewer (rec) | B · Namespace plus mandatory viewer |  |
| G-13 | Right to erasure across derived state | C · B plus re-derivation of survivors (rec) | C · B plus re-derivation of survivors |  |
| G-14 | Retention policy and event log lifetime | D · Profile-specific retention (rec) | D · Profile-specific retention |  |
| G-15 | Audit log scope and default | D · B by default, C in shared and regulated profiles (rec) | D · B by default, C in shared and regulated profiles |  |
| G-16 | Shape of the public Python API | D · B with C-style scoped handles (rec) | D · B with C-style scoped handles |  |
| G-17 | Remote protocols and default authentication | C · B plus principal-bound scopes (rec) | C · B plus principal-bound scopes |  |
| G-18 | Framework adapters (I70) | A · Stay on hold; MCP is the adapter (rec) | A · Stay on hold; MCP is the adapter |  |
| G-19 | Deployment modes and background runners | A · Library-first, inline runner (**diff**) | C · Three named modes |  |
| G-20 | Observability, stats and telemetry | C · B plus optional OpenTelemetry export (rec) | C · B plus optional OpenTelemetry export |  |
| G-21 | Defaults policy and configuration profiles | B · No model; generic base, shaped presets (rec) | B · No model; generic base, shaped presets |  |
| G-22 | Flag graduation and flag retirement | C · B plus retirement budget (rec) | C · B plus retirement budget |  |
| G-23 | Backwards compatibility and migration from v1 | C · B with dual-read period (rec) | C · B with dual-read period |  |
| G-24 | Environment pinning for runs and installs | C · Per-run isolated environment (rec) | C · Per-run isolated environment |  |
| G-25 | Run manifest and comparability key | D · C plus cost and latency fields (rec) | D · C plus cost and latency fields |  |
| G-26 | Claims policy for the MemSpine paper | C · B plus mechanism claims tied to loop metrics (rec) | C · B plus mechanism claims tied to loop metrics |  |

## Owner follow-up answers (2026-10-12)

These refine the answers above where they conflicted; see `VERIFY.md` section 0.

| # | Answer | Changes |
|---|---|---|
| Q1 | Layer field, two layers on | M-02, W-12, R-09, R-10, O-08, O-16 |
| Q2 | Durable outbox | G-19, P-21, W-02, W-10, W-13, W-25 ... |
| Q3 | Value histories plus pattern observations | M-13, O-15, O-04, O-05, O-21, R-17 ... |
| Q4 | Neo4j is the server graph | M-04, M-18, W-10, G-13, M-25, M-05 ... |
| Q5 | Jina on every read if installed | R-14, P-18 |
| Q6 | Jina if installed, permissive fallback | R-14, P-17, P-18 |
| Q7 | Coexist unless marked | W-22, M-12 |
| Q8 | Assistant exemption in the chat preset only | G-06 |
