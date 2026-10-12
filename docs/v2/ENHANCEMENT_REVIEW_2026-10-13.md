# MemSpine v2: gaps and enhancement options

2026-10-13 · Proposed for discussion, not accepted ADRs.

Cross-area plan review, AST dependency checks and targeted source inspection. Not exhaustive line-by-line verification of the engine or other engines; no model evaluation run.

All options proposed. Accepted decisions, implementation and package statuses unchanged.

P0 = settle before dependent contracts/code. P1 = refine or screen before adoption. Confirmed means a plan/source mismatch was directly inspected; design risk is an inference, and optional enhancement is a hypothesis.

## Decision order

1. Atomic commit/readiness; scope and identity; cross-turn evidence.
2. Temporal representation, graph projection, job fencing and erasure.
3. Retrieval units, budgets, offline freshness and cache contracts.
4. Typed schemas after those semantics are settled.
5. Optional enhancements through clean ablations.

## ER-01 — One authoritative decision view

**Area:** Plan and delivery

**Priority:** P0

**Kind:** Confirmed plan inconsistency

**Finding:** The latest L3 default, lift-before-rerank order and non-blocking sleep decisions coexist with older contradictory prose and diagrams.

**Options:** A: manually patch all copies (quick, drift can return). B: canonical decision records with generated summaries (more tooling, fewer contradictions).

**Recommendation:** B incrementally: first reconcile the current contradictions; generate default/config and pipeline summaries from stable decision IDs. Preserve superseded decisions as history.

**Gate:** No active document contradicts the latest owner decisions; diagrams and pipeline order agree.

**Packages:** X-00, X-04

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/ARCHITECTURE.md:24](../../docs/v2/ARCHITECTURE.md), [docs/v2/ARCHITECTURE.md:147](../../docs/v2/ARCHITECTURE.md), [docs/v2/VERIFY.md:47](../../docs/v2/VERIFY.md)

## ER-02 — Enforce prerequisites in the DAG

**Area:** Plan and delivery

**Priority:** P0

**Kind:** Confirmed dependency gap

**Finding:** AST inspection finds no cycles, but R6-01 has no dependency path to P0-02, P0-03, P0-08, MOD-01 or X-01. Coverage of a decision does not enforce completion.

**Options:** A: rely on manual phase checks. B: explicit dependency edges plus machine-checked phase requirements.

**Recommendation:** B. Require environment/manifest before measured gates, schemas before schema consumers, ownership checking before concurrent implementation, and behavioural controls before promotion.

**Gate:** A deliberately incomplete prerequisite blocks the corresponding package or exit; graph remains acyclic.

**Packages:** P0-02, P0-03, P0-08, MOD-01, X-01, all EX gates

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/data/packages.py:10](../../docs/v2/data/packages.py), [docs/v2/data/build_plan.py:46](../../docs/v2/data/build_plan.py)

## ER-03 — Separate moves, migrations and behaviour

**Area:** Plan and delivery

**Priority:** P0

**Kind:** Confirmed classification mismatch

**Finding:** New record envelopes and tri-temporal fields are classified as moves. Registries, DI lifetimes and a new public handle also introduce contracts beyond relocation.

**Options:** A: one large golden-gated PR. B: split pure moves, schema migrations, API changes and behaviour changes.

**Recommendation:** B. Preserve existing outputs during moves; use migration, lifecycle and API contract gates for the other parts. GPU screening is needed for quality claims, not every infrastructure change.

**Gate:** Every package has a precise change class and appropriate gate; canonical replay equivalence is defined separately from byte equality.

**Packages:** F-02a/b, MOD-02, F-08, F-13a

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/PLAN.md:61](../../docs/v2/PLAN.md), [docs/v2/PLAN.md:46](../../docs/v2/PLAN.md)

## ER-04 — Owned files and integration collisions

**Area:** Plan and delivery

**Priority:** P1

**Kind:** Design risk

**Finding:** Serial lanes do not protect shared schemas, migrations, exports, dependencies and generated docs across other lanes; current ownership uses broad wildcards.

**Options:** A: serialize all work (simple, slow). B: exact ownership plus a shared-file integration queue (more tooling, parallel work).

**Recommendation:** B; compute concurrent eligibility from the DAG and check overlapping paths, including tests and generated files. Assign one migration integrator.

**Gate:** Synthetic concurrent conflicts are rejected before agents start; merge CI repeats contracts against the integration head.

**Packages:** X-01, X-02, RESTRUCTURE

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/RESTRUCTURE.md:60](../../docs/v2/RESTRUCTURE.md), [docs/v2/EXECUTION.md:9](../../docs/v2/EXECUTION.md)

