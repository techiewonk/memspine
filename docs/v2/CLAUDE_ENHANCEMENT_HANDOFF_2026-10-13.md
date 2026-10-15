# MemSpine enhancement handoff for Claude

Date: 2026-10-13. This file is self-contained for decision review; use the repository for code verification.

## Request and authority

Review and enhance the existing MemSpine restructure plan in `docs/v2/`. Preserve owner choices. The user explicitly selected the recommendations for ER-06, ER-24 and ER-31 after reviewing the exported choices. These three decisions are confirmed for planning. The original export's other selections are preserved, but nine unanswered entries are not approvals. No engine implementation, model evaluation, GPU restart or deployment is authorised by this document alone.

The 40 findings include inspected inconsistencies, design risks and optional hypotheses; they are not 40 proven implementation bugs. Previous work checked plan consistency, the package dependency graph and selected code paths. It did not certify every source line or demonstrate that proposed changes improve scores. Historical source hashes predate the documentation amendments in this handoff.

## Confirmed amendments

| ID | Confirmed choice | Required contract |
|---|---|---|
| ER-06 | A | Durable acceptance is the default: commit authoritative SQL data and outbox intents atomically, then return per-capability readiness. Callers may explicitly wait for required raw indexes or derived memory/graph through bounded barriers with timeout and readiness reporting. |
| ER-24 | C + A | C + A: adaptive complete evidence bundles under an exact reader-tokenizer budget, including rendered headers, citations and configured reserves. Estimated accounting is an explicitly labelled fallback and must not claim a hard token guarantee. |
| ER-31 | B | Result/context caches include effective scope and viewer/purpose, time view, policy/model versions, record versions and invalidation generations. Recheck admission at use; keep result/context caching disabled until this contract is implemented. Embedding-cache keys separately include encoder/config identity. |

## Documents updated now

- `pipelines/insert.md`: SQL transaction boundary, capability readiness, optional bounded barriers and accepted-write failure semantics.
- `pipelines/retrieve.md`: default reads use ready projections; adaptive complete evidence bundles; exact versus explicitly estimated accounting; scoped/versioned cache prerequisites.
- `ARCHITECTURE.md` and `README.md`: latest-amendment precedence and links.
- `DECISION_AMENDMENTS_2026-10-13.md`: authority for the three confirmed changes.
- `data/decisions_reconciled_2026-10-13.json`: current selections, preserving the original export.

Older scenario shorthand is explicitly superseded; reconcile every affected scenario, diagram, area chapter and package gate before implementation. Package statuses have not been advanced. The older review is historical and must not be regenerated over the current choices.

## Enhancement work to plan next

| Sequence | Work | Existing packages | Acceptance / prerequisite |
|---|---|---|---|
| 1 | Reconcile authoritative decisions, diagrams and dependency edges | X-00, X-04, P0-03 | No active contradictory contract; generated summaries reproduce source decisions. Add prerequisite edges at consumers, not blanket dependencies. |
| 2 | Define durable receipt and optional wait semantics | F-04, RT-01, W2-01 | Crash before/after SQL commit and between projectors; accepted writes survive index failure; timed-out waits do not undo accepted writes. ER-07/09/10 runtime details remain open. |
| 3 | Separate pure refactor, persistence migration and behavioural changes | F-02a/b, F-08, F-13a | Appropriate migration/lifecycle/API gates; CPU golden alone is not sufficient for new semantics. |
| 4 | Specify complete evidence bundles and exact accounting | R3-09a/b, R3-10 | Multilingual and oversize bundles; citations and required qualifiers survive; final rendered text plus configured reserves stays within the exact tokenizer budget. Quality change screened separately. |
| 5 | Specify scoped/versioned caches; enable only after tests | R3-15, T5-03, T5-04 | Different viewers, purposes, time views and generations cannot reuse inadmissible context; erasure and model upgrades invalidate affected entries. |
| 6 | Finalise remaining semantics, then design schemas | MOD-01, F-02a/b, F-16a/c | Resolve identity, evidence bindings, job fencing and temporal precision before freezing fields; separate model output from trusted stored metadata. |
| 7 | Run isolated enhancements through comparable gates | P0-01/02/03/08, R6-01 | Named manifests, exposed validation vs sealed tests, stage metrics, preset cost/regression margins; no unsupported universal improvement claims. |

Receipt readiness field names in the pipeline are illustrative, not a frozen schema. Define the consistency target, timeout and partial-read behaviour before field naming. Durable guarantees apply to durable profiles; ephemeral mode must state its weaker guarantee.

## Open decisions - do not infer approval

ER-07 through ER-15 are unanswered: fencing, overload, progress holes, cancellation, cross-turn citations, selective extraction recovery, cross-session identity, multidimensional uncertainty and bounded repair. Prior recommendations are included below for discussion.

ER-27 C permits bounded retention with limited historical rebuild, but the retained source/event/lineage contract must be specified. ER-29 C requires a viewer on multi-party reads and supersedes the older viewerless shared-only fallback; update all entry points and authenticate viewer binding. Both remain implementation conditions.

Two additional policy questions remain open:

