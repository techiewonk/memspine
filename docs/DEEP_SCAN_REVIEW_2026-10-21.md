# MemSpine working-tree deep review - 2026-10-21

## Follow-up verification: 2026-10-21

The selected DS-01, DS-02 and DS-21 fixes now pass the combined focused gate: **185 passed, no failures or skips** (10 existing LanceDB cleanup warnings). [Recorded verification](provider_verification/ds-fixes-final-state-2026-10-21.json) links the test log, JUnit report and isolated before/after probes. This is a local targeted gate, not full-suite, model, deployment or complete BI-package acceptance.

Staged unaccepted jobs now survive enqueue errors/cancellation; direct readiness waits apply a cooperative deadline around inline work; English rules suppress unsupported conditional/reported/uncertain assertions. Cancellation waits for enqueue acceptance to settle, unknown durable acceptance remains at-least-once, blocking providers need native deadlines, and rule filtering does not implement unresolved annotation storage. BI-01..12 remain planned. Original review observations below describe the recorded baseline.

All current dispositions, completed work and next milestones live only in [PROVIDER_TODO.md](PROVIDER_TODO.md), shared by Claude and Codex. This document is context and evidence.

The foundation is substantial, but the target durable non-generative ingestion contract is not yet integrated. This pass records21 findings/gates: reproduced method failures, unsafe candidates, contract gaps, code-confirmed target gaps and evidence limits. They are not21 newly proven whole-engine bugs. No runtime implementation changed.

## Scope and evidence

| Check | Result |
|---|---|
| Source inventory | 615 Python modules, 152,039 lines, 7,874 symbols; zero AST syntax errors |
| Repository surfaces | 558 test files and 309 Markdown docs inventoried; not all tests or docs read |
| Working tree | HEAD e6596f44; 360 status entries / 277 untracked entries at inventory time |
| Semantic review | Critical intake, identity/retry, window, prepass, grounding, graph, orchestration, assembly, erasure and model/provider seams inspected |
| Focused probes | Actual isolated methods/parser executed; DB, Engine, models and network not started |
| Not claimed | Every line audited, every memory/provider combination tested, security certification, production approval or new full-suite success |

The615-file inventory is AST coverage, not semantic coverage of all152k lines. Some reads inspected selected definitions/regions rather than entire large files. Historical local gates remain historical; this review does not negate all103 DONE items or automatically close/reopen their IDs. New findings have separate DS IDs.

## Actual current flow versus target

| Stage | Current code understanding | Required next boundary |
|---|---|---|
| Startup/config | Provider registry builds scoped resources; named model profiles and shared GLiNER pool exist; Engine recovers accepted intake | Validate the selected non-generative ingestion profile and required layers, independently of optional legacy capabilities |
| Intake | Engine.ingest_messages -> message_ingestion -> identity/retry -> durable gateway -> write/projections | Strict replacement envelope and required-derivation acceptance manifest |
| Raw source | Event/record projection, metadata, firewall/trust and embedding/index hooks | Raw success plus independent required-work/index readiness, not all-layers-success |
| Window observation | Optional after raw write; exception is logged; open state is namespace-local memory | Durable discovery, recoverable open membership and isolated conversation/session scheduling |
| Current extraction | Enabled window path uses a generative PromptWindowExtractor; semantic GLiNER NER is non-generative; structured GLiNER adapter yields candidates | Standalone non-generative window producer with typed grounding and explicit unresolved outcomes |
| Existing offline alternatives | Rule miner selected by consolidation.miner=rules; deterministic extractive summary fallback exists | Reuse them where safe; add attribution/modality and target temporal/state semantics rather than trusting regex spans |
| Derived writes | Assertion envelopes, exact-span mention helpers, conflict ladder and liveness checks exist | Canonical target claim/event schema, independent annotation effects and source-version/erasure fenced commits |
| Graph | Rebuildable record/link projection; optional name-keyed entity nodes; offline extracted graph stage | Stable identities, occurrence-level unasserted mentions, separate validated semantic relations |
| Maintenance | Temporal/Resonate and journal run maintenance pipelines; ingestion outbox executor is another seam | Explicit adapter using one job authority; journal storage locality limits remain real |
| Retrieval | Lexical/vector/graph/context legs, fusion and optional reranking are implemented | Measure true evidence coverage and calibrated unresolved status rather than rank score alone |
| Assembly | AssemblyPolicy; optional compose bundle path, currently fetching up to two support turns | Full dependency manifests, final rendered-budget invariant and actual-path validation |
| Erasure/compression | Existing lineage, erasure state and purge work; zstd cold inflate/deflate | Integrate every new copied surface, prevent stale commits and prove raw recovery during derivation |