## ER-05 — Deliver a small end-to-end slice first

**Area:** Plan and delivery

**Priority:** P1

**Kind:** Optional enhancement

**Finding:** Phase 1 currently waits for the server/Neo4j profile before its exit, increasing the amount of unvalidated foundation work.

**Options:** A: both profiles before any exit. B: embedded internal milestone followed by the mandatory server milestone.

**Recommendation:** B as a proposed sequencing change, retaining the chosen Neo4j server design and both-profile release gates. Prove deposit, outbox, derive, recall and forget together early.

**Gate:** One synthetic end-to-end workload survives restart and erasure before broad feature work; no server release before its matrix passes.

**Packages:** EX-1, F-16d/e/f

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/ROADMAP.md:24](../../docs/v2/ROADMAP.md)

## ER-06 — Atomic commit and write readiness

**Area:** Durability and runtime

**Priority:** P0

**Kind:** Confirmed contract conflict

**Finding:** Insert step 8 includes lexical/vector projections in one SQL transaction, although the embedded profile uses independent backends. It also promises immediate search while outbox_all delays it.

**Options:** A: durable receipt with per-index readiness. B: wait for required raw indexes. C: wait for derivation and graph too.

**Recommendation:** A default, explicit B/C barriers. SQL commits events, SQL rows and intents atomically; independent projectors use idempotent delivery. Report readiness by capability and watermark.

**Gate:** Crash at every commit/projector boundary; receipt never claims an unavailable capability; retries do not duplicate source events.

**Packages:** F-04, W2-01, RT-01

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/insert.md:269](../../docs/v2/pipelines/insert.md), [src/memspine/core/projector.py:19](../../src/memspine/core/projector.py)

## ER-07 — Fence expired workers

**Area:** Durability and runtime

**Priority:** P0

**Kind:** Design risk

**Finding:** Lease expiry and heartbeat are specified, but the shown NamespaceJobLease has no monotonic fencing epoch. Erasure generation and worker ownership solve different races.

**Options:** A: lease owner and TTL only. B: monotonic claim epoch plus conditional commits. C: hold a DB lock throughout inference.

**Recommendation:** B. Check claim epoch, source versions and erasure generation in the deposit transaction; reject the old worker after takeover. Keep model calls outside write locks.

**Gate:** Pause worker A beyond TTL, let B finish, then resume A: A cannot write, acknowledge, advance watermarks or release B’s lease.

**Packages:** F-04, O4-01a

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/offline.md:147](../../docs/v2/pipelines/offline.md), [docs/v2/pipelines/insert.md:677](../../docs/v2/pipelines/insert.md)

## ER-08 — Bound overload honestly

**Area:** Durability and runtime

**Priority:** P0

**Kind:** Design risk

**Finding:** Never drop, writes never block and finite storage cannot all hold during sustained overload. Coalescing saves overhead but cannot remove the underlying raw-data growth.

**Options:** A: unbounded backlog. B: quotas, durable acceptance and explicit admission failure. C: block writers until capacity returns.

**Recommendation:** B with configurable bounded waiting. Never drop already acknowledged work; reject before acceptance when storage limits are reached. Keep emergency space for erasure and recovery.

**Gate:** Sustained overload, disk-full and tenant-skew tests; accepted work is accounted for and rejected writes are distinguishable.

**Packages:** RT-02, W2-12

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/README.md:20](../../docs/v2/pipelines/README.md)

## ER-09 — Projection order, holes and freshness

**Area:** Durability and runtime

**Priority:** P1

**Kind:** Design risk

**Finding:** Watermarks and covers_seq exist, but a highest observed sequence alone cannot prove all earlier work completed when jobs fail or finish out of order.

**Options:** A: maximum seen seq. B: contiguous committed watermark plus holes and source-version fingerprints.

**Recommendation:** B. Separate projector progress, derivation coverage and source membership. Barriers wait for a captured target, not for a queue that can grow forever.

**Gate:** Complete seq 12 before 11; freshness must remain incomplete until 11 is resolved. New writes must not extend an existing barrier target.

**Packages:** O4-01b, F-04, R3-15

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/offline.md:753](../../docs/v2/pipelines/offline.md), [docs/v2/pipelines/offline.md:732](../../docs/v2/pipelines/offline.md)

## ER-10 — Cancellation, shutdown and fairness

**Area:** Durability and runtime

**Priority:** P1

**Kind:** Design risk