1. **In-flight erasure:** permit reads authorised before deletion to finish, or revalidate generation/permissions immediately before returning? Recommendation: final revalidation for strict revocation, with an explicit read/erase linearisation point. Already-delivered content cannot be recalled.
2. **Derived visibility:** check only derived labels, or also effective visibility/purpose of supporting lineage? Recommendation: lineage-aware admission and omission/re-derivation when necessary. Numeric trust is not an access-control policy.

The worksheet should distinguish selected, complete and finalised; custom choices need notes. This is a workflow improvement, not another silently approved architecture decision.

## All 40 findings and current choices

Every row preserves its original options. Confirmed amendments override only ER-06, ER-24 and ER-31. Other selected rows remain subject to the listed conditions and clean adoption gates.

### ER-01: One authoritative decision view

| Field | Detail |
|---|---|
| Area / priority / evidence class | Plan and delivery / P0 / Confirmed plan inconsistency |
| Finding | The latest L3 default, lift-before-rerank order and non-blocking sleep decisions coexist with older contradictory prose and diagrams. |
| Options | A: manually patch all copies (quick, drift can return). B: canonical decision records with generated summaries (more tooling, fewer contradictions). |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Generate active summaries from canonical decisions; preserve history as superseded rather than deleting it. |
| Acceptance check | No active document contradicts the latest owner decisions; diagrams and pipeline order agree. |
| Packages | X-00, X-04 |
| Evidence pointers | docs/v2/ARCHITECTURE.md:24, docs/v2/ARCHITECTURE.md:147, docs/v2/VERIFY.md:47 |

### ER-02: Enforce prerequisites in the DAG

| Field | Detail |
|---|---|
| Area / priority / evidence class | Plan and delivery / P0 / Confirmed dependency gap |
| Finding | AST inspection finds no cycles, but R6-01 has no dependency path to P0-02, P0-03, P0-08, MOD-01 or X-01. Coverage of a decision does not enforce completion. |
| Options | A: rely on manual phase checks. B: explicit dependency edges plus machine-checked phase requirements. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Add prerequisites at the relevant consumers/gates; do not make every package depend on every preparation task. |
| Acceptance check | A deliberately incomplete prerequisite blocks the corresponding package or exit; graph remains acyclic. |
| Packages | P0-02, P0-03, P0-08, MOD-01, X-01, all EX gates |
| Evidence pointers | docs/v2/data/packages.py:10, docs/v2/data/build_plan.py:46 |

### ER-03: Separate moves, migrations and behaviour

| Field | Detail |
|---|---|
| Area / priority / evidence class | Plan and delivery / P0 / Confirmed classification mismatch |
| Finding | New record envelopes and tri-temporal fields are classified as moves. Registries, DI lifetimes and a new public handle also introduce contracts beyond relocation. |
| Options | A: one large golden-gated PR. B: split pure moves, schema migrations, API changes and behaviour changes. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Use separate move, migration, API-contract and quality gates; not every behaviour change requires a GPU test. |
| Acceptance check | Every package has a precise change class and appropriate gate; canonical replay equivalence is defined separately from byte equality. |
| Packages | F-02a/b, MOD-02, F-08, F-13a |
| Evidence pointers | docs/v2/PLAN.md:61, docs/v2/PLAN.md:46 |

### ER-04: Owned files and integration collisions

| Field | Detail |
|---|---|
| Area / priority / evidence class | Plan and delivery / P1 / Design risk |
| Finding | Serial lanes do not protect shared schemas, migrations, exports, dependencies and generated docs across other lanes; current ownership uses broad wildcards. |
| Options | A: serialize all work (simple, slow). B: exact ownership plus a shared-file integration queue (more tooling, parallel work). |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Include shared schemas, migrations, exports, dependency files and generated documents in collision checks. |
| Acceptance check | Synthetic concurrent conflicts are rejected before agents start; merge CI repeats contracts against the integration head. |
| Packages | X-01, X-02, RESTRUCTURE |
| Evidence pointers | docs/v2/RESTRUCTURE.md:60, docs/v2/EXECUTION.md:9 |

### ER-05: Deliver a small end-to-end slice first

| Field | Detail |
|---|---|
| Area / priority / evidence class | Plan and delivery / P1 / Optional enhancement |
| Finding | Phase 1 currently waits for the server/Neo4j profile before its exit, increasing the amount of unvalidated foundation work. |
| Options | A: both profiles before any exit. B: embedded internal milestone followed by the mandatory server milestone. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | An embedded milestone is not permission to release the server profile without its isolation/erasure/rebuild gates. |
| Acceptance check | One synthetic end-to-end workload survives restart and erasure before broad feature work; no server release before its matrix passes. |
| Packages | EX-1, F-16d/e/f |
| Evidence pointers | docs/v2/ROADMAP.md:24 |

### ER-06: Atomic commit and write readiness

