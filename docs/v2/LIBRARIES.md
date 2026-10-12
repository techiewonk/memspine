# Library-first splits (LIB-01..LIB-47)

On 2026-10-13 the owner asked for more parallel agents. The pure decision logic of the behaviour packages was therefore built first as libraries:
- each library is in new files, imports only `memspine.schemas`, merged libraries and the stdlib, and makes no I/O or model calls;
- each is tested in `tests/unit/lib/` and respects the import-linter contracts;
- none is wired into the engine yet.

The parent package named below wires its library in after the pure moves (F-07..F-15) and flag-gates it. Behaviour changes are screened through the paired gate, and the GPU stays stopped until the owner restarts it.

| Library | Module(s) | Wired by | What it decides | Status note |
|---|---|---|---|---|
| LIB-01 | `derive/temporal.py` | W2-14 | temporal anchor ladder (basis, precision, intervals, relations; never invents dates) | derive/temporal.py: anchor ladder, precision intervals, 3-valued comparisons, durations, relations, as-of; 320 tests (7318c37) |
| LIB-02 | `derive/integrate.py` | W2-10 | integration ladder (supersede/retire/coexist/contest/tentative) and cardinality registry, event-time ordered | derive/integrate.py: X-05 contradictions 44/44, order-independent replay (fixes INV-ORDER-SWAP); W2-10 must use change_hint once LIB-05 adds it (6db0ac3) |
| LIB-03 | `derive/ground.py` | W2-08 | grounding rule checks (spans, modality, binding) scored on the X-05 grounding set | derive/ground.py: grounding.v1 unsupported P=1.0 R=0.76, supported P=1.0 R=0.33, undecided 0.39 (to adjudicator) (54b00df) |
| LIB-04 | `derive/resolve.py` | W2-09 | rule entity resolver (alias, homonym, numeric, cross-language) scored on X-05 entity pairs | derive/resolve.py: X-05 entity pairs precision(same) 1.000, recall 0.976, 0 false merges (b7e499e) |
| LIB-05 | `derive/compile.py` | W2-06 | ItemV1 to AssertionEnvelope compiler (reserved names, item rejection, citations, ids) | derive/compile.py + schema fields negated, change_hint, ItemV1 speaker/reported_speaker; is_tentative fixed (a30e896, 41fafd3) |
| LIB-06 | `compose/budget.py`, `compose/bundles.py` | R3-09 | exact tokenizer budget and complete evidence bundles | compose/budget.py + bundles.py: exact counting incl headers, whole bundles, skip-and-continue, shared evidence once, shares (4052243) |
| LIB-07 | `runtime/jobs.py`, `runtime/quotas.py` | F-04 | job leases, epochs, watermark and holes, handles, coalescing, quotas | runtime/jobs.py + quotas.py: fenced leases, coalescing, holes/watermark, drain, recovery budget, triggers, quotas (e59131a) |
| LIB-08 | `trust/families.py` | W2-04 | source families, quarantine release rule, restated count | trust/families.py: S4-2/4-3/4-4, author-known docs join the author family |
| LIB-09 | `trust/erasure_plan.py` | T5-04 | erasure planner (hide then re-derive over lineage) and manifest | trust/erasure_plan.py: hide/unbind/rerun/keep over lineage, manifest, purge steps, restore, retention (7acf6a0) |
| LIB-10 | `read/fusion.py` | R3-06 | family fusion, protected pool, calibrated floors | read/fusion.py: family RRF, protected pool, percentile floors; R3-06 must reconcile schema Family literal with leg families (aa189a5) |
| LIB-11 | `read/lift.py` | R3-08 | lift derived hits to turns before rerank (lift, re-admit, dedupe, cap), unscored marking | read/lift.py: lift, readmit, dedupe, cap, rerank; unscored None; POL-2 strict lineage (6751a47) |
| LIB-12 | `read/gate.py` | R3-11 | store-calibrated relevance gate with missing vs conflicting evidence states | read/gate.py: median/MAD calibration, answerable/missing/conflicting/weak/bypass; R3-11 maps QueryContract to GateContract |
| LIB-13 | `ingest/roles.py` | W2-03 | role policy and echo guard (marker plus last-read overlap) and routing | ingest/roles.py: role policy, echo guard (marker + shingle overlap), routing; user copy-paste = assent candidate (a326ee9) |
| LIB-14 | `ingest/segment.py` | W2-05 | segmentation (adaptive window, time-gap cut, token batching, context lines) | ingest/segment.py: hard/soft cuts, NEW+CONTEXT lines, oversize split, resume, coverage property (ab1760a) |
| LIB-15 | `read/obligations.py` | R3-16 | evidence obligations for joins, durations and lists (what evidence a question needs before answering) | read/obligations.py: join/duration/list/count/comparison/latest/single obligations, typed holes, follow-ups; R3-16 maps contract |
| LIB-16 | `read/contract_rules.py` | R3-02 | typed query contract by rules (shape, time view, personal vs generic, list/count, render header) | read/contract_rules.py: rule contract en/de/nl/es, render header, parity with LIB-01 (c578ef9) |
| LIB-17 | `derive/coverage.py` | W2-15 | extraction coverage check and targeted re-extraction plan | derive/coverage.py: salience signals, omissions, budgeted re-extraction plan (4860d4e) |
| LIB-18 | `trust/consent.py` | T5-03 | privacy tiers, consent and purpose scopes (admission predicates for write and read) | trust/consent.py: tiers, consent registry with generation, purpose narrowing, viewer kinds, do-not-derive (0295561) |
| LIB-19 | `read/cache.py` | R3-15 | cache keys, generation-aware invalidation and POL-1 revalidation | read/cache.py: per-kind keys incl. generations and model/prompt, POL-1 revalidate on hit, off by default (e81bf0c) |
| LIB-20 | `derive/histories.py` | O4-04 | near-duplicate merge and value histories (K_C) | derive/histories.py: guarded near-dup merge, merge_member lineage, value histories via LIB-02 replay (ecd32ad) |
| LIB-21 | `trust/horizon.py` | T5-01 | trust horizon on grants (cross-agent reach bound, attenuation) | trust/horizon.py: grants with horizon and kappa, no amplification, reach bound property-tested vs brute force, POL-2 laundering cap (b98388d) |
| LIB-22 | `read/timeview.py` | R3-14 | time view and two-axis as-of reads over event and source/recorded time | read/timeview.py: current/true_at/known_at/both/history/range, three-valued, rederive hook (85ad6bd) |
| LIB-23 | `read/router_rules.py` | R3-03 | rule router over the LayerRegistry (which legs and layers a query uses; deep tier only rewrites) | read/router_rules.py: legs by layer/service/shape, skips with reasons, deep-only rewrite (27f1e47) |
| LIB-24 | `read/neighbours.py` | R3-10 | neighbour windows after rerank (context turns around hits, budget-aware, viewer-checked) | read/neighbours.py: budget-aware, viewer-checked neighbour windows (LIB-24) |
| LIB-25 | `compose/render_trust.py` | T5-02 | low-trust and tentative rendering in context (labels, quoting, separation) | compose/render_trust.py: bands, contested grouping, neutralised low-trust text (23f2ce5) |
| LIB-26 | `runtime/freshness.py` | O4-01 | background budget and read-side freshness (covers_seq, staleness verdicts) | runtime/freshness.py: covers_seq, freshness verdicts, read barrier, background budget (7888485) |
| LIB-27 | `observability/stage_ledger.py` | W2-12 | per-stage cost ledger, write targets and budget reporting (cost classes, no content) | observability/stage_ledger.py: content-guarded ledger, aggregates, calls/turn, write targets, OTel attrs (87071b5) |
| LIB-28 | `memory/prospective.py`, `memory/procedural.py` | O4-10 | prospective and procedural memory (advisory reminders, procedures; opt-in) | memory/prospective.py + procedural.py: intentions, reminders, procedures with explicit-feedback-only updates (1f93ec2) |
| LIB-29 | `ingest/stamp.py` | W2-02 | stamping (three clocks, time zone, speaker/addressee/participants, provenance class) | ingest/stamp.py: three clocks, tz unknown flagged never shifted, DST, skew, out-of-order, participants, S3-3 write id (b2952a5) |
| LIB-30 | `ingest/intake.py` | W2-01 | source envelope intake, receipt and readiness, write ids and re-ingest | ingest/intake.py: validation, S3-3 write ids, idempotent retry, quota before accept, document version diff plans, readiness (LIB-30) |
| LIB-31 | `derive/patterns.py` | O4-05 | pattern observations from value histories and events (recurrence, frequency, trend; opt-in L4) | derive/patterns.py: recurrence, frequency, trend, co-occurrence, preference stability; lineage, trust cap (b90b016) |
| LIB-32 | `derive/standing.py` | O4-08 | standing answers and changed-group invalidation (L5, ER-28) | derive/standing.py: standing answers, group invalidation, usefulness eviction, guarded standing instructions (81aabf3) |
| LIB-33 | `derive/decay.py` | O4-09 | decay tiers, compression policy and multi-resolution text plan (K_C) | derive/decay.py: activation, hysteresis tiers, protected kinds, multi-resolution compression plans, never deletes (c1d4fc6) |
| LIB-34 | `read/entity_leg.py` | R3-05 | entity leg scoring with anti-hub damping | read/entity_leg.py: R-08 max-with-cap default (cap 0.5 = Mem0 max boost), mem0 hub damping, sum mode optional (e9376da) |
| LIB-35 | `derive/edges.py` | F-02 | edge compiler from assertions (n-ary role edges, tentative tags, link projection) | derive/edges.py: assertion nodes, n-ary role edges, projection, tentative tag, A-B-A keys, retract (24b4bb3) |
| LIB-36 | `derive/communities.py` | O4-06 | entity communities (deterministic clustering over the assertion graph) and summary job plans | derive/communities.py: deterministic label propagation, hub exclusion, stable ids, summary job plans (28075d2) |
| LIB-37 | `derive/reconcile.py` | O4-03 | reconcile backlog and contradiction sweep (staged resolution queue, contest detection across sessions) | derive/reconcile.py: sweep, pending merges, rekey with aliases; X-05 contest recall 1.00 (ea61ad4) |
| LIB-38 | `trust/gates.py` | W2-04 | firewall at three gates (insert, derive, read) with base and extended signals | trust/gates.py: insert/derive-input/derive/read gates, signals en/de/nl/es, release via LIB-08 (a89e071) |
| LIB-39 | `derive/profile.py` | O4-07 | profile card with guarded operations and reflection plans (opt-in) | derive/profile.py: guarded profile ops, kappa_P bound, EMA drift, reflection plan, invalidation (1bf6521) |
| LIB-40 | `derive/session_close.py` | O4-02 | session-close call 2 plan (summary, stitched episode, profile ops) with prompt inputs and validation | derive/session_close.py: call-2 plan, validation, deterministic stitching (LIB-40) |
| LIB-41 | `trust/retention.py` | T5-05 | retention classes and audit records per storage profile | trust/retention.py: retention classes, TTL grammar, plans with limited rebuild, hash-chained audit (e31bf2e) |
| LIB-42 | `models/tiers.py` | MD-01 | model tiers, placement, no-model fallbacks, device plan, thinking per role | models/tiers.py: role catalogue, placement with VRAM budget, no-model fallbacks, thinking off by default, reranker chain |
| LIB-43 | `read/deep.py` | R3-12 | deep tier sufficiency check and one-rewrite plan (model call injected) | read/deep.py: rules first, one sufficiency, at most one rewrite, one retrieve, no loop, calls counted (50a3ca6) |
| LIB-44 | `derive/feedback.py` | O4-11 | explicit feedback events and observer views (multi-agent) | derive/feedback.py: explicit-only feedback, permission checks, assertions via integration, correction latency, attenuated observer views |
| LIB-45 | `read/external.py` | R3-13 | external public knowledge as a separate, labelled, opt-in context block | read/external.py: opt-in plan, labelled public block, domain family, trust cap, dedupe/caps, deposit guard |
| LIB-46 | `prompts/layout.py` | W2-07 | prompt layout rules (no examples, stable prefix, same language) as a checker and builder | prompts/layout.py: stable prefix builder, layout checker; today 51/51 prompts lack same-language rule, 6 have e.g.-quotes, 2 variable-in-prefix (c297f4e) |
| LIB-47 | `read/tiers.py` | R3-01 | depth tiers, ReadRequest normalisation and the evidence unit | read/tiers.py: tier budgets, viewer-bound ReadRequest, tier choice, evidence unit (eb1b1a1) |

## Change log
- 2026-10-13: 47 libraries merged into `restructure`; 2,594 library and schema tests pass; import-linter 14 contracts kept.