## Reusable components we must keep

Provider descriptors/lifecycle and scoped ports; one durable intake; exact GLiNER annotation binding; engine-owned bounded model pool; named task/model profiles; existing pure rule miner; extractive summary fallback; conflict and lineage machinery; graph/lexical/vector projectors; outbox lease/progress bookkeeping; Temporal journal; offline tokenizer resolver; existing optional bundle packer. Reuse does not imply all accepted new semantics already hold.

## Findings and recommended actions

| ID | Priority | Evidence class | Finding / impact | Recommended action | Phase |
|---|---|---|---|---|
| DS-01 | P1 | REPRODUCED_FAILURE | **Staged windows disappear on unexpected enqueue failure**. The method replaces self._staged with an empty list before enqueue and only restores on BackpressureRejected; the probe raises before the first durable enqueue and leaves two windows absent. Raw source is not erased. Evidence: [src/memspine/workers/windows.py:948](../src/memspine/workers/windows.py). | Restore unaccepted work for all failure/cancellation paths; durable discovery must reconcile raw-to-job gaps. | BI-03 |
| DS-02 | P1 | REPRODUCED_FAILURE | **Direct readiness timeout does not bound inline drain**. A 2ms requested wait took about60ms when its inline drain was controlled to take60ms. read.PendingBarrier separately wraps readiness with asyncio.wait_for, limiting that read path. Evidence: [src/memspine/runtime/runner.py:539](../src/memspine/runtime/runner.py). | Make the direct API deadline explicit; schedule durable drain separately or enforce remaining deadline without losing accepted work. | BI-09 |
| DS-03 | P1 | CODE_CONFIRMED_TARGET_GAP | **Current window construction still requires generative extraction**. Enabled windows require llm.roles.window_extract and construct PromptWindowExtractor; online_extractor_v2 without a model returns None. This is current opt-in code, not proof the default silently generates. Evidence: [src/memspine/ingest/windows.py:116](../src/memspine/ingest/windows.py). | Reuse segmentation/grounding with a non-generative candidate producer; validate zero-generation profile and required capabilities at startup. | BI-01/06 |
| DS-04 | P1 | CODE_CONFIRMED_TARGET_GAP | **Window prepass annotations are attached through surviving assertion envelopes**. The stage derives mentions then iterates envelopes, binding annotations only to cited records and emitting them as envelope extras. This window route does not independently persist successful empty-fact/source annotations; semantic direct extraction has separate mention support. Evidence: [src/memspine/derive/prepass.py:594](../src/memspine/derive/prepass.py). | Persist per-source annotation results independently of fact survival, including explicit successful empty results. | BI-05/07 |
| DS-05 | P1 | CODE_CONFIRMED_TARGET_GAP | **Raw acceptance and window observation are separate failure boundaries**. Raw write handling catches window.observe exceptions and logs them without failing the source write. This is fine for optional enrichment, insufficient for required derivation without durable reconciliation. Evidence: [src/memspine/ingest/write.py:671](../src/memspine/ingest/write.py). | Pin required work at acceptance; scan committed raw sources into the durable outbox with atomic cursor/job insertion. | BI-02/03 |
| DS-06 | P1 | CODE_CONFIRMED_TARGET_GAP | **Open window membership is volatile before a cut**. Segmenter/session state is held in the namespace store; outbox payloads are staged when windows close. Durable closed windows do not prove recovery of partial open windows. Evidence: [src/memspine/workers/windows.py:689](../src/memspine/workers/windows.py). | Persist open membership/source cursor or reconstruct it deterministically from accepted manifests and source versions. | BI-03/04 |
| DS-07 | P2 | CODE_CONFIRMED_TARGET_GAP | **One namespace window store serialises interleaved conversation/session state**. A conversation switch flushes the current segmenter; a session switch treats the previous session as closed. This prevents some mixing but does not implement independent concurrent per-conversation windows. Evidence: [src/memspine/workers/windows.py:822](../src/memspine/workers/windows.py). | Key active window state by accepted scope/session/conversation or prove equivalent partitioned scheduling; do not falsely close another live visit. | BI-04 |
| DS-08 | P1 | REPRODUCED_CONTRACT_GAP | **Civil source date accepted and naive time can be localised**. 2026-10-01 passes normalisation; ingest/write._localize assigns read.naive_timezone to naive datetimes. That explicit old policy is different from the accepted no-invented-timezone target. Evidence: [src/memspine/ingest/message_identity.py:23](../src/memspine/ingest/message_identity.py). | Define strict source-time validation or preserve unresolved civil text plus provenance; do not invent source chronology. | BI-01/02/06 |
| DS-09 | P2 | REPRODUCED_CONTRACT_GAP | **Unknown message fields can conceal ID mistakes**. An input id field is accepted as an extra while a different message_id is generated; repeating pure normalisation creates another canonical identity. id is not the current supported message_id field. Evidence: [src/memspine/ingest/message_identity.py:41](../src/memspine/ingest/message_identity.py). | Reject unknown fields in the selected replacement envelope; provide a clear message_id error rather than adding compatibility aliases. | BI-01/02 |
| DS-10 | P1 | CODE_CONFIRMED_TARGET_GAP | **Name-keyed graph nodes are not identity-safe mention occurrences**. Legacy entity IDs are ent:<namespace>:<canonical-name>. The current projector uses record names; source/version/span occurrence projection and stable person identity require additional contracts. Evidence: [src/memspine/memories/associative/entities.py:64](../src/memspine/memories/associative/entities.py). | Separate source mention occurrences, unresolved bindings, canonical entity IDs and asserted relationship edges. | BI-01/08 |
| DS-11 | P1 | CODE_CONFIRMED_TARGET_GAP | **Known-entity prompt context lacks the target evidence manifest**. KnownEntity carries name and aliases; _known_from_store builds those from entity/alias state, not the accepted cited claim/source candidate contract. Existing hints must not become authoritative facts. Evidence: [src/memspine/derive/prompt_context.py:31](../src/memspine/derive/prompt_context.py). | Carry source/version/known-time/visibility evidence through contextual prefill; use current names as hints only. | BI-01/04/06 |
| DS-12 | P2 | CODE_CONFIRMED_TARGET_GAP | **Output prefill caps do not bound candidate collection work**. Refresh traverses entity aliases and the namespace entity index before prompt selection; cached full-name lists are not equivalent to accepted bounded source retrieval. Evidence: [src/memspine/ingest/windows.py:247](../src/memspine/ingest/windows.py). | Bound generation as well as rendering; indexed lookup, resumable cursors and explicit deferred/pruning status. | BI-04/09 |
| DS-13 | P1 | CODE_CONFIRMED_TARGET_GAP | **Live optional fact bundles request at most two source turns**. _BUNDLE_TURNS=2 is passed to _source_turns. General claims may require three or more sources for identity, correction, count or time; two selected turns cannot prove full dependency coverage. Evidence: [src/memspine/compose/pipeline/pack.py:564](../src/memspine/compose/pipeline/pack.py). | Build explicit dependency manifests and preserve all required sources or mark incomplete; avoid global count-of-two as a completeness rule. | BI-01/09 |
| DS-14 | P1 | EVIDENCE_LIMIT | **Benchmarked fitter is not the live Engine assembly implementation**. The CPU probe isolates fit_units with exact fixture costs; live compose uses AssemblyPolicy and optional compose.bundles.pack. Annotated links/spans and supplied48-candidate pools make results conditional. Evidence: [src/memspine/read/tiers.py:439](../src/memspine/read/tiers.py). | Reuse the evidence-bundle contract and validate the actual pure runtime packer against the same cases before claiming Engine2K/3K retention. | BI-09/12 |
| DS-15 | P1 | CODE_CONFIRMED_TARGET_GAP | **AssemblyPolicy can admit an oversized first record**. The budget rejection branch requires selected to be non-empty, so the first record can exceed budget unless a later configured stage fits it. This is a local policy observation, not proof every final endpoint exceeds its cap. Evidence: [src/memspine/compose/pack.py:180](../src/memspine/compose/pack.py). | Enforce the selected hard rendered-evidence cap at the final boundary; return incomplete/oversize instead of breaking it for non-empty output. | BI-09/12 |
| DS-16 | P1 | CODE_CONFIRMED_TARGET_GAP | **Copied-excerpt erasure policy needs expanded surface coverage**. Existing erasure state/lineage/purge machinery is reusable. It does not by itself prove the target20 copy classes, new copied bundles, durable statuses and seven-day backup restore requirements. Evidence: [src/memspine/trust/pipeline/erasure_v2.py:261](../src/memspine/trust/pipeline/erasure_v2.py). | Integrate new source/copy lineage into existing erasure authority; deny reads immediately and track per-surface physical completion honestly. | BI-01/07/10 |
| DS-17 | P1 | CODE_CONFIRMED_TARGET_GAP | **Maintenance Temporal runner does not automatically execute ingestion outbox jobs**. workers.runner selects maintenance execution; ingest windows use runtime.runner/outbox independently. Embedded SQLite journal recovery also requires accessible persisted storage after relocation. Evidence: [src/memspine/workers/temporal_runner.py:124](../src/memspine/workers/temporal_runner.py). | Retain one required-work authority in SQL; register an explicit ingestion Activity adapter after local handler durability is proved. | BI-03/07/12 |
| DS-18 | P2 | EVIDENCE_LIMIT | **Real GLiNER ingestion evidence uses a deterministic window-LLM fixture**. The artifact explicitly says real local GLiNER but window_llm is a deterministic supported-claim fixture. It proves selected real SDK/Engine wiring, not a complete model-free window producer or arbitrary relation accuracy. Evidence: [docs/provider_verification/gliner-ingestion-real.json:1](../docs/provider_verification/gliner-ingestion-real.json). | Preserve the successful narrow gate and add independent non-generative-window/semantic-role tests; do not erase or inflate the historical evidence. | BI-05/06/12 |
| DS-19 | P1 | CODE_CONFIRMED_TARGET_GAP | **Accepted typed schema and predicate catalog are not integrated contracts yet**. Review JSON and52 mappings remain design artifacts; existing AssertionEnvelope/TemporalRef and producers/consumers still have their current semantics. Runtime helpers with similar names are not proof the coordinated rewrite is done. Evidence: [docs/ENRICHMENT_SCHEMA_CONTRACTS.md:1](../docs/ENRICHMENT_SCHEMA_CONTRACTS.md). | Freeze BI-01 contracts, then update all affected writers, events, stores, graph/read consumers and fixtures together without aliases. | BI-01/06/07 |
| DS-20 | P2 | REVIEW_LIMIT | **Large uncommitted working tree weakens replay of historical verification**. The scan sees615 Python modules and360 Git status entries including277 untracked entries at inventory time. Historical DONE gates do not establish regression freedom for the exact current tree; counts are status history, not a fresh test pass. Evidence: [docs/PROVIDER_TODO.md:1](../docs/PROVIDER_TODO.md). | Preserve a file-hash evidence manifest and review coherent changes; refresh affected focused gates before a release candidate, leaving deferred eval work deferred. | BI-12/PA-039 |
| DS-21 | P1 | REPRODUCED_UNSAFE_CANDIDATE | **Rules can propose speaker residence from hypothetical or reported speech**. The actual parser returns Asha home=Lisbon from If I live in Lisbon..., and Asha home=Porto from Daniel said, I live in Porto. Only the output container is stubbed; downstream persistence is not exercised. Evidence: [src/memspine/core/rule_miner.py:240](../src/memspine/core/rule_miner.py). | Retain quote/reporting/conditional scope and require supported speaker attribution and modality before promoting rule matches. Span overlap alone is insufficient. | BI-06 |