| Field | Detail |
|---|---|
| Area / priority / evidence class | Durability and runtime / P0 / Confirmed contract conflict |
| Finding | Insert step 8 includes lexical/vector projections in one SQL transaction, although the embedded profile uses independent backends. It also promises immediate search while outbox_all delays it. |
| Options | A: durable receipt with per-index readiness. B: wait for required raw indexes. C: wait for derivation and graph too. |
| Current choice | A |
| Review status | Confirmed planning amendment; implementation pending |
| Conditions / refinement | Durable acceptance is the default: commit authoritative SQL data and outbox intents atomically, then return per-capability readiness. Callers may explicitly wait for required raw indexes or derived memory/graph through bounded barriers with timeout and readiness reporting. |
| Acceptance check | Crash at every commit/projector boundary; receipt never claims an unavailable capability; retries do not duplicate source events. |
| Packages | F-04, W2-01, RT-01 |
| Evidence pointers | docs/v2/pipelines/insert.md:269, src/memspine/core/projector.py:19 |

### ER-07: Fence expired workers

| Field | Detail |
|---|---|
| Area / priority / evidence class | Durability and runtime / P0 / Design risk |
| Finding | Lease expiry and heartbeat are specified, but the shown NamespaceJobLease has no monotonic fencing epoch. Erasure generation and worker ownership solve different races. |
| Options | A: lease owner and TTL only. B: monotonic claim epoch plus conditional commits. C: hold a DB lock throughout inference. |
| Current choice | Unanswered |
| Review status | Unanswered |
| Conditions / refinement | No selection recorded. Proposed direction: B. Check claim epoch, source versions and erasure generation in the deposit transaction; reject the old worker after takeover. Keep model calls outside write locks. |
| Acceptance check | Pause worker A beyond TTL, let B finish, then resume A: A cannot write, acknowledge, advance watermarks or release B’s lease. |
| Packages | F-04, O4-01a |
| Evidence pointers | docs/v2/pipelines/offline.md:147, docs/v2/pipelines/insert.md:677 |

### ER-08: Bound overload honestly

| Field | Detail |
|---|---|
| Area / priority / evidence class | Durability and runtime / P0 / Design risk |
| Finding | Never drop, writes never block and finite storage cannot all hold during sustained overload. Coalescing saves overhead but cannot remove the underlying raw-data growth. |
| Options | A: unbounded backlog. B: quotas, durable acceptance and explicit admission failure. C: block writers until capacity returns. |
| Current choice | Unanswered |
| Review status | Unanswered |
| Conditions / refinement | No selection recorded. Proposed direction: B with configurable bounded waiting. Never drop already acknowledged work; reject before acceptance when storage limits are reached. Keep emergency space for erasure and recovery. |
| Acceptance check | Sustained overload, disk-full and tenant-skew tests; accepted work is accounted for and rejected writes are distinguishable. |
| Packages | RT-02, W2-12 |
| Evidence pointers | docs/v2/pipelines/README.md:20 |

### ER-09: Projection order, holes and freshness

| Field | Detail |
|---|---|
| Area / priority / evidence class | Durability and runtime / P1 / Design risk |
| Finding | Watermarks and covers_seq exist, but a highest observed sequence alone cannot prove all earlier work completed when jobs fail or finish out of order. |
| Options | A: maximum seen seq. B: contiguous committed watermark plus holes and source-version fingerprints. |
| Current choice | Unanswered |
| Review status | Unanswered |
| Conditions / refinement | No selection recorded. Proposed direction: B. Separate projector progress, derivation coverage and source membership. Barriers wait for a captured target, not for a queue that can grow forever. |
| Acceptance check | Complete seq 12 before 11; freshness must remain incomplete until 11 is resolved. New writes must not extend an existing barrier target. |
| Packages | O4-01b, F-04, R3-15 |
| Evidence pointers | docs/v2/pipelines/offline.md:753, docs/v2/pipelines/offline.md:732 |

### ER-10: Cancellation, shutdown and fairness

| Field | Detail |
|---|---|
| Area / priority / evidence class | Durability and runtime / P1 / Design risk |
| Finding | Async tasks, executors and priorities are selected; cancellation of orchestration does not by itself guarantee an in-flight inference or executor task has stopped. |
| Options | A: best-effort shutdown. B: tracked jobs, bounded drain, cancellation-aware commits and per-tenant resource shares. |
| Current choice | Unanswered |
| Review status | Unanswered |
| Conditions / refinement | No selection recorded. Proposed direction: B. Separate logical cancellation from physical completion; never reuse a model slot while its work still runs. Persist unfinished jobs before close returns. |
| Acceptance check | Cancel during HTTP, encoder inference and deposit; close with a deadline; no resource leak, stale commit or starving tenant. |
| Packages | RT-02, F-08 |
| Evidence pointers | docs/v2/pipelines/README.md:18, docs/v2/pipelines/README.md:19 |

### ER-11: Cite cross-turn bindings

| Field | Detail |
|---|---|
| Area / priority / evidence class | Evidence and extraction / P0 / Confirmed evidence restriction |
| Finding | CONTEXT may resolve references but context citations are rejected. A new statement can then depend on an older name or relationship that is absent from its lineage. |
| Options | A: NEW-only citations. B: NEW assertion trigger plus citeable CONTEXT binding evidence. C: re-extract all context every time. |
| Current choice | Unanswered |
| Review status | Unanswered |
| Conditions / refinement | No selection recorded. Proposed direction: B. Distinguish assertion support from reference-binding support. Include both source versions in lineage and erasure checks; do not count context as a new occurrence. |
| Acceptance check | Invented cross-turn pronouns, country references and elliptical replies retain correct bindings; deleting the binding source invalidates dependents. |
| Packages | W2-06a, W2-08, F-16a |
| Evidence pointers | docs/v2/pipelines/insert.md:533 |