**Finding:** Async tasks, executors and priorities are selected; cancellation of orchestration does not by itself guarantee an in-flight inference or executor task has stopped.

**Options:** A: best-effort shutdown. B: tracked jobs, bounded drain, cancellation-aware commits and per-tenant resource shares.

**Recommendation:** B. Separate logical cancellation from physical completion; never reuse a model slot while its work still runs. Persist unfinished jobs before close returns.

**Gate:** Cancel during HTTP, encoder inference and deposit; close with a deadline; no resource leak, stale commit or starving tenant.

**Packages:** RT-02, F-08

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/README.md:18](../../docs/v2/pipelines/README.md), [docs/v2/pipelines/README.md:19](../../docs/v2/pipelines/README.md)

## ER-11 — Cite cross-turn bindings

**Area:** Evidence and extraction

**Priority:** P0

**Kind:** Confirmed evidence restriction

**Finding:** CONTEXT may resolve references but context citations are rejected. A new statement can then depend on an older name or relationship that is absent from its lineage.

**Options:** A: NEW-only citations. B: NEW assertion trigger plus citeable CONTEXT binding evidence. C: re-extract all context every time.

**Recommendation:** B. Distinguish assertion support from reference-binding support. Include both source versions in lineage and erasure checks; do not count context as a new occurrence.

**Gate:** Invented cross-turn pronouns, country references and elliptical replies retain correct bindings; deleting the binding source invalidates dependents.

**Packages:** W2-06a, W2-08, F-16a

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/insert.md:533](../../docs/v2/pipelines/insert.md)

## ER-12 — Recover missed information selectively

**Area:** Evidence and extraction

**Priority:** P1

**Kind:** Optional enhancement

**Finding:** Schema validity and exact spans test emitted items, not facts the extractor omitted. Lists, subordinate clauses and quantities need completeness controls.

**Options:** A: one pass only. B: cheap coverage diagnostics with bounded recovery. C: second model pass for every window.

**Recommendation:** B behind a screen: retain all raw text; detect likely uncovered named items, quantities and list members; targeted re-extraction only when justified. Coverage heuristics are not proof.

**Gate:** Measure omitted-fact recall and unsupported additions separately on unseen domains, lists and paraphrases; record extra calls.

**Packages:** W2-06b, W2-08, X-05

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/PLAN.md:90](../../docs/v2/PLAN.md), [docs/v2/pipelines/insert.md:468](../../docs/v2/pipelines/insert.md)

## ER-13 — Identity across sessions and occurrences

**Area:** Evidence and extraction

**Priority:** P0

**Kind:** Design risk

**Finding:** The scope tuple includes session and is placed inside every identity. Taken literally, that can fragment a persistent entity across sessions. Content-only IDs can instead collapse repeated real events.

**Options:** A: session-local identity everywhere. B: stable memory scope plus separate source/session identity and event occurrence IDs.

**Recommendation:** B. Choose entity scope explicitly (tenant/user/agent policy); keep session as provenance. Separate retry identity, mention identity, assertion identity and occurrence identity.

**Gate:** Same person across sessions links correctly; homonyms stay apart; retries stay one effect; two identical same-day events remain two.

**Packages:** F-01, F-16c, W2-09

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/areas/model.md:40](../../docs/v2/areas/model.md), [docs/v2/areas/model.md:51](../../docs/v2/areas/model.md)

## ER-14 — Keep uncertainty multidimensional

**Area:** Evidence and extraction

**Priority:** P1

**Kind:** Design risk

**Finding:** A broad confidence or grounded label can hide different uncertainty in extraction, entity binding, time, modality and source trust.

**Options:** A: one confidence float. B: separate assessment fields with explicit allowed uses.

**Recommendation:** B. Keep source trust distinct from support strength and entity/time certainty; define which combinations may supersede, enter the graph or appear as tentative evidence.

**Gate:** A trusted source with an ambiguous pronoun never gains a certain identity; a precise timestamp never upgrades an uncertain claim.

**Packages:** F-02a, W2-08, W2-10

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/insert.md:535](../../docs/v2/pipelines/insert.md)

## ER-15 — Bound repairs and truncation recovery

**Area:** Evidence and extraction

**Priority:** P0

**Kind:** Confirmed budget conflict

**Finding:** The architecture caps extraction at one call per window while the pipeline permits a repair call and optional entailment adjudication. Truncated output also needs reliable source coverage accounting.

**Options:** A: hard one attempt and defer failures. B: one normal call plus bounded recovery budgets. C: retry until valid.

**Recommendation:** B. Count attempts by purpose, cap total tokens/time, cache validated outputs and track processed source ranges. Never infer complete coverage from syntactically valid partial output.