## Required focused gates for each finding

| ID | Gate |
|---|---|
| DS-01 | Failure on first/middle enqueue, cancellation and restart: no missing job, no duplicate effects. |
| DS-02 | Slow prepare/commit and timeout<=0 through both direct readiness and read wrapper; return within defined slack and preserve work. |
| DS-03 | A profile with no LLM role produces grounded allowed candidates/annotations or explicit unresolved outcomes without losing the whole stage. |
| DS-04 | Zero facts, rejected facts and uncited source with valid mentions all yield correct source annotation readiness. |
| DS-05 | Kill/fail after raw append before observation; eventual required job exists exactly once. |
| DS-06 | Restart after each of the first five turns of a six-turn window, plus idle/session-close cuts. |
| DS-07 | Interleaved A1/B1/A2 with separate conversations and sessions; no false cross-session close or mixed support. |
| DS-08 | Date-only, naive datetime, aware offset, generated timestamp and month/year precision variants preserve their supported meaning. |
| DS-09 | Misspelled identity/time/sender fields fail clearly; accepted identical message_id retries remain idempotent. |
| DS-10 | Two different Alex identities and repeated identical spans survive replay without false merges or asserted traversal. |
| DS-11 | Later-known alias and erased/revoked sources cannot create unsupported bindings or leak into historical views. |
| DS-12 | Large entity sets and many namespaces respect work/memory limits without treating a capped set as globally unique. |
| DS-13 | Three/four-source counts, origin binding plus time, and correction chains retain all required support or explicitly abstain. |
| DS-14 | Parity on rendered token cost, dependency closure, caps, known-time and inaccessible/missing sources with the actual runtime packer. |
| DS-15 | One oversized record, leading headers, stable prefix and compression on/off all obey the final budget or explicit failure contract. |
| DS-16 | Forget during inference, copied excerpts/caches/logs, restart and restored backup cannot re-authorise erased source. |
| DS-17 | Activity retries/reopen/reassignment, idempotent effects and journal-volume loss are tested with documented recovery limits. |
| DS-18 | Actual configured non-generative path handles positive and abstention cases with exact spans and zero generation. |
| DS-19 | Structural and behavioural contracts agree for all five synthetic scenarios, typed values, authority, precision and unresolved records. |
| DS-20 | Verification records identify exact revision or working-tree hashes, selected commands, failures/skips and output artifacts. |
| DS-21 | Explicit positive passes; quoted, conditional, negated, denied and planned statements do not become unsupported current residence. |