### ER-12: Recover missed information selectively

| Field | Detail |
|---|---|
| Area / priority / evidence class | Evidence and extraction / P1 / Optional enhancement |
| Finding | Schema validity and exact spans test emitted items, not facts the extractor omitted. Lists, subordinate clauses and quantities need completeness controls. |
| Options | A: one pass only. B: cheap coverage diagnostics with bounded recovery. C: second model pass for every window. |
| Current choice | Unanswered |
| Review status | Unanswered |
| Conditions / refinement | No selection recorded. Proposed direction: B behind a screen: retain all raw text; detect likely uncovered named items, quantities and list members; targeted re-extraction only when justified. Coverage heuristics are not proof. |
| Acceptance check | Measure omitted-fact recall and unsupported additions separately on unseen domains, lists and paraphrases; record extra calls. |
| Packages | W2-06b, W2-08, X-05 |
| Evidence pointers | docs/v2/PLAN.md:90, docs/v2/pipelines/insert.md:468 |

### ER-13: Identity across sessions and occurrences

| Field | Detail |
|---|---|
| Area / priority / evidence class | Evidence and extraction / P0 / Design risk |
| Finding | The scope tuple includes session and is placed inside every identity. Taken literally, that can fragment a persistent entity across sessions. Content-only IDs can instead collapse repeated real events. |
| Options | A: session-local identity everywhere. B: stable memory scope plus separate source/session identity and event occurrence IDs. |
| Current choice | Unanswered |
| Review status | Unanswered |
| Conditions / refinement | No selection recorded. Proposed direction: B. Choose entity scope explicitly (tenant/user/agent policy); keep session as provenance. Separate retry identity, mention identity, assertion identity and occurrence identity. |
| Acceptance check | Same person across sessions links correctly; homonyms stay apart; retries stay one effect; two identical same-day events remain two. |
| Packages | F-01, F-16c, W2-09 |
| Evidence pointers | docs/v2/areas/model.md:40, docs/v2/areas/model.md:51 |

### ER-14: Keep uncertainty multidimensional

| Field | Detail |
|---|---|
| Area / priority / evidence class | Evidence and extraction / P1 / Design risk |
| Finding | A broad confidence or grounded label can hide different uncertainty in extraction, entity binding, time, modality and source trust. |
| Options | A: one confidence float. B: separate assessment fields with explicit allowed uses. |
| Current choice | Unanswered |
| Review status | Unanswered |
| Conditions / refinement | No selection recorded. Proposed direction: B. Keep source trust distinct from support strength and entity/time certainty; define which combinations may supersede, enter the graph or appear as tentative evidence. |
| Acceptance check | A trusted source with an ambiguous pronoun never gains a certain identity; a precise timestamp never upgrades an uncertain claim. |
| Packages | F-02a, W2-08, W2-10 |
| Evidence pointers | docs/v2/pipelines/insert.md:535 |

### ER-15: Bound repairs and truncation recovery

| Field | Detail |
|---|---|
| Area / priority / evidence class | Evidence and extraction / P0 / Confirmed budget conflict |
| Finding | The architecture caps extraction at one call per window while the pipeline permits a repair call and optional entailment adjudication. Truncated output also needs reliable source coverage accounting. |
| Options | A: hard one attempt and defer failures. B: one normal call plus bounded recovery budgets. C: retry until valid. |
| Current choice | Unanswered |
| Review status | Unanswered |
| Conditions / refinement | No selection recorded. Proposed direction: B. Count attempts by purpose, cap total tokens/time, cache validated outputs and track processed source ranges. Never infer complete coverage from syntactically valid partial output. |
| Acceptance check | Malformed, empty-valid and truncated outputs terminate within budget; valid empty extraction is not endlessly retried; unprocessed lines stay pending. |
| Packages | W2-06a/b, W2-12 |
| Evidence pointers | docs/v2/ARCHITECTURE.md:116, docs/v2/pipelines/insert.md:473 |

### ER-16: Prompt economy without losing semantics

| Field | Detail |
|---|---|
| Area / priority / evidence class | Evidence and extraction / P1 / Optional enhancement |
| Finding | Static prompt length alone excludes schema, aliases, context, output and repair costs. Shortening instructions can remove necessary negation and attribution rules. |
| Options | A: optimise instruction tokens only. B: benchmark complete requests and useful grounded information per token. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Measure complete requests including schemas, context, output and retries; preserve fidelity while reducing tokens. |
| Acceptance check | Report full input/output tokens, latency, fact recall and fidelity at equal budgets across several languages and domains. |
| Packages | W2-07, W2-12, P0-09 |
| Evidence pointers | docs/v2/ARCHITECTURE.md:116 |

### ER-17: Independent support and change semantics