**Gate:** Malformed, empty-valid and truncated outputs terminate within budget; valid empty extraction is not endlessly retried; unprocessed lines stay pending.

**Packages:** W2-06a/b, W2-12

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/ARCHITECTURE.md:116](../../docs/v2/ARCHITECTURE.md), [docs/v2/pipelines/insert.md:473](../../docs/v2/pipelines/insert.md)

## ER-16 — Prompt economy without losing semantics

**Area:** Evidence and extraction

**Priority:** P1

**Kind:** Optional enhancement

**Finding:** Static prompt length alone excludes schema, aliases, context, output and repair costs. Shortening instructions can remove necessary negation and attribution rules.

**Options:** A: optimise instruction tokens only. B: benchmark complete requests and useful grounded information per token.

**Recommendation:** B. Version prompt+schema together; cap KNOWN/context by relevance; test compact field aliases against readable keys. Preserve language, polarity, modality, lists and evidence rules.

**Gate:** Report full input/output tokens, latency, fact recall and fidelity at equal budgets across several languages and domains.

**Packages:** W2-07, W2-12, P0-09

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/ARCHITECTURE.md:116](../../docs/v2/ARCHITECTURE.md)

## ER-17 — Independent support and change semantics

**Area:** Evidence and extraction

**Priority:** P1

**Kind:** Design risk

**Finding:** Echo guard, support counts and change hints exist, but repeated mentions, shared source copies and genuine independent corroboration must not be counted equally.

**Options:** A: count matching mentions. B: source-family-aware support with reversible change decisions.

**Recommendation:** B. Track origin/derivation families; do not strengthen a belief from its own summary or agent echo. chg is a hint requiring compatible subject, attribute, time and evidence.

**Gate:** Copy loops never increase independent support; A→B→A history survives; quoted denial and hypothetical changes do not supersede facts.

**Packages:** W2-03, W2-10, O4-04

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/insert.md:975](../../docs/v2/pipelines/insert.md), [docs/v2/pipelines/insert.md:980](../../docs/v2/pipelines/insert.md), [docs/v2/pipelines/offline.md:389](../../docs/v2/pipelines/offline.md)

## ER-18 — Separate temporal anchors from event dates

**Area:** Time and graph

**Priority:** P0

**Kind:** Design risk

**Finding:** The owner’s temporal ladder includes nearby dated lines, session order and recorded_at. These signals do not all establish when the described event occurred.

**Options:** A: choose the strongest available timestamp. B: preserve unresolved expressions, intervals, ordering constraints and explicit anchor provenance.

**Recommendation:** B while retaining I-2’s broad extraction intent. Store weak anchors as hypotheses; do not silently turn source/ingest time into an event date. Contradictory anchors remain visible.

**Gate:** Month/year precision, timezone boundaries, ambiguous relative dates and undated durations never acquire fabricated exact dates.

**Packages:** F-02b, W2-14, R3-14

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/insert.md:974](../../docs/v2/pipelines/insert.md)

## ER-19 — Query evidence sets for joins and durations

**Area:** Time and graph

**Priority:** P1

**Kind:** Optional enhancement

**Finding:** Multi-hop questions need jointly compatible evidence: both endpoints of a duration, intersections of visited places, or all members of a list.

**Options:** A: top-k independent hits. B: deterministic evidence obligations with bounded expansion. C: unrestricted agent loop.

**Recommendation:** B as an opt-in hypothesis. Derive relation/operand requirements from the query contract; retrieve missing operands within a budget; retain incomplete status when evidence is absent.

**Gate:** Unseen intersections, durations and enumeration tasks improve at fixed reader/token budgets without increasing unsupported answers.

**Packages:** R3-02, R3-05b, R3-12

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/PLAN.md:105](../../docs/v2/PLAN.md), [docs/v2/PLAN.md:118](../../docs/v2/PLAN.md)

## ER-20 — Graph as a supported projection

**Area:** Time and graph

**Priority:** P1

**Kind:** Design risk

**Finding:** The graph default is decided, but not every assertion is a safe binary edge; n-ary events, negation and uncertain bindings require richer projection rules.

**Options:** A: force every fact into a triple. B: assertion nodes with optional typed role edges and explicit uncertainty.

**Recommendation:** B, consistent with the assertion-centric plan. Keep invalid or uncertain bindings out of definitive entity edges; make merges and splits replayable with aliases.

**Gate:** Round-trip event roles, negative assertions and ambiguous entities without losing meaning; graph rebuild equals incremental projection.

