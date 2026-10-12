# Review of exported MemSpine decisions

Review only. Original choices preserved. No plan decisions or code changed.

Validated all 40 exported IDs, titles and choices; checked selection interactions and current local plan text. This does not prove implementation correctness or measured improvements.

30 named selections; one incomplete custom choice; nine unanswered. Source drift: none among the frozen review source files.

| ID | Your choice | Verdict | Conditions / remaining gap |
|---|---|---|---|
| ER-01 | B | Compatible direction — conditions apply | Generate active summaries from canonical decisions; preserve history as superseded rather than deleting it. |
| ER-02 | B | Compatible direction — conditions apply | Add prerequisites at the relevant consumers/gates; do not make every package depend on every preparation task. |
| ER-03 | B | Compatible direction — conditions apply | Use separate move, migration, API-contract and quality gates; not every behaviour change requires a GPU test. |
| ER-04 | B | Compatible direction — conditions apply | Include shared schemas, migrations, exports, dependency files and generated documents in collision checks. |
| ER-05 | B | Compatible direction — conditions apply | An embedded milestone is not permission to release the server profile without its isolation/erasure/rebuild gates. |
| ER-06 | custom | Incomplete custom choice | Custom is selected but notes are empty. Define default acknowledgement and available waits. Proposal: durable acceptance by default, explicit raw-index or derivation barriers, with timeout and readiness reported. |
| ER-07 | Unanswered | Unanswered | No selection recorded. Proposed direction: B. Check claim epoch, source versions and erasure generation in the deposit transaction; reject the old worker after takeover. Keep model calls outside write locks. |
| ER-08 | Unanswered | Unanswered | No selection recorded. Proposed direction: B with configurable bounded waiting. Never drop already acknowledged work; reject before acceptance when storage limits are reached. Keep emergency space for erasure and recovery. |
| ER-09 | Unanswered | Unanswered | No selection recorded. Proposed direction: B. Separate projector progress, derivation coverage and source membership. Barriers wait for a captured target, not for a queue that can grow forever. |
| ER-10 | Unanswered | Unanswered | No selection recorded. Proposed direction: B. Separate logical cancellation from physical completion; never reuse a model slot while its work still runs. Persist unfinished jobs before close returns. |
| ER-11 | Unanswered | Unanswered | No selection recorded. Proposed direction: B. Distinguish assertion support from reference-binding support. Include both source versions in lineage and erasure checks; do not count context as a new occurrence. |
| ER-12 | Unanswered | Unanswered | No selection recorded. Proposed direction: B behind a screen: retain all raw text; detect likely uncovered named items, quantities and list members; targeted re-extraction only when justified. Coverage heuristics are not proof. |
| ER-13 | Unanswered | Unanswered | No selection recorded. Proposed direction: B. Choose entity scope explicitly (tenant/user/agent policy); keep session as provenance. Separate retry identity, mention identity, assertion identity and occurrence identity. |
| ER-14 | Unanswered | Unanswered | No selection recorded. Proposed direction: B. Keep source trust distinct from support strength and entity/time certainty; define which combinations may supersede, enter the graph or appear as tentative evidence. |
| ER-15 | Unanswered | Unanswered | No selection recorded. Proposed direction: B. Count attempts by purpose, cap total tokens/time, cache validated outputs and track processed source ranges. Never infer complete coverage from syntactically valid partial output. |
| ER-16 | B | Compatible direction — conditions apply | Measure complete requests including schemas, context, output and retries; preserve fidelity while reducing tokens. |
| ER-17 | B | Compatible direction — conditions apply | Independent support needs source-family lineage. Requires unresolved ER-11/13; repetition and echoes are not independent evidence. |
| ER-18 | B | Compatible direction — conditions apply | Keep weak anchors labelled. Unknown dates must not become recorded_at event dates; settle the time schema before implementation. |
| ER-19 | B | Compatible direction — conditions apply | This remains a screened enhancement, not a guaranteed improvement. Requires ER-11 evidence bindings, ER-13 identity and ER-15 retry budgets. |
| ER-20 | B | Compatible direction — conditions apply | Not every fact becomes a binary edge. Uncertain and n-ary assertions retain assertion nodes and source roles. |
| ER-21 | B | Compatible direction — conditions apply | Recheck permissions and cap AFTER lifting; define ranking of parents with large fan-out. Keep unscored distinct from scored-zero. |
| ER-22 | B | Compatible direction — conditions apply | Re-admit notes, parents, neighbours, graph paths and tool results; no-lineage derived notes cannot bypass admission. |
| ER-23 | B | Compatible direction — conditions apply | Calibrate separately per model/profile and retain missing-evidence vs false-claim distinction. Empty retrieval does not prove absence. |
| ER-24 | C | Valid direction — extra choice needed | C selects adaptive evidence bundles, but leaves exact vs estimated accounting open. Proposed C+A: complete bundles under an exact reader-tokenizer budget. If only an estimate is available, label it estimated and do not claim a hard guarantee. |
| ER-25 | B | Compatible direction — conditions apply | Public knowledge cannot prove private history. Keep tool results and image/OCR uncertainty separate from user memory; cap calls. |
| ER-26 | B | Compatible direction — conditions apply | Capture an immutable coverage target and fenced worker ownership. Depends on unanswered ER-07/09/10. |
| ER-27 | C | Compatible with explicit limits | C intentionally limits historical rebuild. State which raw records, event versions, tombstones and lineage remain available. Block pruning of pending work; advertise capability limits; fail rather than silently return an incomplete rebuild. It need not prevent rebuilding indexes from retained raw records. |
| ER-28 | B | Compatible direction — conditions apply | Profile, observation and standing-answer invalidation depend on ER-11/17/26/30 and retained source availability under ER-27. |
| ER-29 | C | Valid policy change — reconcile plan | C requires a viewer on every multi-party read. It replaces the existing shared-only viewerless fallback. Apply it to REST, MCP, internal/system callers and migrations; derive viewer identity from an authenticated principal, not an arbitrary untrusted parameter. |
| ER-30 | B | Compatible direction — conditions apply | Specify logical invisibility, physical purge and backup retention separately; cached model results must not resurrect erased data. |
| ER-31 | A | Conflict — revise before finalising | A keys a cache only by query/namespace. For result/context caches this conflicts with ER-29 C, ER-22 B and ER-30 B, and retrieve.md:586-588. Choose B, or disable result/context caching. An embedding-only cache may be narrower but still needs encoder/config identity and the privacy policy. |
| ER-32 | B | Compatible direction — conditions apply | The model must never author scope, trust, verified state, fencing tokens or erasure generation. Resolve semantics before fields. |
| ER-33 | B | Compatible direction — conditions apply | Keep durable event/API compatibility distinct from transient internal typing; exported internal schemas need not become public promises. |
| ER-34 | B | Compatible direction — conditions apply | Discovery does not mean enablement. Pin selected plugins in manifests; fail duplicate registrations and scope lifetime violations. |
| ER-35 | B | Compatible direction — conditions apply | Freeze actual comparable run manifests, not merely the historical 81.4 headline. Retain all attempted arms. |
| ER-36 | B | Compatible direction — conditions apply | Previously inspected questions are validation, not blind. Preserve a genuinely unexposed final partition and an exposure ledger. |
| ER-37 | B | Compatible direction — conditions apply | Use paired stage and end-to-end metrics with preset margins; no claim of improving every question. |
| ER-38 | B | Compatible direction — conditions apply | Use reviewed provenance and narrow justified exceptions; passing token lint alone is not contamination clearance. |
| ER-39 | B | Compatible direction — conditions apply | Separate generative, embedding, reranking and tool costs; state hardware, concurrency, warm/cold state and fallback behaviour. |
| ER-40 | B | Compatible direction — conditions apply | Keep reproducible historical evidence while removing retired APIs; B does not require preserving runtime compatibility shims. |

## Additional cross-decision questions

### FOLLOW-01: Erasure during an in-flight read

docs/v2/pipelines/retrieve.md:622 permits the current read snapshot to include a forgotten record; ER-30 recommends immediate logical invisibility. These need one explicit policy.

A: reads authorised before erasure may finish; new reads cannot see it. B: revalidate generation/permissions immediately before returning context and retry or redact on change.

Recommendation: B for strict revocation; define the read/erase linearisation point. Content already delivered to an external reader cannot be recalled.

### FOLLOW-02: Trust and visibility propagation differ

ER-11/17/22/29/30 together require every supporting parent to remain authorised; a minimum numeric trust score does not establish visibility.

A: check only derived-record labels. B: lineage-aware effective visibility/purpose checks, with re-derivation or omission if an essential parent is unavailable.

Recommendation: B; enforce on cache hits, expansions and final rendering. Avoid inferring shared visibility from a trusted parent.

### FOLLOW-03: Incomplete worksheet choices

The export selected custom without notes. The worksheet records choices but cannot establish that all required sub-decisions have been supplied.

A: leave completion to manual review. B: distinguish selected, complete and finalised; require notes for custom choices and explicit follow-ups for conditional options.

Recommendation: B for the next worksheet revision; preserve the original export unchanged.