| Field | Detail |
|---|---|
| Area / priority / evidence class | Evidence and extraction / P1 / Design risk |
| Finding | Echo guard, support counts and change hints exist, but repeated mentions, shared source copies and genuine independent corroboration must not be counted equally. |
| Options | A: count matching mentions. B: source-family-aware support with reversible change decisions. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Independent support needs source-family lineage. Requires unresolved ER-11/13; repetition and echoes are not independent evidence. |
| Acceptance check | Copy loops never increase independent support; A→B→A history survives; quoted denial and hypothetical changes do not supersede facts. |
| Packages | W2-03, W2-10, O4-04 |
| Evidence pointers | docs/v2/pipelines/insert.md:975, docs/v2/pipelines/insert.md:980, docs/v2/pipelines/offline.md:389 |

### ER-18: Separate temporal anchors from event dates

| Field | Detail |
|---|---|
| Area / priority / evidence class | Time and graph / P0 / Design risk |
| Finding | The owner’s temporal ladder includes nearby dated lines, session order and recorded_at. These signals do not all establish when the described event occurred. |
| Options | A: choose the strongest available timestamp. B: preserve unresolved expressions, intervals, ordering constraints and explicit anchor provenance. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Keep weak anchors labelled. Unknown dates must not become recorded_at event dates; settle the time schema before implementation. |
| Acceptance check | Month/year precision, timezone boundaries, ambiguous relative dates and undated durations never acquire fabricated exact dates. |
| Packages | F-02b, W2-14, R3-14 |
| Evidence pointers | docs/v2/pipelines/insert.md:974 |

### ER-19: Query evidence sets for joins and durations

| Field | Detail |
|---|---|
| Area / priority / evidence class | Time and graph / P1 / Optional enhancement |
| Finding | Multi-hop questions need jointly compatible evidence: both endpoints of a duration, intersections of visited places, or all members of a list. |
| Options | A: top-k independent hits. B: deterministic evidence obligations with bounded expansion. C: unrestricted agent loop. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | This remains a screened enhancement, not a guaranteed improvement. Requires ER-11 evidence bindings, ER-13 identity and ER-15 retry budgets. |
| Acceptance check | Unseen intersections, durations and enumeration tasks improve at fixed reader/token budgets without increasing unsupported answers. |
| Packages | R3-02, R3-05b, R3-12 |
| Evidence pointers | docs/v2/PLAN.md:105, docs/v2/PLAN.md:118 |

### ER-20: Graph as a supported projection

| Field | Detail |
|---|---|
| Area / priority / evidence class | Time and graph / P1 / Design risk |
| Finding | The graph default is decided, but not every assertion is a safe binary edge; n-ary events, negation and uncertain bindings require richer projection rules. |
| Options | A: force every fact into a triple. B: assertion nodes with optional typed role edges and explicit uncertainty. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Not every fact becomes a binary edge. Uncertain and n-ary assertions retain assertion nodes and source roles. |
| Acceptance check | Round-trip event roles, negative assertions and ambiguous entities without losing meaning; graph rebuild equals incremental projection. |
| Packages | F-02c, F-01, W2-09 |
| Evidence pointers | docs/v2/ARCHITECTURE.md:70, docs/v2/areas/model.md:49 |

### ER-21: Lift, admit, cap, rerank

| Field | Detail |
|---|---|
| Area / priority / evidence class | Retrieval and composition / P0 / Confirmed ordering inconsistency |
| Finding | Architecture and detailed retrieval disagree about lifting order. Lifting can also expand a capped pool beyond the reranker limit; the current adapter leaves documents after its window at zero. |
| Options | A: rerank facts then inherit scores. B: lift and re-admit, deduplicate and cap before reranking. C: score evidence bundles. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Recheck permissions and cap AFTER lifting; define ranking of parents with large fan-out. Keep unscored distinct from scored-zero. |
| Acceptance check | Post-lift pool never exceeds configured capacity; no tail gets silently treated as measured zero; fan-out does not crowd out other families. |
| Packages | R3-08, R3-07 |
| Evidence pointers | docs/v2/pipelines/retrieve.md:322, src/memspine/services/rerank/jina_rerank.py:67 |

### ER-22: Admission must survive every expansion

| Field | Detail |
|---|---|
| Area / priority / evidence class | Retrieval and composition / P0 / Design risk |
| Finding | The pipeline rechecks parents and neighbours, but no-lineage notes and derived records with no admitted parent need explicit eligibility rules too. |
| Options | A: enforce scope at initial search only. B: enforce admission on every evidence/notes expansion and again before rendering. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Re-admit notes, parents, neighbours, graph paths and tool results; no-lineage derived notes cannot bypass admission. |
| Acceptance check | Disallowed facts never leak through notes, aliases, graph paths, rerank text, neighbours or deep retries. |
| Packages | R3-08, T5-02, F-16a |
| Evidence pointers | docs/v2/pipelines/retrieve.md:330, docs/v2/pipelines/retrieve.md:458 |

### ER-23: Relevance scores and abstention

| Field | Detail |
|---|---|
| Area / priority / evidence class | Retrieval and composition / P1 / Design risk |
| Finding | Per-pool min-max reranker scores are relative; a best hit can still be irrelevant. An empty retrieval result is not proof that a fact is false. |
| Options | A: threshold relative scores globally. B: calibrated relevance by model/profile plus explicit missing/conflicting evidence states. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Calibrate separately per model/profile and retain missing-evidence vs false-claim distinction. Empty retrieval does not prove absence. |
| Acceptance check | No-evidence, weak-evidence and contradictory queries produce appropriate abstention without destroying recall on named references. |
| Packages | R3-11, R3-12 |
| Evidence pointers | docs/v2/pipelines/retrieve.md:358, docs/v2/PLAN.md:117 |