**Packages:** F-02c, F-01, W2-09

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/ARCHITECTURE.md:70](../../docs/v2/ARCHITECTURE.md), [docs/v2/areas/model.md:49](../../docs/v2/areas/model.md)

## ER-21 — Lift, admit, cap, rerank

**Area:** Retrieval and composition

**Priority:** P0

**Kind:** Confirmed ordering inconsistency

**Finding:** Architecture and detailed retrieval disagree about lifting order. Lifting can also expand a capped pool beyond the reranker limit; the current adapter leaves documents after its window at zero.

**Options:** A: rerank facts then inherit scores. B: lift and re-admit, deduplicate and cap before reranking. C: score evidence bundles.

**Recommendation:** B matches the latest detailed pipeline. Preserve family coverage during the second cap; test C separately rather than silently changing the chosen single-hit unit.

**Gate:** Post-lift pool never exceeds configured capacity; no tail gets silently treated as measured zero; fan-out does not crowd out other families.

**Packages:** R3-08, R3-07

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/retrieve.md:322](../../docs/v2/pipelines/retrieve.md), [src/memspine/services/rerank/jina_rerank.py:67](../../src/memspine/services/rerank/jina_rerank.py)

## ER-22 — Admission must survive every expansion

**Area:** Retrieval and composition

**Priority:** P0

**Kind:** Design risk

**Finding:** The pipeline rechecks parents and neighbours, but no-lineage notes and derived records with no admitted parent need explicit eligibility rules too.

**Options:** A: enforce scope at initial search only. B: enforce admission on every evidence/notes expansion and again before rendering.

**Recommendation:** B. Missing lineage never provides a bypass for erasure or visibility. Define separate policy for legitimate directly supplied records and unverifiable derived records.

**Gate:** Disallowed facts never leak through notes, aliases, graph paths, rerank text, neighbours or deep retries.

**Packages:** R3-08, T5-02, F-16a

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/retrieve.md:330](../../docs/v2/pipelines/retrieve.md), [docs/v2/pipelines/retrieve.md:458](../../docs/v2/pipelines/retrieve.md)

## ER-23 — Relevance scores and abstention

**Area:** Retrieval and composition

**Priority:** P1

**Kind:** Design risk

**Finding:** Per-pool min-max reranker scores are relative; a best hit can still be irrelevant. An empty retrieval result is not proof that a fact is false.

**Options:** A: threshold relative scores globally. B: calibrated relevance by model/profile plus explicit missing/conflicting evidence states.

**Recommendation:** B. Calibrate on positive and negative queries; keep raw scores and failure/degradation traces; separate open-world inference from explicit memory claims.

**Gate:** No-evidence, weak-evidence and contradictory queries produce appropriate abstention without destroying recall on named references.

**Packages:** R3-11, R3-12

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/retrieve.md:358](../../docs/v2/pipelines/retrieve.md), [docs/v2/PLAN.md:117](../../docs/v2/PLAN.md)

## ER-24 — Exact budget and complete evidence bundles

**Area:** Retrieval and composition

**Priority:** P0

**Kind:** Confirmed guarantee mismatch

**Finding:** The packer promises always within budget while permitting chars/4 fallback. Approximation cannot guarantee the reader tokenizer’s exact limit.

**Options:** A: exact count required for hard guarantees. B: approximate mode with explicit uncertainty. C: adaptive evidence bundles with either accounting mode.

**Recommendation:** A for certified budgets; expose B as estimated rather than guaranteed. Test C for token savings while preserving claims with their necessary qualifiers, citations and temporal endpoints.

**Gate:** Final rendered prompt, headers, schemas and reply reserve fit the specified tokenizer; multilingual and long-token probes included.

**Packages:** R3-09a/b, R3-10

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/retrieve.md:488](../../docs/v2/pipelines/retrieve.md), [src/memspine/core/policies/assembly.py:288](../../src/memspine/core/policies/assembly.py)

## ER-25 — Separate public knowledge and assets

**Area:** Retrieval and composition

**Priority:** P1

**Kind:** Optional enhancement

**Finding:** External knowledge and image description are planned opt-ins. They require provenance and task boundaries to avoid confusing general knowledge with personal evidence.

**Options:** A: memory-only. B: opt-in public lookup or asset extraction after an evidence gap. C: always enrich.

**Recommendation:** B with bounded tools, source dates, asset hashes and separate result blocks. Public lookup can support a plausibility judgement but cannot establish private ownership or past behaviour.

**Gate:** Memory-only results remain reproducible; tool-enabled arm reports extra calls and failures; OCR uncertainty and conflicting sources remain visible.