## Memory types and source authority

All nine memory implementations exist. Semantic supplies claims/state; episodic source chronology; working active context/page-out; resource document lineage; associative graph links; procedural skills/plans; reflective parent-grounded reflections; prospective watch work; shared grants/revocation. Their provider dependencies differ: graph is not required by every type, and storing a procedural/reflection/watch record is not inherently a generative operation.

Existing PA-075..081/083 selected local gates remain recorded DONE; PA-082 is a deployment check. New unresolved identity, corrected state history, retrospective known-time links and complete evidence bundles require separate BI integration tests. Mention presence is neither trust nor asserted truth. The example L0/L1/L2 terms were explanatory, not three replacements for these nine memory types.

## Recommended implementation order

1. Resolve DS-01 queue loss and DS-02 direct deadline semantics with narrow regressions before advertising durability/bounded readiness. Restore durable work on cancellation as well as exceptions.
2. Complete BI-01: strict source envelope/time/identity/receipt and typed claim/event/evidence contracts; enforce zero-generative ingestion policy. Preserve the accepted breaking rewrite and existing provider boundary.
3. BI-02/03/04: accepted-work manifests, durable source discovery, open-window recovery and independent conversation scheduling.
4. BI-05/06/07: persist every source annotation outcome; implement rule/non-generative candidate producers; require modality/attribution/time and fenced source-aware effects.
5. BI-08/09/10: mention occurrence projection, full support manifests, true rendered token budgets, copied-data erasure and recoverable compression.
6. BI-12: validate those paths with fictional positive/negative and restart cases, then benchmark the actual runtime packer; BI-11 separately selected summaries must not reintroduce hidden generation.