### ER-24: Exact budget and complete evidence bundles

| Field | Detail |
|---|---|
| Area / priority / evidence class | Retrieval and composition / P0 / Confirmed guarantee mismatch |
| Finding | The packer promises always within budget while permitting chars/4 fallback. Approximation cannot guarantee the reader tokenizer’s exact limit. |
| Options | A: exact count required for hard guarantees. B: approximate mode with explicit uncertainty. C: adaptive evidence bundles with either accounting mode. |
| Current choice | C + A |
| Review status | Confirmed planning amendment; implementation pending |
| Conditions / refinement | C + A: adaptive complete evidence bundles under an exact reader-tokenizer budget, including rendered headers, citations and configured reserves. Estimated accounting is an explicitly labelled fallback and must not claim a hard token guarantee. |
| Acceptance check | Final rendered prompt, headers, schemas and reply reserve fit the specified tokenizer; multilingual and long-token probes included. |
| Packages | R3-09a/b, R3-10 |
| Evidence pointers | docs/v2/pipelines/retrieve.md:488, src/memspine/core/policies/assembly.py:288 |

### ER-25: Separate public knowledge and assets

| Field | Detail |
|---|---|
| Area / priority / evidence class | Retrieval and composition / P1 / Optional enhancement |
| Finding | External knowledge and image description are planned opt-ins. They require provenance and task boundaries to avoid confusing general knowledge with personal evidence. |
| Options | A: memory-only. B: opt-in public lookup or asset extraction after an evidence gap. C: always enrich. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Public knowledge cannot prove private history. Keep tool results and image/OCR uncertainty separate from user memory; cap calls. |
| Acceptance check | Memory-only results remain reproducible; tool-enabled arm reports extra calls and failures; OCR uncertainty and conflicting sources remain visible. |
| Packages | R3-13, W2-11 |
| Evidence pointers | docs/v2/PLAN.md:119, docs/v2/PLAN.md:93 |

### ER-26: Snapshot jobs and coalesced follow-ups

| Field | Detail |
|---|---|
| Area / priority / evidence class | Offline learning / P1 / Design risk |
| Finding | One running job and one follow-up is decided. Their coverage targets must be stable so late arrivals neither disappear nor keep a job alive indefinitely. |
| Options | A: keep extending the active job. B: immutable source/version snapshot per job and a coalesced next target. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Capture an immutable coverage target and fenced worker ownership. Depends on unanswered ER-07/09/10. |
| Acceptance check | Continuous writes, late turns and concurrent sleep calls finish finite jobs and process every accepted source eventually under available capacity. |
| Packages | O4-01a/b, O4-02 |
| Evidence pointers | docs/v2/pipelines/offline.md:808, docs/v2/pipelines/offline.md:662 |

### ER-27: Dependency-aware maintenance and pruning

| Field | Detail |
|---|---|
| Area / priority / evidence class | Offline learning / P1 / Design risk |
| Finding | The plan accounts for watermarks and pending work in pruning, but disabled stages and reduced-retention logs also need an explicit rebuild boundary. |
| Options | A: retain full history. B: checkpoint plus verified replay tail. C: bounded retention with explicitly limited rebuild guarantees. |
| Current choice | C |
| Review status | Compatible with explicit limits |
| Conditions / refinement | C intentionally limits historical rebuild. State which raw records, event versions, tombstones and lineage remain available. Block pruning of pending work; advertise capability limits; fail rather than silently return an incomplete rebuild. It need not prevent rebuilding indexes from retained raw records. |
| Acceptance check | Fresh-store restore succeeds for advertised capabilities; pruned dependencies fail clearly; newly enabled layers report unavailable historical coverage. |
| Packages | T5-05, F-16b |
| Evidence pointers | docs/v2/pipelines/offline.md:616, src/memspine/core/projector.py:7 |

### ER-28: Profiles, observations and standing answers

| Field | Detail |
|---|---|
| Area / priority / evidence class | Offline learning / P1 / Optional enhancement |
| Finding | These layers are planned; the improvement opportunity is controlled invalidation and usefulness measurement, not merely adding more summaries. |
| Options | A: regenerate everything on every sleep. B: update changed source groups, preserve independent evidence and expire invalidated outputs. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Profile, observation and standing-answer invalidation depend on ER-11/17/26/30 and retained source availability under ER-27. |
| Acceptance check | Correction and erasure propagate to all three layers; preference utility improves without reinforcing unsupported traits or stale answers. |
| Packages | O4-05, O4-07, O4-08 |
| Evidence pointers | docs/v2/PLAN.md:133, docs/v2/PLAN.md:136 |

### ER-29: Unknown visibility is not shared