**Packages:** R3-13, W2-11

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/PLAN.md:119](../../docs/v2/PLAN.md), [docs/v2/PLAN.md:93](../../docs/v2/PLAN.md)

## ER-26 — Snapshot jobs and coalesced follow-ups

**Area:** Offline learning

**Priority:** P1

**Kind:** Design risk

**Finding:** One running job and one follow-up is decided. Their coverage targets must be stable so late arrivals neither disappear nor keep a job alive indefinitely.

**Options:** A: keep extending the active job. B: immutable source/version snapshot per job and a coalesced next target.

**Recommendation:** B. Persist target_seq and member versions; advance only completed stage coverage; late session turns create a new version. Expose partial/deferred status per stage.

**Gate:** Continuous writes, late turns and concurrent sleep calls finish finite jobs and process every accepted source eventually under available capacity.

**Packages:** O4-01a/b, O4-02

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/offline.md:808](../../docs/v2/pipelines/offline.md), [docs/v2/pipelines/offline.md:662](../../docs/v2/pipelines/offline.md)

## ER-27 — Dependency-aware maintenance and pruning

**Area:** Offline learning

**Priority:** P1

**Kind:** Design risk

**Finding:** The plan accounts for watermarks and pending work in pruning, but disabled stages and reduced-retention logs also need an explicit rebuild boundary.

**Options:** A: retain full history. B: checkpoint plus verified replay tail. C: bounded retention with explicitly limited rebuild guarantees.

**Recommendation:** Support A/C now as explicit capabilities; add B only with restore tests. Do not let an inactive stage hold storage forever or claim a full rebuild after required events are pruned.

**Gate:** Fresh-store restore succeeds for advertised capabilities; pruned dependencies fail clearly; newly enabled layers report unavailable historical coverage.

**Packages:** T5-05, F-16b

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/offline.md:616](../../docs/v2/pipelines/offline.md), [src/memspine/core/projector.py:7](../../src/memspine/core/projector.py)

## ER-28 — Profiles, observations and standing answers

**Area:** Offline learning

**Priority:** P1

**Kind:** Optional enhancement

**Finding:** These layers are planned; the improvement opportunity is controlled invalidation and usefulness measurement, not merely adding more summaries.

**Options:** A: regenerate everything on every sleep. B: update changed source groups, preserve independent evidence and expire invalidated outputs.

**Recommendation:** B. Stated preferences and inferred patterns remain distinct. Standing answers need dependency fingerprints and a fallback to live evidence when stale.

**Gate:** Correction and erasure propagate to all three layers; preference utility improves without reinforcing unsupported traits or stale answers.

**Packages:** O4-05, O4-07, O4-08

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/PLAN.md:133](../../docs/v2/PLAN.md), [docs/v2/PLAN.md:136](../../docs/v2/PLAN.md)

## ER-29 — Unknown visibility is not shared

**Area:** Scope and lifecycle

**Priority:** P0

**Kind:** Confirmed ambiguity

**Finding:** Viewerless reads are described as shared-only but include unmarked records. The plan does not establish that every unmarked legacy record is explicitly shared.

**Options:** A: unmarked means shared. B: unmarked means unknown and excluded from viewerless access. C: require a viewer for all multi-party reads.

**Recommendation:** B as a refinement of shared-only, or C if the product wants a stricter contract. Require an explicit migration decision for legacy visibility.

**Gate:** Cross-user, cross-agent and viewerless probes include legacy and derived records; unknown visibility never broadens access silently.

**Packages:** F-16c, T5-01

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/retrieve.md:129](../../docs/v2/pipelines/retrieve.md)

## ER-30 — Erasure, caches and model outputs

**Area:** Scope and lifecycle

**Priority:** P0

**Kind:** Design risk

**Finding:** Lineage and generation checks are planned. The same revocation must cover cached extraction outputs, model prompts/results, graph projections, exports and rebuilt stores.

**Options:** A: best-effort deletion of current rows. B: deletion manifest, generation-aware reuse and explicit physical purge progress.

**Recommendation:** B. Prevent resurrection at deposit and cache reuse; distinguish immediate logical invisibility from physical cleanup and backup retention.

**Gate:** Forget during inference, retry, restore and reindex cannot reintroduce content; per-backend purge completion is auditable.

**Packages:** T5-04, F-16f

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/insert.md:677](../../docs/v2/pipelines/insert.md), [docs/v2/PLAN.md:149](../../docs/v2/PLAN.md)

## ER-31 — Read snapshots and cache keys