No broad quality claim from the supplied48-candidate synthetic benchmark:2K/3K are proposed profiles, accepted4K design remains unchanged. No new model/provider is selected by this report. PA-107 evaluation migration remains deferred. No full suite, paid calls, external-service startup, database copying or historical re-embedding was performed.

## Reproduction and artifacts

```powershell
$py = 'C:/Users/hembad/AppData/Local/Temp/memspine-ner-runtime-313/Scripts/python.exe'
& $py evals/bench/deep_scan_review_probes.py `
  --out 'docs/schema_review/deep_scan_probes_2026-10-21.json'
```

The probe intentionally records current gaps; gap_reproduced=true is not a passing fix regression. The rule probe replaces only the ExtractedFact output container, so it proves parser candidate behaviour, not unsafe Engine persistence. The wait probe uses a controlled drain, so elapsed times show the mechanism rather than deployed SLO performance.

- [All-source hashes, symbols and imports](schema_review/deep_scan_inventory_2026-10-21.json)
- [Structured findings](schema_review/deep_scan_findings_2026-10-21.json)
- [Isolated probe results](schema_review/deep_scan_probes_2026-10-21.json)
- [Context handoff](CLAUDE_CONTEXT_AND_TRACKER_2026-10-20.md)
- [Provider tracker](PROVIDER_TODO.md) and [background plan](BACKGROUND_INGESTION_PLAN.md)