| Field | Detail |
|---|---|
| Area / priority / evidence class | Scope and lifecycle / P0 / Confirmed ambiguity |
| Finding | Viewerless reads are described as shared-only but include unmarked records. The plan does not establish that every unmarked legacy record is explicitly shared. |
| Options | A: unmarked means shared. B: unmarked means unknown and excluded from viewerless access. C: require a viewer for all multi-party reads. |
| Current choice | C |
| Review status | Valid policy change — reconcile plan |
| Conditions / refinement | C requires a viewer on every multi-party read. It replaces the existing shared-only viewerless fallback. Apply it to REST, MCP, internal/system callers and migrations; derive viewer identity from an authenticated principal, not an arbitrary untrusted parameter. |
| Acceptance check | Cross-user, cross-agent and viewerless probes include legacy and derived records; unknown visibility never broadens access silently. |
| Packages | F-16c, T5-01 |
| Evidence pointers | docs/v2/pipelines/retrieve.md:129 |

### ER-30: Erasure, caches and model outputs

| Field | Detail |
|---|---|
| Area / priority / evidence class | Scope and lifecycle / P0 / Design risk |
| Finding | Lineage and generation checks are planned. The same revocation must cover cached extraction outputs, model prompts/results, graph projections, exports and rebuilt stores. |
| Options | A: best-effort deletion of current rows. B: deletion manifest, generation-aware reuse and explicit physical purge progress. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Specify logical invisibility, physical purge and backup retention separately; cached model results must not resurrect erased data. |
| Acceptance check | Forget during inference, retry, restore and reindex cannot reintroduce content; per-backend purge completion is auditable. |
| Packages | T5-04, F-16f |
| Evidence pointers | docs/v2/pipelines/insert.md:677, docs/v2/PLAN.md:149 |

### ER-31: Read snapshots and cache keys

| Field | Detail |
|---|---|
| Area / priority / evidence class | Scope and lifecycle / P1 / Design risk |
| Finding | SQL snapshot, independent index freshness and cached assembled context can disagree during updates, consent revocation or erasure. |
| Options | A: cache by query and namespace. B: cache by effective scope, policy/model versions and data generations, with final admission checks. |
| Current choice | B |
| Review status | Confirmed planning amendment; implementation pending |
| Conditions / refinement | Result/context caches include effective scope and viewer/purpose, time view, policy/model versions, record versions and invalidation generations. Recheck admission at use; keep result/context caching disabled until this contract is implemented. Embedding-cache keys separately include encoder/config identity. |
| Acceptance check | Same query under different viewer/purpose/time view never reuses unauthorised context; revocation invalidates prior cached results. |
| Packages | R3-15, T5-03 |
| Evidence pointers | docs/v2/areas/model.md:40, docs/v2/pipelines/retrieve.md:641 |

### ER-32: Model output is not the storage schema

| Field | Detail |
|---|---|
| Area / priority / evidence class | Schemas and modularity / P0 / Design risk |
| Finding | MOD-01 versions every step; ItemV1 compiles into AssertionEnvelope. The compiler must distinguish model-supplied content from trusted scope, IDs, status and runtime metadata. |
| Options | A: reuse one model for prompts, storage and API. B: distinct extraction, canonical event and public API schemas with explicit compilers. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | The model must never author scope, trust, verified state, fencing tokens or erasure generation. Resolve semantics before fields. |
| Acceptance check | A model cannot set tenant, trust, verified state or erasure generation; schema export and version compatibility are tested. |
| Packages | MOD-01, F-02a, W2-06a |
| Evidence pointers | docs/v2/PLAN.md:37, docs/v2/pipelines/insert.md:38 |

### ER-33: Avoid versioning every temporary object as public API

| Field | Detail |
|---|---|
| Area / priority / evidence class | Schemas and modularity / P1 / Design risk |
| Finding | Every pipeline step needs a typed contract, but identical migration promises for transient internal values and persisted events create unnecessary coupling. |
| Options | A: public semantic versions for all step objects. B: strict persistence/API versions and typed internal contracts with narrower compatibility. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Keep durable event/API compatibility distinct from transient internal typing; exported internal schemas need not become public promises. |
| Acceptance check | Internal refactor does not require event migration; persisted changes require explicit upcasters and compatibility fixtures. |
| Packages | MOD-01, F-16b, MOD-02 |
| Evidence pointers | docs/v2/pipelines/README.md:24 |

### ER-34: Plugin reproducibility and lifecycle

| Field | Detail |
|---|---|
| Area / priority / evidence class | Schemas and modularity / P1 / Design risk |
| Finding | Import-time registration and entry points can make enabled behaviour depend on installed packages rather than only the configuration. |
| Options | A: auto-enable every discovered plugin. B: discover plugins but explicitly enable a pinned capability manifest. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Discovery does not mean enablement. Pin selected plugins in manifests; fail duplicate registrations and scope lifetime violations. |
| Acceptance check | Installing an unrelated plugin does not change answers; duplicate/unsupported providers fail clearly; job/request finalisers run on exceptions. |
| Packages | MOD-02, F-08, P0-03 |
| Evidence pointers | docs/v2/pipelines/README.md:21, docs/v2/pipelines/README.md:22 |

### ER-35: One frozen baseline and comparability contract