**Area:** Scope and lifecycle

**Priority:** P1

**Kind:** Design risk

**Finding:** SQL snapshot, independent index freshness and cached assembled context can disagree during updates, consent revocation or erasure.

**Options:** A: cache by query and namespace. B: cache by effective scope, policy/model versions and data generations, with final admission checks.

**Recommendation:** B. Separate embedding cache from result/context caches; bound stale reads explicitly and bypass invalidated cached content immediately.

**Gate:** Same query under different viewer/purpose/time view never reuses unauthorised context; revocation invalidates prior cached results.

**Packages:** R3-15, T5-03

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/areas/model.md:40](../../docs/v2/areas/model.md), [docs/v2/pipelines/retrieve.md:641](../../docs/v2/pipelines/retrieve.md)

## ER-32 — Model output is not the storage schema

**Area:** Schemas and modularity

**Priority:** P0

**Kind:** Design risk

**Finding:** MOD-01 versions every step; ItemV1 compiles into AssertionEnvelope. The compiler must distinguish model-supplied content from trusted scope, IDs, status and runtime metadata.

**Options:** A: reuse one model for prompts, storage and API. B: distinct extraction, canonical event and public API schemas with explicit compilers.

**Recommendation:** B. Use discriminated kinds, explicit unknowns and reserved system fields; validate semantic invariants in code in addition to JSON shape. Discuss field layout after the contracts are settled.

**Gate:** A model cannot set tenant, trust, verified state or erasure generation; schema export and version compatibility are tested.

**Packages:** MOD-01, F-02a, W2-06a

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/PLAN.md:37](../../docs/v2/PLAN.md), [docs/v2/pipelines/insert.md:38](../../docs/v2/pipelines/insert.md)

## ER-33 — Avoid versioning every temporary object as public API

**Area:** Schemas and modularity

**Priority:** P1

**Kind:** Design risk

**Finding:** Every pipeline step needs a typed contract, but identical migration promises for transient internal values and persisted events create unnecessary coupling.

**Options:** A: public semantic versions for all step objects. B: strict persistence/API versions and typed internal contracts with narrower compatibility.

**Recommendation:** B. Define which records are durable, externally exposed or replayed. Publish plugin capability versions separately from internal implementation types.

**Gate:** Internal refactor does not require event migration; persisted changes require explicit upcasters and compatibility fixtures.

**Packages:** MOD-01, F-16b, MOD-02

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/README.md:24](../../docs/v2/pipelines/README.md)

## ER-34 — Plugin reproducibility and lifecycle

**Area:** Schemas and modularity

**Priority:** P1

**Kind:** Design risk

**Finding:** Import-time registration and entry points can make enabled behaviour depend on installed packages rather than only the configuration.

**Options:** A: auto-enable every discovered plugin. B: discover plugins but explicitly enable a pinned capability manifest.

**Recommendation:** B. Fail duplicate names, validate capabilities at startup, record selected implementations and close scoped resources deterministically. Keep optional imports out of the slim core.

**Gate:** Installing an unrelated plugin does not change answers; duplicate/unsupported providers fail clearly; job/request finalisers run on exceptions.

**Packages:** MOD-02, F-08, P0-03

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/README.md:21](../../docs/v2/pipelines/README.md), [docs/v2/pipelines/README.md:22](../../docs/v2/pipelines/README.md)

## ER-35 — One frozen baseline and comparability contract

**Area:** Evaluation and adoption

**Priority:** P0

**Kind:** Confirmed gate ambiguity

**Finding:** The plan mixes 81.4 headline targets, focus references and historical contaminated screens. Equal percentages do not establish matched protocols.

**Options:** A: compare headline scores. B: require exact dataset/reader/judge/prompt/config/environment manifests and paired question IDs.

**Recommendation:** B. Designate the accepted clean run by immutable manifest; keep contaminated evidence diagnostic only. Report uncertainty, not a target score as proof.

**Gate:** Changed reader, missing questions or incompatible budget causes INCOMPARABLE/INCOMPLETE, not PROMOTE.

**Packages:** P0-01/02/03, R6-01

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/ROADMAP.md:14](../../docs/v2/ROADMAP.md), [docs/v2/PLAN.md:24](../../docs/v2/PLAN.md)

## ER-36 — Protect the blind sets across repeated decisions

**Area:** Evaluation and adoption

**Priority:** P0

**Kind:** Design risk

**Finding:** Several feature gates use the same named blind sets before a final sealed release run. Repeated feedback can turn those sets into development data.

**Options:** A: reuse blind sets for every feature. B: dev/validation subsets plus a genuinely sealed final partition. C: new hidden partition per adoption.

**Recommendation:** B or C depending on data availability. Track exposure by question and run; once observed, label it validation. Predeclare category margins and stopping rules.

**Gate:** Final paper test partition has no tuning exposure; all attempted variants and regressions remain in the experiment ledger.

**Packages:** P0-08, R6-01

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/VERIFY.md:95](../../docs/v2/VERIFY.md), [docs/v2/ROADMAP.md:67](../../docs/v2/ROADMAP.md)

## ER-37 — Measure the whole causal chain

**Area:** Evaluation and adoption

**Priority:** P1

**Kind:** Design risk

**Finding:** Answer accuracy alone cannot locate loss in extraction, evidence retrieval, packing or reader reasoning, and no architecture guarantees improvement on every question.

**Options:** A: QA score only. B: stage metrics plus paired end-to-end ablations and regression controls.

**Recommendation:** B. Track extraction omission/fidelity, evidence recall, bundle completeness, unsupported answers, temporal precision, latency, tokens, queue age and erasure. Include correct baseline cases.

**Gate:** Every enhancement declares its intended metric, acceptable regression margin and cost budget before its results are opened.

**Packages:** X-05, P0-08, W2-12

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/PLAN.md:36](../../docs/v2/PLAN.md), [docs/v2/PLAN.md:94](../../docs/v2/PLAN.md)

## ER-38 — Gold lint with provenance, not just token overlap

**Area:** Evaluation and adoption

**Priority:** P1

**Kind:** Design risk

**Finding:** A rule rejecting any gold token can also reject ordinary language shared with answers. Passing a lexical lint cannot prove absence of benchmark contamination.

**Options:** A: reject every overlapping token. B: lexical alerts plus reviewed provenance and synthetic-example policy.

**Recommendation:** B. Block copied benchmark-specific examples; review unavoidable generic overlaps; preserve rendered-prompt hashes and a narrow justified allowlist. Do not expose gold to the engine.

**Gate:** Seeded leaks are caught; generic words do not make all prompts unusable; dataset-isolated runtime is tested.

**Packages:** P0-09, X-02

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/PLAN.md:30](../../docs/v2/PLAN.md)

## ER-39 — Performance and model-call accounting

**Area:** Evaluation and adoption

**Priority:** P1

**Kind:** Optional enhancement

**Finding:** Zero-model read wording conflicts with neural embedding/reranking work. A 300 ms target needs hardware, warm/cold state, load and input-size conditions.

**Options:** A: count generative calls only. B: separate generative, embedding, reranker and tool work with end-to-end cost/latency.

**Recommendation:** B. Say zero generative calls where applicable. Report cold/warm p50/p95, throughput and memory under background load on a named reference machine.

**Gate:** SLO tests declare concurrency, pool size and text lengths; model fallback changes are visible in traces and evaluation manifests.

**Packages:** RT-02, R3-15, W2-12

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/pipelines/retrieve.md:350](../../docs/v2/pipelines/retrieve.md), [docs/v2/ROADMAP.md:45](../../docs/v2/ROADMAP.md)

## ER-40 — Keep optional features unless evidence supports retirement

**Area:** Evaluation and adoption

**Priority:** P1

**Kind:** Optional enhancement

**Finding:** A negative result in one harness supports keeping a feature off there; it does not establish uselessness across all workloads. The plan already blocks unscreened retirement for owner review.

**Options:** A: delete every negative arm immediately. B: retire implementation burden with recorded scope, preserve reproducible research evidence and feature rationale.

**Recommendation:** B. Keep no compatibility aliases as decided, but record the tested workload, alternative use cases and conditions for revisiting each removed feature.

**Gate:** Every retirement has evidence and owner disposition; old experiments remain reproducible from pinned commits.

**Packages:** F-14a/b

**Decision:** Proposed — not approved

**Evidence:** [docs/v2/PLAN.md:59](../../docs/v2/PLAN.md)

## Supporting primary references

Transaction atomicity applies to the operations participating in that database transaction; extending it to independent backends requires an additional protocol (ER-06 inference). Async cancellation needs explicit cleanup (ER-10). JSON schema structure rules do not replace domain-specific validation (ER-32).

- [PostgreSQL transaction semantics](https://www.postgresql.org/docs/current/tutorial-transactions.html)
- [Python asyncio cancellation and task groups](https://docs.python.org/3/library/asyncio-task.html)
- [JSON Schema object validation](https://json-schema.org/understanding-json-schema/reference/object)