| Field | Detail |
|---|---|
| Area / priority / evidence class | Evaluation and adoption / P0 / Confirmed gate ambiguity |
| Finding | The plan mixes 81.4 headline targets, focus references and historical contaminated screens. Equal percentages do not establish matched protocols. |
| Options | A: compare headline scores. B: require exact dataset/reader/judge/prompt/config/environment manifests and paired question IDs. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Freeze actual comparable run manifests, not merely the historical 81.4 headline. Retain all attempted arms. |
| Acceptance check | Changed reader, missing questions or incompatible budget causes INCOMPARABLE/INCOMPLETE, not PROMOTE. |
| Packages | P0-01/02/03, R6-01 |
| Evidence pointers | docs/v2/ROADMAP.md:14, docs/v2/PLAN.md:24 |

### ER-36: Protect the blind sets across repeated decisions

| Field | Detail |
|---|---|
| Area / priority / evidence class | Evaluation and adoption / P0 / Design risk |
| Finding | Several feature gates use the same named blind sets before a final sealed release run. Repeated feedback can turn those sets into development data. |
| Options | A: reuse blind sets for every feature. B: dev/validation subsets plus a genuinely sealed final partition. C: new hidden partition per adoption. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Previously inspected questions are validation, not blind. Preserve a genuinely unexposed final partition and an exposure ledger. |
| Acceptance check | Final paper test partition has no tuning exposure; all attempted variants and regressions remain in the experiment ledger. |
| Packages | P0-08, R6-01 |
| Evidence pointers | docs/v2/VERIFY.md:95, docs/v2/ROADMAP.md:67 |

### ER-37: Measure the whole causal chain

| Field | Detail |
|---|---|
| Area / priority / evidence class | Evaluation and adoption / P1 / Design risk |
| Finding | Answer accuracy alone cannot locate loss in extraction, evidence retrieval, packing or reader reasoning, and no architecture guarantees improvement on every question. |
| Options | A: QA score only. B: stage metrics plus paired end-to-end ablations and regression controls. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Use paired stage and end-to-end metrics with preset margins; no claim of improving every question. |
| Acceptance check | Every enhancement declares its intended metric, acceptable regression margin and cost budget before its results are opened. |
| Packages | X-05, P0-08, W2-12 |
| Evidence pointers | docs/v2/PLAN.md:36, docs/v2/PLAN.md:94 |

### ER-38: Gold lint with provenance, not just token overlap

| Field | Detail |
|---|---|
| Area / priority / evidence class | Evaluation and adoption / P1 / Design risk |
| Finding | A rule rejecting any gold token can also reject ordinary language shared with answers. Passing a lexical lint cannot prove absence of benchmark contamination. |
| Options | A: reject every overlapping token. B: lexical alerts plus reviewed provenance and synthetic-example policy. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Use reviewed provenance and narrow justified exceptions; passing token lint alone is not contamination clearance. |
| Acceptance check | Seeded leaks are caught; generic words do not make all prompts unusable; dataset-isolated runtime is tested. |
| Packages | P0-09, X-02 |
| Evidence pointers | docs/v2/PLAN.md:30 |

### ER-39: Performance and model-call accounting

| Field | Detail |
|---|---|
| Area / priority / evidence class | Evaluation and adoption / P1 / Optional enhancement |
| Finding | Zero-model read wording conflicts with neural embedding/reranking work. A 300 ms target needs hardware, warm/cold state, load and input-size conditions. |
| Options | A: count generative calls only. B: separate generative, embedding, reranker and tool work with end-to-end cost/latency. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Separate generative, embedding, reranking and tool costs; state hardware, concurrency, warm/cold state and fallback behaviour. |
| Acceptance check | SLO tests declare concurrency, pool size and text lengths; model fallback changes are visible in traces and evaluation manifests. |
| Packages | RT-02, R3-15, W2-12 |
| Evidence pointers | docs/v2/pipelines/retrieve.md:350, docs/v2/ROADMAP.md:45 |

### ER-40: Keep optional features unless evidence supports retirement

| Field | Detail |
|---|---|
| Area / priority / evidence class | Evaluation and adoption / P1 / Optional enhancement |
| Finding | A negative result in one harness supports keeping a feature off there; it does not establish uselessness across all workloads. The plan already blocks unscreened retirement for owner review. |
| Options | A: delete every negative arm immediately. B: retire implementation burden with recorded scope, preserve reproducible research evidence and feature rationale. |
| Current choice | B |
| Review status | Compatible direction — conditions apply |
| Conditions / refinement | Keep reproducible historical evidence while removing retired APIs; B does not require preserving runtime compatibility shims. |
| Acceptance check | Every retirement has evidence and owner disposition; old experiments remain reproducible from pinned commits. |
| Packages | F-14a/b |
| Evidence pointers | docs/v2/PLAN.md:59 |

## Requested Claude output

1. Confirm each of the three amendments is consistently reflected in the plan, or identify a concrete conflict with source evidence.
2. Propose package/dependency and acceptance-gate changes without marking unbuilt work Done.
3. Distinguish already-built, built-off, missing and proposed functionality using the current commit, not old line numbers alone.
4. Return unresolved questions for ER-07 through ER-15 and the two cross-decision policies. Do not treat recommendations as owner answers.
5. Produce the schema discussion agenda after semantic contracts are settled; do not silently freeze illustrative fields.
6. Keep proposed improvements separate from measured results. No dataset-specific prompts or copied benchmark answers.
